"""财务四页（只读）的服务层：收入账单 / 支出账单 / 申请发票 / 开票信息。

数据全部来自平台（`mtop.idle.finance.*`），本机**不落库** —— 财务是「看一眼就走」
的只读信息，缓存只会让用户看到过期金额，而金额过期比没有更糟（会以为账对不上）。

三条口径（与商品数据页一致，写在这里免得被顺手改掉）：

1. **认不出的数字一律 `None`**，前端显示「-」，**不补 0**。把「拿不到」画成 `0.00`
   会让用户以为账对不上，这是财务页最容易做错的地方。
2. **登录态过期如实上抛** → 409 `verification_required` + `login_state=need_relogin`。
   真机实测（`C:\\Users\\Administrator\\.dsh-tmp\\finance_probe.py`，2026-10-09）这族接口用
   **网页版 Cookie** 就能调通，10/10 成功，不受 `seller.pc.*` 那种「要卖家工作台会话」
   的限制 —— 所以这里回 `FAIL_SYS_SESSION_EXPIRED` 就是账号真掉线，**不能**像
   `item_stats` 那样降级成本地统计（那会把真掉线瞒过去，用户永远等不到重新扫码的提示）。
3. **只读**：四个页面都不提交任何写操作，开票申请也不代发（平台侧不可逆）。

`fetched_at` 用 **UTC** 的 `'%Y-%m-%d %H:%M:%S'`（与库里 `CURRENT_TIMESTAMP` 同口径）：
`frontend/src/format.ts::formatTime` 对不带时区的这种字符串会补 `Z` 再本地化，
这里要是写本地时间，页面上就会白多 8 小时。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core import db
from ..xianyu import client as client_mod
from . import common, platform_errors

KINDS: Tuple[str, ...] = ('income', 'expense')
KIND_LABEL = {'income': '收入账单', 'expense': '支出账单'}
BILL_CODE = {
    'income': client_mod.BILL_CODE_INCOME,
    'expense': client_mod.BILL_CODE_EXPENSE,
}
#: 支出账单在官方页面上默认选中的业务大类（真机截图：同城服务软件服务费）。
#: 这是**兜底**：正常路径是从 metadata 的第一个大类取（见 `_first_category_code`）。
DEFAULT_BIZ_TYPE = {
    'income': '',
    'expense': client_mod.EXPENSE_BIZ_DEFAULT,
}

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

#: 账期只认 `YYYYMM`（官方页面就是「202609」这种月粒度）。
CYCLE_RE = re.compile(r'^(\d{4})(0[1-9]|1[0-2])$')

WARN_NO_METADATA = '没能取到业务大类筛选项（平台没返回 metadata），分类下拉是空的，账单本身照常显示'
WARN_NO_SUMMARY = '平台没有返回这个账期的汇总，只看得到流水'
WARN_NO_DETAIL = '平台没有返回这个账期的流水，只看得到汇总'
WARN_NO_INVOICE_META = '没能取到可开票业务类型（平台没返回 metadata），申请发票页少一列说明'
WARN_NO_INVOICE_BILLS = '没能取到待申请发票的账单，这一页的可开票金额是空的'
WARN_NO_REQUESTS = '没能取到已提交的开票申请列表'


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _account_exists(account_id: str) -> bool:
    return db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,)) is not None


def _credential_failure(account_id: str) -> Optional[Dict[str, Any]]:
    """账号没有可用 Cookie 时返回 409 信封，否则 None。

    与 `item_stats._credential_failure` 逐字同口径（**故意重复这一小段**）：
    它决定「这个账号要重新扫码」是 409 而不是 `common.get_client` 的
    `xianyu_not_ready`(503) —— 前端据此弹重新登录。两处必须一起改，
    合并到 common 里属于另一件事（会动到已验证的商品数据路径）。
    """
    row = db.query_one(
        'SELECT cookie, login_state FROM account_credentials WHERE account_id = ?',
        (account_id,),
    )
    cookie = _text(row['cookie']).strip() if row is not None else ''
    state = _text(row['login_state']).strip() if row is not None else ''
    if not cookie:
        return common.fail(
            f'账号 {account_id} 还没有 Cookie，请先在「账号」页完成扫码登录',
            'verification_required', login_state='need_relogin',
        )
    if state == 'need_relogin':
        return common.fail(
            f'账号 {account_id} 登录态已过期，需要重新扫码登录',
            'verification_required', login_state='need_relogin',
        )
    return None


def _fetched_at() -> str:
    """UTC 时间戳字符串（见模块 docstring 里的时区说明）。"""
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def _cycle_pair(cycle_from: str, cycle_to: str) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """校验/补全账期。返回 `(from, to, 失败信封)`。

    都不传时默认**上一个自然月**，不是本月：平台当月账单要等次月 5 号之后才生成，
    查本月必然拿到 `BILL_QUERY_DELAY_ERROR::暂无法查看 2026 年 10 月的账单，
    请在 2026-11-05 后查看`（真机 2026-10-09 实测）。
    """
    start = _text(cycle_from).strip()
    end = _text(cycle_to).strip()
    if not start and not end:
        previous = (datetime.now().replace(day=1) - timedelta(days=1)).strftime('%Y%m')
        return previous, previous, None
    if not start:
        start = end
    if not end:
        end = start
    for value in (start, end):
        if not CYCLE_RE.match(value):
            return '', '', common.fail(
                f'账期格式必须是 YYYYMM（例如 202609），收到「{value}」', 'invalid_fields')
    if start > end:
        return '', '', common.fail(
            f'开始账期 {start} 不能晚于结束账期 {end}', 'invalid_fields')
    return start, end, None


def _page_pair(page: Any, size: Any) -> Tuple[int, int, Optional[Dict[str, Any]]]:
    try:
        page_no = int(page)
    except (TypeError, ValueError):
        page_no = 1
    try:
        page_size = int(size)
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE
    if page_no < 1:
        return 1, DEFAULT_PAGE_SIZE, common.fail('页码必须从 1 开始', 'invalid_fields')
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        return 1, DEFAULT_PAGE_SIZE, common.fail(
            f'每页条数必须在 1~{MAX_PAGE_SIZE} 之间', 'invalid_fields')
    return page_no, page_size, None


def _biz_types(value: Any) -> List[str]:
    """`?biz_types=a,b` → `['a','b']`（空串/空列表都当「不筛」）。"""
    if isinstance(value, (list, tuple)):
        items: Sequence[Any] = value
    else:
        items = _text(value).replace('，', ',').split(',')
    return [str(item).strip() for item in items if str(item).strip()]


def _first_category_code(categories: Any) -> str:
    """metadata 里**第一级**业务大类的编码（子级是商品类目，不能拿来当 bizType）。

    真机实测：不传 `bizType` 时平台直接拒 ——
    `INVALID_PARAM::业务大类不能为空`（`mtop.idle.finance.monthbill.income.summary.query`）。
    所以默认值必须从平台自己的 metadata 取，而不是自己写死一个编码：
    收入账单的 `categories[0].code` 就是 `goods_income`（名字「交易货款」）。
    """
    for node in categories if isinstance(categories, list) else []:
        if isinstance(node, dict):
            code = _text(node.get('code')).strip()
            if code:
                return code
    return ''


def _call(client: Any, name: str, *args: Any) -> Tuple[bool, Any, str, Optional[Dict[str, Any]]]:
    """调一个协议层方法 → `(ok, 归一化后的值, 失败原因, 失败信封)`。

    失败信封非 None 时**必须原样返回给调用方**（那是登录态过期这类要决定 HTTP 状态码的
    东西）；`ok=False` 且信封为 None 时只是「这个方法这次没成功」，调用方可以降级。
    """
    status, value = common.call_method(client, (name,), *args)
    if status == common.CALL_OK:
        if value is None:
            return False, None, '平台返回的财务数据无法识别（协议层没有给出可解析的内容）', None
        return True, value, '', None
    failure = common.call_failed(status, value)
    if failure is not None and failure.get('code') == 'verification_required':
        return False, None, '', failure
    reason = ''
    if isinstance(failure, dict):
        reason = _text(failure.get('message'))
    reason = reason or _text(value) or '未知错误'
    # 平台错误码翻人话：账单延迟（当月账单要次月 5 号后才有）、风控、接口下线这些
    # 光看错误码用户没法行动。认不出的码 explain() 返回空串，原样透传。
    hint = platform_errors.explain(reason)
    if hint:
        reason = f'{reason}。{hint}'
    return False, None, reason, None


def _client_or_failure(account_id: str) -> Tuple[Any, Optional[Dict[str, Any]]]:
    client, failure = common.get_client(account_id)
    if client is None:
        return None, failure or common.not_ready()
    return client, None


# --------------------------------------------------------------------------- 账单


def account_bills(account_id: str, kind: str = 'income', cycle_from: str = '',
                  cycle_to: str = '', biz_type: str = '', page: Any = 1,
                  size: Any = DEFAULT_PAGE_SIZE) -> Dict[str, Any]:
    """某个账号某个月的收入/支出账单（汇总 + 流水 + 业务大类树）。

    一次请求打三次平台接口（metadata / summary / detail）—— 官方页面也是这么打的，
    金额与分类必须来自同一个账期，分开请求会让「合计」和「明细」对不上。
    """
    account_id = _text(account_id).strip()
    if not account_id:
        return common.fail('缺少账号 ID', 'bad_request')
    kind = _text(kind).strip().lower()
    if kind not in KINDS:
        return common.fail(f'账单类型只能是 {" 或 ".join(KINDS)}', 'invalid_fields')
    start, end, bad = _cycle_pair(cycle_from, cycle_to)
    if bad is not None:
        return bad
    page_no, page_size, bad = _page_pair(page, size)
    if bad is not None:
        return bad
    if not _account_exists(account_id):
        return common.fail(f'账号 {account_id} 不存在', 'not_found')
    failure = _credential_failure(account_id)
    if failure is not None:
        return failure
    client, failure = _client_or_failure(account_id)
    if client is None:
        return failure

    bill_code = BILL_CODE[kind]
    warnings: List[str] = []

    ok, meta, reason, failure = _call(client, 'finance_metadata', bill_code)
    if failure is not None:
        return failure
    categories: List[Dict[str, Any]] = []
    bill_name = ''
    if ok:
        categories = list(meta.get('categories') or [])
        bill_name = _text(meta.get('bill_name'))
    else:
        warnings.append(f'{WARN_NO_METADATA}（{reason}）')

    # 业务大类：调用方给了就用；没给就从 metadata 取第一个（平台不接受空值）。
    biz = _text(biz_type).strip()
    if not biz:
        biz = _first_category_code(categories) or DEFAULT_BIZ_TYPE[kind]
    if not biz:
        return common.fail(
            '平台要求必须指定业务大类，但没能取到分类列表（metadata 为空），请稍后重试',
            'protocol_error')

    ok_sum, summary, reason_sum, failure = _call(
        client, 'finance_month_summary', kind, start, end, bill_code, biz)
    if failure is not None:
        return failure
    ok_det, detail, reason_det, failure = _call(
        client, 'finance_month_detail', kind, start, end, bill_code, biz, page_no, page_size)
    if failure is not None:
        return failure
    if not ok_sum and not ok_det:
        return common.fail(
            f'平台财务接口调用失败: {reason_sum or reason_det}', 'protocol_error')
    if not ok_sum:
        warnings.append(f'{WARN_NO_SUMMARY}（{reason_sum}）')
    if not ok_det:
        warnings.append(f'{WARN_NO_DETAIL}（{reason_det}）')

    detail = detail or {}
    return {
        'account_id': account_id,
        'kind': kind,
        'bill_code': bill_code,
        'bill_name': bill_name or KIND_LABEL[kind],
        'period': {'from': start, 'to': end},
        'biz_type': biz,
        'summary': summary or None,
        'bills': list(detail.get('bills') or []),
        'total': detail.get('total'),
        'has_next': detail.get('has_next'),
        'page': page_no,
        'size': page_size,
        'biz_categories': categories,
        'warnings': warnings,
        'fetched_at': _fetched_at(),
        'source': 'platform',
    }


# --------------------------------------------------------------------------- 发票


def account_invoices(account_id: str, page: Any = 1, size: Any = DEFAULT_PAGE_SIZE,
                     biz_types: Any = '') -> Dict[str, Any]:
    """待申请发票（按业务类型分组的可开票金额）+ 已提交的申请列表 + 业务类型说明。"""
    account_id = _text(account_id).strip()
    if not account_id:
        return common.fail('缺少账号 ID', 'bad_request')
    page_no, page_size, bad = _page_pair(page, size)
    if bad is not None:
        return bad
    if not _account_exists(account_id):
        return common.fail(f'账号 {account_id} 不存在', 'not_found')
    failure = _credential_failure(account_id)
    if failure is not None:
        return failure
    client, failure = _client_or_failure(account_id)
    if client is None:
        return failure

    wanted = _biz_types(biz_types)
    warnings: List[str] = []

    ok_bill, bills, reason_bill, failure = _call(
        client, 'finance_invoice_bills', page_no, page_size, wanted)
    if failure is not None:
        return failure
    ok_req, requests, reason_req, failure = _call(
        client, 'finance_invoice_requests', page_no, page_size)
    if failure is not None:
        return failure
    if not ok_bill and not ok_req:
        return common.fail(
            f'平台发票接口调用失败: {reason_bill or reason_req}', 'protocol_error')

    # 业务类型说明是「锦上添花」：拿不到只少一列文案，不能让整页失败。
    ok_meta, meta, reason_meta, failure = _call(client, 'finance_invoice_meta')
    if failure is not None:
        return failure

    if not ok_bill:
        warnings.append(f'{WARN_NO_INVOICE_BILLS}（{reason_bill}）')
    if not ok_req:
        warnings.append(f'{WARN_NO_REQUESTS}（{reason_req}）')
    if not ok_meta:
        warnings.append(f'{WARN_NO_INVOICE_META}（{reason_meta}）')

    bills = bills or {}
    requests = requests or {}
    total_count = bills.get('total_count')
    # `has_next` 平台没给（invoice.bill.query 只回 totalAmount/totalCount），
    # 用平台自己的 totalCount 算，而不是猜 —— 算不出来就 None，前端会禁用「下一页」。
    has_next = None if total_count is None else bool(page_no * page_size < total_count)
    return {
        'account_id': account_id,
        'groups': list(bills.get('groups') or []),
        'total_amount': bills.get('total_amount'),
        'total_count': total_count,
        'page': page_no,
        'size': page_size,
        'has_next': has_next,
        'requests': list(requests.get('requests') or []),
        'request_total': requests.get('total'),
        'meta': list(meta or []),
        'warnings': warnings,
        'fetched_at': _fetched_at(),
        'source': 'platform',
    }


def account_purchaser(account_id: str) -> Dict[str, Any]:
    """开票信息：发票抬头 / 纳税类型 / 支持的票种。"""
    account_id = _text(account_id).strip()
    if not account_id:
        return common.fail('缺少账号 ID', 'bad_request')
    if not _account_exists(account_id):
        return common.fail(f'账号 {account_id} 不存在', 'not_found')
    failure = _credential_failure(account_id)
    if failure is not None:
        return failure
    client, failure = _client_or_failure(account_id)
    if client is None:
        return failure

    ok, value, reason, failure = _call(client, 'finance_purchaser')
    if failure is not None:
        return failure
    if not ok:
        return common.fail(f'平台开票信息接口调用失败: {reason}', 'protocol_error')
    return {
        'account_id': account_id,
        'purchaser_name': _text(value.get('purchaser_name')),
        'purchaser_type': _text(value.get('purchaser_type')),
        'support_invoice_types': list(value.get('support_invoice_types') or []),
        'fetched_at': _fetched_at(),
        'source': 'platform',
    }
