"""扫码登录：走 passport.goofish.com 的二维码流程。

流程（与旧项目实测一致）：
1. ``GET/POST mtop.gaia...index.get`` 拿 ``_m_h5_tk`` / ``_m_h5_tk_enc``（MTOP 握手）；
2. ``GET /mini_login.htm`` 取 ``window.viewData`` 里的 ``loginFormData``（一次性的
   登录参数，缺了它 generate.do 会拒）；
3. ``GET /newlogin/qrcode/generate.do`` 拿二维码内容，本地渲染成 PNG；
4. 前端轮询 ``POST /newlogin/qrcode/query.do``，状态机
   ``NEW → SCANED → CONFIRMED``。

**为什么不用旧项目的后台监控协程**：那样 ``poll`` 只是读内存，状态推进依赖一个
后台 task，进程一挂状态就僵住；而且「复用同一个 AsyncClient」的必要性在
前端驱动的单次轮询下自然消失。这里 ``poll_qr_login`` 每次真发一次请求，语义
直白、可测。

**``iframeRedirect`` 的处理是最容易写错的地方**：CONFIRMED 但还没拿到 ``unb``
说明服务端要求先过人脸/短信验证。此时**必须保持会话活着继续轮询**，绝不能
break —— 旧项目一进验证态就结束监控，用户刷完脸页面永远停在「请完成验证」。
"""

from __future__ import annotations

import base64
import io
import random
import re
import time
import uuid
from typing import Any, Dict, Optional

from loguru import logger

from . import cookies as cookie_utils
from . import fingerprint, net

PASSPORT_HOST = 'https://passport.goofish.com'
API_MINI_LOGIN = '/mini_login.htm'
API_GENERATE_QR = '/newlogin/qrcode/generate.do'
API_SCAN_STATUS = '/newlogin/qrcode/query.do'
API_H5_TK = ('https://h5api.m.goofish.com/h5/'
             'mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get/1.0/')

# 二维码有效期。旧项目实测 5 分钟，进入人脸验证后延长一次。
QR_EXPIRE_SECONDS = 300
VERIFICATION_EXTRA_SECONDS = 300

# 会话状态。前端按这个渲染：waiting=等扫码，scanned=已扫码待确认，
# verification_required=要过人脸，success=完成，expired=二维码过期。
STATUS_WAITING = 'waiting'
STATUS_SCANNED = 'scanned'
STATUS_VERIFICATION = 'verification_required'
STATUS_SUCCESS = 'success'
STATUS_EXPIRED = 'expired'
STATUS_CANCELLED = 'cancelled'
STATUS_UNKNOWN = 'unknown'

# viewData 的正则。非贪婪匹配到第一个 ``};`` —— 贪婪写法会把后面的脚本一起吞掉。
VIEW_DATA_RE = re.compile(r'window\.viewData\s*=\s*(\{.*?\});', re.S)


def login_query() -> Dict[str, str]:
    """mini_login.htm 与 generate.do 共用的查询参数。

    **这些参数一个都不能省**：不带 ``appName`` 时 passport 照样返回 200，
    但页面正文是「非法请求 [appNameError]」而没有 ``window.viewData``，
    上层只能看到「未能解析出 viewData」，完全看不出真因（用户实测踩过）。
    ``rnd`` 每次都要新生成。
    """
    return {
        'lang': 'zh_cn',
        'appName': 'xianyu',
        'appEntrance': 'web',
        'styleType': 'vertical',
        'bizParams': '',
        'notLoadSsoView': 'False',
        'notKeepLogin': 'False',
        'isMobile': 'False',
        'qrCodeFirst': 'False',
        'stie': '77',
        'rnd': _random_rnd(),
    }


class QRLoginError(RuntimeError):
    """登录流程异常。"""


