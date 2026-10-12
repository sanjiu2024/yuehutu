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
from ..xianyu import order_address as order_addr_proto
from ..xianyu import trade_write as proto
from . import common
# 账号/凭据检查只有一份，直接复用只读服务里的私有助手（同包内，避免两套判定漂移）
from .trade import _client, _stamp, _text

#: 未确认时的失败码：`common.FAIL_STATUS` 里没有它 → `common.respond` 落到 400。
CONFIRM_CODE = 'confirmation_required'


# --------------------------------------------------------------------------- 前置检查


def _confirm_required(action: str) -> Dict[str, Any]:
    return common.fail(
        f'{action} 是写操作，需要显式确认（confirm=true）后才会提交',
        CONFIRM_CODE,
    )


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
    """写请求的公共骨架：**已确认**之后才进来（调用方负责闸门与参数校验）。"""
    client, failure = _client(account_id)
    if failure:
        return failure
    try:
        api, data = builder()
    except (proto.WriteNotConfirmed, ValueError) as exc:
        return common.fail(str(exc), CONFIRM_CODE if isinstance(exc, proto.WriteNotConfirmed)
                           else 'invalid_fields')
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
    if not confirm:
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
    if not confirm:
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
    if not confirm:
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


# --------------------------------------------------------------------------- 退款处理


def refund_agree(account_id: str, refund_id: Any, auth_token: Any,
                 *, confirm: bool = False) -> Dict[str, Any]:
    """同意退款：`mtop.taobao.idle.merchant.refund.agree.refund`。

    `auth_token` 是平台下发的操作令牌（退款详情/渲染接口里给），必须原样回传；
    服务层**不会**自己去猜或缓存它。
    """
    if not confirm:
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
    if not confirm:
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
    if not confirm:
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
    if not confirm:
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


def _truthy(value: Any) -> bool:
    """只有字面量 `True` / `'true'` / `1` 才算确认，别的（含 `'false'`、`'yes'`）都不算。

    与 `services/items.py::_truthy`、`services/item_templates.py::_truthy`、
    `mcp_server/client.py::_truthy` 同一口径 —— 三条路（HTTP / 主管 AI / MCP）必须对
    「什么算点头」有完全一样的判断。改退货地址是不可撤销的写操作，`"confirm": "false"`
    这种字符串**绝不能**被当成点头。
    """
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() == 'true'
    return value == 1


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
                            detail_address: Any) -> Optional[Dict[str, Any]]:
    """必填 + 长度 + 手机号 —— **纯本地**，所以排在建客户端之前（掉线时也该先报「名字没填」）。"""
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

    **三步顺序：确认 → 字段校验 → 建客户端**。`area_id` 是区级区域码，我们没有来源，
    留空就**不发这个键**（不伪造）；官方还会发 `division` 级联数组，我们故意不发
    （理由见 `xianyu/address_write.py` 的模块 docstring）。
    """
    if not _truthy(confirm):
        return _confirm_required('新增退货地址')
    failure = _address_fields_problem(contact_name, mobile_phone, province_name, city_name,
                                      district_name, detail_address)
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
                                      district_name, detail_address)
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
