# 安全中心写接口契约（security_write）

范围：官方「安全中心」的**写**动作 —— 申诉表单获取 / 提交申诉 / 催办。
实现：`backend/app/xianyu/security_write.py`（协议）+ `backend/app/services/security_write.py`（服务）
+ `backend/app/api/security_write.py`（HTTP）。测试：`backend/tests/test_security_write_api.py`。

## 0. 红线

1. **不对真实平台发写请求**。提交申诉会真的改变账号处罚状态，本模块只在单测里对着假协议层跑。
2. **每个写入口都要 `confirm: bool`**，未确认直接失败（HTTP 400 / `confirmation_required`），
   并且**一个平台请求都不发**（`FakeSecurityClient.apis() == []` 有断言）。
3. **平台没返回的字段一律 `null`**，不用 `0` / `''` / `{}` 顶替。
4. **失败信封原样透出**（`platform_errors.explain` 是平台原文）。
5. **无权限 ≠ 掉线**：不写 `need_relogin`；登录态只由 `common._is_session_expired` 决定。

## 1. 证据来源（静态 bundle 反解，零风控）

官方安全中心微应用：

```
https://g.alicdn.com/idle-pc/idle-seller-security-center/0.0.6/index.html
https://g.alicdn.com/idle-pc/idle-seller-security-center/0.0.6/js/main.js   # 4441635 B
```

本地副本（审计用）：`C:\Users\Administrator\AppData\Local\Temp\secwrite\main.js`。
复现：`python dl.py`（urllib 拉 index.html → 取 `js/main.js` → 存盘），再按下面的字节偏移
`open('main.js','rb').read()[偏移-200:偏移+700]` 复核原文。

bundle 里共 23 个 `mtop.*` 名字；下表每行的「偏移」是调用点原文位置。
**这些常量是从官方前端发出的请求里逐条抄下来的，不是猜的。**

## 2. 前置条件：站点 CODE 头（必须带）

真机事实：这批接口不带请求头 `idle_site_biz_code: COMMONPRO` 时，服务端回
`FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`，或 `data.errorMsg="站点CODE不能为空"`。
权限码 `permissionCode` 是 `security:score:get`。

头从客户端借（`backend/app/xianyu/client.py:146` `SECURITY_SITE_BIZ_CODE = 'COMMONPRO'`，
`client.py:941-949` `_security_headers()`），复用 `backend/app/xianyu/item_templates.py:48-57`
的 `site_headers(client)`；拿不到就传 `None`（不补头、也不假装）。测试里有断言：
`extra_headers['idle_site_biz_code'] == 'COMMONPRO'`。

## 3. 接口清单

| 动作 | mtop api | v | 请求 data | 响应读取 | bundle 偏移 | 真机验证 |
| --- | --- | --- | --- | --- | --- | --- |
| 申诉表单获取（只读） | `mtop.idle.pc.security.appeal.form.get` | 1.0 | `{formId}` | `data.formData` | 3110899 | **未验证** |
| 提交申诉（pc 弹窗） | `mtop.idle.pc.security.appeal.submit` | 1.0 | `{parameter}` | `data.appealId` | 3111437 | **未验证** |
| 催办（加急） | `mtop.idle.pc.security.appeal.urge` | 1.0 | `{punishId:Number}` | `data` 为真 | 3066600 | **未验证** |
| 提交申诉（复核 cro） | `mtop.idle.security.appeal.submit` | 1.0 | `{parameter:JSON字符串}` | `data.appealId` | 2860925 | **未验证** |

**为什么全部是「未验证」**：写接口禁止对真实平台试打（红线 1），只保证「与官方前端发出的
请求完全一致」。表单获取虽是只读，但需要一条真实违规单号才能构造有效 `formId`，
审计时没有 → 同样标未验证。

### 3.1 申诉表单获取

bundle 偏移 3110899 原文（`o` 是调用方传进来的表单 id）：

```js
L7({api:"mtop.idle.pc.security.appeal.form.get", v:"1.0", data:{formId:o}, needLoginPC:!0, showErrorToast:!0})
```

调用点读的是 `n?.data?.formData`。本实现返回 `form_data`（`None` 表示平台没给）。

### 3.2 提交申诉（pc 弹窗）

bundle 偏移 3111437：`s={parameter:e}` → `data:s`；成功判据是 `o?.data?.appealId` 为真
（然后 `message.success('提交成功')`）。

`parameter` 的形状：bundle 只显示 `data:{parameter:e}`，**没显示 `e` 的类型**；同类调用点
（`mtop.idle.security.appeal.submit`，偏移 3052626）用的是 `JSON.stringify(...)`，
所以「JSON 字符串」一定是合法形状之一。本实现两种都收：`dict` → `json.dumps(ensure_ascii=False)`，
`str` → 原样透传，**不替调用方编内容**。

