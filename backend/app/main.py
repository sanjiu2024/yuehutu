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
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from .core import config, db, events, security

# 按需注册的路由模块（api/<name>.py 里必须有 router 对象）
ROUTE_MODULES: List[str] = [
    'auth',
    'system',
    'ws',
    'accounts',
    'items',
    'chats',
    'agents',
    'providers',
    'orders',
    'risk',
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
    config.ensure_dirs()
    db.init_db()
    username, password = security.ensure_admin()
    security.local_secret()
    security.purge_expired()
    events.bind_loop(asyncio.get_running_loop())
    logger.info('数据库就绪: {}', config.DB_PATH)
    logger.info('管理员账号: {}', username)
    if password:
        logger.info('首次运行已生成随机密码，见 {}', config.CRED_PATH)
    yield
    logger.info('后端退出')


def create_app() -> FastAPI:
    app = FastAPI(title=config.APP_TITLE, version=config.VERSION, lifespan=lifespan)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
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

    if config.FRONTEND_DIST.exists():
        app.mount('/', StaticFiles(directory=str(config.FRONTEND_DIST), html=True), name='frontend')
    else:
        @app.get('/', response_class=HTMLResponse)
        async def placeholder():  # pragma: no cover - 仅开发期
            return PLACEHOLDER.format(title=config.APP_TITLE, version=config.VERSION, app_dir=config.APP_DIR)

    return app


app = create_app()


def run_server(host: str = config.HOST, port: int = 0) -> int:
    """阻塞式启动；返回实际监听的端口调用方可先传 0 再用 `pick_free_port()`。"""
    import uvicorn

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
