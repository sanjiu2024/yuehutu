"""闲鱼协议层：指纹 / Cookie / 风控 / MTOP 客户端 / 扫码登录 / 人工验证。

这一层只做「与平台对话」，不含任何业务逻辑（商品、聊天、订单的业务规则在
``backend/app/services``）。搬运自旧项目 ``xianyu-super-butler`` 里踩过坑的部分，
按新项目的形状重写，不整包复制。

模块地图：
- ``fingerprint``：UA / Client Hints 唯一来源（实测值，不许自造）
- ``cookies``：Cookie 字符串的纯函数（x5sec 判定、挑战标记清理）
- ``risk``：按 cookie_id 独立的熔断阶梯 + 登录态过期判定
- ``mtop``：MTOP 签名与响应解析底座
- ``client``：``XianyuClient`` —— mtop / 商品 / 图片 / IM
- ``stealth``：反检测脚本与 locale（浏览器链路的唯一来源）
- ``captcha``：系统 Chrome + 持久化 profile 的人工验证
- ``login``：扫码登录会话管理
- ``store``：账号与 Cookie 的落库读写
"""

from __future__ import annotations

__all__ = [
    'fingerprint',
    'cookies',
    'risk',
    'mtop',
    'client',
    'stealth',
    'captcha',
    'login',
    'store',
]