class QRLoginSession:
    """一次扫码会话的全部状态。"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.status = STATUS_WAITING
        self.qr_code_url = ''
        self.qr_image_base64 = ''
        self.qr_content = ''
        self.cookies = ''
        self.unb = ''
        self.nickname = ''
        self.created_time = time.time()
        self.expire_time = QR_EXPIRE_SECONDS
        self.params: Dict[str, Any] = {}
        self.verification_url = ''
        self.verification_extended = False
        self.last_remote_status = ''
        self.failure: Optional[str] = None

    def extend_for_verification(self) -> None:
        """人脸验证时延长一次有效期（只延一次，避免无限续命）。"""
        if self.verification_extended:
            return
        self.expire_time += VERIFICATION_EXTRA_SECONDS
        self.verification_extended = True

    def is_expired(self) -> bool:
        return time.time() - self.created_time > self.expire_time

    def to_dict(self) -> Dict[str, Any]:
        return {
            'session_id': self.session_id,
            'status': self.status,
            'qr_url': self.qr_code_url,
            'qr_image_base64': self.qr_image_base64,
            'verification_url': self.verification_url,
            'expire_in': max(0, int(self.expire_time - (time.time() - self.created_time))),
        }


def _random_rnd() -> str:
    """passport 接口要一个随机数参数，旧项目用 JS 的 ``Math.random()`` 格式。"""
    return f'{random.random():.16f}'


class QRLoginManager:
    """管理所有扫码会话。

    ``transport`` 只用于测试注入（``httpx.MockTransport``）。生产传 None，
    走 ``net.async_client`` —— 那里的代理消毒是登录能跑起来的前提。
    """

    def __init__(self, transport=None):
        self.transport = transport
        self.sessions: Dict[str, QRLoginSession] = {}
        self.headers = fingerprint.headers({
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'accept-encoding': 'gzip, deflate, br',
            'connection': 'keep-alive',
            'referer': 'https://passport.goofish.com/',
            'origin': 'https://passport.goofish.com',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'none',
            'upgrade-insecure-requests': '1',
        })

    # -- 会话管理 ---------------------------------------------------------

    def get_session(self, session_id: str) -> Optional[QRLoginSession]:
        session = self.sessions.get(session_id)
        if session is not None and session.is_expired() and session.status not in (
                STATUS_SUCCESS, STATUS_VERIFICATION):
            session.status = STATUS_EXPIRED
        return session

    def cleanup_expired(self) -> int:
        dead = [sid for sid, s in self.sessions.items()
                if s.is_expired() and s.status != STATUS_SUCCESS]
        for sid in dead:
            self.sessions.pop(sid, None)
        return len(dead)

    # -- 网络步骤 ---------------------------------------------------------

    async def _get_mh5tk(self, client) -> str:
        """MTOP 握手，拿 ``_m_h5_tk``。

        先 GET 再 POST：只发一次时首次必然失败（服务端要靠这次失败种下
        ``_m_h5_tk``），拿到后再 POST 才算完成握手。这是 MTOP 的既定行为。
        """
        try:
            await client.get(API_H5_TK)
            response = await client.post(API_H5_TK)
        except Exception as exc:
            raise QRLoginError(f'MTOP 握手失败: {exc}') from exc
        token = response.cookies.get('_m_h5_tk') or client.cookies.get('_m_h5_tk') or ''
        return token.split('_')[0]

    async def _get_login_params(self, client) -> Dict[str, Any]:
        """取 ``loginFormData``。"""
        response = await client.get(f'{PASSPORT_HOST}{API_MINI_LOGIN}', params=login_query())
        match = VIEW_DATA_RE.search(response.text or '')
        if not match:
            raise QRLoginError('未能从 mini_login 页面解析出 viewData')
        try:
            import json

            view_data = json.loads(match.group(1))
        except ValueError as exc:
            raise QRLoginError(f'viewData 不是合法 JSON: {exc}') from exc
        params = view_data.get('loginFormData')
        if not isinstance(params, dict) or not params:
            raise QRLoginError('viewData 里没有 loginFormData')
        params = dict(params)
        # 服务端要求带上这个标记，缺了 generate.do 会返回参数错误。
        params['umidTag'] = 'SERVER'
        return params

    async def _generate_qr(self, client, params: Dict[str, Any]) -> Dict[str, Any]:
        """调 generate.do 拿二维码内容。"""
        query = dict(params)
        query.update(login_query())
        response = await client.get(f'{PASSPORT_HOST}{API_GENERATE_QR}', params=query)
        try:
            payload = response.json()
        except Exception as exc:
            raise QRLoginError(f'generate.do 响应不是 JSON: {(response.text or "")[:200]}') from exc
        content = (payload or {}).get('content') or {}
        if content.get('success') is not True:
            raise QRLoginError(f'generate.do 失败: {str(payload)[:200]}')
        data = content.get('data') or {}
        code_content = data.get('codeContent') or ''
        if not code_content:
            raise QRLoginError('generate.do 未返回 codeContent')
        return {
            'code_content': code_content,
            't': data.get('t') or params.get('t') or '',
            'ck': data.get('ck') or params.get('ck') or '',
        }

    @staticmethod
    def _render_qr(code_content: str) -> tuple:
        """把二维码内容渲染成 PNG data URL。"""
        import qrcode

        # version=5 + 纠错等级 L 是旧项目实测值：阿里给的 codeContent 偏长，
        # 低版本装不下，高纠错等级会让码过于密集、手机难扫。
        image = qrcode.QRCode(
            version=5, error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10, border=2,
        )
        image.add_data(code_content)
        image.make(fit=True)
        buffer = io.BytesIO()
        image.make_image(fill_color='black', back_color='white').save(buffer, format='PNG')
        encoded = base64.b64encode(buffer.getvalue()).decode('ascii')
        return f'data:image/png;base64,{encoded}', encoded

    # -- 对外接口 ---------------------------------------------------------

    async def start(self) -> Dict[str, Any]:
        """开一次扫码会话，返回 ``{session_id, qr_url, qr_image_base64}``。"""
        session = QRLoginSession(uuid.uuid4().hex)
        async with net.async_client(transport=self.transport) as client:
            await self._get_mh5tk(client)
            params = await self._get_login_params(client)
            generated = await self._generate_qr(client, params)
            session.cookies = cookie_utils.merge_cookies('', client.cookies)
        session.params = params
        session.qr_content = generated['code_content']
        data_url, raw_b64 = self._render_qr(generated['code_content'])
        session.qr_code_url = data_url
        session.qr_image_base64 = raw_b64
        # t / ck 必须回填到轮询参数里：query.do 要靠它们定位这次二维码。
        session.params.update({'t': generated['t'], 'ck': generated['ck']})
        self.sessions[session.session_id] = session
        logger.info(f'扫码会话已创建: {session.session_id}')
        return session.to_dict()

    async def poll(self, session_id: str) -> Dict[str, Any]:
        """轮询一次。返回 ``{status, cookie, nickname, user_id}``。"""
        session = self.get_session(session_id)
        if session is None:
            return {'status': STATUS_EXPIRED, 'cookie': '', 'nickname': '',
                    'user_id': '', 'message': '会话不存在或已过期'}
        if session.status == STATUS_SUCCESS:
            return self._success_payload(session)

        async with net.async_client(transport=self.transport) as client:
            if session.cookies:
                client.cookies.update(cookie_utils.parse_cookie_string(session.cookies))
            response = await client.post(
                f'{PASSPORT_HOST}{API_SCAN_STATUS}',
                data={
                    't': session.params.get('t', ''),
                    'ck': session.params.get('ck', ''),
                    'appName': 'xianyu',
                    'fromSite': '77',
                    'lang': 'zh_cn',
                },
            )
            try:
                payload = response.json()
            except Exception:
                return {'status': session.status, 'cookie': session.cookies,
                        'nickname': '', 'user_id': session.unb,
                        'message': 'query.do 响应不是 JSON'}
            session.cookies = cookie_utils.merge_cookies(session.cookies, response.cookies)

        data = ((payload or {}).get('content') or {}).get('data') or {}
        remote = data.get('qrCodeStatus') or ''
        if remote != session.last_remote_status:
            logger.info(f'二维码状态: {session_id} {session.last_remote_status or "INIT"} -> {remote or "EMPTY"}')
            session.last_remote_status = remote

        if remote == 'CONFIRMED':
            self._apply_cookies(session, response.cookies, data)
            if data.get('iframeRedirect') is True and not session.unb:
                # 需要人脸/短信验证。**不能 break** —— 用户验证完只有继续轮询
                # 才会拿到带登录态的 CONFIRMED。
                if session.status != STATUS_VERIFICATION:
                    session.status = STATUS_VERIFICATION
                    session.verification_url = data.get('iframeRedirectUrl') or ''
                    session.extend_for_verification()
                    logger.warning(f'账号需人工验证: {session_id}')
                return {'status': STATUS_VERIFICATION, 'cookie': session.cookies,
                        'nickname': '', 'user_id': '',
                        'verification_url': session.verification_url,
                        'message': '需要完成手机/人脸验证'}
            if not session.unb:
                # 没有 unb 就没有账号 ID，不能算登录成功。
                session.failure = '扫码确认响应未包含 unb'
                return {'status': STATUS_WAITING, 'cookie': session.cookies,
                        'nickname': '', 'user_id': '', 'message': session.failure}
            session.status = STATUS_SUCCESS
            await self._fill_profile(session)
            return self._success_payload(session)

        if remote == 'NEW':
            # 保持 waiting。旧实现这里 continue 跳过了节流会空转，本设计由
            # 前端轮询节奏决定，天然有间隔。
            return {'status': STATUS_WAITING, 'cookie': session.cookies,
                    'nickname': '', 'user_id': ''}

        if remote == 'EXPIRED':
            if session.status == STATUS_VERIFICATION:
                # 手机确认后登录二维码本身就会 EXPIRED，这是正常的：后续登录
                # 由服务端验证页完成，不能把会话判死。
                return {'status': STATUS_VERIFICATION, 'cookie': session.cookies,
                        'nickname': '', 'user_id': '',
                        'verification_url': session.verification_url,
                        'message': '等待手机端完成验证'}
            session.status = STATUS_EXPIRED
            return {'status': STATUS_EXPIRED, 'cookie': session.cookies,
                    'nickname': '', 'user_id': '', 'message': '二维码已过期'}

        if remote == 'SCANED':
            if session.status == STATUS_WAITING:
                session.status = STATUS_SCANNED
            return {'status': STATUS_SCANNED, 'cookie': session.cookies,
                    'nickname': '', 'user_id': ''}

        # 未知/空状态：保持当前状态，不判死（验证期间远端会短暂返回空）。
        return {'status': session.status, 'cookie': session.cookies,
                'nickname': '', 'user_id': session.unb}

    @staticmethod
    def _apply_cookies(session: QRLoginSession, response_cookies, data: Dict[str, Any]) -> None:
        """收下 CONFIRMED 那一轮响应里的 Cookie。

        验证走完的那一轮响应里就带着完整登录态，必须当场收，否则后面几轮
        拿到的都是「已登录」的空壳。
        """
        session.cookies = cookie_utils.merge_cookies(session.cookies, response_cookies)
        parsed = cookie_utils.parse_cookie_string(session.cookies)
        if parsed.get('unb'):
            session.unb = parsed['unb']
        # 有些版本把 unb 放在 data 里而不是 Set-Cookie。
        if not session.unb and data.get('unb'):
            session.unb = str(data['unb'])

    async def _fill_profile(self, session: QRLoginSession) -> None:
        """尽力取昵称/头像。**失败不影响登录成功**。

        取资料要再发一次 mtop，这一步失败（网络抖动、接口改版）不该让用户
        重新扫码 —— 他已经扫完了。

        ``transport`` 从 ``self`` 透传：测试用 MockTransport 时这一步也必须
        走 mock，否则一次单元测试就会真的打到闲鱼接口上。
        """
        try:
            from .client import XianyuClient

            client = XianyuClient(session.cookies, transport=self.transport)
            try:
                profile = await client.user_profile(session.unb)
                session.nickname = profile.get('nickname') or ''
            finally:
                await client.close()
        except Exception as exc:
            logger.warning(f'取账号资料失败（不影响登录）: {exc}')

    @staticmethod
    def _success_payload(session: QRLoginSession) -> Dict[str, Any]:
        return {
            'status': STATUS_SUCCESS,
            'cookie': cookie_utils.drop_challenge_markers(session.cookies),
            'nickname': session.nickname,
            'user_id': session.unb,
        }


# 全局管理器。API 层直接用这几个函数。
manager = QRLoginManager()


async def start_qr_login() -> Dict[str, Any]:
    """开始扫码登录。"""
    return await manager.start()


async def poll_qr_login(session_id: str) -> Dict[str, Any]:
    """轮询扫码状态。"""
    return await manager.poll(session_id)


async def manual_verify(cookie_id: str, cookie_str: Optional[str] = None,
                        headless: bool = False) -> Dict[str, Any]:
    """人工过滑块验证（薄封装，便于 API 层与测试替换）。"""
    from . import captcha

    return await captcha.manual_verify(cookie_id, cookie_str, headless=headless)


def get_session_status(session_id: str) -> Dict[str, Any]:
    session = manager.get_session(session_id)
    if session is None:
        return {'status': STATUS_EXPIRED, 'exists': False}
    return {
        'status': session.status,
        'exists': True,
        'has_unb': bool(session.unb),
        'cookie_count': len(cookie_utils.parse_cookie_string(session.cookies)),
    }
