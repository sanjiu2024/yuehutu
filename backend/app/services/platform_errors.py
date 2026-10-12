"""平台错误码 → 人话（发布 / 编辑，以及只读接口的平台拒绝）。

真机上用户看到的原文长这样（第七轮截图，发布商品后右下角 toast）：

    协议层调用失败: FAIL_BIZ_IDLE_UNKNOWN_THROWABLE::发布失败，请稍后重试吧~

「发布失败，请稍后重试吧~」对用户没有任何可行动信息：他不知道是自己填错了、
被平台限流了、还是图片没过审 —— 只能一遍遍地点「发布到闲鱼」，
而每一次点击都在加重风控。

只读接口同样会甩原文（第二十六批真机巡检，35 条 GET 逐个打）：

    协议层调用失败: MTOP 调用失败 [mtop.taobao.idle.cco.shop.complain.list]: NO_PERMISSION::用户没有权限

用户看到「用户没有权限」会以为是自己没登录，去重新扫码 —— 白扫一次，问题还在。
所以「这个账号没开这个功能」和「登录态掉了」必须在文案上分开。

这个模块把**真机见过的**错误码翻成「发生了什么 + 现在能做什么」。
三条口径：

1. **只收录真机见过的码**。没见过的码原样透传（``explain()`` 返回空串），
   不许编解释 —— 讲错原因比不讲更糟，用户会照着错的解释去改商品。
2. **按词边界匹配**，不是子串包含。``..._CAN_NOT_PUBLISH_V2`` 不该命中
   ``..._CAN_NOT_PUBLISH`` 的解释（这是复审 D4 报过的问题）。
   ``FAIL_SYS_NO_PERMISSION`` 也不该命中裸码 ``NO_PERMISSION`` 的解释。
3. 只做**解释**，不改业务判定。风控/登录态该走哪个分支，由协议层与服务层
   （``RiskControlError`` / ``SessionExpiredError`` / ``risk.py``）决定。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

#: 错误码 -> 人话。顺序即匹配优先级（先命中的先返回）。
PLATFORM_ERROR_HINTS: Tuple[Tuple[str, str], ...] = (
    (
        'FAIL_BIZ_IDLE_UNKNOWN_THROWABLE',
        '闲鱼没有接受这次操作，只回了一句「发布失败，请稍后重试吧」。'
        '真机上这次是因为售价/库存填得离谱（售价 99999999、库存 19999999）。'
        '先把售价、库存改回正常范围，隔几分钟再试一次；连续失败就先别操作了。',
    ),
    (
        'FAIL_BIZ_JSON_ERROR',
        '平台解不开这次提交的数据。多半是某个字段格式不对'
        '（价格、库存、分类 ID 不是数字或超出范围），把这几项改回正常值再试一次。',
    ),
    (
        'FAIL_SYS_USER_VALIDATE',
        '被闲鱼风控拦下了（不是账号掉线，也不是你填错了）：平台要求先停一会儿再操作。'
        '冷却剩余时间见「风控」页；冷却期内继续操作只会让风控更重。',
    ),
    (
        'RGV587_ERROR',
        '被闲鱼风控拦下了（平台原话「哎哟喂，被挤爆啦，请稍后重试」）：这是平台在限流。'
        '等冷却结束再操作，冷却期间反复重试会让风控阶梯往上走。',
    ),
    (
        'FAIL_SYS_SESSION_EXPIRED',
        '登录态已经过期：去「账号」页重新扫码登录，然后重试。',
    ),
    (
        # 平台自己的拼写就是 EXOIRED（不是 EXPIRED），照抄才能匹配上。
        'FAIL_SYS_TOKEN_EXOIRED',
        '登录令牌过期：去「账号」页重新扫码登录，然后重试。',
    ),
    (
        'FAIL_SYS_API_NOT_FOUND',
        '闲鱼这个接口已经改名或下线了，当前版本调不通：升级软件版本后再试。',
    ),
    (
        'BILL_QUERY_DELAY_ERROR',
        '这个月的账单平台还没生成：闲鱼的月度账单要等次月 5 号之后才出'
        '（平台原话「请在 2026-11-05 后查看」）。把账期改成上一个月再查，'
        '或者等次月 5 号之后再看本月。',
    ),
    (
        # 真机（2026-10-12 operation_logs id 196）：发货接口回 `ORDER_STATUS_ERROR::订单状态不正确`。
        # 措辞必须**通用**：这个码不只在发货出现（改价、同意退款也会回），写成「不允许发货」会讲错。
        'ORDER_STATUS_ERROR',
        '闲鱼说这一单现在的状态不允许这个操作（比如还没付款、已经发过货、已经退款）。'
        '先刷新订单看最新状态，再决定要不要继续。',
    ),
    (
        # 真机（2026-10-12 只读巡检）：子账号列表、退货地址列表、商品数据族都回这个。
        'FAIL_BIZ_IDLE_USER_UNAUTHORIZED',
        '这个账号没有开通这个功能的权限（平台原话「无权限访问」）。'
        '**不是登录掉线**，重新扫码不会变好；换一个有权限的账号，'
        '或者去官方卖家工作台看这一块。',
    ),
    (
        # 真机（2026-10-12 只读巡检）：投诉列表回这个。
        'NO_PERMISSION',
        '这个账号没有这个功能的权限（平台原话「用户没有权限」）。'
        '**不是登录掉线**，重新扫码不会变好；换一个有权限的账号再试。',
    ),
    (
        # 真机（2026-10-12 只读巡检）：资质管理的模板 / 可复用资质回这个。
        'E_AD_BP_COMMON_SYSTEM_EXCEPTION',
        '平台的资质服务自己报错了（原话「闲鱼太累了，请稍后再试」），'
        '不是你的资料或权限问题。过几分钟点「重试」；一直这样就是平台侧故障，等它恢复。',
    ),
    (
        # 真机（2026-10-12 只读巡检）：资质详情回这个。
        'COMMON_SERVER_ERROR',
        '平台侧服务繁忙（原话「服务繁忙，请稍后再试」），不是你的账号或权限问题。'
        '过几分钟点「重试」。',
    ),
)

#: 风控类错误码（调用方据此把提示写成「等冷却」而不是「你填错了」）。
RISK_ERROR_CODES: Tuple[str, ...] = ('FAIL_SYS_USER_VALIDATE', 'RGV587_ERROR')

#: 登录态类错误码（调用方据此提示「重新扫码」，而不是当成偶发网络错误）。
SESSION_ERROR_CODES: Tuple[str, ...] = (
    'FAIL_SYS_SESSION_EXPIRED',
    'FAIL_SYS_TOKEN_EXOIRED',
)

_CODE_CACHE: Dict[str, 're.Pattern[str]'] = {}


def _pattern(code: str) -> 're.Pattern[str]':
    """错误码的正则（带词边界）。缓存，避免每次调用都编译。"""
    cached = _CODE_CACHE.get(code)
    if cached is None:
        cached = re.compile(r'(?<![A-Za-z0-9_])' + re.escape(code) + r'(?![A-Za-z0-9_])')
        _CODE_CACHE[code] = cached
    return cached


def match_code(text: Any, code: str) -> bool:
    """``text`` 里是否**恰好**出现了错误码 ``code``（按词边界）。

    不能写成 ``code in text``：``FAIL_BIZ_X_CAN_NOT_PUBLISH_V2`` 会被
    ``FAIL_BIZ_X_CAN_NOT_PUBLISH`` 的 ``in`` 命中，于是给用户讲了一个错的原因。
    """
    if not text or not code:
        return False
    return bool(_pattern(code).search(str(text)))


def _blob(texts: Any) -> str:
    if isinstance(texts, str):
        return texts
    return ' '.join(str(t) for t in (texts or []) if t)


def find_code(*texts: Optional[Any]) -> str:
    """在若干段文本里找出第一个命中的**已知**错误码（找不到返回空串）。

    参数可以是字符串，也可以是字符串列表（异常上的 ``ret`` 就是列表）。
    """
    blob = _blob(texts if len(texts) != 1 else texts[0])
    if not blob:
        return ''
    for code, _hint in PLATFORM_ERROR_HINTS:
        if match_code(blob, code):
            return code
    return ''


def hint_for(code: str) -> str:
    """已知错误码 -> 人话；没收录的码返回空串（调用方原样透传）。"""
    for known, hint in PLATFORM_ERROR_HINTS:
        if known == code:
            return hint
    return ''


def explain(*texts: Optional[Any]) -> str:
    """把平台原文翻成一句人话；没见过的码返回 ``''``。"""
    code = find_code(*texts)
    return hint_for(code) if code else ''


def is_risk(text: Any) -> bool:
    """这段文案是不是风控类错误码。"""
    return bool(find_code_in(text, RISK_ERROR_CODES))


def is_session_expired(text: Any) -> bool:
    """这段文案是不是登录态类错误码。"""
    return bool(find_code_in(text, SESSION_ERROR_CODES))


def find_code_in(text: Any, codes: Tuple[str, ...]) -> str:
    """在给定的一组码里找出命中的那个（用于风控/登录态这类子集判断）。"""
    if not text:
        return ''
    for code in codes:
        if match_code(text, code):
            return code
    return ''


def _texts_of(problem: Dict[str, Any], exc: Any) -> List[str]:
    texts: List[str] = [str(problem.get('message') or '')]
    ret = getattr(exc, 'ret', None)
    if isinstance(ret, (list, tuple)):
        texts.extend(str(item) for item in ret)
    if exc is not None:
        texts.append(str(exc))
    return texts


def decorate(problem: Any, exc: Any = None) -> Any:
    """给失败信封补一句人话（认得出来才补）。

    - 返回**新字典**，不改传入的对象（调用方可能还留着原信封）。
    - ``message`` 后面追加人话，用户一眼能看到原因；
    - 另外单独给 ``platform_code`` / ``platform_hint`` 两个字段，
      前端要分开排版时不必再切字符串；
    - 命中风控时额外给 ``risk=True``，前端据此把红色错误改成「等冷却」的提示。
      **不改 ``code``**：``code`` 决定 HTTP 状态码（见 ``common.FAIL_STATUS``），
      把 ``protocol_error`` 改成别的会连带改状态码，不是这个模块该管的事。

    非失败信封原样返回。
    """
    if not isinstance(problem, dict) or problem.get('success') is not False:
        return problem
    code = find_code(_texts_of(problem, exc))
    if not code:
        return problem
    hint = hint_for(code)
    message = str(problem.get('message') or '')
    if hint and hint not in message:
        message = f'{message}。{hint}' if message else hint
    out = dict(problem)
    out['message'] = message
    out['platform_code'] = code
    out['platform_hint'] = hint
    if code in RISK_ERROR_CODES:
        out['risk'] = True
    return out
