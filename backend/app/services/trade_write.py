# -*- coding: utf-8 -*-
"""交易写服务（订单发货 / 改价 / 退款处理 / 评价 / 投诉处理）。

**红线**（每一条都有对应单测）：

- 每个写函数都有 `confirm` 参数，默认 False → 返回 `confirmation_required` 失败信封，
  **一个平台请求都不发**；
- 平台没返回的字段一律 `None`，**不补 0**；平台失败信封原样透出（带 `platform_errors.explain`）；
- 「无权限」≠「掉线」：`NO_PERMISSION` / `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 走 `protocol_error`，
  **不会**把账号写成 `need_relogin`（只有真的 `SESSION_EXPIRED` 才会，由 `common.call_method` 判定）；
- 金额单位只在协议层换算一次（元 → 分），本层一律收「元」。

**接口来源**：`backend/app/xianyu/trade_write.py`（静态 bundle 反解，**全部未经真机验证**，
写接口禁止对真实平台试打）。本模块自身在单测里用假协议层，生产上只有用户显式确认才会走 `client.mtop`。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

from loguru import logger

from ..xianyu import address_write as addr_proto
from ..xianyu import compensate as cp_proto
from ..xianyu import order_address as order_addr_proto
from ..xianyu import trade_write as proto
from . import common
# 账号/凭据检查只有一份，直接复用只读服务里的私有助手（同包内，避免两套判定漂移）
from .trade import _client, _stamp, _text

#: 未确认时的失败码：`common.FAIL_STATUS` 里没有它 → `common.respond` 落到 400。
CONFIRM_CODE = common.CONFIRM_CODE


# --------------------------------------------------------------------------- 前置检查


def _confirm_required(action: str) -> Dict[str, Any]:
    """未确认信封：判据与措辞都走 services/common 单点。"""
    return common.confirm_failure(
        action, message=f'{action} 是写操作，需要显式确认（confirm=true）后才会提交')


def _missing(*pairs: Tuple[str, Any]) -> Optional[Dict[str, Any]]:
    for label, value in pairs:
        if value is None or value == '' or value == [] or value == () or value == {}:
            return common.fail(f'{label}不能为空', 'invalid_fields')
        if isinstance(value, str) and not value.strip():
            return common.fail(f'{label}不能为空', 'invalid_fields')
    return None


def _amount(label: str, value: Any, *, positive: bool = False) -> Optional[Dict[str, Any]]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return common.fail(f'{label}必须是数字', 'invalid_fields')
    if number < 0:
        return common.fail(f'{label}不能为负', 'invalid_fields')
    if positive and number == 0:
        return common.fail(f'{label}必须大于 0', 'invalid_fields')
    return None


def _call_api(client: Any, api: str, data: Any, version: str,
              value_type: Optional[str] = None) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """调一次 mtop（写接口与「改地址」那条只读查询共用）。

    `value_type` **只有传了才加**：官方 `valueType:"string"` 只出现在隐私地址解密那一个接口，
    给别的请求塞上会改变它们的响应形状。
    """
    kwargs: Dict[str, Any] = {'v': version}
    if value_type:
        kwargs['value_type'] = value_type
    status, value = common.call_method(client, ('mtop',), api, data, **kwargs)
    if status != common.CALL_OK:
        failure = common.call_failed(status, value) or common.fail('协议层调用失败', 'protocol_error')
        failure.setdefault('platform_errors', {
            'api': api,
            'explain': _text(value) or '协议层未提供该方法',
        })
        return None, failure

    explain = proto.envelope_error(value)
    if explain:
        # 平台级失败（含 NO_PERMISSION / FAIL_BIZ_IDLE_USER_UNAUTHORIZED）原样透出。
        # 这里**不**判定登录态：无权限不等于掉线，别让用户白扫一次码。
        logger.warning(f'[trade_write] 平台拒绝 {api}: {explain}')
        return None, common.fail(
            f'平台接口失败: {explain}',
            'protocol_error',
            platform_errors={
                'api': api,
                'explain': explain,
                'message': proto.envelope_message(value),
            },
        )
    return value, None


def _call_write(client: Any, api: str, data: Any, version: str) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """调一次写接口。与只读层的 `_call` 的区别：**不做** `assert_read_only`（这里本来就是写）。"""
    return _call_api(client, api, data, version)


def _submit(account_id: str, action: str, item_id: str, builder, parse, version: str,
            audit: Any = None) -> Dict[str, Any]:
    """写请求的公共骨架：**已确认**之后才进来（调用方负责闸门与参数校验）。

    幂等（第 59 批，§10-11）：请求带 `Idempotency-Key` 时，同一个键 + 同一个请求指纹**只打平台一次**，
    重复请求回放上次的响应（带 `idempotent_replay`）；不带这个头的请求行为与加机制之前完全一致。
    指纹在 `builder()` **之后**算：闸门拒绝（没确认 / 参数不全）不该占用键，也不该写幂等表。
    """
    client, failure = _client(account_id)
    if failure:
        return failure
    try:
        api, data = builder()
    except (proto.WriteNotConfirmed, ValueError) as exc:
        return common.fail(str(exc), CONFIRM_CODE if isinstance(exc, proto.WriteNotConfirmed)
                           else 'invalid_fields')

    return common.idempotent_call(
        account_id, action, _text(item_id), data,
        lambda: _submit_once(client, account_id, action, item_id, api, data, version, parse, audit),
    )


def _submit_once(client: Any, account_id: str, action: str, item_id: str, api: str, data: Any,
                 version: str, parse, audit: Any) -> Dict[str, Any]:
    """真正打平台的那一次：成功/失败都写 `operation_logs`（回放时不会走到这里）。"""
    raw, failure = _call_write(client, api, data, version)
    if failure:
        common.log_operation(account_id, action, item_id, False, failure.get('message') or '')
        return failure

    payload = parse(raw)
    payload['account_id'] = account_id
    payload['api'] = api
    payload['action'] = action
    common.log_operation(account_id, action, item_id, True, audit if audit is not None else data)
    return _stamp(payload)


# --------------------------------------------------------------------------- 发货


def ship_offline(account_id: str, trade_ids: Sequence[Any], mail_no: Any, cp_code: Any,
                 address_id: Any, *, confirm: bool = False) -> Dict[str, Any]:
    """订单发货（自己联系物流）：`mtop.taobao.idle.logistics.merchant.consign.offline`。

    `address_id` 是发货/退货地址 id（`trade.address.list.query` 拿到的 `address_id`）。
    """
    if not _truthy(confirm):
        return _confirm_required('订单发货')
    failure = _missing(('订单号列表', trade_ids), ('运单号', mail_no),
                       ('物流公司编码', cp_code), ('发货地址', address_id))
    if failure:
        return failure
    first = trade_ids[0] if isinstance(trade_ids, (list, tuple)) and trade_ids else ''
    return _submit(account_id, 'ship_offline', _text(first),
                   lambda: proto.ship_offline_request(trade_ids, mail_no, cp_code, address_id,
                                                      confirm=True),
                   proto.parse_ship_result, proto.SHIP_OFFLINE_VERSION)


def ship_dummy(account_id: str, order_id: Any, trade_text: Any = '', pic_urls: Any = (),
               *, confirm: bool = False) -> Dict[str, Any]:
    """无需物流发货（虚拟发货）：`mtop.taobao.idle.logistics.merchant.consign.dummy`。"""
    if not _truthy(confirm):
        return _confirm_required('无需物流发货')
    failure = _missing(('订单号', order_id))
    if failure:
        return failure
    return _submit(account_id, 'ship_dummy', _text(order_id),
                   lambda: proto.ship_dummy_request(order_id, trade_text, pic_urls, confirm=True),
                   proto.parse_write_result, proto.SHIP_DUMMY_VERSION)


# --------------------------------------------------------------------------- 改价


def adjust_price(account_id: str, order_id: Any, modify_fee: Any, new_transport_fee: Any,
                 *, confirm: bool = False) -> Dict[str, Any]:
    """订单改价：`mtop.taobao.idle.trade.merchant.user.adjust.price`。

    入参 `modify_fee` / `new_transport_fee` 是**元**；平台口径是分，换算在协议层做一次。
    官方前端拒绝「商品价格为 0」，这里同样拦下。
    """
    if not _truthy(confirm):
        return _confirm_required('订单改价')
    failure = _missing(('订单号', order_id))
    if failure:
        return failure
    for label, value, positive in (('改后价格', modify_fee, True), ('改后运费', new_transport_fee, False)):
        failure = _amount(label, value, positive=positive)
        if failure:
            return failure
    return _submit(account_id, 'adjust_price', _text(order_id),
                   lambda: proto.adjust_price_request(order_id, modify_fee, new_transport_fee,
                                                      confirm=True),
                   proto.parse_write_result, proto.ADJUST_PRICE_VERSION)


def adjust_price_render(account_id: str, order_id: Any) -> Dict[str, Any]:
    """改价前的渲染数据 —— **只读**，所以没有 `confirm`。

    官方改价弹窗打开时先调它：拿当前商品价（官方口径 = 总价 − 运费）、当前运费、
    商品信息与 `modifyPriceTips`，再拿这些值预填两个输入框、并且**只在有改动时才允许提交**
    （官方两条校验：「商品价格必须不能为0～」「价格没有修改～」）。

    我们之前的改价面板是空白输入框 —— 卖家得凭记忆手打两个数字，打错了就是改错价。
    平台没给 module 时给 `found=False` + 一条中文 warning（不是「价格为 0」）。
    """
    failure = _missing(('订单号', order_id))
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure
    api, data = proto.adjust_price_render_request(order_id)
    raw, failure = _call_api(client, api, data, proto.ADJUST_PRICE_RENDER_VERSION)
    if failure:
        return failure
    payload = proto.parse_adjust_price_render(raw)
    payload['account_id'] = account_id
    payload['order_id'] = _text(order_id)
    payload['api'] = api
    warnings = []
    if not payload.get('found'):
        warnings.append({
            'code': 'adjust_price_render_missing',
            'message': '平台没有返回这一单的改价数据（可能改了字段名，或者这单不能改价），'
                       '这里不替你编价格；可以手动填写，或者去官方卖家工作台改。',
        })
    elif payload.get('origin_item_price') is None:
        # 官方口径：商品价初值 = 订单总价 − 运费（bundle 里的 `ej(totalPrice, '-', postFee)`），
        # 所以「算出来的」是常态、不是异常，不该每次都弹 warning；
        # 只有**算不出来**（缺总价或缺运费）才提示，并且绝不拿 0 顶上。
        warnings.append({
            'code': 'adjust_price_render_item_price_unknown',
            'message': '平台没给订单总价或运费，算不出「改后商品价格」的初值；'
                       '这里不替你编数字，可以去官方卖家工作台改，或者手动填。',
        })
    payload['warnings'] = warnings
    return _stamp(payload)


# --------------------------------------------------------------------------- 退款处理


def refund_agree(account_id: str, refund_id: Any, auth_token: Any,
                 *, confirm: bool = False) -> Dict[str, Any]:
    """同意退款：`mtop.taobao.idle.merchant.refund.agree.refund`。

    `auth_token` 是平台下发的操作令牌（退款详情/渲染接口里给），必须原样回传；
    服务层**不会**自己去猜或缓存它。
    """
    if not _truthy(confirm):
        return _confirm_required('同意退款')
    failure = _missing(('退款单号', refund_id), ('操作令牌', auth_token))
    if failure:
        return failure
    return _submit(account_id, 'refund_agree', _text(refund_id),
                   lambda: proto.refund_agree_request(refund_id, auth_token, confirm=True),
                   proto.parse_write_result, proto.REFUND_AGREE_VERSION)


def refund_refuse(account_id: str, refund_id: Any, order_id: Any, refuse_reason_id: Any,
                  proof_desc: Any = '', proof_urls: Any = (), negotiation: Any = None,
                  *, confirm: bool = False) -> Dict[str, Any]:
    """拒绝退款：`mtop.taobao.idle.merchant.refund.refuse`。

    `refuse_reason_id` 来自 `mtop.taobao.idle.merchant.refund.refuse.render` 的
    `refuseReasonList[].refuseReasonId`（该接口只读，不在这里调用）。
    """
    if not _truthy(confirm):
        return _confirm_required('拒绝退款')
    failure = _missing(('退款单号', refund_id), ('订单号', order_id), ('拒绝原因', refuse_reason_id))
    if failure:
        return failure
    return _submit(account_id, 'refund_refuse', _text(refund_id),
                   lambda: proto.refund_refuse_request(refund_id, order_id, refuse_reason_id,
                                                       proof_desc, proof_urls, negotiation,
                                                       confirm=True),
                   proto.parse_write_result, proto.REFUND_REFUSE_VERSION)


# --------------------------------------------------------------------------- 评价


def rate_create(account_id: str, trade_ids: Any, feedback: Any, rate: Any = -1,
                image_urls: Any = (), anonymous: bool = False,
                *, confirm: bool = False) -> Dict[str, Any]:
    """卖家评价：`mtop.taobao.idle.merchant.rate.create`。

    bundle 里卖家侧**只有这一个**评价写接口（没有单独的「回复评价」接口）；
    `rate` 是评价等级，不需要等级时官方传 `-1`。
    """
    if not _truthy(confirm):
        return _confirm_required('评价')
    failure = _missing(('订单号列表', trade_ids), ('评价内容', feedback))
    if failure:
        return failure
    return _submit(account_id, 'rate_create', '',
                   lambda: proto.rate_create_request(trade_ids, feedback, rate, image_urls,
                                                     anonymous, confirm=True),
                   proto.parse_rate_result, proto.RATE_CREATE_VERSION)


# --------------------------------------------------------------------------- 投诉处理


def _complain_guard(account_id: str, action: str, complain_id: Any, proof_urls: Any,
                    builder, version: str, confirm: bool) -> Dict[str, Any]:
    if not _truthy(confirm):
        return _confirm_required(action)
    failure = _missing(('投诉单号', complain_id), ('举证图片', proof_urls))
    if failure:
        return failure
    result = _submit(account_id, action, _text(complain_id), builder,
                     proto.parse_write_result, version)
    if common.is_failure(result):
        return result
    # 投诉类的**业务级**失败：顶层 success 仍是 true，官方前端读 `data.success` + `data.failReason`。
    body = result.get('platform_body') if isinstance(result.get('platform_body'), dict) else {}
    if body.get('success') is False:
        reason = _text(body.get('failReason')) or '平台拒绝了本次投诉操作'
        return common.fail(f'平台接口失败: {reason}', 'protocol_error',
                           platform_errors={'api': result.get('api'), 'explain': reason})
    return result


def complain_refuse(account_id: str, complain_id: Any, proof_desc: Any, proof_urls: Any,
                    *, confirm: bool = False) -> Dict[str, Any]:
    """投诉拒绝（举证反驳）：`mtop.taobao.idle.cco.shop.complain.refuse`。"""
    return _complain_guard(
        account_id, 'complain_refuse', complain_id, proof_urls,
        lambda: proto.complain_refuse_request(complain_id, proof_desc, proof_urls, confirm=True),
        proto.COMPLAIN_REFUSE_VERSION, confirm)


def complain_proof(account_id: str, complain_id: Any, proof_desc: Any, proof_urls: Any,
                   kind: str = 'sellerSubmitProof', *, confirm: bool = False) -> Dict[str, Any]:
    """投诉举证：`complain.submit.proof` / `.active.proof` / `.passive.proof` 三选一。"""
    if kind not in proto.COMPLAIN_PROOF_APIS:
        return common.fail(f'未知的投诉举证类型: {kind}', 'invalid_fields')
    return _complain_guard(
        account_id, f'complain_proof:{kind}', complain_id, proof_urls,
        lambda: proto.complain_proof_request(complain_id, proof_desc, proof_urls, kind, confirm=True),
        proto.COMPLAIN_PROOF_VERSION, confirm)


# --------------------------------------------------------------------------- 退款凭证（只读）


def refund_verify_token(account_id: str, refund_id: Any) -> Dict[str, Any]:
    """拿退款凭证（`verifyUrl` + `token`）—— **只读**，所以没有 `confirm`。

    为什么需要它：同意退款的 `authToken` 不是退款列表帧里的字段（真机
    `rightVO.btnList[].clickEvent.data.url` 是空串），用户在我们页面上根本无处可抄。
    官方前端的做法是打开弹窗时先查一次 `mtop.idle.alipay.verify.url.query`，
    把 `data.module.token` 填进 `authToken`、把 `data.module.verifyUrl` 塞进 iframe 让用户
    完成支付宝身份验证。我们照这条路走（证据见 `xianyu/trade_write.py` 的常量注释）。

    **不假装成功**：平台没给 `token` 就是 `None`（上层照实提示），不生成、不缓存、不落库。
    """
    failure = _missing(('退款单号', refund_id))
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure

    api, data = proto.verify_url_request(refund_id)
    status, value = common.call_method(client, ('mtop',), api, data, v=proto.VERIFY_URL_VERSION)
    if status != common.CALL_OK:
        failure = common.call_failed(status, value) or common.fail('协议层调用失败', 'protocol_error')
        failure.setdefault('platform_errors', {
            'api': api,
            'explain': _text(value) or '协议层未提供该方法',
        })
        return failure

    explain = proto.envelope_error(value)
    if explain:
        # 官方这条只在 `userInfo.isAdmin` 为真时走；非管理员态平台很可能直接拒绝。
        # 照实透出原文（不引导用户重新扫码 —— 这不是掉线）。
        logger.warning(f'[trade_write] 平台拒绝 {api}: {explain}')
        return common.fail(
            f'平台接口失败: {explain}',
            'protocol_error',
            platform_errors={
                'api': api,
                'explain': explain,
                'message': proto.envelope_message(value),
            },
        )

    payload = proto.parse_verify_result(value)
    payload['account_id'] = account_id
    payload['refund_id'] = _text(refund_id)
    payload['api'] = api
    return _stamp(payload)


# --------------------------------------------------------------------------- 业务级失败


def complain_business_failure(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """投诉类接口的业务级失败（顶层 success 仍是 true，读的是 `data.success` + `data.failReason`）。

    官方前端就是这么判的；这类失败同样**不**碰登录态。
    """
    body = result.get('platform_body') if isinstance(result, dict) else None
    if not isinstance(body, dict):
        return None
    if body.get('success') is False:
        reason = _text(body.get('failReason')) or '平台拒绝了本次投诉操作'
        return common.fail(f'平台接口失败: {reason}', 'protocol_error',
                           platform_errors={'api': result.get('api'), 'explain': reason})
    return None


# --------------------------------------------------------------------------- 退货地址（写）


#: 判据单点：services/common.confirm_truthy（宽松档，全仓唯一一份实现）。
_truthy = common.confirm_truthy


def _mobile_problem(value: Any) -> Optional[Dict[str, Any]]:
    """联系方式校验：去掉空格/横线后必须是 11 位数字。

    官方 schema 的 `x-validator:"phone"` 就是中国大陆手机号；这里**不**用 `1[3-9]` 前缀硬卡
    （号段会变，卡死会把用户的合法号码拦下来），只钉位数。
    """
    digits = ''.join(ch for ch in _text(value) if ch.isdigit())
    if not digits:
        return common.fail('联系方式不能为空', 'invalid_fields')
    if len(digits) != addr_proto.MOBILE_DIGITS:
        return common.fail(
            f'联系方式要是 {addr_proto.MOBILE_DIGITS} 位数字（现在 {len(digits)} 位）', 'invalid_fields')
    return None


def _mobile_digits(value: Any) -> str:
    """统一成纯数字再发。

    官方表单是 `max 11` + `x-validator:"phone"`，用户不可能在官方页面输入空格/横线；
    我们允许用户粘贴带分隔符的号码（校验按数字位数算），但**发给平台的是纯数字**，
    与官方一致，避免平台按「格式非法」拒掉。
    """
    return ''.join(ch for ch in _text(value) if ch.isdigit())


def _too_long(label: str, value: Any, limit: int) -> Optional[Dict[str, Any]]:
    text = _text(value)
    if len(text) > limit:
        return common.fail(f'{label}最多 {limit} 个字（现在 {len(text)} 个）', 'invalid_fields')
    return None


def _address_fields_problem(contact_name: Any, mobile_phone: Any, province_name: Any,
                            city_name: Any, district_name: Any,
                            detail_address: Any, area_id: Any = None,
                            default_addr: Any = None) -> Optional[Dict[str, Any]]:
    """必填 + 长度 + 手机号 —— **纯本地**，所以排在建客户端之前（掉线时也该先报「字段没填」，
    而不是「去重新登录」）。

    `area_id` / `default_addr` 是**平台侧硬必填**（第 61 批真机取证，见
    `logs/write61/05_address_add.json` 与 `05b_address_add_with_area.json`）：

    - 两个都不给 -> `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数areaId`
    - 只补 `areaId` -> `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数defaultAddr`
    - 补上 `areaId` 之后才轮到权限校验（本账号随后回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`）

    所以这里把两者当必填在本地拦下：平台原话是「缺少业务参数」，而用户看到 502
    只会以为是我们挂了。返回 400 `invalid_fields` + 说清「官方页面的区级区域码」，
    与「不伪造 areaId」的协议层口径一致（我们仍然不猜码，只是不再静默丢掉它）。

    第 61 批起**有码源**：`GET /api/logistics/{account_id}/divisions`
    （`mtop.cainiao.geography.frontend.divisionsinfo.get`）可从省逐级取到区，
    所以 400 文案指向它，而不是「本项目没有码源」。
    """
    if _text(area_id).strip() == '':
        return common.fail(
            '退货地址需要区级区域码 area_id（平台硬必填；先用 GET /api/logistics/'
            '{account_id}/divisions 从省逐级取到区，传叶子节点的 value，形如 110101/东城区）',
            'invalid_fields')
    if default_addr is None:
        return common.fail('退货地址需要 default_addr（平台硬必填，是否设为默认地址）',
                           'invalid_fields')
    failure = _missing(('联系人姓名', contact_name), ('联系方式', mobile_phone),
                       ('所在省', province_name), ('所在市', city_name), ('所在区', district_name),
                       ('详细地址', detail_address))
    if failure:
        return failure
    failure = _too_long('联系人姓名', contact_name, addr_proto.CONTACT_NAME_MAX)
    if failure:
        return failure
    failure = _too_long('详细地址', detail_address, addr_proto.DETAIL_ADDRESS_MAX)
    if failure:
        return failure
    return _mobile_problem(mobile_phone)


def _address_submit(account_id: str, action: str, fields: Dict[str, Any],
                    audit: Any = None) -> Dict[str, Any]:
    """退货地址写请求的公共骨架：**已确认、已校验**之后才进来。"""
    client, failure = _client(account_id)
    if failure:
        common.log_operation(account_id, action, '', False, failure.get('message') or '')
        return failure

    api, data = addr_proto.request_of(action, confirm=True, **fields)
    raw, failure = _call_write(client, api, data, addr_proto.DEFAULT_VERSION)
    if failure:
        common.log_operation(account_id, action, '', False, failure.get('message') or '')
        return failure

    payload: Dict[str, Any] = {
        'action': action,
        'contact_id': addr_proto.contact_id_of(raw),
        'api': api,
        'warnings': [],
    }
    if action == 'add' and payload['contact_id'] is None:
        # 官方前端新增后是重拉列表找新条目，从来没读过响应体；我们不假装知道新 id。
        payload['warnings'].append({
            'code': 'no_contact_id',
            'message': '平台没有回显新的地址 ID，请重新拉一次列表（新地址就在里面）',
        })
    payload['account_id'] = account_id
    common.log_operation(account_id, action, payload['contact_id'] or '', True,
                         audit if audit is not None else data)
    return _stamp(payload)


def address_add(account_id: str, contact_name: Any, mobile_phone: Any, province_name: Any,
                city_name: Any, district_name: Any, detail_address: Any,
                area_id: Any = None, default_addr: Any = None,
                *, confirm: Any = False) -> Dict[str, Any]:
    """新增退货地址（`mtop.alibaba.idle.seller.platform.merchant.delivery.address.add`）。

    **三步顺序：确认 → 字段校验 → 建客户端**。`area_id`（区级区域码）与 `default_addr`
    是**平台硬必填**（第 61 批真机取证：不给前者回 `缺少业务参数areaId`，补上后者回
    `缺少业务参数defaultAddr`，见 `logs/write61/05_address_add.json` / `05b_…` / `05d_…`）
    ⇒ 本地预检拦下，缺就 400，不往平台发。我们仍然**不伪造**区域码：码值必须由调用方给出，
    官方那个 `division` 级联数组也照旧不发（理由见 `xianyu/address_write.py` 的模块 docstring）。
    """
    if not _truthy(confirm):
        return _confirm_required('新增退货地址')
    failure = _address_fields_problem(contact_name, mobile_phone, province_name, city_name,
                                      district_name, detail_address, area_id, default_addr)
    if failure:
        return failure
    return _address_submit(account_id, 'add', {
        'contact_name': contact_name, 'mobile_phone': _mobile_digits(mobile_phone),
        'province_name': province_name, 'city_name': city_name,
        'district_name': district_name, 'detail_address': detail_address,
        'area_id': area_id, 'default_addr': default_addr,
    })


def address_update(account_id: str, contact_id: Any, contact_name: Any, mobile_phone: Any,
                   province_name: Any, city_name: Any, district_name: Any, detail_address: Any,
                   area_id: Any = None, default_addr: Any = None,
                   *, confirm: Any = False) -> Dict[str, Any]:
    """修改退货地址（`…delivery.address.update`）。`contact_id` 是官方必带的 `contactId`。"""
    if not _truthy(confirm):
        return _confirm_required('修改退货地址')
    failure = _missing(('退货地址 ID', contact_id))
    if failure:
        return failure
    failure = _address_fields_problem(contact_name, mobile_phone, province_name, city_name,
                                      district_name, detail_address, area_id, default_addr)
    if failure:
        return failure
    return _address_submit(account_id, 'update', {
        'contact_id': contact_id, 'contact_name': contact_name,
        'mobile_phone': _mobile_digits(mobile_phone),
        'province_name': province_name, 'city_name': city_name,
        'district_name': district_name, 'detail_address': detail_address,
        'area_id': area_id, 'default_addr': default_addr,
    })


def address_delete(account_id: str, contact_id: Any, *, confirm: Any = False) -> Dict[str, Any]:
    """删除退货地址（`…delivery.address.delete`）。**不可撤销**。"""
    if not _truthy(confirm):
        return _confirm_required('删除退货地址')
    failure = _missing(('退货地址 ID', contact_id))
    if failure:
        return failure
    return _address_submit(account_id, 'delete', {'contact_id': contact_id})


def address_set_default(account_id: str, contact_id: Any, *,
                        confirm: Any = False) -> Dict[str, Any]:
    """设为默认退货地址（`…delivery.address.set.default`）。"""
    if not _truthy(confirm):
        return _confirm_required('设为默认退货地址')
    failure = _missing(('退货地址 ID', contact_id))
    if failure:
        return failure
    return _address_submit(account_id, 'set_default', {'contact_id': contact_id})


# --------------------------------------------------------------------------- 改地址申请（读 + 同意/拒绝）


def order_address_info(account_id: str, order_id: Any) -> Dict[str, Any]:
    """查这个订单上的改地址申请 —— **只读**，所以没有 `confirm`。

    `mtop.idle.merchant.order.get.modify.address.info`，`data.module` 里给
    `{modifyStatus, modifyAddressInfoVO, nowAddressInfoVO, modifyDeliverId}`。
    只有 `modifyStatus === 'WAITING'` 才算「有申请待处理」，也只有那一档平台才给 `modifyDeliverId`。

    **隐私号订单**（`nowAddressInfoVO.privacyFlag` 为真）平台给的是打码字段，官方会再调一次
    `mtop.idle.trade.order.detail.privacy.address`（带 `valueType:'string'`）换明文。
    我们也调，但**解不开就如实说**：`decrypted` 给 `None` + 一条中文 warning，
    `now_address` 仍是平台原样透传的打码字段 —— 不编明文、不把打码字段当明文展示。
    """
    failure = _missing(('订单号', order_id))
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure

    api, data = order_addr_proto.info_request(order_id)
    raw, failure = _call_api(client, api, data, order_addr_proto.DEFAULT_VERSION)
    if failure:
        return failure

    payload = order_addr_proto.parse_info(raw)
    payload['account_id'] = account_id
    payload['order_id'] = _text(order_id)
    payload['api'] = api

    warnings = []
    decrypted = None
    if payload.get('privacy_flagged'):
        warnings.append({
            'code': 'privacy_flagged',
            'message': '这个订单被平台标了隐私（隐私号），平台给的姓名/电话/地址是打码的；'
                       '我们试过解密接口，但解不开就照原样显示，不会替你编明文。',
        })
        papi, pdata = order_addr_proto.privacy_request(order_id)
        # 解密是**尽力而为**：解不开就 decrypted=None + warning，绝不因此把整个只读页判成失败
        # （用户至少还能看到平台原样给的打码字段）。
        pvalue, pfailure = _call_api(client, papi, pdata, order_addr_proto.DEFAULT_VERSION,
                                     order_addr_proto.PRIVACY_VALUE_TYPE)
        if pfailure is None:
            decrypted = order_addr_proto.parse_privacy(pvalue)
        if decrypted is None:
            warnings.append({
                'code': 'privacy_decrypt_failed',
                'message': '隐私地址解密没有拿到明文（平台没给或拒绝），上面显示的是平台原样给的字段。',
            })
    payload['decrypted'] = decrypted
    payload['warnings'] = warnings
    return _stamp(payload)


def _order_address_write(account_id: str, action: str, order_id: Any, deliver_id: Any,
                         confirm: Any) -> Dict[str, Any]:
    """同意 / 拒绝的公共骨架。三步顺序：**确认 → 字段校验 → 建客户端**。"""
    if action == 'agree':
        problem = None if _truthy(confirm) else _confirm_required('同意修改收货地址')
    else:
        problem = None if _truthy(confirm) else _confirm_required('拒绝修改收货地址')
    if problem:
        return problem
    failure = _missing(('订单号', order_id), ('改地址申请 ID（deliver_id）', deliver_id))
    if failure:
        return failure

    client, failure = _client(account_id)
    if failure:
        return failure
    api, data = order_addr_proto.request_of(action, order_id=order_id,
                                            deliver_id=deliver_id, confirm=True)
    raw, failure = _call_write(client, api, data, order_addr_proto.DEFAULT_VERSION)
    if failure:
        common.log_operation(account_id, f'order_address_{action}', _text(order_id), False,
                             failure.get('message') or '')
        return failure

    payload = order_addr_proto.parse_write(raw)
    payload.update({
        'account_id': account_id,
        'order_id': _text(order_id),
        'deliver_id': _text(deliver_id),
        'action': action,
        'api': api,
    })
    common.log_operation(account_id, f'order_address_{action}', _text(order_id), True, data)
    common.publish('trade.changed', {'account_id': account_id, 'order_id': _text(order_id)})
    return _stamp(payload)


def order_address_agree(account_id: str, order_id: Any, deliver_id: Any, *,
                        confirm: Any = False) -> Dict[str, Any]:
    """同意买家修改收货地址（`mtop.idle.merchant.order.address.modify.agree`）。

    **这是会真的改订单收货地址的写操作**：`deliver_id` 必须来自同一条订单的
    `order_address_info()`（只有 `status == 'WAITING'` 时平台才给）。
    """
    return _order_address_write(account_id, 'agree', order_id, deliver_id, confirm)


def order_address_refuse(account_id: str, order_id: Any, deliver_id: Any, *,
                         confirm: Any = False) -> Dict[str, Any]:
    """拒绝买家修改收货地址（`mtop.idle.merchant.order.address.modify.refuse`）。"""
    return _order_address_write(account_id, 'refuse', order_id, deliver_id, confirm)


# --------------------------------------------------------------------------- 关单 / 延时 / 提醒
#
# 第三十八批：订单页的五个写能力。取证全部来自本地 bundle（零平台请求）：
#   close.by.seller      @2762506  v2.0  {tid, bizOrderId, closeReason}
#   close.reason.get     @2585779  v1.0  {bizOrderId} → data.closeReasons（字符串数组，只读）
#   batch.delay.confirm  @2758810  v1.0  {orderIdList, delayTime(秒)}
#   batch.remind.confirm @2023848  v1.0  {orderIdList, remindAllOrder}
#   postage.refund.pay   @2835556  v1.0  {orderId, refundId, payType} + ttid + needLoginPC
#   complain.apply.revoke@2814089  v1.0  {complainId} + needLoginPC → data.success/failReason


def close_reasons(account_id: str, order_id: Any) -> Dict[str, Any]:
    """关单原因候选 —— **只读**，所以没有 `confirm`。

    官方关单弹窗先调这个接口，把 `data.closeReasons`（字符串数组）直接当选项。
    平台没给列表键时 `close_reasons` 给 `None` + 一条中文 warning（不是空数组：
    「平台没给」和「平台给了空列表」是两件事，前者不该让用户以为「就是没有原因可选」）。
    """
    failure = _missing(('订单号', order_id))
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure
    api, data = proto.close_reason_request(order_id)
    raw, failure = _call_api(client, api, data, proto.CLOSE_REASON_VERSION)
    if failure:
        return failure
    payload = proto.parse_close_reasons(raw)
    payload['account_id'] = account_id
    payload['order_id'] = _text(order_id)
    payload['api'] = api
    warnings = []
    if payload.get('close_reasons') is None:
        warnings.append({
            'code': 'close_reasons_missing',
            'message': '平台没有返回关单原因列表（可能改了字段名），这里不替你编原因；'
                       '可以手填一个原因，或者去官方卖家工作台看当前可选值。',
        })
    payload['warnings'] = warnings
    return _stamp(payload)


def close_order(account_id: str, order_id: Any, close_reason: Any, *,
                confirm: Any = False) -> Dict[str, Any]:
    """关闭订单（`mtop.taobao.idle.trade.merchant.close.by.seller` v2.0）。

    **不可撤销**：关掉之后交易就结束了。`close_reason` 必须先选（官方也是必填），
    候选来自 `close_reasons()`。官方原话提醒：「确认关闭后，请确保已和买家协商达成一致，
    否则您将面临投诉或赔偿」。
    """
    if not _truthy(confirm):
        return _confirm_required('关闭订单')
    failure = _missing(('订单号', order_id), ('关闭原因', close_reason))
    if failure:
        return failure
    return _submit(account_id, 'trade_close_order', _text(order_id),
                   lambda: proto.close_order_request(order_id, close_reason, confirm=True),
                   proto.parse_write_result, proto.CLOSE_BY_SELLER_VERSION)


def delay_confirm(account_id: str, order_ids: Any, days: Any, *,
                  confirm: Any = False) -> Dict[str, Any]:
    """延长收货时间（`...merchant.batch.delay.confirm`，`delayTime` 是秒）。

    `days` 只认官方那四档（3/5/7/10）；`order_ids` 收数组，也收逗号串（AI 侧传串）。
    """
    if not _truthy(confirm):
        return _confirm_required('延长收货时间')
    failure = _missing(('订单号列表', order_ids), ('延长天数', days))
    if failure:
        return failure
    return _submit(account_id, 'trade_delay_confirm', '',
                   lambda: proto.delay_confirm_request(order_ids, days, confirm=True),
                   proto.parse_batch_result, proto.BATCH_DELAY_VERSION,
                   audit={'order_ids': _text(order_ids), 'days': _text(days)})


def remind_confirm(account_id: str, order_ids: Any, remind_all: Any = False, *,
                   confirm: Any = False) -> Dict[str, Any]:
    """提醒买家确认收货（`...merchant.batch.remind.confirm`）。"""
    if not _truthy(confirm):
        return _confirm_required('提醒确认收货')
    failure = _missing(('订单号列表', order_ids))
    if failure:
        return failure
    return _submit(account_id, 'trade_remind_confirm', '',
                   lambda: proto.remind_confirm_request(order_ids, remind_all, confirm=True),
                   proto.parse_batch_result, proto.BATCH_REMIND_VERSION,
                   audit={'order_ids': _text(order_ids), 'remind_all': _text(remind_all)})


def postage_refund_pay(account_id: str, order_id: Any, refund_id: Any,
                       pay_type: Any = 'alipay', amount: Any = None, *,
                       confirm: Any = False) -> Dict[str, Any]:
    """支付运费退款（`mtop.taobao.idle.merchant.postage.refund.pay`）—— **真的付钱**。

    `amount` 不参与请求（官方也不发），只进审计与返回值，方便事后对账；
    调用方（界面/主管 AI）应当先把它念给用户看再确认。
    """
    if not _truthy(confirm):
        return _confirm_required('支付运费退款')
    failure = _missing(('订单号', order_id), ('退款单号', refund_id))
    if failure:
        return failure
    result = _submit(account_id, 'trade_postage_refund_pay', _text(order_id),
                     lambda: proto.postage_refund_pay_request(order_id, refund_id, pay_type,
                                                              confirm=True),
                     proto.parse_write_result, proto.POSTAGE_PAY_VERSION,
                     audit={'order_id': _text(order_id), 'refund_id': _text(refund_id),
                            'pay_type': _text(pay_type), 'amount': _text(amount)})
    if common.is_failure(result):
        return result
    result['amount'] = _text(amount) or None
    result['pay_type'] = _text(pay_type).strip() or 'alipay'
    return result


def complain_revoke(account_id: str, complain_id: Any, *,
                    confirm: Any = False) -> Dict[str, Any]:
    """撤销投诉申请（`mtop.taobao.idle.cco.shop.complain.apply.revoke`）。

    平台在这条上**顶层 success 仍可能是 true**，业务失败在 `data.success` + `data.failReason`
    （与投诉拒绝同族），所以复用 `complain_business_failure()`。
    """
    if not _truthy(confirm):
        return _confirm_required('撤销投诉申请')
    failure = _missing(('投诉单号', complain_id))
    if failure:
        return failure
    result = _submit(account_id, 'trade_complain_revoke', _text(complain_id),
                     lambda: proto.complain_revoke_request(complain_id, confirm=True),
                     proto.parse_write_result, proto.COMPLAIN_REVOKE_VERSION)
    if common.is_failure(result):
        return result
    business = complain_business_failure(result)
    if business:
        return business
    return result


def add_memo(account_id: str, order_id: Any, memo: Any, memo_group: Any = 1, *,
             confirm: Any = False) -> Dict[str, Any]:
    """订单备注（`mtop.taobao.idle.merchant.add.memo`）。

    **这条备注不对买家展示**（官方输入框 placeholder 原话），只是卖家自己的标记；
    官方把备注与标记**整份**提交（`memo` + `memoGroup` 1~5）。
    校验提示逐字照抄官方（非空 / 换行 / emoji / 白名单），50 字上限由我们自己拦。
    平台这条**没有回执字段**，所以成功但拿不到业务体时只加 warning ——
    不谎报「平台已确认这条备注生效了」。
    """
    if not _truthy(confirm):
        return _confirm_required('订单备注')
    failure = _missing(('订单号', order_id), ('备注内容', memo))
    if failure:
        return failure
    try:
        api, data = proto.add_memo_request(order_id, memo, memo_group, confirm=True)
    except (proto.WriteNotConfirmed, ValueError) as exc:
        code = CONFIRM_CODE if isinstance(exc, proto.WriteNotConfirmed) else 'invalid_fields'
        return common.fail(str(exc), code)
    audit = {'memo_length': len(_text(memo)), 'memo_group': _text(memo_group)}
    payload = _submit(account_id, 'trade_add_memo', _text(order_id),
                      lambda: (api, data), proto.parse_memo, proto.ADD_MEMO_VERSION,
                      audit=audit)
    if common.is_failure(payload):
        return payload
    if not payload.get('echoed'):
        payload['warnings'] = [{
            'code': 'trade_memo_no_echo',
            'message': ('平台接受了这次提交，但这条接口没有回执字段 —— '
                        '无法确认备注是否已生效，请到闲鱼订单页核对。'),
        }]
    payload['submitted'] = audit
    return payload


# --------------------------------------------------------------------------- IM 侧订单处理 + 商品 SKU / 验货报告 / 智能表单
#
# 第 60 批：官方交易页在 IM 聊天侧另有一组同功能不同名的接口（bundle 偏移见协议层注释）：
#   order.cancel          @1832140 v2.0 {tid,bizOrderId,closeReason}（写）
#   close.by.seller       @1831960 v2.0 {tid,bizOrderId,closeReason}（写，与上面同载荷不同名）
#   seller.delay.confirm  @1832406 v1.0 {delayTime(秒),orderId}（写，单订单）
#   user.adjust.price     @1831813 v1.0 {orderId, <skuKey>: '<分>'}（写，逐 SKU）
#   common.sku.selector   @1841759 v1.0 {itemId} → data.{components,skuInfo}（只读）
#   order.backfill.report @2608355 v1.0 {orderId} → data.code/data.data（只读）
#   smart.form.query      @3077683 v1.0 <URL 查询参数> → data.result.formSchema（只读）
#   smart.form.callback   @3081308 v1.0 {callbackCode,extJson}（写）
#
# 官方工作台**没有**前四条中的任何一条（main.js 里查无此名），所以不合并成别名：
# 各自一个服务函数、各自一条路由，由调用方选路。


def _read_api(account_id: str, api: str, data: Any, version: str, parse) -> Dict[str, Any]:
    """只读接口的公共骨架：`_call_api` + 解析 + 打戳（写类**不走**这里）。"""
    client, failure = _client(account_id)
    if failure:
        return failure
    raw, failure = _call_api(client, api, data, version)
    if failure:
        return failure
    payload = parse(raw)
    payload['account_id'] = account_id
    payload['api'] = api
    return _stamp(payload)


def order_cancel(account_id: str, order_id: Any, close_reason: Any, *,
                 confirm: Any = False) -> Dict[str, Any]:
    """取消订单（IM 侧 `mtop.taobao.idle.trade.order.cancel` v2.0）。

    与 `close_order` **同载荷不同接口名**：官方 IM 聊天页在同一处按开关二选一
    （偏移 1853279/1853504），两个名字都真实存在，所以保留两条路。
    **不可撤销**，原因必填（候选来自 `close_reasons`）。
    """
    if not _truthy(confirm):
        return _confirm_required('取消订单')
    failure = _missing(('订单号', order_id), ('取消原因', close_reason))
    if failure:
        return failure
    return _submit(account_id, 'trade_order_cancel', _text(order_id),
                   lambda: proto.order_cancel_request(order_id, close_reason, confirm=True),
                   proto.parse_write_result, proto.ORDER_CANCEL_VERSION)


def close_by_seller_im(account_id: str, order_id: Any, close_reason: Any, *,
                       confirm: Any = False) -> Dict[str, Any]:
    """关闭订单（IM 侧 `mtop.taobao.idle.trade.close.by.seller` v2.0）。

    与工作台那条（`...merchant.close.by.seller`）同载荷、不同接口名；工作台 bundle 里
    没有这个名字，所以单独登记，不拿 `in` 判成同一个。**不可撤销**，原因必填。
    """
    if not _truthy(confirm):
        return _confirm_required('关闭订单')
    failure = _missing(('订单号', order_id), ('关闭原因', close_reason))
    if failure:
        return failure
    return _submit(account_id, 'trade_close_by_seller_im', _text(order_id),
                   lambda: proto.close_by_seller_im_request(order_id, close_reason, confirm=True),
                   proto.parse_write_result, proto.CLOSE_BY_SELLER_IM_VERSION)


def seller_delay_confirm(account_id: str, order_id: Any, days: Any, *,
                         confirm: Any = False) -> Dict[str, Any]:
    """延长收货（**单个订单**，`mtop.taobao.idle.trade.seller.delay.confirm` v1.0）。

    `delayTime` 单位秒（协议层按 `days × 86400` 换算），`days` 只认 3/5/7/10 四档。
    批量版是 `delay_confirm`（另一个接口、另一个载荷），别混。
    """
    if not _truthy(confirm):
        return _confirm_required('延长收货时间')
    failure = _missing(('订单号', order_id), ('延长天数', days))
    if failure:
        return failure
    return _submit(account_id, 'trade_seller_delay_confirm', _text(order_id),
                   lambda: proto.seller_delay_confirm_request(order_id, days, confirm=True),
                   proto.parse_write_result, proto.SELLER_DELAY_CONFIRM_VERSION,
                   audit={'order_id': _text(order_id), 'days': _text(days)})


def user_adjust_price(account_id: str, order_id: Any, skus: Any, *,
                      confirm: Any = False) -> Dict[str, Any]:
    """改价（IM 侧 `mtop.taobao.idle.trade.user.adjust.price` v1.0）—— **逐 SKU**。

    `skus` 是 `{skuKey: 价格}`：键名由调用方原样透传（平台字段名不替它编），
    值口径见协议层 `_sku_price_fen`（非整数按元、整数按分）。
    """
    if not _truthy(confirm):
        return _confirm_required('修改订单价格')
    failure = _missing(('订单号', order_id), ('SKU 价格', skus))
    if failure:
        return failure
    try:
        api, data = proto.user_adjust_price_request(order_id, skus, confirm=True)
    except (proto.WriteNotConfirmed, ValueError) as exc:
        code = CONFIRM_CODE if isinstance(exc, proto.WriteNotConfirmed) else 'invalid_fields'
        return common.fail(str(exc), code)
    audit = {'order_id': _text(order_id), 'sku_keys': sorted(k for k in data if k != 'orderId')}
    return _submit(account_id, 'trade_user_adjust_price', _text(order_id),
                   lambda: (api, data), proto.parse_write_result,
                   proto.USER_ADJUST_PRICE_VERSION, audit=audit)


def sku_selector(account_id: str, item_id: Any) -> Dict[str, Any]:
    """SKU 选择器 —— **只读**，所以没有 `confirm`。

    官方读 `data.{components,skuInfo}`，在 `components` 里按 `render` 找
    `skuItemInfo` / `specificationInfo` / `quantityInfo`。平台没给 `components` 时
    加一条中文 warning —— 不替用户编规格出来。
    """
    failure = _missing(('商品 ID', item_id))
    if failure:
        return failure
    api, data = proto.sku_selector_request(item_id)
    payload = _read_api(account_id, api, data, proto.SKU_SELECTOR_VERSION, proto.parse_sku_selector)
    if common.is_failure(payload):
        return payload
    payload['item_id'] = _text(item_id)
    warnings: list = []
    if payload.get('components') is None:
        warnings.append({
            'code': 'sku_selector_missing',
            'message': '平台没有返回 SKU 组件（可能改了字段名），这里不替你编规格；请到官方商品页核对。',
        })
    payload['warnings'] = warnings
    return payload


def backfill_report(account_id: str, order_id: Any) -> Dict[str, Any]:
    """验货报告 —— **只读**，所以没有 `confirm`。

    官方只在 `data.code == 'success'` 时取 `data.data`；成功但没给报告体时
    `report=None` + warning（如实报「平台没给」，不造空对象）。
    """
    failure = _missing(('订单号', order_id))
    if failure:
        return failure
    api, data = proto.backfill_report_request(order_id)
    payload = _read_api(account_id, api, data, proto.BACKFILL_REPORT_VERSION,
                        proto.parse_backfill_report)
    if common.is_failure(payload):
        return payload
    payload['order_id'] = _text(order_id)
    warnings: list = []
    if not payload.get('report_ok'):
        warnings.append({
            'code': 'backfill_report_unavailable',
            'message': '平台没有给出这一单的报告（data.code=%s），这里不替你编报告内容；'
                       '可以去官方卖家工作台核对。' % (payload.get('code') or '未知'),
        })
    elif payload.get('report') is None:
        warnings.append({
            'code': 'backfill_report_empty',
            'message': '平台回了成功但没带报告体（可能字段名变了），这里不替你编报告内容。',
        })
    payload['warnings'] = warnings
    return payload


def smart_form_query(account_id: str, params: Any) -> Dict[str, Any]:
    """智能表单元数据 —— **只读**，所以没有 `confirm`。

    `params` 是页面 URL 的查询参数（官方原样发出去的那个对象），键名不替平台编。
    """
    failure = _missing(('表单参数', params))
    if failure:
        return failure
    try:
        api, data = proto.smart_form_query_request(params)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')
    payload = _read_api(account_id, api, data, proto.SMART_FORM_QUERY_VERSION,
                        proto.parse_smart_form_query)
    if common.is_failure(payload):
        return payload
    payload['params'] = data
    warnings: list = []
    if payload.get('schema') is None:
        warnings.append({
            'code': 'smart_form_schema_missing',
            'message': '平台没有返回表单结构（可能字段名变了），这里不替你编表单；请到官方页面核对。',
        })
    payload['warnings'] = warnings
    return payload


def smart_form_callback(account_id: str, callback_code: Any, ext: Any = None, *,
                        confirm: Any = False) -> Dict[str, Any]:
    """提交智能表单（`mtop.taobao.merchant.heracles.inno.smart.form.callback` v1.0）。

    成功判据是 `data.result.success`（官方成功 toast「填写成功」），**不是**顶层 `success`；
    平台没给这个字段时 `form_success=None` + warning —— 不拿顶层成功冒充业务成功。
    """
    if not _truthy(confirm):
        return _confirm_required('提交智能表单')
    failure = _missing(('callbackCode', callback_code), ('表单内容', ext))
    if failure:
        return failure
    try:
        api, data = proto.smart_form_callback_request(callback_code, ext, confirm=True)
    except (proto.WriteNotConfirmed, ValueError) as exc:
        code = CONFIRM_CODE if isinstance(exc, proto.WriteNotConfirmed) else 'invalid_fields'
        return common.fail(str(exc), code)
    audit = {'callback_code': _text(callback_code), 'ext_length': len(_text(data.get('extJson')))}
    payload = _submit(account_id, 'trade_smart_form_callback', _text(callback_code),
                      lambda: (api, data), proto.parse_smart_form_callback,
                      proto.SMART_FORM_CALLBACK_VERSION, audit=audit)
    if common.is_failure(payload):
        return payload
    if payload.get('form_success') is None:
        payload['warnings'] = [{
            'code': 'smart_form_no_receipt',
            'message': ('平台接受了这次提交，但没有回执字段（data.result.success）—— '
                        '无法确认表单是否已生效，请到官方页面核对。'),
        }]
    payload['submitted'] = audit
    return payload


# --------------------------------------------------------------------------- 赔付 / 维权服务
#
#   detail.query   @831798  v1.0  {orderId, serviceCode} + **valueType:'string'**（只读）
#   refuse.render  @827668  v1.0  {serviceCode} → data.module.reasonChoices（只读）
#   refuse         bundle 模块 26675 v1.0 {orderId, serviceCode, sellerRefuseReason(JSON 串),
#                  refuseProofs(JSON 串), isManual} + needLoginPC（凭证必填）
#   同族的 compensate.service.pay（真付钱）**故意不做**，理由见协议层 docstring。


def compensate_detail(account_id: str, order_id: Any, service_code: Any) -> Dict[str, Any]:
    """赔付详情 —— **只读**，所以没有 `confirm`。

    官方这一条带 `valueType:'string'`（`data` 是 JSON 字符串），必须原样传下去，
    否则响应形状可能不是官方看到的那一种（协议层 `_module_of` 两种都认）。
    平台没给 `module` 时给 `found=False` + 中文 warning —— **不替用户编一笔赔付出来**。
    """
    failure = _missing(('订单号', order_id), ('赔付服务号', service_code))
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure
    api, data = cp_proto.detail_request(order_id, service_code)
    raw, failure = _call_api(client, api, data, cp_proto.DEFAULT_VERSION,
                             cp_proto.DETAIL_VALUE_TYPE)
    if failure:
        return failure
    payload = cp_proto.parse_detail(raw)
    payload['account_id'] = account_id
    payload['order_id'] = payload.get('order_id') or _text(order_id)
    payload['service_code'] = payload.get('service_code') or _text(service_code)
    payload['api'] = api
    warnings = []
    if not payload.get('found'):
        warnings.append({
            'code': 'compensate_detail_missing',
            'message': '平台没有返回这笔赔付的详情（可能订单号 / 赔付服务号不对，'
                       '也可能平台改了字段名）；这里不替你猜一笔赔付出来，'
                       '请到官方卖家工作台核对这笔维权。',
        })
    payload['warnings'] = warnings
    return _stamp(payload)


def compensate_refuse_form(account_id: str, service_code: Any) -> Dict[str, Any]:
    """拒绝赔付的原因候选 —— **只读**。

    平台没给 `reasonChoices` 时给 `None` + warning（**不是空数组**：
    「平台没给」和「平台给了空列表」是两件事，前者不该让用户以为「就是没有原因可选」）。
    """
    failure = _missing(('赔付服务号', service_code))
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure
    api, data = cp_proto.refuse_form_request(service_code)
    raw, failure = _call_api(client, api, data, cp_proto.DEFAULT_VERSION)
    if failure:
        return failure
    payload = cp_proto.parse_refuse_form(raw)
    payload['account_id'] = account_id
    payload['service_code'] = _text(service_code)
    payload['api'] = api
    warnings = []
    if payload.get('reason_choices') is None:
        warnings.append({
            'code': 'compensate_reasons_missing',
            'message': '平台没有返回拒绝原因候选（可能改了字段名）；这里不替你编原因，'
                       '可以手填一个原因，或者去官方卖家工作台看当前可选值。',
        })
    payload['warnings'] = warnings
    return _stamp(payload)


def compensate_refuse(account_id: str, order_id: Any, service_code: Any, reason_choices: Any,
                      reason_desc: Any = '', proof_urls: Any = (), is_manual: Any = False, *,
                      confirm: Any = False) -> Dict[str, Any]:
    """拒绝赔付（`...merchant.compensate.service.refuse`）—— **不可撤销**。

    官方的两条硬约束照搬：**凭证必填**（原话「请上传凭证」）、说明最多 200 字。
    字段校验排在「建客户端」**之前**（确认 → 字段校验 → 建客户端），
    所以拼错原因 / 忘传凭证时不会白跑一次凭据检查，也一个平台请求都不发。
    """
    if not _truthy(confirm):
        return _confirm_required('拒绝赔付')
    failure = _missing(('订单号', order_id), ('赔付服务号', service_code))
    if failure:
        return failure
    try:
        api, data = cp_proto.refuse_request(order_id, service_code, reason_choices,
                                            reason_desc, proof_urls, is_manual, confirm=True)
    except (cp_proto.UnknownApi, cp_proto.WriteNotConfirmed, ValueError) as exc:
        code = CONFIRM_CODE if isinstance(exc, cp_proto.WriteNotConfirmed) else 'invalid_fields'
        return common.fail(str(exc), code)
    audit = {
        'order_id': _text(order_id),
        'service_code': _text(service_code),
        'reason_choices': len(cp_proto._choices(reason_choices)),
        'proof_count': len(cp_proto._urls(proof_urls)),
        'is_manual': bool(is_manual),
    }
    payload = _submit(account_id, 'trade_compensate_refuse', _text(order_id),
                      lambda: (api, data), cp_proto.parse_refuse, cp_proto.DEFAULT_VERSION,
                      audit=audit)
    if common.is_failure(payload):
        return payload
    if payload.get('platform_success') is False:
        # 平台收下了请求但明确说「没成功」（`data.success=false` + `data.failReason`）：
        # 这种情况**绝不能**当成功返回，否则用户会以为拒绝赔付已经生效。
        reason = payload.get('platform_message') or '平台拒绝了这次拒绝赔付'
        logger.warning(f'[trade_write] 平台拒绝赔付 {payload.get("api")}: {reason}')
        return common.fail(f'平台接口失败: {reason}', 'protocol_error',
                           platform_errors={'api': payload.get('api'), 'explain': reason})
    payload['submitted'] = audit
    return payload
