# -*- coding: utf-8 -*-
"""安全中心**写**接口（申诉表单获取 / 提交申诉 / 催办）—— 协议层。

**接口来源（静态 bundle 反解，零风控）**：官方安全中心微应用
`https://g.alicdn.com/idle-pc/idle-seller-security-center/0.0.6/js/main.js`（4441635 B）。
下面每个 `api` / `v` / `data` 键名都是从这份 bundle 里逐条抄下来的，常量后注明
**字节偏移**（本地副本 `C:\\Users\\Administrator\\AppData\\Local\\Temp\\secwrite\\main.js`，
可用偏移复核原文）。

**全部未经真机验证**：写接口禁止对真实平台试打（提交申诉会真的改变账号处罚状态），
参数形状只保证「与官方前端发出的完全一致」。

**站点 CODE 头（关键前提）**：这批接口不带 `idle_site_biz_code: COMMONPRO` 时，
服务端回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` 或 `data.errorMsg="站点CODE不能为空"`
（真机事实，与只读安全中心同因）。头由 `site_headers()` 从客户端借
（`XianyuClient._security_headers()`，`backend/app/xianyu/client.py:941-949`）。

**确认闸门**：三个写请求构造器都要 `confirm=True`，默认 False 时抛 `WriteNotConfirmed`。
`appeal_form_request()` **刻意不带闸门** —— 它是只读（获取申诉表单），不是写操作，
详见 `docs/contracts/security_write.md`。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from .item_templates import site_headers
from .trade import body_of, decode_payload, envelope_error, envelope_message
from .trade_write import (
    WriteNotConfirmed,
    _int_or_none,
    _list_of,
    _or_none,
    _text,
    parse_write_result,
    require_confirm,
)

__all__ = [
    'APPEAL_FORM_GET_API', 'APPEAL_SUBMIT_API', 'APPEAL_URGE_API', 'APPEAL_SUBMIT_CRO_API',
    'APPEAL_VERSION', 'APPEAL_PROOF_CODES', 'CRO_READ_APIS', 'WRITE_APIS', 'READ_APIS',
    'WriteNotConfirmed', 'require_confirm', 'site_headers',
    'appeal_form_request', 'appeal_submit_request', 'appeal_submit_cro_request',
    'appeal_urge_request', 'proof_item',
    'parse_appeal_form', 'parse_appeal_submit', 'parse_appeal_urge', 'appeal_submit_failure',
]

APPEAL_VERSION = '1.0'

#: 申诉表单获取（**只读**）。bundle 偏移 3110899：
#: `{formId:o}` → `L7({api:"mtop.idle.pc.security.appeal.form.get",v:"1.0",data:a,needLoginPC:!0,showErrorToast:!0})`，
#: 调用点读的是 `n.data.formData`（`v(n?.data?.formData)`）。
APPEAL_FORM_GET_API = 'mtop.idle.pc.security.appeal.form.get'

#: 提交申诉（安全中心 pc 弹窗「违规申诉」）。bundle 偏移 3111437：
#: `{parameter:e}` → `L7({api:"mtop.idle.pc.security.appeal.submit",v:"1.0",data:s,needLoginPC:!0,showErrorToast:!0})`，
#: 成功判据是 `o?.data?.appealId` 为真（然后 `message.success('提交成功')`）。
APPEAL_SUBMIT_API = 'mtop.idle.pc.security.appeal.submit'

#: 催办（加急处理）。bundle 偏移 3066600：
#: `{punishId:Number(s)}` → `L7({api:"mtop.idle.pc.security.appeal.urge",v:"1.0",data:c,...})`，
#: 成功判据是 `t?.data` 为真（然后 `message.success('已为您加急处理')`）。
APPEAL_URGE_API = 'mtop.idle.pc.security.appeal.urge'

#: 提交申诉（复核 cro 流程；当 `viewConfig.appealConfig.appealUrl` 含 `cro-security-center`
#: 时官方走这一支，见 bundle 偏移 3066600 附近的 `N.includes("cro-security-center")`）。
#: bundle 偏移 2860925：`L7({api:"mtop.idle.security.appeal.submit",v:"1.0",data:{parameter:e}})`
#: —— 注意这里**没有** `needLoginPC`；成功判据同样是 `data.appealId`。
APPEAL_SUBMIT_CRO_API = 'mtop.idle.security.appeal.submit'

#: cro 提交的 `proofItems[].code` 取值。bundle 偏移 3050908 / 3051071 / 3052626 三处同一形状：
#: `[{code:"appealUpload",name:"上传凭证",index:t,details:(e.mediaList||[]).map(e=>({value:JSON.stringify(e),desc:JSON.stringify(e)}))},
#:   {code:"appealDesc",…}, {code:"appealReason",…}, {code:"questionId",…}, {code:"questionName",…}]`
#: 拼成 `{punishId:Number(c), proofItems:n}` 后 `JSON.stringify` 作为 `parameter` 传。
APPEAL_PROOF_CODES: Tuple[str, ...] = (
    'appealUpload', 'appealDesc', 'appealReason', 'questionId', 'questionName',
)

#: 申诉/复核相关但**本模块不实现**的接口：bundle 里调用点把 `data` 直接透传
#: （`L7({api:"…",v:"1.0",data:e})`），字段形状没挖到；而且它们读的是凭证/结果列表
#: （只读）。**不实现、不猜字段**。
CRO_READ_APIS: Dict[str, str] = {
    'proofs': 'mtop.taobao.idle.selected.re.verify.cro.appeal.proofs',        # bundle 偏移 3077366
    'results': 'mtop.taobao.idle.selected.re.verify.cro.appeal.results',      # bundle 偏移 3081199
    'canAppealAnswers': 'mtop.taobao.idle.selected.re.verify.cro.can.appeal.answers',  # 偏移 2860254
}

#: 本模块声明的**写**接口（全部有 `confirm` 闸门）。
WRITE_APIS: Tuple[str, ...] = (APPEAL_SUBMIT_API, APPEAL_URGE_API, APPEAL_SUBMIT_CRO_API)

#: 本模块声明的**只读**接口（没有闸门）。
READ_APIS: Tuple[str, ...] = (APPEAL_FORM_GET_API,)


# --------------------------------------------------------------------------- 请求构造


def appeal_form_request(form_id: Any, *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """获取申诉表单（**只读**，`data:{formId}`）。`formId` 来自违规详情下发的表单 id。

    刻意**不**加 `confirm` 闸门：它不改账号状态。`confirm` 参数保留只为签名统一。
    """
    return APPEAL_FORM_GET_API, {'formId': _text(form_id)}


def appeal_submit_request(parameter: Any, *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """提交申诉（安全中心 pc 弹窗）。

    `parameter` 是表单值：`dict` → `json.dumps(ensure_ascii=False)`，`str` → 原样透传。
    bundle 只显示 `data:{parameter:e}`，没显示 `e` 的类型；同类调用点
    （`mtop.idle.security.appeal.submit`，偏移 3052626）用的是 `JSON.stringify(...)`，
    所以「JSON 字符串」一定是合法形状之一 —— 两种都收，**不替调用方编内容**。
    """
    require_confirm(confirm, '提交申诉')
    if isinstance(parameter, str):
        text = parameter.strip()
        if not text:
            raise ValueError('申诉表单内容不能为空')
        return APPEAL_SUBMIT_API, {'parameter': text}
    if isinstance(parameter, dict) and parameter:
        return APPEAL_SUBMIT_API, {'parameter': json.dumps(parameter, ensure_ascii=False)}
    raise ValueError('申诉表单内容必须是表单对象或 JSON 字符串')


def proof_item(code: Any, details: Any, index: Any = 0, name: Any = '') -> Dict[str, Any]:
    """构造一个 `proofItems[]` 元素：`{code,name,index,details:[{value,desc}]}`。

    `details` 官方是三处 `{value,desc}` 的数组（凭证是 `JSON.stringify(media)`）。
    """
    code_text = _text(code)
    if not code_text:
        raise ValueError('proofItems 的 code 不能为空')
    items: List[Dict[str, Any]] = []
    for detail in _list_of(details):
        if isinstance(detail, dict):
            items.append({'value': _text(detail.get('value')), 'desc': _text(detail.get('desc'))})
        else:
            items.append({'value': _text(detail), 'desc': _text(detail)})
    return {
        'code': code_text,
        'name': _text(name),
        'index': _int_or_none(index) if _int_or_none(index) is not None else 0,
        'details': items,
    }


def appeal_submit_cro_request(punish_id: Any, proof_items: Any, *,
                              confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """提交申诉（复核 cro 流程）：`parameter = JSON.stringify({punishId:Number(...), proofItems:[...]})`。"""
    require_confirm(confirm, '提交申诉（复核流程）')
    number = _int_or_none(punish_id)
    if number is None:
        raise ValueError('处罚单号必须是数字')
    items = [_item if isinstance(_item, dict) else proof_item(**_item)
             for _item in _list_of(proof_items)]
    return APPEAL_SUBMIT_CRO_API, {
        'parameter': json.dumps({'punishId': number, 'proofItems': items}, ensure_ascii=False),
    }


def appeal_urge_request(punish_id: Any, *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """催办（加急处理）：`data:{punishId:Number(...)}`（官方对 `punishId` 做了 `Number()`）。"""
    require_confirm(confirm, '申诉催办')
    number = _int_or_none(punish_id)
    if number is None:
        raise ValueError('处罚单号必须是数字')
    return APPEAL_URGE_API, {'punishId': number}


# --------------------------------------------------------------------------- 响应解析


def _body(raw: Any) -> Dict[str, Any]:
    raw = decode_payload(raw)
    body = body_of(raw)
    return body if isinstance(body, dict) else {}


def parse_appeal_form(raw: Any) -> Dict[str, Any]:
    """表单响应：官方读 `data.formData`；平台没给就是 `None`（**不补 `{}`**）。"""
    payload = parse_write_result(raw)
    payload['form_data'] = _or_none(_body(raw).get('formData'))
    return payload


def parse_appeal_submit(raw: Any) -> Dict[str, Any]:
    """提交响应：官方读 `data.appealId`；平台没给就是 `None`（**不补 0**）。"""
    payload = parse_write_result(raw)
    payload['appeal_id'] = _or_none(_body(raw).get('appealId'))
    return payload


def parse_appeal_urge(raw: Any) -> Dict[str, Any]:
    """催办响应：官方判据是 `data` 为真（没有具体字段），所以只透出 `platform_body`。

    `urged` 为 `True` 表示平台确实回了 `data`；平台没回就是 `None`（**不补 False**）。
    """
    payload = parse_write_result(raw)
    payload['urged'] = True if payload.get('platform_body') is not None else None
    return payload


def appeal_submit_failure(raw: Any) -> str:
    """申诉提交的**业务级**失败：官方唯一成功判据是 `data.appealId` 为真。

    这类失败顶层 `success` 仍是 true（`envelope_error()` 抓不到），所以单独提供。
    返回 `''` 表示没判定出业务级失败。
    """
    raw = decode_payload(raw)
    if not isinstance(raw, dict):
        return '平台没有返回申诉受理结果'
    body = _body(raw)
    if _or_none(body.get('appealId')) is not None:
        return ''
    return _text(body.get('errorMsg')) or '平台没有返回申诉受理结果（缺少 appealId）'


#: 供文档/自检核对：本模块关心的全部 mtop 名（写 + 只读 + 未实现）。
ALL_APIS: Tuple[str, ...] = WRITE_APIS + READ_APIS + tuple(CRO_READ_APIS.values())

#: `envelope_message` 在这里 re-export，方便服务层只依赖本模块。
__all__ += ['ALL_APIS', 'envelope_message']
