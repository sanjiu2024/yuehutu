"""话术（官方 IM 工作台的「快捷回复」）协议层：列表读取 + 分组/短语的增删改。

证据全部来自本地 bundle 副本（**零平台请求**）：
`C:\\Users\\Administrator\\.dsh-tmp\\seller_scan\\js_008_vendors.js`（5,857,322 字符单行压缩 JS）。
提取脚本与原始窗口：`logs/quickreply_evidence.txt`、`logs/quickreply_callsites.txt`、
`logs/quickreply_payloads.txt`（`C:\\Users\\Administrator\\.dsh-tmp\\quickreply_*.py`）。

三个官方接口（逐字）：

* ``LIST_API`` @1386170 / @1895784
  ``e3.G({api:'mtop.taobao.idlemessage.quickreply.list.get.v1', v:'1.0', data:eu})``
  → 读 ``resp.data.groups``；调用点 ``b$({})``（**不带参数**），组件里 ``ef.groups``。
* ``GROUP_API`` @1882034
  ``e3.G({api:'…quickreply.group.operate', v:'1.0', needLoginPC:!0, data:eu})``
  → 读 ``resp.data.groupInfo``；三个动作的入参逐字：
    create @1884723 ``{groupName, operateType:'create'}``
    update @1884274 ``{groupId, groupName, operateType:'update'}``
    delete @1883628 ``{groupId, operateType:'delete'}``
* ``CONTENT_API`` @1885821
  ``e3.G({api:'…quickreply.content.operate', v:'1.0', needLoginPC:!0, data:eu})``
  → 读 ``resp.data.contentInfo``；三个动作的入参逐字：
    create @1888550 ``{groupId, content, operateType:'create'}``
    update @1887993 ``{groupId, contentId, content, operateType:'update'}``
    delete @1887159 ``{groupId, contentId, operateType:'delete'}``

官方的两个上限（我们照抄）：分组名输入框 ``maxLength:10``（@1889xxx ``placeholder:"请输入"``）、
短语内容 ``maxLength:200`` 且 ``onChange`` 里再 ``slice(0,200)``。

成功判据照官方：回包里 ``groupInfo.groupId`` / ``contentInfo.contentId`` 存在；
失败时官方读 ``data.error``（由 ``ret[0]`` 归一化而来）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .trade import _text, body_of, decode_payload, envelope_error, envelope_message

# --------------------------------------------------------------------------- 常量

LIST_API = 'mtop.taobao.idlemessage.quickreply.list.get.v1'
GROUP_API = 'mtop.taobao.idlemessage.quickreply.group.operate'
CONTENT_API = 'mtop.taobao.idlemessage.quickreply.content.operate'

DEFAULT_VERSION = '1.0'

GROUP_NAME_MAX = 10
CONTENT_MAX = 200

GROUP_OPERATE_TYPES = ('create', 'update', 'delete')
CONTENT_OPERATE_TYPES = ('create', 'update', 'delete')

#: 写动作 → 接口与「发出去的键」，每条都带 bundle 偏移证据。
WRITE_APIS: Dict[str, Dict[str, Any]] = {
    'group_create': {
        'api': GROUP_API,
        'data': ('groupName', 'operateType'),
        'evidence': "js_008_vendors.js @1884723 `e_({groupName:eu,operateType:'create'})`",
    },
    'group_update': {
        'api': GROUP_API,
        'data': ('groupId', 'groupName', 'operateType'),
        'evidence': "js_008_vendors.js @1884274 `e_({groupId:eg.groupId,groupName:eu,operateType:'update'})`",
    },
    'group_delete': {
        'api': GROUP_API,
        'data': ('groupId', 'operateType'),
        'evidence': "js_008_vendors.js @1883628 `e_({groupId:eu,operateType:'delete'})`",
    },
    'phrase_create': {
        'api': CONTENT_API,
        'data': ('groupId', 'content', 'operateType'),
        'evidence': "js_008_vendors.js @1888550 `eS({groupId:eu,content:eg,operateType:'create'})`",
    },
    'phrase_update': {
        'api': CONTENT_API,
        'data': ('groupId', 'contentId', 'content', 'operateType'),
        'evidence': "js_008_vendors.js @1887993 `eS({groupId:eu,contentId:eC.contentId,content:eg,operateType:'update'})`",
    },
    'phrase_delete': {
        'api': CONTENT_API,
        'data': ('groupId', 'contentId', 'operateType'),
        'evidence': "js_008_vendors.js @1887159 `eS({groupId:eC,contentId:em.contentId,operateType:'delete'})`",
    },
}

READ_APIS = (LIST_API,)

#: 写接口只有这两个 operate 家族；`LIST_API` 是只读。
WRITE_API_SET = frozenset([GROUP_API, CONTENT_API])
ALL_APIS = frozenset([LIST_API, GROUP_API, CONTENT_API])

ACTIONS = tuple(WRITE_APIS)


class UnknownApi(ValueError):
    """不是这个族的接口。"""


class WriteNotConfirmed(ValueError):
    """写操作没拿到显式 confirm。"""


# --------------------------------------------------------------------------- 守卫


def assert_write(api: str) -> str:
    """只允许本族的两个写接口通过。"""
    if api not in WRITE_API_SET:
        raise UnknownApi(f'不是话术族的写接口: {api}')
    return api


def assert_read(api: str) -> str:
    if api not in READ_APIS:
        raise UnknownApi(f'不是话术族的只读接口: {api}')
    return api


def _need_confirm(confirm: Any) -> None:
    """只有 ``confirm is True`` 算点头（``bool('false')`` 是 True，不能用真值判断）。"""
    if confirm is not True:
        raise WriteNotConfirmed('这是写操作，会真的改动闲鱼上的快捷回复，必须显式 confirm=true')


# --------------------------------------------------------------------------- 校验


def _required_text(label: str, value: Any) -> str:
    text = _text(value).strip()
    if not text:
        raise ValueError(f'{label}不能为空')
    return text


def normalize_group_name(value: Any) -> str:
    text = _required_text('分组名称', value)
    if len(text) > GROUP_NAME_MAX:
        raise ValueError(f'分组名称不能超过 {GROUP_NAME_MAX} 个字')
    return text


def normalize_content(value: Any) -> str:
    text = _required_text('短语内容', value)
    if len(text) > CONTENT_MAX:
        raise ValueError(f'短语内容不能超过 {CONTENT_MAX} 个字')
    return text


def _id_value(label: str, value: Any) -> Any:
    """id 原样透传（官方给的就是 int）；空值报错，数字字符串转 int。"""
    if value is None or isinstance(value, bool) or _text(value).strip() == '':
        raise ValueError(f'{label}不能为空')
    if isinstance(value, int):
        return value
    text = _text(value).strip()
    try:
        return int(text)
    except ValueError:
        return text


def normalize_group_id(value: Any) -> Any:
    return _id_value('分组 id', value)


def normalize_content_id(value: Any) -> Any:
    return _id_value('短语 id', value)


def _one_of(label: str, value: Any, allowed: Tuple[str, ...]) -> str:
    text = _text(value).strip()
    if text not in allowed:
        raise ValueError(f'{label}只能是 ' + ' / '.join(allowed))
    return text


# --------------------------------------------------------------------------- 请求构造


def list_request() -> Tuple[str, Dict[str, Any]]:
    """官方就是 ``b$({})`` —— 不带任何参数。"""
    assert_read(LIST_API)
    return LIST_API, {}


def group_request(
    operate_type: str,
    group_name: Any = None,
    group_id: Any = None,
    *,
    confirm: Any = None,
) -> Tuple[str, Dict[str, Any]]:
    """分组增删改。``create`` 只要名字，``update`` 要 id + 名字，``delete`` 只要 id。"""
    _need_confirm(confirm)
    action = _one_of('分组操作', operate_type, GROUP_OPERATE_TYPES)
    assert_write(GROUP_API)
    if action == 'create':
        return GROUP_API, {'groupName': normalize_group_name(group_name), 'operateType': action}
    if action == 'update':
        return GROUP_API, {
            'groupId': normalize_group_id(group_id),
            'groupName': normalize_group_name(group_name),
            'operateType': action,
        }
    return GROUP_API, {'groupId': normalize_group_id(group_id), 'operateType': action}


def content_request(
    operate_type: str,
    group_id: Any = None,
    content: Any = None,
    content_id: Any = None,
    *,
    confirm: Any = None,
) -> Tuple[str, Dict[str, Any]]:
    """短语增删改。``create`` 要分组 + 内容，``update`` 还要短语 id，``delete`` 不要内容。"""
    _need_confirm(confirm)
    action = _one_of('短语操作', operate_type, CONTENT_OPERATE_TYPES)
    assert_write(CONTENT_API)
    if action == 'create':
        return CONTENT_API, {
            'groupId': normalize_group_id(group_id),
            'content': normalize_content(content),
            'operateType': action,
        }
    if action == 'update':
        return CONTENT_API, {
            'groupId': normalize_group_id(group_id),
            'contentId': normalize_content_id(content_id),
            'content': normalize_content(content),
            'operateType': action,
        }
    return CONTENT_API, {
        'groupId': normalize_group_id(group_id),
        'contentId': normalize_content_id(content_id),
        'operateType': action,
    }


def request_of(action: str, **kwargs: Any) -> Tuple[str, Dict[str, Any]]:
    """按动作名构造（``group_create`` / ``phrase_delete`` …）。"""
    if action not in WRITE_APIS:
        raise UnknownApi(f'未知的写动作: {action}')
    kind, _, operate_type = action.partition('_')
    if kind == 'group':
        return group_request(operate_type, **kwargs)
    return content_request(operate_type, **kwargs)


# --------------------------------------------------------------------------- 解析


def envelope(raw: Any) -> Dict[str, Any]:
    raw = decode_payload(raw)
    return raw if isinstance(raw, dict) else {}


def business(raw: Any) -> Dict[str, Any]:
    """业务体：``data.data`` 与 ``data`` 都认（``body_of`` 已覆盖这两种容器）。"""
    return body_of(raw)


def code_of(raw: Any) -> str:
    body = business(raw)
    return _text(body.get('code'))


def message_of(raw: Any) -> str:
    """平台文案优先级：业务体 ``msg`` → 业务体 ``message`` → 业务体 ``error`` → 客户端 message → ret[0]。"""
    body = business(raw)
    text = _text(body.get('msg')) or _text(body.get('message')) or _text(body.get('error'))
    if text:
        return text
    text = envelope_message(raw)
    if text:
        return text
    ret = envelope(raw).get('ret')
    if isinstance(ret, list) and ret:
        return _text(ret[0])
    return ''


def is_success(raw: Any) -> bool:
    """客户端层没报错、业务码（有的话）是 success、且没有 ``error`` 字段。"""
    raw = decode_payload(raw)
    if not isinstance(raw, dict):
        return False
    if envelope_error(raw):
        return False
    body = business(raw)
    if _text(body.get('error')):
        return False
    code = _text(body.get('code'))
    if code and code.lower() != 'success':
        return False
    return True


def failure_reason(raw: Any) -> str:
    client_error = envelope_error(raw)
    if client_error:
        return client_error
    text = message_of(raw)
    if text:
        return text
    return '平台接口返回失败'


def _entry(row: Any) -> Optional[Dict[str, Any]]:
    """列表行：``{groupId, groupName, contents:[{contentId, content}]}``，缺的一律 None。"""
    if not isinstance(row, dict):
        return None
    raw_contents = row.get('contents')
    contents: Optional[List[Dict[str, Any]]] = None
    if isinstance(raw_contents, list):
        contents = []
        for item in raw_contents:
            if not isinstance(item, dict):
                continue
            contents.append({
                'content_id': _text(item.get('contentId')) or None,
                'content': _text(item.get('content')) or None,
            })
    return {
        'group_id': _text(row.get('groupId')) or None,
        'group_name': _text(row.get('groupName')) or None,
        'contents': contents,
    }


def parse_groups(raw: Any) -> Dict[str, Any]:
    """列表解析。

    ``groups`` 认不出列表键时给 ``None``（**不是 ``[]``**）——「平台没给」与「真的没有话术」
    是两件事。``total``：平台给了数值就用它，否则用我们**数出来的**条数（``total_from_platform``
    说明这个数是谁给的），平台没给且列表也认不出时是 ``None``。
    """
    body = business(raw)
    raw_groups = body.get('groups')
    found = isinstance(raw_groups, list)
    groups: Optional[List[Dict[str, Any]]] = None
    if found:
        groups = [entry for entry in (_entry(row) for row in raw_groups) if entry is not None]
    platform_total = body.get('total')
    total_from_platform = isinstance(platform_total, int) and not isinstance(platform_total, bool)
    if total_from_platform:
        total: Optional[int] = platform_total
    elif groups is not None:
        total = len(groups)
    else:
        total = None
    return {'groups': groups, 'total': total, 'total_from_platform': total_from_platform, 'found_key': found}


def _info_of(raw: Any, key: str) -> Dict[str, Any]:
    body = business(raw)
    info = decode_payload(body.get(key))
    return info if isinstance(info, dict) else {}


def parse_group_write(raw: Any) -> Dict[str, Any]:
    """分组写回执：成功判据照官方 —— ``groupInfo.groupId`` 存在。"""
    info = _info_of(raw, 'groupInfo')
    group_id = _text(info.get('groupId')) or None
    return {
        'saved': bool(group_id) and is_success(raw),
        'group_id': group_id,
        'group_name': _text(info.get('groupName')) or None,
        'platform_message': message_of(raw),
    }


def parse_content_write(raw: Any) -> Dict[str, Any]:
    """短语写回执：成功判据照官方 —— ``contentInfo.contentId`` 存在。"""
    info = _info_of(raw, 'contentInfo')
    content_id = _text(info.get('contentId')) or None
    return {
        'saved': bool(content_id) and is_success(raw),
        'content_id': content_id,
        'group_id': _text(info.get('groupId')) or None,
        'content': _text(info.get('content')) or None,
        'platform_message': message_of(raw),
    }


def json_body(value: Any) -> str:
    """与官方 ``JSON.stringify`` 贴近的字节（不转义非 ASCII、无多余空格）。"""
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
