"""FastAPI 应用入口。

可以独立跑（开发）：
    set PYTHONPATH=. && python -m backend.app.main --port 8080

桌面外壳（desktop/main.py）会起一个线程调用 `run_server()` 并等 `/api/health` 就绪。

重要：路由模块按名单动态注册，**缺失的模块只警告不报错** —— 这样并行开发期间
（有人还没写完自己的模块）应用始终能起来。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import os
import threading
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from .core import config, db, events, security
from .services import im_service, risk_service, stats_worker
from .services import purge as purge_service

# 按需注册的路由模块（api/<name>.py 里必须有 router 对象）
ROUTE_MODULES: List[str] = [
    'auth',
    'system',
    'ws',
    'accounts',
    # `item_stats` 必须排在 `items` **之前**：`GET /api/items/{account_id}/stats` 会被
    # `items` 里的 `GET /api/items/{account_id}/{item_id}` 抢先匹配，把字面量 `stats`
    # 当成 item_id（真机探针实测：挂错顺序 → 404「商品不存在: probe_acc/stats」）。
    'item_stats',
    'items',
    'chats',
    'uploads',
    'agents',
    'providers',
    'orders',
    'risk',
    # 财务四页（只读）：`/api/finance/{account_id}/{income|expense|invoices|purchaser}`。
    # 前缀与其它模块不重叠，顺序无所谓。
    'finance',
]

PLACEHOLDER = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<style>
 body{{font-family:"Microsoft YaHei",system-ui,sans-serif;background:#0f172a;color:#e2e8f0;
      display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
 .box{{max-width:560px;padding:32px;border-radius:16px;background:#1e293b;line-height:1.8}}
 code{{background:#0f172a;padding:2px 6px;border-radius:6px;color:#7dd3fc}}
 .ok{{color:#4ade80}} .warn{{color:#fbbf24}}
</style></head><body><div class="box">
<h2>{title} <span class="ok">后端已就绪</span></h2>
<p>版本 <code>{version}</code>，数据目录 <code>{app_dir}</code></p>
<p class="warn">前端还没构建：<code>cd frontend &amp;&amp; npm run build</code>，
构建产物会被后端自动托管（<code>frontend/dist</code>）。</p>
<p>接口文档：<a style="color:#7dd3fc" href="/docs">/docs</a></p>
</div></body></html>"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.setup_logging()
    config.ensure_dirs()
    db.init_db()
    username, password = security.ensure_admin()
    security.local_secret()
    security.purge_expired()
    security.refresh_mcp_token()
    events.bind_loop(asyncio.get_running_loop())
    logger.info('数据库就绪: {}', config.DB_PATH)
    logger.info('管理员账号: {}', username)
    if password:
        logger.info('首次运行已生成随机密码，见 {}', config.CRED_PATH)
    # 协议层的风控锁只在内存里，重启即失；库里 blocked_until 还在，就得回灌，
    # 否则 UI 说「冷却中」而协议层立刻放行（与「协议层熔断不落库」同类的错位）。
    restored = risk_service.resync_from_db()
    if restored:
        logger.info('已从库里恢复 {} 个账号的风控冷却', restored)
    # 删号自愈：老版本删账号只清账号/风控/会话表，商品、统计缓存、操作日志、订单会留下
    # （真机现象：删掉的账号 39 件商品还挂在商品页）。只删「account_id 不在 accounts 里」
    # 的行，判断依据只有账号表本身，所以不会误伤正常账号。
    purge_service.purge_orphans()
    _start_im_services()
    stats_task = _start_stats_worker()
    _write_port_file()
    yield
    _remove_port_file()
    _stop_stats_worker(stats_task)
    logger.info('后端退出')


def _start_im_services() -> None:
    """给每个「已启用且在库里有 Cookie」的账号挂上私信入站。

    不给账号挂钩子，买家来信就永远进不了 `chats.handle_incoming` —— 后端能收 WS 事件、
    却收不到任何真实消息。钩子必须由后端自己拉起，不能指望 UI 记得点一下。

    单独开线程执行：`im_service.start()` 会去建真实 IM 连接，网络不通时会等很久，
    不能让它拖住后端就绪（后端没起来，UI 连健康检查都过不了）。
    """
    def worker() -> None:
        try:
            rows = db.query(
                'SELECT a.id AS id FROM accounts a '
                'JOIN account_credentials c ON c.account_id = a.id '
                "WHERE a.enabled = 1 AND COALESCE(c.cookie, '') <> '' ORDER BY a.id"
            )
        except Exception as exc:  # noqa: BLE001 - 拉不起 IM 不该影响后端可用
            logger.warning('读取账号列表失败，跳过 IM 入站: {}', exc)
            return
        for row in rows:
            # 注意 db.query 回来的是 sqlite3.Row，没有 .get()（用它会 AttributeError，
            # 而异常发生在后台线程里会**静默**把整轮挂载吞掉）。
            account_id = str(row['id'] or '')
            if not account_id:
                continue
            try:
                result = im_service.start(account_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning('账号 {} 的 IM 入站启动异常: {}', account_id, exc)
                continue
            if isinstance(result, dict) and result.get('attached'):
                logger.info('账号 {} 的 IM 入站已挂载', account_id)
            else:
                reason = result.get('message') if isinstance(result, dict) else result
                logger.warning('账号 {} 的 IM 入站未挂载: {}', account_id, reason)

    threading.Thread(target=worker, name='im-bootstrap', daemon=True).start()


# --------------------------------------------------------------------------- 商品数据后台拉取

#: 两轮之间等多久。平台这个接口只能按商品逐个查，一个 45 件商品的账号点一次页面就是
#: 45 连击 mtop（真机已复现 `stock_refresh_stopped=risk`）—— 所以绝不能由页面触发，
#: 只能后台一小口一小口地吃。
STATS_PULL_INTERVAL = float(os.environ.get('XYWB_STATS_INTERVAL', '300'))
#: 每个账号每轮最多查几件。
STATS_PULL_LIMIT = int(os.environ.get('XYWB_STATS_LIMIT', '5'))
#: 每件之间停几秒（连续请求比批量更扎眼）。
STATS_PULL_PAUSE = float(os.environ.get('XYWB_STATS_PAUSE', '3'))
#: 启动后先等一会儿再拉：启动瞬间本身就在同步商品/挂 IM，别叠加平台压力。
STATS_PULL_FIRST_DELAY = float(os.environ.get('XYWB_STATS_FIRST_DELAY', '60'))


async def _stats_pull_loop() -> None:
    """后台慢慢把「商品数据」拉进缓存；页面只读缓存，不等网络。

    **失败绝不能让循环退出**：一次网络抖动就把 worker 弄死的话，用户看到的是
    「商品数据永远是空的」，而且没有任何日志线索。
    """
    await asyncio.sleep(STATS_PULL_FIRST_DELAY)
    while True:
        try:
            result = await stats_worker.pull_once(
                limit_per_account=STATS_PULL_LIMIT, pause=STATS_PULL_PAUSE
            )
            if result.get('pulled') or result.get('failed'):
                logger.info('商品数据后台拉取: {}', result)
                # 拉完一轮就广播，让「商品数据」页自己静默重拉 —— 用户不用点刷新，
                # 也就不需要在前端上定时轮询（全站的实时刷新都走这条事件总线）。
                _publish_stats_updated(result)
            elif result.get('paused'):
                logger.debug('商品数据后台拉取暂停（账号在风控冷却里）')
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 一轮失败不该让 worker 死掉
            logger.warning('商品数据后台拉取失败（下一轮再试）: {}', exc)
        await asyncio.sleep(STATS_PULL_INTERVAL)


def _publish_stats_updated(result: Dict[str, Any]) -> None:
    """把「缓存变了」广播给前端（topic `items.stats.updated`）。

    逐个账号发一条：页面是按账号看缓存的，`payload` 里带 `account_id` 才能让
    「正在看的那个账号」精准重拉；`pulled/failed` 只是给人看的数字。
    """
    accounts = result.get('accounts') or {}
    for account_id, info in accounts.items():
        if not isinstance(info, dict):
            continue
        if not (info.get('pulled') or info.get('failed')):
            continue
        try:
            events.publish('items.stats.updated', {
                'account_id': str(account_id),
                'pulled': int(info.get('pulled') or 0),
                'failed': int(info.get('failed') or 0),
            })
        except Exception as exc:  # noqa: BLE001 - 广播失败不该影响拉取
            logger.debug('商品数据事件广播失败: {}', exc)


def _start_stats_worker() -> 'asyncio.Task[None]':
    """把后台拉取挂进当前事件循环，返回 task 以便退出时取消。"""
    task = asyncio.create_task(_stats_pull_loop(), name='stats-pull')
    logger.info(
        '商品数据后台拉取已启动: 每 {}s 一轮，每账号每轮 {} 件，间隔 {}s',
        STATS_PULL_INTERVAL, STATS_PULL_LIMIT, STATS_PULL_PAUSE,
    )
    return task


def _stop_stats_worker(task: 'asyncio.Task[None]') -> None:
    """退出时取消后台拉取（不 await —— 退出路径不该等一轮网络请求）。"""
    task.cancel()


def _write_port_file() -> None:
    """把实际监听端口写盘，供 MCP 插件/脚本发现随机端口的后端。

    桌面形态 `--port 0` 由内核挑端口，插件不可能猜中；`XYWB_BASE_URL` 也没法由
    用户填（每次启动都变）。所以由后端主动把端口写进 `data/backend_port.txt`。
    """
    port = os.environ.get('XYWB_PORT', '')
    if not port:
        return
    try:
        config.ensure_dirs()
        config.PORT_PATH.write_text(str(port), encoding='utf-8')
        logger.debug('端口已写盘: {} -> {}', config.PORT_PATH, port)
    except OSError as exc:  # pragma: no cover - 写不了端口文件不该拦住启动
        logger.warning('端口文件写入失败: {}', exc)


def _remove_port_file() -> None:
    """退出时删掉端口文件。

    不删的话后端已经退出、文件还指向那个死端口：MCP 插件下次启动会拿它去连，
    拿到的是连接失败而不是「没有端口文件 → 回退默认值」，排查时会以为是别的问题。
    """
    try:
        config.PORT_PATH.unlink()
        logger.debug('端口文件已删除: {}', config.PORT_PATH)
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - 删不掉不影响退出
        logger.warning('端口文件删除失败: {}', exc)


def create_app() -> FastAPI:
    app = FastAPI(title=config.APP_TITLE, version=config.VERSION, lifespan=lifespan)

    # 必须挂在 **Starlette** 的 HTTPException 上：FastAPI 的 HTTPException 是它的子类，
    # 而「路由没匹配上」的 404/405 是 Starlette 路由层直接抛父类，只注册子类抓不到，
    # 于是同一个后端一半错误是统一信封、一半是 {"detail":"Not Found"}。
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        """所有 HTTPException 都套成统一信封：{"success":false,"message":...,"code":...}。"""
        detail = exc.detail
        message = detail if isinstance(detail, str) else str(detail)
        code = {400: 'bad_request', 401: 'unauthorized', 403: 'forbidden', 404: 'not_found'}.get(
            exc.status_code, 'http_error'
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={'success': False, 'message': message, 'code': code},
            headers=getattr(exc, 'headers', None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={'success': False, 'message': str(exc.errors()[:1]), 'code': 'validation_error'},
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # pragma: no cover - 兜底
        logger.exception('未处理异常 {} {}', request.method, request.url.path)
        return JSONResponse(status_code=500, content={'success': False, 'message': str(exc), 'code': 'internal_error'})

    for name in ROUTE_MODULES:
        try:
            module = importlib.import_module(f'.api.{name}', package=__package__)
        except ImportError as exc:
            logger.warning('路由模块 {} 暂不可用（并行开发中？）: {}', name, exc)
            continue
        router = getattr(module, 'router', None)
        if router is None:
            logger.warning('路由模块 {} 里没有 router 对象，跳过', name)
            continue
        app.include_router(router)
        logger.debug('已注册路由: {}', name)

    if config.resolve_frontend_dist().joinpath('index.html').exists():
        # 注意：挂载点用空字符串（不是 '/'），这样 API 路由优先于静态兜底。
        app.mount('', StaticFiles(directory=str(config.resolve_frontend_dist()), html=True), name='frontend')
    else:
        @app.get('/', response_class=HTMLResponse)
        async def placeholder():  # pragma: no cover - 仅开发期
            return PLACEHOLDER.format(title=config.APP_TITLE, version=config.VERSION, app_dir=config.APP_DIR)

    return app


app = create_app()


def run_server(host: str = config.HOST, port: int = 0) -> int:
    """阻塞式启动；返回实际监听的端口调用方可先传 0 再用 `pick_free_port()`。"""
    import uvicorn

    # 桌面外壳直接调 run_server(port=...)，不经过 main() 的 argparse，所以这里也要
    # 记一次端口 —— 否则 lifespan 里的 _write_port_file() 拿不到值。
    os.environ['XYWB_PORT'] = str(port)
    uvicorn.run(app, host=host, port=port, log_level='info', access_log=False)
    return port


def pick_free_port(host: str = config.HOST) -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def main() -> None:
    parser = argparse.ArgumentParser(description=config.APP_TITLE)
    parser.add_argument('--host', default=config.HOST)
    parser.add_argument('--port', type=int, default=int(os.environ.get('XYWB_PORT', '8080')))
    args = parser.parse_args()
    os.environ['XYWB_PORT'] = str(args.port)
    run_server(args.host, args.port)


if __name__ == '__main__':
    main()
