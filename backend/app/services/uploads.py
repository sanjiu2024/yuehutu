"""图片上传服务：拖进来的本地图片 → 本地落盘 → 闲鱼图床 URL。

`backend/app/api/uploads.py` 只做 multipart 拆包与信封，所有判断都在这里：

1. **校验**：扩展名白名单（jpg/jpeg/png/webp）、非空、≤ 10MB，并且真的用 Pillow
   解一次 —— 扩展名写着 `.jpg` 但内容是文本的文件在落盘前就被拒掉，而不是拖到
   协议层换回一句看不懂的报错。
2. **落盘**：`config.DATA_DIR / 'uploads'`，文件名 `uuid4().hex` + 原扩展名。
   用户给的文件名只用来取扩展名（顺便留一份给人看）：中文名 / `../` / 同名覆盖
   都在这一步消失。
3. **转码**：协议层 `client.upload_image` 只按 `.jpg` 发送
   （见 `backend/app/xianyu/client.py:618`），平台对「png 改扩展名」会拒，所以
   非 jpg 先用 Pillow 转成 RGB JPEG 写一个临时文件，上传完就删。透明通道会被
   丢成黑底（JPEG 没有 alpha），这是平台侧的硬限制。
4. **上传**：`common.get_client(account_id)` + `common.call_method(client, ('upload_image',), path)`；
   错误映射统一走 `common.call_failed` —— 协议层没实现 → `xianyu_not_ready`，
   Cookie 失效 → `verification_required` + `login_state: need_relogin`。
5. **清理**：上传失败时删掉落盘文件，不留没人认领的垃圾。

账号怎么选：`account_id` 给了就用（不存在给 `not_found`）；没给则按
「**第一个 enabled 且有 Cookie** 的账号」—— 和 `main.py:105` 挂 IM 入站的口径一致；
若一个 enabled 的都没有，再退一步用任意**有 Cookie** 的账号。这一步是必要的宽容：
账号表里 `enabled` 的默认值是 0（`db.py:50`），只认 `enabled=1` 会让「刚扫码登录
但还没在前端点启用」的账号连图都传不上去，而传图本身不依赖启用状态。
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

from loguru import logger

from ..core import config, db
from . import common

#: 落盘目录名（相对 `config.DATA_DIR`）
UPLOAD_DIR_NAME = 'uploads'

#: 收哪些扩展名。平台侧只认 jpg，别的都要先转（见模块说明第 3 条）。
ALLOWED_EXTENSIONS = ('jpg', 'jpeg', 'png', 'webp')
JPEG_EXTENSIONS = ('jpg', 'jpeg')

#: 单文件上限
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

CONTENT_TYPES = {
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'png': 'image/png',
    'webp': 'image/webp',
}

#: 我们写出来的文件名长这样（`uuid4().hex` + 扩展名）。回显路由据此白名单，
#: 注意它天然**不允许**第二个点，所以转码用的 `<uuid>.upload.jpg` 临时文件
#: 不可能被 `GET /api/uploads/local/{name}` 取出去。
SAFE_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp)$', re.I)


# --------------------------------------------------------------------------- 目录与文件名


def upload_dir() -> Path:
    """落盘目录（不存在就建）。"""
    directory = Path(config.DATA_DIR) / UPLOAD_DIR_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # 建目录失败留给后面的写盘报更具体的错
        logger.warning(f'创建上传目录失败: {directory}: {exc}')
    return directory


def local_path_for(name: str) -> Optional[Path]:
    """把回显路由里的 `{name}` 收成一个真实路径；不安全或不存在返回 None。

    两道闸：名字必须匹配 `SAFE_NAME_RE`（`..`、`/`、`\\`、第二个点全被这关挡住），
    且 `resolve()` 之后的**父目录必须就是落盘目录本身** —— 软链、大小写、
    `%2e%2e` 之类解码后的花样到这里都已经变成普通字符串，再过一次真实路径判等。
    """
    text = str(name or '')
    if not SAFE_NAME_RE.match(text):
        return None
    directory = upload_dir()
    try:
        root = directory.resolve()
        target = (directory / text).resolve()
    except OSError:
        return None
    if target.parent != root:
        return None
    if not target.is_file():
        return None
    return target


def content_type_for(name: str) -> str:
    return CONTENT_TYPES.get(extension_of(name), 'application/octet-stream')


def extension_of(filename: str) -> str:
    """取小写扩展名（不带点）；没有点返回空串。"""
    text = str(filename or '').strip().lower()
    if '.' not in text:
        return ''
    return text.rsplit('.', 1)[1]


def _display_name(filename: str) -> str:
    """只留文件名本身（`C:\\a\\b.jpg` 与 `../../b.jpg` 都收成 `b.jpg`），限长。"""
    text = str(filename or '').replace('\\', '/').split('/')[-1].strip()
    return text[:120]


# --------------------------------------------------------------------------- 图片探测 / 转码


_PIL: Any = None
_PIL_CHECKED = False


def _pillow() -> Any:
    """懒拿 Pillow 的 `Image`；没装返回 None（不抛，让调用方降级）。"""
    global _PIL, _PIL_CHECKED
    if not _PIL_CHECKED:
        _PIL_CHECKED = True
        try:
            from PIL import Image  # noqa: PLC0415 - 懒导入：缺 Pillow 也要能起服务

            _PIL = Image
        except Exception:  # noqa: BLE001
            _PIL = None
    return _PIL


def probe_image(data: bytes) -> Optional[Tuple[int, int]]:
    """用 Pillow 读一次图头，返回 (宽, 高)。不是图片 / Pillow 不可用返回 None。

    `Image.open` 只解头部，不 `load()`：既能挡掉「扩展名是 jpg、内容是文本」，
    又不会为了校验把一张 10MB 的图整个解进内存。
    """
    image = _pillow()
    if image is None:
        return None
    try:
        with image.open(io.BytesIO(data)) as handle:
            width, height = handle.size
        return int(width), int(height)
    except Exception:  # noqa: BLE001 - 解码失败就是「不是图片」
        return None


def _prepare_for_upload(local_path: Path, extension: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """返回 (给协议层的路径, 失败信封)。jpg 原样，其它转码到临时文件。

    临时文件名带两个点（`<uuid>.upload.jpg`），因此不可能被回显路由取走；
    调用方负责删。
    """
    if extension in JPEG_EXTENSIONS:
        return str(local_path), None
    image = _pillow()
    if image is None:
        return '', common.fail(
            '服务端缺少 Pillow，无法把 png/webp 转成平台接受的 jpg；请改用 jpg 图片或安装 Pillow',
            'bad_request',
        )
    temp = local_path.parent / f'{uuid4().hex}.upload.jpg'
    try:
        with image.open(str(local_path)) as handle:
            handle.convert('RGB').save(str(temp), 'JPEG', quality=92)
    except Exception as exc:  # noqa: BLE001
        _remove(temp)
        return '', common.fail(f'图片转码失败: {exc}', 'invalid_fields')
    return str(temp), None


def _remove(path: Any) -> None:
    """删掉一个文件，失败只记日志（清理不能反过来把请求搞崩）。"""
    try:
        target = Path(path)
        if target.is_file():
            target.unlink()
    except OSError as exc:
        logger.warning(f'删除上传临时文件失败: {path}: {exc}')


# --------------------------------------------------------------------------- 账号


def resolve_account(account_id: str = '') -> Tuple[str, Optional[Dict[str, Any]]]:
    """返回 (account_id, None) 或 ('', 失败信封)。选号规则见模块说明。"""
    wanted = str(account_id or '').strip()
    if wanted:
        if db.query_one('SELECT id FROM accounts WHERE id = ?', (wanted,)) is None:
            return '', common.fail(f'账号不存在: {wanted}', 'not_found')
        return wanted, None

    preferred = db.query_one(
        'SELECT a.id AS id FROM accounts a '
        'JOIN account_credentials c ON c.account_id = a.id '
        "WHERE a.enabled = 1 AND COALESCE(c.cookie, '') <> '' "
        'ORDER BY a.updated_at DESC, a.id LIMIT 1'
    )
    if preferred is not None:
        return str(preferred['id']), None
    fallback = db.query_one(
        'SELECT a.id AS id FROM accounts a '
        'JOIN account_credentials c ON c.account_id = a.id '
        "WHERE COALESCE(c.cookie, '') <> '' "
        'ORDER BY a.updated_at DESC, a.id LIMIT 1'
    )
    if fallback is not None:
        return str(fallback['id']), None
    return '', common.not_ready('还没有可用的闲鱼账号，请先在「账号」页完成扫码登录')


# --------------------------------------------------------------------------- 落盘


def save_upload(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    """校验 + 落盘。成功返回描述 dict，失败返回失败信封（不落任何文件）。

    返回：`{local_path, name, extension, size, width, height}`
    """
    data = file_bytes or b''
    if not data:
        return common.fail('图片内容为空', 'invalid_fields')
    # 先判大小再看内容：10MB 的文件不该为了报格式错被 Pillow 读一遍
    if len(data) > MAX_UPLOAD_BYTES:
        return common.fail(
            f'图片过大：{len(data) / (1024 * 1024):.1f}MB，上限 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB',
            'invalid_fields',
        )
    extension = extension_of(filename)
    if extension not in ALLOWED_EXTENSIONS:
        return common.fail('不支持的图片格式：只收 jpg / jpeg / png / webp', 'invalid_fields')

    size = probe_image(data)
    if size is None and _pillow() is not None:
        return common.fail('这不是一张能识别的图片（文件已损坏，或扩展名与实际内容不符）', 'invalid_fields')
    width, height = size or (0, 0)

    directory = upload_dir()
    target = directory / f'{uuid4().hex}.{extension}'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError as exc:
        return common.fail(f'图片保存失败: {exc}', 'bad_request')
    return {
        'local_path': str(target),
        'name': _display_name(filename) or target.name,
        'extension': extension,
        'size': len(data),
        'width': width,
        'height': height,
    }


# --------------------------------------------------------------------------- 上传


def _upload_url(value: Any) -> str:
    """从协议层返回值里抠 URL（真客户端已经归一化过，这里只做容错）。"""
    if isinstance(value, dict):
        for key in ('url', 'fileUrl', 'file_url'):
            candidate = value.get(key)
            if candidate:
                return str(candidate)
        for key in ('data', 'object', 'result'):
            nested = _upload_url(value.get(key))
            if nested:
                return nested
    return ''


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def upload_image_file(local_path: Any, account_id: str = '') -> Dict[str, Any]:
    """把**已经落盘**的图片交给协议层上传，返回 `{url, file_id, width, height}` 或失败信封。"""
    account, failure = resolve_account(account_id)
    if failure is not None:
        return failure
    path = str(local_path or '')
    if not path or not Path(path).is_file():
        return common.fail(f'待上传的图片不存在: {path}', 'not_found')

    client, failure = common.get_client(account)
    if client is None:
        return failure or common.not_ready()

    status, value = common.call_method(client, ('upload_image',), path)
    failure = common.call_failed(status, value)
    if failure is not None:
        return failure

    url = _upload_url(value)
    if not url:
        return common.fail(f'协议层上传成功但没有返回图片 URL: {str(value)[:200]}', 'protocol_error')
    width = height = 0
    file_id = ''
    if isinstance(value, dict):
        file_id = str(value.get('fileId') or value.get('file_id') or '')
        width = _int(value.get('width'))
        height = _int(value.get('height'))
    return {'url': url, 'file_id': file_id, 'width': width, 'height': height}


def handle_upload(file_bytes: bytes, filename: str, account_id: str = '') -> Dict[str, Any]:
    """`POST /api/uploads/images` 的全部逻辑。

    成功返回**响应体 `data` 该长的样子**：
    `{url, file_id, width, height, name, local_path}`；失败返回失败信封。
    """
    account, failure = resolve_account(account_id)
    if failure is not None:
        # 还没落盘就能拒的环境错误：不为一个必然失败的请求写文件
        return failure

    saved = save_upload(file_bytes, filename)
    if common.is_failure(saved):
        common.log_operation(account, 'upload_image', '', False, {
            'name': _display_name(filename),
            'bytes': len(file_bytes or b''),
            'code': saved.get('code'),
            'message': saved.get('message'),
        })
        return saved

    local_path = Path(saved['local_path'])
    temp_path = ''
    try:
        temp_path, failure = _prepare_for_upload(local_path, saved['extension'])
        uploaded = failure if failure is not None else upload_image_file(temp_path, account)
    except Exception as exc:  # noqa: BLE001 - 意外异常也要清理并给出信封
        logger.warning(f'图片上传异常: {exc}')
        uploaded = common.fail(f'图片上传失败: {exc}', 'protocol_error')
    finally:
        if temp_path and Path(temp_path) != local_path:
            _remove(temp_path)

    if common.is_failure(uploaded):
        _remove(local_path)
        common.log_operation(account, 'upload_image', '', False, {
            'name': saved['name'],
            'bytes': saved['size'],
            'code': uploaded.get('code'),
            'message': uploaded.get('message'),
        })
        return uploaded

    payload = {
        'url': uploaded['url'],
        'file_id': str(uploaded.get('file_id') or ''),
        # 尺寸以**本地真实像素**为准：`parse_upload_response` 在响应里没有 `object.pix`
        # 时会兜底给 800x800（client.py:800），那是个假值，拿它盖掉真尺寸会让前端
        # 排版跳。协议层的值只在 Pillow 不可用时（本地探不出尺寸 = 0）才用。
        'width': _int(saved.get('width')) or _int(uploaded.get('width')),
        'height': _int(saved.get('height')) or _int(uploaded.get('height')),
        'name': saved['name'],
        'local_path': str(local_path),
    }
    common.log_operation(account, 'upload_image', '', True, {
        'name': saved['name'],
        'bytes': saved['size'],
        'url': payload['url'],
        'file_id': payload['file_id'],
    })
    return payload
