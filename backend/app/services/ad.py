"""官方「推广（投放）」页面缺的那几块：投放包 / 人群包 / 计划日志 / 消息中心。

与 `services/promotion.py`、`services/ad_write.py` 的分工：

* `promotion.py` 管**首页与报表**（首页 / 账户卡片 / 会员指标 / 计划与商品报表 / 结算 / 流水）；
* `ad_write.py` 管**写**（计划增删改停启 / 活动报名领奖 / 流水操作）；
* 本模块管**上面两处没盖到的只读面**（`mtop.taobao.idle.ad.account.info` / `package.info` /
  `item.searchV2` / `customize.bid.estimates` / `group.list` / `group.data.query` /
  `campaign.log.query` / `message.list` / `message.detail` / `message.unread.info`）
  以及**唯一一个写接口** `message.read`（标记推广消息已读）。

硬规则（与 `docs/contracts/ad.md` 一一对应）：

1. **协议层不在就 503**：`common.protocol_module('ad')` 拿不到模块 → `not_ready`，
   **绝不**把「协议层没装」当成「平台没数据」——后者会让页面显示一片空。
2. **不猜数据**：平台没返回的字段一律 `None`，**禁止 0 顶替**。本模块的接口**都还没有
   真机验证**（协议层 docstring 写了），所以更不做「认字段」的猜测：响应 `data` 原样透出。
3. **无权限 ≠ 掉线**：平台拒绝（`FAIL_BIZ_*`）原样透出成 `protocol_error`（502），
   不写 `need_relogin`；只有协议层认出 `SESSION_EXPIRED` 才回写登录态（由
   `common.call_method` / `common.call_failed` 负责）。
4. **写必须显式确认**：`message_read` 的 `confirm` 只认字面 `True`（与
   `services/ad_write.py` 同严格档），没确认就 400 `confirm_required`，且**一次协议调用都不发**；
   带 `Idempotency-Key` 时同一个键 + 同一个指纹只打平台一次。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core import db
from . import common, platform_errors

#: 只读 kind 的顺序（也是 `GET /apis` 的展示顺序）。
#: **抄一份是有意的**：协议层可能整个导入不进来（懒导入就是为这个），那时目录仍要能列出
#: 「本该有哪些接口」。协议层测试 `test_read_kinds_match_protocol` 盯着这两份不许漂移。
READ_KINDS: tuple = (
    'account_info', 'package_info', 'item_search', 'customize_bid_estimates',
    'group_list', 'group_data', 'campaign_log',
    'message_list', 'message_detail', 'message_unread',
    'coupon_info', 'coupon_statistic', 'account_card_v2', 'activity_detail',
)

#: 写接口目录：action -> 协议层函数名。**只有一条**，且必须 `confirm=True`。
WRITE_ACTIONS: Dict[str, str] = {'message_read': 'message_read'}

#: **登记但不接**的官方接口：mtop 名 -> 理由。只在 `GET /apis` 的目录里透出，
#: 不进 `READ_KINDS` / `READ_APIS`，所以没有任何路由能打到它（与
#: `services/ad_write.py:26` 的 `UNSUPPORTED` 同一套写法）。
UNSUPPORTED: Dict[str, str] = {
    'mtop.taobao.idle.ad.bp.data.download':
        '官方「下载报表」按钮调它**创建异步导出任务**（响应 data.taskId / data.status，'
        '没有 downloadUrl 时官方拿 taskId 每 60s 轮询；targetType=CAMPAIGN|MEMBER|ITEM）'
        '—— 服务端有副作用（建任务 / 落库），不是读一个现成文件，'
        '所以本仓库只登记接口名与载荷形状、不接（bundle @3115691，'
        '调用点 @3118414 / @3174876 / @3236322）',
}


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _credential_failure(account_id: str) -> Optional[Dict[str, Any]]:
    """Cookie / 登录态前置检查，与 `services/promotion.py` 同口径。"""
    row = db.query_one(
        'SELECT cookie, login_state FROM account_credentials WHERE account_id = ?',
        (account_id,),
    )
    if row is None or not _text(row['cookie']).strip():
        return common.fail(
            f'账号 {account_id} 还没有 Cookie，请先在「账号」页完成扫码登录',
            'verification_required', login_state='need_relogin',
        )
    if _text(row['login_state']).strip() == 'need_relogin':
        return common.fail(
            f'账号 {account_id} 登录态已过期，需要重新扫码登录',
            'verification_required', login_state='need_relogin',
        )
    return None


def _client(account_id: str):
    if db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,)) is None:
        return None, common.fail(f'账号 {account_id} 不存在', 'not_found')
    failure = _credential_failure(account_id)
    if failure:
        return None, failure
    value, problem = common.get_client(account_id)
    return (value, None) if value is not None else (None, problem or common.not_ready())


def _date(value: Any, *, default: str = '') -> str:
    """日期只做「8 位数字」的形状校验：真假日期由平台判，我们不替平台编日历。"""
    text = _text(value).strip()
    if not text:
        return default
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f'日期必须是 YYYYMMDD 的 8 位数字，收到 {text!r}')
    return text


def _page(value: Any, *, default: int = 1, name: str = 'page_num') -> int:
    text = _text(value).strip()
    if not text:
        return default
    if not text.isdigit() or int(text) < 1:
        raise ValueError(f'{name} 必须是 >= 1 的整数，收到 {text!r}')
    return int(text)


def _int(value: Any, *, default: int = 0, name: str = 'value') -> int:
    text = _text(value).strip()
    if not text:
        return default
    if text.startswith('-'):
        body = text[1:]
    else:
        body = text
    if not body.isdigit():
        raise ValueError(f'{name} 必须是整数，收到 {text!r}')
    return int(text)


def _flag(value: Any, *, default: bool = False) -> bool:
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        return value
    return _text(value).strip().lower() in ('1', 'true', 'yes')


#: `read()` 里被当成「便利参数」取走的键，不许留在整包 payload 里（真机复打时曾把
#: `entity_type` / `entity_list` 原样透给平台，见 docs/contracts/ad.md）。
_CONVENIENCE_KEYS = ('entity_type', 'entity_list', 'is_goods_selected_tab_change')


def _payload_only(data: Dict[str, Any]) -> Dict[str, Any]:
    """整包 payload：剔掉便利参数键，其余原样透传。"""
    return {key: value for key, value in data.items() if key not in _CONVENIENCE_KEYS}


def _entities(entity_type: Any = '', entity_list: Any = '') -> Dict[str, Any]:
    """官方 `entityList` 那两个必填键：`entityType` 是**数字**（1=商品 / 2=线索）。

    `entity_list` 收 JSON 数组字符串或已是数组的值，元素原样透传 —— 官方每项是
    `{entityId, catId, price, areaTarget, startTime}`，这里不替平台编字段。
    """
    body: Dict[str, Any] = {}
    text = _text(entity_type).strip()
    if text:
        body['entityType'] = _int(text, name='entity_type')
    raw: Any = entity_list
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith('['):
            try:
                raw = json.loads(raw)
            except ValueError as exc:
                raise ValueError(f'entity_list 不是合法 JSON 数组：{exc}') from exc
    if raw:
        if not isinstance(raw, (list, tuple)):
            raise ValueError('entity_list 必须是数组（官方是对象数组）')
        body['entityList'] = list(raw)
    return body


class _BoundProtocol:
    """把「模块级协议函数」和「调用它的 client」绑成可交给 `call_method` 的对象。

    **为什么必须包一层**：`common.call_method` 在协议层抛登录态过期时，会用**第一个参数**
    去回写 `need_relogin`（读它的 `cookie_id` / `account_id`）。直接传协议模块的话，
    模块身上没有账号身份，掉线就写不回库里。转发方向与 `services/datacenter.py` 一致：
    协议函数转发到模块，账号身份只从 client 上取。
    """

    def __init__(self, module: Any, client: Any) -> None:
        self._module = module
        self._client = client

    @property
    def account_id(self) -> str:
        return str(getattr(self._client, 'account_id', '')
                   or getattr(self._client, 'persist_account_id', '') or '')

    @property
    def cookie_id(self) -> str:
        return str(getattr(self._client, 'cookie_id', '') or '')

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)


def _protocol():
    """协议层（`backend.app.xianyu.ad`）。拿不到 → (None, not_ready 信封)。"""
    module = common.protocol_module('ad')
    if module is None:
        return None, common.not_ready('协议层未提供推广接口（backend.app.xianyu.ad）')
    return module, None


def _call(name: str, client: Any, *args: Any):
    """调协议层的 `name(client, ...)`，失败翻成信封。"""
    module, failure = _protocol()
    if module is None:
        return None, failure
    status, value = common.call_method(_BoundProtocol(module, client), (name,), client, *args)
    if status == common.CALL_OK:
        if not isinstance(value, dict):
            # 协议层把信封剥了一层；这里拿到的应该是平台响应里的 data（dict）。
            # 空 dict 是合法答复（平台就是没给字段），但非 dict 是协议异常。
            return None, common.fail('推广接口没有返回响应对象', 'protocol_error')
        return value, None
    # 协议层自己拦下的「残包」要翻成 400：真机上平台只回 502 +
    # `E_AD_BP_COMMON_PARAM_NULL::必填参数为空`，说不清是谁的问题（照
    # `WriteNotConfirmed` -> `confirm_required` 的先例）。
    missing = getattr(module, 'MissingRequiredParams', None)
    if missing is not None and isinstance(value, missing):
        return None, common.fail(str(value), 'invalid_fields')
    problem = common.call_failed(status, value)
    if problem is not None:
        return None, problem
    hint = platform_errors.explain(str(value)) if value is not None else ''
    return None, common.fail(hint or '推广接口调用失败', 'protocol_error')


def _envelope(account_id: str, kind: str, api: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'account_id': account_id,
        'kind': kind,
        'api': api,
        'data': payload,
        'fetched_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'platform',
    }


def _read(account_id: str, kind: str, *args: Any) -> Dict[str, Any]:
    """只读接口的唯一出口：kind 白名单 -> 账号 -> 协议层 -> 信封。"""
    module, failure = _protocol()
    if module is None:
        return failure
    spec = module.READ_APIS.get(kind)
    if spec is None:
        return common.fail(f'不支持的推广只读接口 {kind}', 'not_found')
    client, failure = _client(account_id)
    if failure:
        return failure
    payload, failure = _call(kind, client, *args)
    if failure:
        return failure
    return _envelope(account_id, kind, spec['api'], payload)


def catalog() -> Dict[str, Any]:
    """接口目录：哪些只读接口、哪个写接口、各自的 mtop 名。"""
    module = common.protocol_module('ad')
    reads: List[Dict[str, Any]] = []
    if module is not None:
        for kind, spec in module.READ_APIS.items():
            reads.append({'kind': kind, 'api': spec['api'], 'method': 'GET'})
    writes = [{'action': name, 'api': getattr(module, 'MESSAGE_READ_API', '') if module else '',
               'method': 'POST', 'confirm_required': True}
              for name in WRITE_ACTIONS]
    return {
        'reads': reads,
        'writes': writes,
        'unsupported': [{'api': api, 'reason': reason} for api, reason in UNSUPPORTED.items()],
        'protocol_ready': module is not None,
    }


# -- 只读：每条一个显式函数（服务层 API 稳定，调用方不必记 kind 字符串） ------------


def account_info(account_id: str, order_price: str = '', promotion_mode: str = '',
                 bid_type: str = '', scene: str = '') -> Dict[str, Any]:
    """投放账户信息。官方调用点必带 `scene`（如 `CREATION_REAL_TIME`）。"""
    body: Dict[str, Any] = {}
    if _text(order_price).strip():
        body['orderPrice'] = _text(order_price).strip()
    if _text(promotion_mode).strip():
        body['promotionMode'] = _text(promotion_mode).strip()
    if _text(bid_type).strip():
        body['bidType'] = _text(bid_type).strip()
    if _text(scene).strip():
        body['scene'] = _text(scene).strip()
    return _read(account_id, 'account_info', body)


def package_info(account_id: str, payload: Any = None, entity_type: Any = '',
                 entity_list: Any = '', is_goods_selected_tab_change: Any = '') -> Dict[str, Any]:
    """投放包信息：官方必填 `entityType`（数字 1=商品 / 2=线索）+ `entityList`。

    `payload` 是 `initPlanInfo` 用的整包查询对象（原样透传）；`entity_type` /
    `entity_list` 是给「发不出 GET body」的调用方的便利入口（`entity_list` 收 JSON
    数组字符串）。缺必填键时**不发平台**，直接 400 —— 真机上平台只回 502
    `E_AD_BP_COMMON_PARAM_NULL`（协议层 `BODY_REQUIRED`）。
    """
    body = dict(payload) if isinstance(payload, dict) else {}
    try:
        body.update(_entities(entity_type, entity_list))
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')
    if _text(is_goods_selected_tab_change).strip() != '':
        body['isGoodsSelectedTabChange'] = _flag(is_goods_selected_tab_change)
    return _read(account_id, 'package_info', body)


def item_search(account_id: str, need_tabs: Any = '', item_id: str = '', search_key: str = '',
                campaign_id: str = '', sort_field: str = '', sort_type: str = '',
                page_num: Any = '', page_size: Any = '', tab: str = '') -> Dict[str, Any]:
    """商品搜索 V2：`itemId` 与 `searchKey` 至少给一个；`tab` 官方恒带。

    `tab` / `needTabs` 不给时由协议层补官方首次加载的默认值
    （`tab='platReco'`、`needTabs=true`），见 `backend/app/xianyu/ad.py`。
    """
    try:
        body: Dict[str, Any] = {}
        if _text(tab).strip():
            body['tab'] = _text(tab).strip()
        if _text(need_tabs).strip() != '':
            body['needTabs'] = _flag(need_tabs)
        if _text(item_id).strip():
            body['itemId'] = _text(item_id).strip()
        if _text(search_key).strip():
            body['searchKey'] = _text(search_key).strip()
        if _text(campaign_id).strip():
            body['campaignId'] = _int(campaign_id, name='campaign_id')
        if _text(sort_field).strip():
            body['sortField'] = _text(sort_field).strip()
        if _text(sort_type).strip():
            body['sortType'] = _text(sort_type).strip()
        if _text(page_num).strip():
            body['pageNum'] = _page(page_num)
        if _text(page_size).strip():
            body['pageSize'] = _page(page_size, name='page_size')
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')
    if 'itemId' not in body and 'searchKey' not in body:
        return common.fail('item.searchV2 需要 item_id 或 search_key 至少给一个', 'invalid_fields')
    return _read(account_id, 'item_search', body)


def customize_bid_estimates(account_id: str, payload: Any = None, entity_type: Any = '',
                            entity_list: Any = '') -> Dict[str, Any]:
    """自定义出价估算：官方必填 `{entityType, entityList}`（`pcFirstRequest` 协议层补）。

    出价那条（`bidTarget` / `bidPrice` / `noBid` / `pcFirstRequest:false`）走 `payload`。
    """
    body = dict(payload) if isinstance(payload, dict) else {}
    try:
        body.update(_entities(entity_type, entity_list))
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')
    if not body:
        return common.fail('customize.bid.estimates 需要请求体（entityType / entityList 等）',
                           'invalid_fields')
    return _read(account_id, 'customize_bid_estimates', body)


def group_list(account_id: str, start_date: str = '', end_date: str = '',
               page_num: Any = '', page_size: Any = '', campaign_id: str = '',
               group_status: Any = '', promotion_mode: str = '', title_like: str = '') -> Dict[str, Any]:
    """投放单元列表。`includeEndDate` 官方恒为 `true`；`campaignId` 官方是 `Number(x)||0`。"""
    try:
        body: Dict[str, Any] = {
            'startDate': _date(start_date),
            'endDate': _date(end_date),
            'includeEndDate': True,
        }
        if _text(page_num).strip():
            body['pageNum'] = _page(page_num)
        if _text(page_size).strip():
            body['pageSize'] = _page(page_size, name='page_size')
        if _text(campaign_id).strip():
            body['campaignId'] = _int(campaign_id, name='campaign_id')
        if _text(group_status).strip():
            body['groupStatus'] = _int(group_status, name='group_status')
        if _text(promotion_mode).strip():
            body['promotionMode'] = _text(promotion_mode).strip()
        if _text(title_like).strip():
            body['campaignTitleLike'] = _text(title_like).strip()
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')
    return _read(account_id, 'group_list', body)


def group_data(account_id: str, ad_group_id: str = '', start_date: str = '', end_date: str = '',
               all_period: Any = '') -> Dict[str, Any]:
    """投放单元数据：`adGroupId` 必填；`allPeriod` 官方传 `false`。"""
    try:
        body: Dict[str, Any] = {
            'startDate': _date(start_date),
            'endDate': _date(end_date),
            'includeEndDate': True,
        }
        if _text(all_period).strip() != '':
            body['allPeriod'] = _flag(all_period)
        if not _text(ad_group_id).strip():
            return common.fail('group.data.query 需要 ad_group_id', 'invalid_fields')
        body['adGroupId'] = _int(ad_group_id, name='ad_group_id')
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')
    return _read(account_id, 'group_data', body)


def campaign_log(account_id: str, start_date: str = '', end_date: str = '',
                 campaign_id: str = '', page_num: Any = '', page_size: Any = '',
                 operator_type: Any = '', operation_type: Any = '') -> Dict[str, Any]:
    """计划操作日志：`operatorType` 0=全部/1=用户/2=系统，`operationType` 0=全部/1=添加/2=更新。"""
    try:
        body: Dict[str, Any] = {
            'startDate': _date(start_date),
            'endDate': _date(end_date),
            'campaignId': _int(campaign_id, name='campaign_id') if _text(campaign_id).strip() else 0,
        }
        if _text(page_num).strip():
            body['pageNum'] = _page(page_num)
        if _text(page_size).strip():
            body['pageSize'] = _page(page_size, name='page_size')
        if _text(operator_type).strip():
            body['operatorType'] = _int(operator_type, name='operator_type')
        if _text(operation_type).strip():
            body['operationType'] = _int(operation_type, name='operation_type')
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')
    return _read(account_id, 'campaign_log', body)


def message_list(account_id: str, page_num: Any = '', page_size: Any = '') -> Dict[str, Any]:
    """推广消息列表。"""
    try:
        body: Dict[str, Any] = {}
        if _text(page_num).strip():
            body['pageNum'] = _page(page_num)
        if _text(page_size).strip():
            body['pageSize'] = _page(page_size, name='page_size')
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')
    return _read(account_id, 'message_list', body)


def message_detail(account_id: str, message_id: str = '') -> Dict[str, Any]:
    """推广消息详情：`messageId` 必填。"""
    if not _text(message_id).strip():
        return common.fail('message.detail 需要 message_id', 'invalid_fields')
    body = {'messageId': _text(message_id).strip()}
    return _read(account_id, 'message_detail', body)


def message_unread(account_id: str) -> Dict[str, Any]:
    """推广消息未读数。官方 `data` 为空、不套 requestJson，所以这里**不给参数**。"""
    # 官方这条 data 为空、不套 requestJson —— 一个参数都不给（协议层签名就是 message_unread(client)）
    return _read(account_id, 'message_unread')


def coupon_info(account_id: str, page_num: Any = '', page_size: Any = '',
                coupon_type: Any = '', source: str = '', payload: Any = None) -> Dict[str, Any]:
    """活动奖励页的券列表（红包 / 礼金）。

    官方发的是**页面筛选状态**对象 `{pageNum,pageSize,couponType,source}`
    （bundle @3265220 的 `sWe(h)`），初值由协议层 `BODY_DEFAULTS` 补
    （`{pageNum:1,pageSize:10,couponType:0,source:'BONUS'}`，见 `backend/app/xianyu/ad.py`）；
    `coupon_type` 0=全部/2=礼金/3=红包/4=新人券/5=代理商礼金，
    `source` BONUS=生效中/COUPON=未生效/HISTORY=已失效。
    其余键（官方以后加的筛选条件）走 `payload` 原样透传。
    """
    body = dict(payload) if isinstance(payload, dict) else {}
    try:
        if _text(page_num).strip():
            body['pageNum'] = _page(page_num)
        if _text(page_size).strip():
            body['pageSize'] = _page(page_size, name='page_size')
        if _text(coupon_type).strip():
            body['couponType'] = _int(coupon_type, name='coupon_type')
        if _text(source).strip():
            body['source'] = _text(source).strip()
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')
    return _read(account_id, 'coupon_info', body)


def coupon_statistic(account_id: str) -> Dict[str, Any]:
    """活动奖励页的券统计。官方 `requestJson` 恒为 `{}`、**无参**（bundle @3246428），
    所以这里也不给参数 —— 不给参数不等于失败，协议层会发空对象。"""
    return _read(account_id, 'coupon_statistic')


def account_card_v2(account_id: str, payload: Any = None) -> Dict[str, Any]:
    """投放账户卡片 V2（活动奖励页的账户卡）。

    官方包装是 `requestJson: JSON.stringify(e)`，但**载荷 `e` 在别的 chunk 里**（bundle
    @3268292 只有包装、取不到键），所以这里不设默认值、不猜键，整包对象原样透传
    （`payload` 省略 = 发空对象，平台接受空对象，见 docs/contracts/ad.md 真机原话）。
    """
    body = dict(payload) if isinstance(payload, dict) else {}
    return _read(account_id, 'account_card_v2', body)


def activity_detail(account_id: str) -> Dict[str, Any]:
    """活动奖励详情渲染。官方 `data` 为空、不套 requestJson（bundle @3268518），**无参**
    —— 与 `message_unread` 同形。"""
    return _read(account_id, 'activity_detail')


def read(account_id: str, kind: str, params: Any = None) -> Dict[str, Any]:
    """通用只读入口：按 `kind` 分发（AI / MCP 侧一个口子进来）。

    只做分发与类型校验，不做「猜参数」：未知 kind → 404 `not_found`。
    """
    name = _text(kind).strip()
    data = params if isinstance(params, dict) else {}
    if name == 'account_info':
        return account_info(account_id, **{k: data.get(k, '') for k in
                                           ('order_price', 'promotion_mode', 'bid_type', 'scene')})
    if name == 'package_info':
        return package_info(account_id, _payload_only(data), data.get('entity_type', ''),
                            data.get('entity_list', ''),
                            data.get('is_goods_selected_tab_change', ''))
    if name == 'item_search':
        return item_search(account_id, **{k: data.get(k, '') for k in
                                          ('need_tabs', 'item_id', 'search_key', 'campaign_id',
                                           'sort_field', 'sort_type', 'page_num', 'page_size',
                                           'tab')})
    if name == 'customize_bid_estimates':
        return customize_bid_estimates(account_id, _payload_only(data), data.get('entity_type', ''),
                                       data.get('entity_list', ''))
    if name == 'group_list':
        return group_list(account_id, **{k: data.get(k, '') for k in
                                         ('start_date', 'end_date', 'page_num', 'page_size',
                                          'campaign_id', 'group_status', 'promotion_mode',
                                          'title_like')})
    if name == 'group_data':
        return group_data(account_id, **{k: data.get(k, '') for k in
                                         ('ad_group_id', 'start_date', 'end_date', 'all_period')})
    if name == 'campaign_log':
        return campaign_log(account_id, **{k: data.get(k, '') for k in
                                           ('start_date', 'end_date', 'campaign_id', 'page_num',
                                            'page_size', 'operator_type', 'operation_type')})
    if name == 'message_list':
        return message_list(account_id, **{k: data.get(k, '') for k in ('page_num', 'page_size')})
    if name == 'message_detail':
        return message_detail(account_id, _text(data.get('message_id')))
    if name == 'message_unread':
        return message_unread(account_id)
    if name == 'coupon_info':
        # 便利键必须从整包 payload 里剔掉：留着会让平台收到 `page_num` 这种蛇形键
        # （真机复打时 `entity_type` / `entity_list` 就这么漏过一次）。
        taken = ('page_num', 'page_size', 'coupon_type', 'source')
        extra = {k: v for k, v in _payload_only(data).items() if k not in taken}
        return coupon_info(account_id, **{k: data.get(k, '') for k in taken}, payload=extra)
    if name == 'coupon_statistic':
        return coupon_statistic(account_id)
    if name == 'account_card_v2':
        return account_card_v2(account_id, _payload_only(data))
    if name == 'activity_detail':
        return activity_detail(account_id)
    return common.fail(f'不支持的推广只读接口 {name}', 'not_found')


# -- 写：标记推广消息已读 --------------------------------------------------------


def message_read(account_id: str, message_ids: Any = None, confirm: Any = False) -> Dict[str, Any]:
    """标记推广消息已读（**写**）。

    闸门顺序与 `services/ad_write.py` 一致：确认 -> 参数 -> 账号 -> 平台。
    带 `Idempotency-Key` 时同一键 + 同一指纹只打平台一次（`common.idempotent_call`）。
    """
    problem = common.confirm_required(
        'message_read', confirm, strict=True,
        message='标记推广消息已读是写操作，必须显式确认（confirm=true）后才会执行')
    if problem is not None:
        return problem
    ids = _message_ids(message_ids)
    if ids is None:
        return common.fail('message.read 需要 message_ids（非空数组）', 'invalid_fields')
    client, failure = _client(account_id)
    if failure:
        return failure
    body = {'messageIds': ids}
    item_id = ','.join(str(i) for i in ids)

    def _once() -> Dict[str, Any]:
        module, failure = _protocol()
        if module is None:
            return failure
        status, value = common.call_method(
            _BoundProtocol(module, client), ('message_read',), client, body, True)
        if status == common.CALL_OK:
            payload = value if isinstance(value, dict) else {}
            common.log_operation(account_id, 'message_read', item_id, True, body)
            return _envelope(account_id, 'message_read', module.MESSAGE_READ_API, payload)
        # 与 `services/ad_write.py:90` 同顺序：协议层自己拒了「没确认」要单独翻成
        # 400 confirm_required，**不能**先走 `call_failed`（那会变成 502 protocol_error）。
        if isinstance(value, module.WriteNotConfirmed):
            return common.fail('协议层拒绝了没有确认的写请求', 'confirm_required')
        hint = platform_errors.explain(str(value)) if value is not None else ''
        # 失败也留痕：平台可能已经生效、只是响应丢了（与 trade_write 同口径）。
        common.log_operation(account_id, 'message_read', item_id, False,
                             hint or '推广消息已读失败')
        return (common.call_failed(status, value)
                or common.fail(hint or '推广消息已读失败', 'protocol_error'))

    result = common.idempotent_call(account_id, 'message_read', item_id, body, _once)
    if common.is_failure(result):
        return result
    result['confirmed'] = True
    result['written_at'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    return result


def _message_ids(value: Any) -> Optional[List[Any]]:
    """`message_ids` 统一成非空列表：接受 list / JSON 数组字符串 / 逗号分隔字符串。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith('['):
            try:
                value = json.loads(text)
            except ValueError:
                return None
        else:
            value = [part for part in (p.strip() for p in text.split(',')) if part]
    if not isinstance(value, (list, tuple)):
        return None
    ids: List[Any] = []
    for item in value:
        text = _text(item).strip()
        if not text:
            continue
        ids.append(int(text) if text.lstrip('-').isdigit() else text)
    return ids or None


def execute(account_id: str, action: str, payload: Any = None, confirm: Any = False) -> Dict[str, Any]:
    """写入口（与 `services/ad_write.py::execute` 同形状）。"""
    name = _text(action).strip()
    if name != 'message_read':
        return common.fail(f'不支持的推广写操作 {name}', 'not_found')
    ids = payload.get('message_ids') if isinstance(payload, dict) else payload
    return message_read(account_id, ids, confirm)
