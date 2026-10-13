# 粉丝价（全部粉 / 老粉 / 已购粉）契约（读 + 写）

> 为什么并进 `/api/items` 而不是单开前缀：粉丝价是**某一件商品的属性**（跟价格、库存、图片同级），
> 官方也是挂在商品列表行内的弹窗里。单开后缀只会让「商品属性」这一族被劈成两半。
>
> 与 `quick-edit`（快捷改价）的区别：`quick-edit` 改的是**商品售价**（`price` / `quantity`），
> 本页改的是**粉丝专属价**（三组），两者互不影响，别顺手一起发。

证据来源（零平台请求，全部静态提取）：`C:\Users\Administrator\.dsh-tmp\adapp\item-main.js`
（4,402,065 字符单行；官方卖家工作台商品域 bundle）。

## 1. 两个平台接口

| 用途 | mtop | 版本 | 请求 | 响应 |
| --- | --- | --- | --- | --- |
| 读配置 | `mtop.alibaba.idle.seller.platform.item.operate.fansprice.get` | 1.0 | `{itemId}` | `data.data.fansPriceConfig.fansPriceList[]`，元素 `{fansGroup, priceString}` |
| 写配置 | `mtop.alibaba.idle.seller.platform.item.operate.fansprice.set` | 1.0 | `{itemId, fansPriceConfig: {fansPriceList: [...]}}` | `data.data` 有值 = 成功 |

三组 `fansGroup` 的取值与官方顺序：`all`（全部粉）/ `old`（老粉）/ `buy`（已购粉）。

官方调用点逐字（`item-main.js` @4086493 读、@4101088 写）：

- 读成功判据 `t.data?.data?.code === 'success'` → 取 `fansPriceConfig.fansPriceList`；
- 写时**只把有值的组放进列表**：`["all","old","buy"].forEach(g => e[g] && list.push({fansGroup: g, priceString: e[g]}))`
  —— 所以**留空 = 清空**，这就是「整份替换」的出处；
- 写成功 toast「编辑成功」，失败读 `data.msg` 或回落到「编辑失败，请重试」。

官方校验原文（文案要保留原意）：小数位超过 2 位 → 「粉丝价最多为2位小数，请修改」；
填 `0` → 「粉丝价必须大于0，请修改」。

## 1.1 HTTP 面（同一路径 1 读 + 1 写）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/items/{account_id}/{item_id}/fans-price` | 读当前三组粉丝价 |
| POST | `/api/items/{account_id}/{item_id}/fans-price` | 整份替换三组，body `{groups?, prices?, confirm}` |

`groups` 与 `prices` 至少给一个（都给时以 `prices` 为准）。错误码：
`400 confirmation_required`（未确认）/ `400 invalid_fields`（三组全空、价格非法、组名认不出）/
`409 verification_required`（无 Cookie 或已判定需重登）/ `404 not_found`（账号不存在）/
`502 protocol_error`（平台拒绝，附人话）/ `503 xianyu_not_ready`（协议层缺失）。

## 1.2 参数与本地校验（全部零平台请求）

- 价格只认十进制定点写法（正则 `^(\d+(\.\d+)?|\.\d+)$`）：**不认** `1e3` / `0x10` / 带空格或千分位；
- 小数位 ≤ **2**（`PRICE_DECIMALS`），必须 **> 0**，上限 **99999999.99**；
- 组名只认 `all` / `old` / `buy`（大小写不敏感，落库统一小写）；认不出 → 400；
- `groups` 里值为 `null` / `''` / 全空白 = 这一组提交时**不带**（= 平台侧清空）；
- 三组都不带 → 400 `invalid_fields`「至少要填一组」；
- `item_id` strip 后为空 → 400；`confirm` **只在服务层判**，且只认 `confirm is True`
  （`'false'` / `0` / `''` / `None` 一律不放行）。路由层**不许**写 `bool(body.get('confirm'))`
  —— `bool('false')` 是 `True`（第 222 条缺陷就是这么绕过写闸门的）。

## 1.3 三态（与「不造数据」的对照）

