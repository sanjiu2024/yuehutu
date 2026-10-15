"""站点 CODE 头（`idle_site_biz_code`）的契约测试。

**不打真实平台**：这里用一个「鉴权门」假 mtop 服务端 —— 请求没带（或带空）站点 CODE
就回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` + `data.errorMsg=站点CODE不能为空`，带了才 SUCCESS。
这正是 2026-10-16 真机查出的根因形态（见 `docs/audit/permission_rootcause62.md`）：
`seller.platform` 一族的 502「本账号没权限」其实是**我们少发一个头**，不是账号权限缺失。

跑法（仓库根目录）：
    PYTHONPATH=. C:/Python310/python.exe backend/tests/test_xianyu_site_header.py
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from backend.app.xianyu import mtop  # noqa: E402
from backend.app.xianyu.client import XianyuClient, XianyuError  # noqa: E402

COOKIE = "unb=2200123456; _m_h5_tk=abcdef123456_1712345678901; cookie2=xyz"

#: 真机查证的三条：前两条补头后 SUCCESS，第三条补头后鉴权过了、错误码推进到参数层。
SELLER_PLATFORM_APIS = (
    "mtop.taobao.idle.seller.platform.pc.item.status.statistics",
    "mtop.taobao.idle.seller.platform.item.pc.search.categories",
    "mtop.alibaba.idle.seller.platform.order.backfill.report",
)


class SiteGateStub:
    """按「有没有带站点 CODE」回话的假 mtop 服务端，记录每次请求的 api 与请求头。"""

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        api = request.url.params.get("api", "")
        site = request.headers.get("idle_site_biz_code")
        self.calls.append({"api": api, "site": site, "headers": dict(request.headers)})
        if not site:
            return httpx.Response(200, json={
                "ret": ["FAIL_BIZ_IDLE_USER_UNAUTHORIZED::用户无权限访问当前接口"],
                "data": {"errorMsg": "站点CODE不能为空", "permissionCode": "item:search:count"},
            })
        return httpx.Response(200, json={"ret": ["SUCCESS::调用成功"], "data": {"ok": True}})


def make_client():
    stub = SiteGateStub()
    return XianyuClient(COOKIE, transport=httpx.MockTransport(stub)), stub


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class SiteBizCodeTest(unittest.TestCase):
    """站点 CODE 是**通用** mtop 头，不是逐接口补丁。"""

    def test_constant_is_commonpro(self):
        # 官方工作台 URL 的 `?site=COMMONPRO`。
        self.assertEqual(mtop.SITE_BIZ_CODE, "COMMONPRO")

    def test_every_mtop_call_carries_the_site_code(self):
        apis = SELLER_PLATFORM_APIS + (
            "mtop.idle.web.xyh.item.list",
            "mtop.taobao.idle.ad.account.info",
            "mtop.cainiao.geography.frontend.divisionsinfo.get",
        )
        for api in apis:
            client, stub = make_client()
            run(client.mtop(api, {"a": 1}))
            self.assertEqual(stub.calls[0]["site"], mtop.SITE_BIZ_CODE, api)

    def test_platform_gate_opens_with_the_header(self):
        for api in SELLER_PLATFORM_APIS:
            client, stub = make_client()
            result = run(client.mtop(api, {}))
            self.assertTrue(result["success"], api)
            self.assertEqual(result["data"], {"ok": True}, api)

    def test_empty_site_code_is_refused_like_the_real_platform(self):
        # `extra_headers` 能覆盖默认值 ⇒ 用它把站点 CODE 打回空串，复现真机那条拒绝。
        # 空串在平台眼里等于没带（真机：用户无权限访问当前站点[]）。
        client, _ = make_client()
        with self.assertRaises(XianyuError) as ctx:
            run(client.mtop(SELLER_PLATFORM_APIS[0], {}, extra_headers={"idle_site_biz_code": ""}))
        self.assertIn("FAIL_BIZ_IDLE_USER_UNAUTHORIZED", str(ctx.exception))

    def test_security_headers_still_override_origin_and_referer(self):
        # 安全中心还要把 origin/referer 指向 seller.goofish.com（通用头不受影响）。
        client, stub = make_client()
        run(client.mtop("mtop.idle.pc.security.score.get", {},
                        extra_headers=XianyuClient._security_headers()))
        headers = stub.calls[0]["headers"]
        self.assertEqual(headers.get("origin"), "https://seller.goofish.com")
        self.assertEqual(headers.get("referer"), "https://seller.goofish.com/")
        self.assertEqual(headers.get("idle_site_biz_code"), mtop.SITE_BIZ_CODE)


if __name__ == "__main__":
    unittest.main()