### 3.3 催办

bundle 偏移 3066600：`data:{punishId:Number(s)}`（官方对 `punishId` 做了 `Number()`），
成功判据是 `t?.data` 为真（`message.success('已为您加急处理')`）。
同一处还能看到 `viewConfig.appealConfig.appealUrl`、`canAppealAgain`、`canUrge`，
以及 `appealUrl.includes("cro-security-center")` 时切换到 cro 分支 —— 这就是 3.4 的入口条件。

### 3.4 提交申诉（复核 cro）

bundle 偏移 2860925：`L7({api:"mtop.idle.security.appeal.submit", v:"1.0", data:{parameter:e}})`
—— 注意**没有** `needLoginPC`。偏移 3052626 拼请求：

```js
i = {punishId: Number(c), proofItems: n}
y9(JSON.stringify(i))
```

`proofItems[]` 元素形状（偏移 3050908 / 3051071）：`{code, name, index, details:[{value, desc}]}`，
`code` 取 5 个值（`APPEAL_PROOF_CODES`）：`appealUpload`(上传凭证，`details` 来自
`(e.mediaList||[]).map(m=>({value:JSON.stringify(m),desc:JSON.stringify(m)}))`)、
`appealDesc`(填写申诉描述)、`appealReason`(选择申诉原因)、`questionId`(问题ID)、`questionName`(问题名称)。
读回进度时按 `code` 找 `appealDesc` / `appealUpload`（`progressRecords[].changedProofItems`）。

## 4. 确认闸门

| 入口 | 闸门 |
| --- | --- |
| `appeal_form_request()` / `POST .../appeal/form` | **无**（只读） |
| `appeal_submit_request()` / `POST .../appeal/submit` | 需要 `confirm=True` |
| `appeal_submit_cro_request()` / `POST .../appeal/cro/submit` | 需要 `confirm=True` |
| `appeal_urge_request()` / `POST .../appeal/urge` | 需要 `confirm=True` |

`appeal_form_request` 刻意不带闸门：它是**只读**（获取表单），不改账号状态，
给它加确认只会训练用户无脑点确认。这是判断，不是漏掉 —— 若认为不该例外，改一行即可。
协议层未确认抛 `WriteNotConfirmed`，服务层把它翻成 `confirmation_required`。

## 5. 响应口径

- `parse_*` 返回 `{platform_success, platform_message, platform_body, ...}`；
  `platform_success` 是顶层 `success`（`None` 表示平台没给这个字段）。
- `platform_message` / `platform_body` 用 `or None`：**没有就是 `None`**。
- `appeal_submit_failure(raw)`：官方唯一成功判据是 `data.appealId` 为真。
  顶层 `success=true` 但没有 `appealId` = 平台没受理 → 服务层返回
  `protocol_error`，`message` 是 `平台接口失败: 平台没有返回申诉受理结果（缺少 appealId）`。
- 催办同理：`data` 缺失 → `urged` 为 `None` → `protocol_error`（不补 `False`）。

## 6. 未实现（**故意**）

`CRO_READ_APIS` 记了名字但**不实现**：

```
mtop.taobao.idle.selected.re.verify.cro.appeal.proofs        # 偏移 3077366
mtop.taobao.idle.selected.re.verify.cro.appeal.results       # 偏移 3081199
mtop.taobao.idle.selected.re.verify.cro.can.appeal.answers   # 偏移 2860254
```

原因：bundle 里调用点是 `L7({api:"…", v:"1.0", data:e})` —— `data` **直接透传**，
字段形状没挖到；而且它们读的是凭证/结果列表（只读）。**不实现、不猜字段**。

## 7. HTTP 路由

| 方法 | 路径 | body |
| --- | --- | --- |
| POST | `/api/security-center/{account_id}/appeal/form` | `{form_id}` |
| POST | `/api/security-center/{account_id}/appeal/submit` | `{parameter, confirm}` |
| POST | `/api/security-center/{account_id}/appeal/cro/submit` | `{punish_id, proof_items, confirm}` |
| POST | `/api/security-center/{account_id}/appeal/urge` | `{punish_id, confirm}` |

前缀与只读安全中心一致（`api/security_center.py:7`）；只读那条是 `GET /{account_id}`，
方法不同，不冲突。路由挂载由 Lead 在 `backend/app/main.py` 完成（本模块不改 main.py）。

## 8. 测试

```
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe backend\tests\test_security_write_api.py"
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe backend\tests\run_all.py"
```

用 `FakeSecurityClient` 替换 `backend.app.xianyu.client`（惰性 import 才生效），
断言「未确认时零平台请求」「站点 CODE 头已带」「缺字段是 `null` 不是 0」
「无权限不是 `need_relogin`」。