| 情况 | 返回值 |
| --- | --- |
| 平台返回了配置 | `prices: [{fans_group, price_string}, …]`，`groups` 三键有值 |
| 平台返回空列表 | `prices: []`（真的没配），`groups` 三键为 `null` |
| 平台没给列表键 | `prices: null` + warning `fans_price_config_missing`（**不等于「没配」**） |
| 某一组拿不到 | `groups[g] = null`，界面显示 `-`（**不补 0**） |
| 写：平台接受但没回 `data.data` | `saved: false` + warning `fans_price_no_echo`（**不谎报成功**） |
| 平台拒绝（无权限等） | 502 `protocol_error` + `platform_errors{api, code, message}`，**不碰 `login_state`** |

`price_key` 这类平台内部键名不外泄（服务层剥掉，只留契约里的两个键）。

## 1.4 三步顺序

写接口一律 **确认（`_confirm_gate`）→ 字段校验（协议层构造，纯本地）→ 建客户端**。
字段校验排在凭据检查之前：价格填错时不应该先听到「去重新登录」。
成功后才记 `operation_logs`（action `fans_price_set`）并播 `items.changed`。

## 2. 界面口径

行内「粉丝价」按钮打开弹窗（`frontend/src/pages/Items.tsx::FansPriceModal`）：

- **预填当前三组值，且只填一次**（`items.changed` 的静默重拉如果又填一遍，会把用户敲进去的数字冲掉）；
- 四种情况分开说：接口未就绪 / 报错（显示后端中文原话）/ `prices === null`（平台没返回配置）/
  三组全 `null`（真的没配）；
- 底部实时列出「本次会提交（整份替换）」，并明写「留空的组会被清空」；
- 保存前**必须**弹 `ConfirmDialog`，逐条列出「哪几组改成多少 / 哪几组会被清空」，并写明
  「会在闲鱼上真的改掉，不可撤销」；
- 唯一写出口 `postFansPrice` → `postItemWrite`（请求体 `confirm: true` 字面量）；
  `saved === false` 一律 `toast.error`，不当成功。

## 3. 测试

`backend/tests/test_fans_price_api.py`：**67 条**。协议 12 + 只读 8 + 闸门 6 + 校验 6 +
happy path 8 + 失败 8 + 路由（含 openapi 断言）若干。
文件头列了本族最容易犯的七个错（整份替换被当成改一组、未确认就发、`bool('false')`、
平台没返回补成 `[]`、拿不到的价格补 `0`、价格校验漏项、字段校验排在凭据检查之后）。

> 写路由测试的坑：直接 `from backend.app.main import create_app` 会炸
> `AttributeError: module 'backend.app.xianyu.client' has no attribute 'BILL_CODE_INCOME'`
> —— 假协议模块是空壳，而 `services/finance.py` **导入时**就读那个常量。
> 必须 `remove_fake_protocol()` → import/create_app → `finally` 里把假模块装回去。

## 4. AI 侧

2 个工具：`items_fans_price`（只读、无 `confirm`）与 `items_set_fans_price`（写、必带 `confirm`）。
写工具的 description 里**必须**写明「整份替换：只想改一组时要把另外两组当前值一起给」——
这是这个接口最容易造成误伤的地方（AI 很容易只给一组，结果把另外两组清空）。
对账口径：`scripts/reconcile_tools.py` 的 `WRITE_PREFIX` 加的是完整写工具名
（`items_set_fans_price`），故意不加 `items_` 宽前缀，否则只读的 `items_fans_price` 会被误判成写工具。

## 5. 未验证（诚实边界）

- 写接口**一次真机请求都没发过**（会真的改平台上的三组粉丝价）；只验到路由挂载 + 闸门
  （`confirm:false` → 400 `confirmation_required`，零平台写请求）与假客户端下的协议形状。
- 只读接口的**真机形状**没有样例：本机账号（`2217970177348`）在数据域一直回
  `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`，粉丝价读接口属同一权限域，未验证平台真实回执。
- `fanPrice.fansPriceTitle` / `fansPriceMinString` 这两个字段出现在**商品列表行**的渲染里，
  不一定是 `fansprice.get` 的响应字段 ⇒ 解析按存在性读，拿不到就是 `null`（不猜）。
