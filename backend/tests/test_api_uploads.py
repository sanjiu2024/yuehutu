"""图片上传 HTTP 契约测试（`backend/app/api/uploads.py` + `services/uploads.py`）。

**为什么需要**：用户要「图片直接拖进去」，但 `client.upload_image` 之前只在
`publish_item` 内部被调用，没有任何 HTTP 面。这里锁死三件事：

1. `POST /api/uploads/images` 的**响应形状**（前端按它写拖拽组件）：
   `{"success": true, "data": {"url", "file_id", "width", "height", "name", "local_path"}}`。
2. 落盘 / 转码 / 清理行为：png 交给协议层前必须是 JPEG 字节、临时文件用完即删、
   上传失败时不留孤儿文件。
3. `GET /api/uploads/local/{name}` 的路径穿越闸门（`..`、`/`、第二个点、软链外的名字）。

**不打真实闲鱼平台**：协议层用假模块注入（`sys.modules['backend.app.xianyu.client']`），
上传只记录调用、不联网。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_api_uploads.py"
"""
from __future__ import annotations

import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from backend.app.api import uploads as uploads_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import common, uploads  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'upl_acc'
ACC_OFF = 'aaa_disabled'
PROTOCOL_MODULE = 'backend.app.xianyu.client'


class SessionExpiredError(RuntimeError):
    """与 `backend/app/xianyu/client.py::SessionExpiredError` **同名**。

    `common._is_session_expired` 按 MRO 里的类名识别（`common.py:307`），所以假协议层
    抛这个类就能走到与真机完全相同的那条 409 分支。
    """


# --------------------------------------------------------------------------- 假协议层


class FakeClient:
    """假协议层：只记调用、不联网。`upload_image` 与真客户端一样是 async。"""

    def __init__(self, url: str = 'https://img.test/uploaded.jpg'):
        self.url = url
        #: `common.get_client(account_id)` 收到的 account_id（选号规则靠它断言）
        self.account_ids: List[str] = []
        #: 每次上传看到的 (路径, 后缀, 字节头部, 全量字节)
        self.uploads: List[Dict[str, Any]] = []
        self.error: Optional[BaseException] = None
        self.persist_account_id = ''

    async def upload_image(self, path: str) -> Dict[str, Any]:
        raw = Path(path).read_bytes()
        self.uploads.append({
            'path': path,
            'suffix': Path(path).suffix,
            'head': raw[:3],
            'bytes': raw,
        })
        if self.error is not None:
            raise self.error
        return {'url': self.url, 'fileId': 'F-1', 'width': 800, 'height': 600}


def install_fake_protocol(fake: FakeClient) -> None:
    module = types.ModuleType(PROTOCOL_MODULE)

    def _factory(account_id):
        fake.account_ids.append(str(account_id))
        return fake

    module.get_client = _factory  # type: ignore[attr-defined]
    module.XianyuClient = lambda cookie: fake  # type: ignore[attr-defined]
    sys.modules[PROTOCOL_MODULE] = module


def block_protocol() -> None:
    """让 `import backend.app.xianyu.client` 抛 ImportError（协议层未就绪）。"""
    sys.modules[PROTOCOL_MODULE] = None  # type: ignore[assignment]


def unblock_protocol() -> None:
    sys.modules.pop(PROTOCOL_MODULE, None)


# --------------------------------------------------------------------------- 夹具


def jpeg_bytes(size=(60, 40), color=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, 'JPEG')
    return buffer.getvalue()


def png_bytes(size=(30, 20), color=(10, 200, 10)) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, 'PNG')
    return buffer.getvalue()


def make_account(account_id: str, enabled: int = 1, cookie: str = 'unb=1; _m_h5_tk=t') -> None:
    db.execute(
        'INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) VALUES(?,?,?,?)',
        (account_id, f'账号 {account_id}', enabled, 1),
    )
    db.execute(
        'INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) VALUES(?,?,?)',
        (account_id, cookie, 'active'),
    )


class UploadsApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        use_temp_app_dir(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        unblock_protocol()
        db.reset_connection()
        cls.tmp.cleanup()

    def setUp(self):
        db.reset_connection()
        unblock_protocol()
        for table in ('operation_logs', 'account_credentials', 'accounts', 'users'):
            db.execute(f'DELETE FROM {table}')
        # 只挂 uploads 这一个 router：路由注册由 Lead 在 main.py 做，这里测的是
        # 本模块自己的契约（`test_router_exposes_contract_paths` 锁路径表）。
        app = FastAPI()
        app.include_router(uploads_api.router)
        self.ctx = TestClient(app)
        self.client = self.ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('uploads','x',1)"))
        self.token = security.issue_token(self.user_id)
        self.headers = {'Authorization': 'Bearer ' + self.token}
        self.fake = FakeClient()
        self._clear_uploads()

    def tearDown(self):
        self._clear_uploads()
        db.execute('DELETE FROM users')
        self.ctx.__exit__(None, None, None)

    # -- 辅助 ---------------------------------------------------------------

    def _clear_uploads(self) -> None:
        """用例之间必须互不影响：`_stored_files()` 是「没有孤儿文件」的判据。"""
        for item in uploads.upload_dir().iterdir():
            if item.is_file():
                item.unlink()

    def _stored_files(self) -> List[str]:
        return sorted(p.name for p in uploads.upload_dir().iterdir() if p.is_file())

    def _post(self, filename: str, data: bytes, account_id: Optional[str] = None,
              content_type: str = 'application/octet-stream', headers: Optional[dict] = None):
        form = {} if account_id is None else {'account_id': account_id}
        return self.client.post(
            '/api/uploads/images',
            files={'file': (filename, data, content_type)},
            data=form,
            headers=self.headers if headers is None else headers,
        )

    def _logs(self, action: str = 'upload_image') -> List[dict]:
        return [dict(row) for row in db.query(
            'SELECT * FROM operation_logs WHERE action = ? ORDER BY id', (action,))]

    # -- 成功路径 -----------------------------------------------------------

    def test_upload_success_returns_remote_url_and_local_path(self):
        make_account(ACC)
        install_fake_protocol(self.fake)
        raw = jpeg_bytes((60, 40))

        resp = self._post('我的图.jpg', raw, account_id=ACC, content_type='image/jpeg')
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body['success'], resp.text)
        data = body['data']

        self.assertEqual(data['url'], 'https://img.test/uploaded.jpg')
        self.assertEqual(data['file_id'], 'F-1')
        self.assertEqual(data['name'], '我的图.jpg')
        # 尺寸取本地真实像素（60x40），**不是**假客户端的 800x600：
        # `parse_upload_response` 在响应缺 `object.pix` 时兜底 800x800，那是假值。
        self.assertEqual((data['width'], data['height']), (60, 40))

        local = Path(data['local_path'])
        self.assertTrue(local.is_file(), data['local_path'])
        self.assertEqual(local.read_bytes(), raw)
        # 文件名是 uuid4().hex + 扩展名，不是用户给的名字
        # （中文名 / 穿越 / 同名覆盖都在这里消失）
        self.assertRegex(local.name, r'^[0-9a-f]{32}\.jpg$')
        self.assertEqual(self._stored_files(), [local.name])

        self.assertEqual(self.fake.account_ids[-1], ACC)
        self.assertEqual(len(self.fake.uploads), 1)
        self.assertEqual(self.fake.uploads[0]['suffix'], '.jpg')
        self.assertEqual(self.fake.uploads[0]['bytes'], raw)
        self.assertEqual(self.fake.uploads[0]['path'], str(local))

        logs = self._logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['success'], 1)
        self.assertEqual(logs[0]['account_id'], ACC)

    def test_upload_png_is_transcoded_to_jpeg_and_temp_is_removed(self):
        make_account(ACC)
        install_fake_protocol(self.fake)
        raw = png_bytes((30, 20))

        resp = self._post('截图.png', raw, account_id=ACC, content_type='image/png')
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']

        # 交给协议层的必须是 JPEG 字节（平台对「png 改扩展名」会拒）
        seen = self.fake.uploads[0]
        self.assertEqual(seen['suffix'], '.jpg')
        self.assertEqual(seen['head'], b'\xff\xd8\xff')
        self.assertNotEqual(seen['bytes'], raw)

        # 本地留下的仍是原图（png），前端预览拿到的就是用户拖进来的那张
        local = Path(data['local_path'])
        self.assertEqual(local.suffix, '.png')
        self.assertEqual(local.read_bytes(), raw)

        # 转码临时文件用完即删：目录里只剩这一张图
        self.assertEqual(self._stored_files(), [local.name])

    def test_upload_without_account_id_prefers_enabled_account(self):
        make_account(ACC_OFF, enabled=0)
        make_account(ACC, enabled=1)
        install_fake_protocol(self.fake)

        resp = self._post('a.jpg', jpeg_bytes(), content_type='image/jpeg')
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.fake.account_ids[-1], ACC)

    def test_disabled_account_with_cookie_is_still_usable(self):
        """账号表 `enabled` 默认 0；只认 enabled=1 会让刚扫码的账号连图都传不上去。"""
        make_account(ACC_OFF, enabled=0)
        install_fake_protocol(self.fake)

        resp = self._post('a.jpg', jpeg_bytes(), content_type='image/jpeg')
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.fake.account_ids[-1], ACC_OFF)

    # -- 拒绝路径 -----------------------------------------------------------

    def test_rejects_non_image_extension(self):
        make_account(ACC)
        install_fake_protocol(self.fake)

        resp = self._post('notes.txt', b'hello', account_id=ACC, content_type='text/plain')
        self.assertEqual(resp.status_code, 400, resp.text)
        body = resp.json()
        self.assertFalse(body['success'])
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertEqual(self.fake.uploads, [])
        self.assertEqual(self._stored_files(), [])

    def test_rejects_bytes_that_are_not_an_image(self):
        """扩展名是 .jpg 但内容是文本 —— 必须在落盘前拒掉。"""
        make_account(ACC)
        install_fake_protocol(self.fake)

        resp = self._post('fake.jpg', b'this is definitely not a jpeg', account_id=ACC,
                          content_type='image/jpeg')
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self._stored_files(), [])

    def test_rejects_oversized_file(self):
        make_account(ACC)
        install_fake_protocol(self.fake)

        resp = self._post('big.jpg', b'\xff' * (uploads.MAX_UPLOAD_BYTES + 1), account_id=ACC,
                          content_type='image/jpeg')
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertIn('过大', resp.json()['message'])
        self.assertEqual(self._stored_files(), [])

    def test_rejects_empty_file(self):
        make_account(ACC)
        install_fake_protocol(self.fake)

        resp = self._post('empty.jpg', b'', account_id=ACC, content_type='image/jpeg')
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')

    def test_upload_without_any_account_returns_not_ready(self):
        install_fake_protocol(self.fake)

        resp = self._post('a.jpg', jpeg_bytes(), content_type='image/jpeg')
        self.assertEqual(resp.status_code, 503, resp.text)
        body = resp.json()
        self.assertEqual(body['code'], common.NOT_READY_CODE)
        self.assertIn('账号', body['message'])
        # 没落盘、也没进协议层：不为一个必然失败的请求写文件
        self.assertEqual(self._stored_files(), [])
        self.assertEqual(self.fake.uploads, [])

    def test_unknown_account_id_returns_not_found(self):
        make_account(ACC)
        install_fake_protocol(self.fake)

        resp = self._post('a.jpg', jpeg_bytes(), account_id='ghost_acc', content_type='image/jpeg')
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()['code'], 'not_found')
        self.assertEqual(self._stored_files(), [])

    def test_protocol_missing_returns_not_ready_and_cleans_up(self):
        make_account(ACC)
        block_protocol()

        resp = self._post('a.jpg', jpeg_bytes(), account_id=ACC, content_type='image/jpeg')
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(resp.json()['code'], common.NOT_READY_CODE)
        # 上传失败 → 本地文件不能留下
        self.assertEqual(self._stored_files(), [])
        logs = self._logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['success'], 0)
        self.assertIn(common.NOT_READY_CODE, logs[0]['detail'])

    def test_session_expired_maps_to_verification_required(self):
        """Cookie 失效必须给 409 + `need_relogin`，不能降级成「偶发网络错误」。"""
        make_account(ACC)
        install_fake_protocol(self.fake)
        self.fake.error = SessionExpiredError('图片上传返回登录页，Cookie 已失效')

        resp = self._post('a.jpg', jpeg_bytes(), account_id=ACC, content_type='image/jpeg')
        self.assertEqual(resp.status_code, 409, resp.text)
        body = resp.json()
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body['login_state'], 'need_relogin')
        self.assertEqual(self._stored_files(), [])
        self.assertEqual(self._logs()[0]['success'], 0)

    def test_upload_requires_auth(self):
        make_account(ACC)
        install_fake_protocol(self.fake)

        resp = self._post('a.jpg', jpeg_bytes(), account_id=ACC, headers={})
        self.assertEqual(resp.status_code, 401, resp.text)
        self.assertEqual(self._stored_files(), [])

    def test_missing_file_field_is_rejected(self):
        make_account(ACC)
        install_fake_protocol(self.fake)

        resp = self.client.post('/api/uploads/images', data={'account_id': ACC},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(self._stored_files(), [])

    # -- 本地回显 -----------------------------------------------------------

    def test_local_image_served_with_bearer_and_with_query_token(self):
        make_account(ACC)
        install_fake_protocol(self.fake)
        raw = jpeg_bytes((20, 10))
        data = self._post('p.jpg', raw, account_id=ACC, content_type='image/jpeg').json()['data']
        name = Path(data['local_path']).name

        with_header = self.client.get(f'/api/uploads/local/{name}', headers=self.headers)
        self.assertEqual(with_header.status_code, 200, with_header.text)
        self.assertEqual(with_header.content, raw)
        self.assertEqual(with_header.headers['content-type'], 'image/jpeg')

        # `<img src>` 带不了 Authorization 头，所以还要吃 ?token=
        with_query = self.client.get(f'/api/uploads/local/{name}?token={self.token}')
        self.assertEqual(with_query.status_code, 200, with_query.text)
        self.assertEqual(with_query.content, raw)

        self.assertEqual(self.client.get(f'/api/uploads/local/{name}').status_code, 401)

    def test_local_image_unknown_name_is_404(self):
        make_account(ACC)
        install_fake_protocol(self.fake)

        resp = self.client.get(f'/api/uploads/local/{uuid_name()}.jpg', headers=self.headers)
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()['code'], 'not_found')

    def test_local_image_rejects_unsafe_names(self):
        make_account(ACC)
        # 落盘目录外面放一个真实文件：穿越如果没挡住，它会被读出去
        secret = Path(self.tmp.name) / 'secret.jpg'
        secret.write_bytes(b'TOP-SECRET')
        inside = uploads.upload_dir() / 'real.jpg'
        inside.write_bytes(jpeg_bytes((8, 8)))

        # 直接判 `local_path_for`（HTTP 那一层只能测到客户端不规范化掉的名字：
        # httpx 会把 `real.jpg/../real.jpg` 先规范化成 `real.jpg`，所以这类花样
        # 在白名单函数上断言才作数）
        for name in ('../secret.jpg', '..%2Fsecret.jpg', 'secret.jpg', 'a/b.jpg',
                     'real.jpg/../real.jpg', '...jpg', 'real.jpg.bak', '.hidden.jpg',
                     '', 'x' * 200 + '.jpg'):
            with self.subTest(name=name):
                self.assertIsNone(uploads.local_path_for(name), name)

        for name in ('../secret.jpg', '..%2Fsecret.jpg', 'secret.jpg', 'a/b.jpg',
                     '...jpg', 'real.jpg.bak', '.hidden.jpg', 'x' * 200 + '.jpg'):
            with self.subTest(http=name):
                resp = self.client.get(f'/api/uploads/local/{name}', headers=self.headers)
                self.assertEqual(resp.status_code, 404, f'{name} -> {resp.status_code} {resp.text}')

        # 白名单内的正常名字仍然能取到
        resp = self.client.get('/api/uploads/local/real.jpg', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertNotEqual(resp.content, b'TOP-SECRET')

    # -- 路由表 -------------------------------------------------------------

    def test_router_exposes_contract_paths(self):
        paths = {route.path for route in uploads_api.router.routes}
        self.assertEqual(paths, {'/api/uploads/images', '/api/uploads/local/{name}'})


def uuid_name() -> str:
    """一个合法的、但一定不存在的 uuid 文件名。"""
    from uuid import uuid4

    return uuid4().hex


if __name__ == '__main__':
    unittest.main(verbosity=2)
