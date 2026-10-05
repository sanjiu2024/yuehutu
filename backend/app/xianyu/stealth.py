"""反检测脚本与浏览器 locale —— 浏览器链路的唯一来源。

**这个脚本刻意只做两件事**：藏掉 ``navigator.webdriver`` 与 Playwright 留下的
全局变量。旧项目曾有一版 17.5KB 的「全家桶」伪装（伪造 UA、屏幕尺寸、时区、
WebGL 厂商……），结果有两个致命问题：

1. 顶层重复声明 ``const originalQuery`` —— 解析期 SyntaxError，**整段脚本一行
   都没执行**，看起来「加了反检测」其实裸奔；
2. ``webdriver`` 被 ``defineProperty`` 两次，第二次抛 TypeError，后面的语句
   全部跳过。

而且伪造 UA / 屏幕 / 时间会制造**自相矛盾**的指纹（比如 UA 说 Windows 但
``navigator.platform`` 说 Linux，或时区是 UTC 而 IP 在上海），比只藏自动化标记
更容易被判定为机器人。所以这里保持最小：**只藏自动化痕迹，不伪造身份**。
"""

from __future__ import annotations

# 与 IP 地理位置一致的语言/时区。中国用户 + 上海时区是自洽的；用默认的
# UTC + en-US 反而是明显的自动化特征。
LOCALE = 'zh-CN'
TIMEZONE_ID = 'Asia/Shanghai'

# 反检测脚本。每段独立 try/catch：任何一段失败都不影响其它段，
# 避免出现「一处报错整段作废」的老问题。
STEALTH_SCRIPT = """
(() => {
  // 1. navigator.webdriver：Playwright 会把它设成 true。
  //    同时删掉原型上的同名属性，否则原型链上仍能读到 true。
  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: () => undefined,
      configurable: true,
    });
  } catch (e) {}
  try {
    delete Object.getPrototypeOf(navigator).webdriver;
  } catch (e) {}

  // 2. 清掉各版本 Playwright / CDP 注入的全局标记。
  //    cdc_ 前缀是 ChromeDriver 留下的，逐个前缀匹配。
  try {
    const leaked = ['playwright', '__playwright', '__pw_manual', '__PW_inspect'];
    for (const key of leaked) {
      try { delete window[key]; } catch (e) {}
    }
    for (const key of Object.keys(window)) {
      if (key.startsWith('cdc_')) {
        try { delete window[key]; } catch (e) {}
      }
    }
  } catch (e) {}
})();
"""
