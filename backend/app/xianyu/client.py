"""``XianyuClient`` —— 一个账号的 mtop / 商品 / 图片 / 聊天门面。

设计取舍：**不引入 aiohttp**。旧项目 mtop 用 aiohttp、上传用 aiohttp、登录用
httpx，三套客户端各自读环境变量，于是 ``NO_PROXY`` 一坏就有三种不同的坏法。
这里统一走 httpx（``net.async_client`` 已经把代理显式关掉），少一个变量。

风控与登录态过期的分工在 ``_check_response`` 里，这是本文件最要紧的一段：
- 命中 ``RISK_CONTROL_MARKERS`` → ``risk.registry.trip()`` 熔断 + 抛 ``RiskControlError``；
- 命中 ``SESSION_EXPIRED_MARKERS`` → **不熔断**，抛 ``SessionExpiredError``，
  由调用方把账号置 ``need_relogin``。
把两者混在一起（旧项目早期就这么干过）会让「重新登录一下就好」的账号白等
20 分钟冷却。
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from loguru import logger

from . import cookies as cookie_utils
from . import fingerprint, im, mtop, net, risk

# ---------------------------------------------------------------------------
# 接口常量。版本号**必须逐个硬编码**：``*.pc.idleitem.*`` 族是 1.0，
# ``downshelf`` / ``polish`` / ``kgraph`` 族是 2.0。写错时服务端返回
# 「接口不存在」，很容易被误判成签名错误，排查半天。
# ---------------------------------------------------------------------------
ITEM_DETAIL_API = 'mtop.taobao.idle.pc.detail'
ITEM_DETAIL_VERSION = '1.0'
ITEM_DETAIL_SPM = 'a21ybx.im.0.0'

EDIT_API = 'mtop.idle.pc.idleitem.edit'
EDIT_VERSION = '1.0'
EDIT_DETAIL_API = 'mtop.idle.pc.idleitem.editDetail'

PUBLISH_API = 'mtop.idle.pc.idleitem.publish'
PUBLISH_VERSION = '1.0'

IM_TOKEN_API = 'mtop.taobao.idlemessage.pc.login.token'
IM_TOKEN_VERSION = '1.0'
IM_TOKEN_SPM = 'a21ybx.im.0.0'
USER_PROFILE_API = 'mtop.idle.web.user.page.head'


class XianyuError(RuntimeError):
    """协议层通用错误。"""


class SessionExpiredError(XianyuError):
    """登录态过期。**不是风控** —— 调用方应把账号置 need_relogin。"""


class RiskControlError(XianyuError):
    """被风控拦截。调用方应读 ``retry_after`` 决定等待时长。"""

    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = retry_after


class XianyuClient:
    """一个闲鱼账号的协议客户端。

    ``cookie_str`` 是完整 Cookie 串（必须含 ``unb``，否则拿不到自己的 IM id）。
    实例是**有状态**的：mtop 响应续期的 ``_m_h5_tk`` 会写回 ``self.cookie_str``，
    IM 连接也会被复用。所以一个账号一个实例，不要跨账号共享。
    """

    def __init__(self, cookie_str: str, client=None, transport=None):
        if not cookie_str:
            raise ValueError('cookie_str 不能为空')
        self.cookie_str = str(cookie_str)
        self.cookie_id = cookie_utils.parse_cookie_string(self.cookie_str).get('unb', '')
        if not self.cookie_id:
            # 没有 unb 就没法定位账号，也没法按 cookie_id 熔断。宁可现在报错，
            # 也不要带着空 id 跑，最后变成「所有账号共用一个熔断桶」。
            raise ValueError('Cookie 里缺少 unb，无法确定账号 ID')
        self._client = client
        self._transport = transport
        self._im: Optional[im.ImSession] = None
        self._im_token: Optional[str] = None

    # -- 基础设施 ---------------------------------------------------------

    async def _http(self):
        if self._client is None:
            self._client = net.async_client(transport=self._transport)
        return self._client

    async def close(self) -> None:
        if self._im is not None:
            await self._im.close()
            self._im = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    # -- 风控/登录态分流 --------------------------------------------------

    def _check_response(self, payload: Any, message: str) -> None:
        """按响应内容决定「熔断」「置重登」还是「放行」。

        顺序要紧：先判登录态过期。``FAIL_SYS_SESSION_EXPIRED`` 的文本里
        不含风控标记，但如果将来平台在过期消息里夹带了 ``请稍后重试``，
        先判风控就会把「重登即可」误熔断。
        """
        if risk.is_session_expired(message):
            logger.warning(f'[{self.cookie_id}] 登录态过期，需要重新登录: {message}')
            raise SessionExpiredError(message)
        if risk.is_risk_control_error(message):
            retry_after = self._extract_retry_after(payload)
            cooldown = risk.registry.trip(self.cookie_id, message, retry_after)
            logger.warning(
                f'[{self.cookie_id}] 命中风控，冷却 {cooldown}s: {message}'
            )
            raise RiskControlError(message, retry_after=cooldown)

    @staticmethod
    def _extract_retry_after(payload: Any) -> int:
        """从响应里挖「建议等待秒数」。挖不到就返回 0，由本地阶梯决定。"""
        if not isinstance(payload, dict):
            return 0
        data = payload.get('data')
        if not isinstance(data, dict):
            return 0
        for key in ('retryAfter', 'retry_after', 'waitSeconds'):
            value = data.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        return 0

    # -- MTOP -------------------------------------------------------------

    async def mtop(self, api: str, data: Any = None, v: str = '1.0',
                   spm_cnt: str = mtop.DEFAULT_SPM_CNT) -> Dict[str, Any]:
        """调一次 MTOP，返回 ``{"success", "data", "message", "ret", "cookies_str"}``。

        与旧实现的差别：**成功才 reset 熔断计数**。旧实现只在调用方显式
        ``guard.reset()`` 时清零，结果「连续命中」几乎永远在累加，阶梯很快
        爬到 20 分钟。
        """
        guard = risk.registry.get(self.cookie_id)
        if guard.is_blocked:
            raise RiskControlError(
                f'账号处于风控冷却中，还需 {guard.remaining_seconds}s',
                retry_after=guard.remaining_seconds,
            )

        data_str = mtop.encode_data(data)
        t = str(int(time.time() * 1000))
        token = mtop.token_from_cookies(self.cookie_str)
        params = mtop.call_params(api, v, data_str, token, t, spm_cnt)
        headers = fingerprint.headers({
            'accept': 'application/json',
            'content-type': 'application/x-www-form-urlencoded',
            'origin': 'https://www.goofish.com',
            'referer': 'https://www.goofish.com/',
            'cookie': self.cookie_str.replace('\n', '').replace('\r', ''),
        })

        client = await self._http()
        try:
            response = await client.post(
                mtop.api_url(api, v), params=params, data={'data': data_str},
                headers=headers,
            )
        except Exception as exc:
            raise XianyuError(f'MTOP 请求异常: {exc}') from exc

        # 无论成败都要吸收续期的 token cookie：失败响应里也常带着新的 _m_h5_tk，
        # 丢掉它下一次调用还是用旧 token，签名继续失败，形成死循环。
        self.cookie_str = mtop.merge_token_cookies(self.cookie_str, response)

        try:
            payload = response.json()
        except Exception:
            text = (response.text or '')[:200]
            raise XianyuError(f'MTOP 响应不是 JSON（HTTP {response.status_code}）: {text}')

        success, message, ret_list = mtop.parse_ret(payload)
        if not success:
            self._check_response(payload, message)
            raise XianyuError(f'MTOP 调用失败 [{api}]: {message}')

        guard.reset()
        return {
            'success': True,
            'data': payload.get('data') if isinstance(payload, dict) else None,
            'message': message,
            'ret': ret_list,
            'cookies_str': self.cookie_str,
        }

    # -- 商品 -------------------------------------------------------------

    async def item_detail(self, item_id: str) -> Dict[str, Any]:
        """商品原始详情（``data`` 段原样返回）。"""
        result = await self.mtop(
            ITEM_DETAIL_API, {'itemId': str(item_id)},
            v=ITEM_DETAIL_VERSION, spm_cnt=ITEM_DETAIL_SPM,
        )
        return result.get('data') or {}

    async def get_item(self, item_id: str) -> Dict[str, Any]:
        """商品详情（归一化：id/标题/价格/图片/状态）。

        归一化放在协议层，是因为不同接口给的字段名不一样（``itemDO`` /
        ``item`` / 扁平），业务层不该关心这些。
        """
        data = await self.item_detail(item_id)
        return normalize_item(data, str(item_id))

    async def edit_item(self, item_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """编辑商品。``payload`` 里的字段直接并进 ``data``。"""
        body = {'itemId': str(item_id)}
        body.update(payload or {})
        result = await self.mtop(EDIT_API, body, v=EDIT_VERSION)
        return result.get('data') or {}

    async def edit_item_detail(self, item_id: str) -> Dict[str, Any]:
        """编辑页回填用的详情（与 ``item_detail`` 是两个接口，字段不同）。"""
        result = await self.mtop(EDIT_DETAIL_API, {'itemId': str(item_id)}, v=EDIT_VERSION)
        return result.get('data') or {}

    async def publish_item(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """发布商品。"""
        result = await self.mtop(PUBLISH_API, payload or {}, v=PUBLISH_VERSION)
        return result.get('data') or {}

    async def user_profile(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """账号公开资料（昵称 / 头像 / 卖家等级）。"""
        result = await self.mtop(
            USER_PROFILE_API, {'userId': str(user_id or self.cookie_id)}, v='1.0'
        )
        return parse_user_profile(result.get('data') or {})

    # -- 图片上传 ---------------------------------------------------------

    async def upload_image(self, path: str) -> Dict[str, Any]:
        """上传本地图片到闲鱼 CDN，返回 ``{"url", "fileId", "width", "height"}``。

        ``floderId=0`` + ``appkey=fleamarket`` 是发布场景（聊天场景用 xy_chat）。
        尺寸取响应里的 ``object.pix``（形如 ``"800x800"``），解析不出来退 800x800。
        """
        if not path or not os.path.isfile(path):
            raise XianyuError(f'图片不存在: {path}')
        with open(path, 'rb') as handle:
            raw = handle.read()
        filename = os.path.basename(path)
        if not filename.lower().endswith(('.jpg', '.jpeg')):
            filename = os.path.splitext(filename)[0] + '.jpg'

        client = await self._http()
        headers = fingerprint.headers({
            'cookie': self.cookie_str,
            'referer': 'https://www.goofish.com/',
            'x-requested-with': 'XMLHttpRequest',
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
        })
        response = await client.post(
            mtop.UPLOAD_URL,
            params={'floderId': '0', 'appkey': mtop.UPLOAD_APPKEY, '_input_charset': 'utf-8'},
            files={'file': (filename, raw, 'image/jpeg')},
            headers=headers,
        )
        text = response.text or ''
        if '<html' in text.lower():
            # 返回登录页 = Cookie 失效。这里必须报「重登」而不是「上传失败」，
            # 否则用户会一直重试上传，永远看不到「该重新登录了」。
            raise SessionExpiredError('图片上传返回登录页，Cookie 已失效')
        try:
            payload = response.json()
        except Exception as exc:
            raise XianyuError(f'图片上传响应不是 JSON: {text[:200]}') from exc
        return parse_upload_response(payload)

    # -- IM ---------------------------------------------------------------

    async def _im_token(self) -> str:
        """取 IM 的 accessToken（走 mtop，带缓存）。"""
        if self._im_token:
            return self._im_token
        device_id = mtop.generate_device_id(self.cookie_id)
        result = await self.mtop(
            IM_TOKEN_API,
            {'appKey': im.IM_APP_KEY, 'deviceId': device_id},
            v=IM_TOKEN_VERSION, spm_cnt=IM_TOKEN_SPM,
        )
        token = ((result.get('data') or {}).get('accessToken')) or ''
        if not token:
            raise XianyuError('IM token 接口未返回 accessToken')
        self._im_token = token
        return token

    async def _ensure_im(self) -> im.ImSession:
        if self._im is not None and self._im.connected:
            return self._im
        token = await self._im_token()
        session = im.ImSession(self.cookie_str, token, self.cookie_id)
        await session.connect()
        self._im = session
        return session

    async def send_message(self, cid: str, text: str, toid: Optional[str] = None) -> Dict[str, Any]:
        """给会话发文本。``toid`` 省略时用 ``cid``（单聊里两者相同）。"""
        session = await self._ensure_im()
        return await session.send_message(cid, toid or cid, text)

    async def fetch_messages(self, cid: str, start_timestamp: Optional[int] = None,
                             limit: int = 20) -> List[Dict[str, Any]]:
        session = await self._ensure_im()
        return await session.fetch_messages(cid, start_timestamp, limit)

    async def fetch_conversations(self, start_timestamp: int = 0,
                                  limit: int = 50) -> List[Dict[str, Any]]:
        session = await self._ensure_im()
        return await session.fetch_conversations(start_timestamp, limit)


# ---------------------------------------------------------------------------
# 响应解析（纯函数，便于单测）
# ---------------------------------------------------------------------------

def normalize_item(data: Dict[str, Any], item_id: str = '') -> Dict[str, Any]:
    """把详情接口的响应归一化成业务层好用的形状。"""
    if not isinstance(data, dict):
        return {'id': item_id}
    # 不同版本的详情接口外层键名不同，逐个试。
    inner = data.get('itemDO') or data.get('item') or data
    if not isinstance(inner, dict):
        inner = data
    images: List[str] = []
    for key in ('imageInfos', 'images', 'imageList'):
        raw = inner.get(key)
        if isinstance(raw, list):
            for entry in raw:
                url = entry.get('url') if isinstance(entry, dict) else entry
                if url:
                    images.append(_https(str(url)))
            if images:
                break
    price = inner.get('price')
    if isinstance(price, dict):
        price = price.get('price') or price.get('value')
    return {
        'id': str(inner.get('itemId') or item_id),
        'title': inner.get('title') or '',
        'desc': inner.get('desc') or inner.get('description') or '',
        'price': price,
        'images': images,
        'status': inner.get('itemStatus') or inner.get('status'),
        'raw': data,
    }


def parse_upload_response(payload: Any) -> Dict[str, Any]:
    """从上传响应里挖出图片 URL 与尺寸。

    响应形状有五种（``data.url`` / ``object.url`` / 根 ``url`` / ``result.url`` /
    ``data.fileUrl``），旧项目为此写了五段 if。这里保留全部兼容分支，因为
    不同场景（发布 / 聊天）走的其实是不同后端，形状确实不一样。
    """
    if not isinstance(payload, dict):
        raise XianyuError('图片上传响应不是 JSON 对象')

    url = ''
    for getter in (
        lambda p: (p.get('data') or {}).get('url'),
        lambda p: (p.get('object') or {}).get('url'),
        lambda p: p.get('url'),
        lambda p: (p.get('result') or {}).get('url'),
        lambda p: (p.get('data') or {}).get('fileUrl'),
        lambda p: (p.get('data') or {}).get('file_url'),
    ):
        try:
            candidate = getter(payload)
        except Exception:
            candidate = None
        if candidate:
            url = str(candidate)
            break
    if not url:
        raise XianyuError(f'无法从上传响应提取图片 URL: {str(payload)[:200]}')

    obj = payload.get('object') if isinstance(payload.get('object'), dict) else {}
    width, height = 800, 800
    pix = str(obj.get('pix') or '')
    if 'x' in pix:
        parts = pix.split('x')
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            width, height = int(parts[0]), int(parts[1])
    return {
        'url': _https(url),
        'fileId': obj.get('fileId') or (payload.get('data') or {}).get('fileId'),
        'width': width,
        'height': height,
    }


def parse_user_profile(data: Dict[str, Any]) -> Dict[str, Any]:
    """解析 ``user.page.head`` 的响应。

    昵称在 ``module.base.displayName``，头像在 ``module.base.avatar.avatar``；
    头像接口回的是 http，页面用 https 加载会被浏览器拦掉，必须升级协议。
    信用标签里买家和卖家两个维度都有，只取 ``attributes.role == 'seller'`` 的。
    """
    module = (data or {}).get('module') or {}
    base = module.get('base') or {}
    social = module.get('social') or {}
    avatar = base.get('avatar') or {}
    avatar_url = avatar.get('avatar') if isinstance(avatar, dict) else avatar

    seller_level = None
    for tag in base.get('ylzTags') or []:
        attrs = (tag or {}).get('attributes') or {}
        if attrs.get('role') == 'seller':
            seller_level = tag.get('text') or ''
            break

    def _count(value):
        text = str(value or '').replace(',', '').strip()
        try:
            return int(float(text))
        except ValueError:
            return None

    return {
        'user_id': str(base.get('userId') or ''),
        'nickname': base.get('displayName') or '',
        'avatar_url': _https(str(avatar_url or '')),
        'seller_level': seller_level,
        'followers': _count(social.get('followerNum')),
        'following': _count(social.get('followingNum')),
    }


def _https(url: str) -> str:
    if url.startswith('http://'):
        return 'https://' + url[len('http://'):]
    return url
