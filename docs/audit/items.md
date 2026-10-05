# 审计报告：商品链路（拉取 / 发布 / 编辑 / 上下架 / 批量 / 详情）

- 审计员：`audit-items`（只读审计，**未修改任何源码**）
- 审计时间：2026-10-06
- 被审对象：`C:\Users\Administrator\Projects\xianyu-ai-workbench` @ `2f3425a`
- 证据来源：**运行中的真实后端**（临时数据目录 `C:\Users\Administrator\AppData\Local\Temp\xy_audit_items`，端口 18091）+ 进程内自省 + 仓库自带回归锁
- 边界遵守：未打真实闲鱼平台（下面的失败全部发生在**发请求之前**，是 Python 层 `TypeError` / 方法缺失，没有一次网络出站）、未调 LLM、未 git commit、未改源码

## 0. 直接回答用户原话「商品无法拉取过来发布」

用户说的是对的，而且**不是登录问题**：

| 动作 | 用户点下去的真实结果 | 平台侧 | 判定 |
|---|---|---|---|
| 「同步商品 / 拉取商品」 | 已登录（有合法 Cookie）→ **HTTP 503 `xianyu_not_ready`「协议层尚未实现该方法」**；未登录 → HTTP 503「还没有 Cookie，请先扫码登录」 | 一次请求都没发出 | **不通** |
| 「发布」 | **HTTP 502 `protocol_error`**「`XianyuClient.publish_item() takes 2 positional arguments but 3 were given`」 | 一次请求都没发出 | **不通** |
| 「编辑 / 改价 / 改标题」 | HTTP 200，本地 SQLite 已改，`synced:false` + warning「协议层修改失败，改动仅本地生效」 | 一次请求都没发出 | **假通**（本地成功、平台没动） |
| 「上架 / 下架」 | HTTP 200，本地 `status` 已改，`ok:0 / local_only:1` | 一次请求都没发出 | **假通**（本地成功、平台没动） |
| 「批量修改」 | HTTP 200，`ok:0`，逐条 `warnings[].code=not_pushed` | 一次请求都没发出 | **假通**（口径诚实，但功能未实现） |
| 「批量发布」 | HTTP 200，`ok:0`，`failed[]` 逐条带 TypeError 文本 | 一次请求都没发出 | **不通** |
| 商品列表 / 详情 | HTTP 200，返回**本地 SQLite 缓存**（`synced_at` 为 null） | 不读平台 | **读得到，但是旧快照** |

**核心结论：`common.get_client` 那条历史断链（缺陷 11）确实已经修好了**——有 Cookie 时它返回的是**真的** `backend.app.xianyu.client.XianyuClient`（`unb` 已正确解析），不是假对象，也不再静默只写本地。
但**下一环全断**：服务层调协议层的**方法名和参数个数对不上**。所以「拉取商品」在任何账号上都不可能成功，「发布」在任何账号上都必然 502，编辑/上下架永远只写本地。修复缺陷 11 只治好了「拿不到客户端」，没有治好「拿到了也不会用」。

---

## 1. `GET /api/items/{account_id}`（拉取商品列表）在没有 Cookie 时返回什么

### 结论

1. 任务卡里写的这个字面路径**在代码里不存在**。`GET /api/items/{account_id}` 被通用单条路由 `GET /api/items/{item_id}` 吞掉，返回 **404 `not_found`「商品不存在: acc_no_cookie」**。
2. 真正的「列表」接口是 `GET /api/items?account_id=<id>`，它**永远读本地 SQLite，不碰平台**——没 Cookie 也照样 200 返回缓存（不是空列表、不是 503）。
3. 真正的「拉取」接口 `POST /api/items/{account_id}/sync`：
   - **没有 Cookie** → 503 `xianyu_not_ready`「账号 … 还没有 Cookie，请先在「账号」页完成扫码登录」（诚实）；
   - **有 Cookie** → 503 `xianyu_not_ready`「协议层尚未实现该方法」（**这才是用户「拉取不到」的真凶**）。

### 证据

起后端（原样命令，本报告所有 HTTP 结果均来自这个实例）：

```
$repo='C:\Users\Administrator\Projects\xianyu-ai-workbench'
$appdir='C:\Users\Administrator\AppData\Local\Temp\xy_audit_items'
$cmd = 'cmd /c "set XIANYU_APP_DIR=' + $appdir + '&& set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe -m backend.app.main --port 18091 > ' + $appdir + '\backend.log 2>&1"'
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$cmd; CurrentDirectory=$repo}
# -> ReturnValue=0, ProcessId=49188；backend.log: "Uvicorn running on http://127.0.0.1:18091"
```

探针脚本 `C:\Users\Administrator\AppData\Local\Temp\xy_audit_items\probe_items.py`（账号 `acc_no_cookie` 的 `account_credentials.cookie=''`，账号 `acc_with_cookie` 的 cookie 为 `unb=2200000001; _m_h5_tk=aaaabbbbcccc_1699999999999; cookie2=xyz; _tb_token_=abc`）：

```
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe C:\Users\Administrator\AppData\Local\Temp\xy_audit_items\probe_items.py 18091"
```

关键输出（原样）：

```
--- 1a GET /api/items?account_id=acc_no_cookie
HTTP 200 :: {"success":true,"data":{"items":[{"id":3,"account_id":"acc_no_cookie","item_id":"itemB1","title":"无Cookie账号的商品","price":50.0,...,"synced_at":null}],"total":1,"page":1,"size":50,"pages":1}}
--- 1b GET /api/items/acc_no_cookie  (任务卡字面路径)
HTTP 404 :: {"success":false,"message":"商品不存在: acc_no_cookie","code":"not_found"}
--- 1c POST /api/items/acc_no_cookie/sync
HTTP 503 :: {"success":false,"message":"账号 acc_no_cookie 还没有 Cookie，请先在「账号」页完成扫码登录","code":"xianyu_not_ready"}
--- 1d POST /api/items/sync (扁平路径)
HTTP 503 :: {"success":false,"message":"账号 acc_no_cookie 还没有 Cookie，请先在「账号」页完成扫码登录","code":"xianyu_not_ready"}
--- 2a POST /api/items/acc_with_cookie/sync
HTTP 503 :: {"success":false,"message":"协议层尚未实现该方法","code":"xianyu_not_ready"}
```

进程内自省（证明 `2a` 的 503 不是「没登录」而是「方法不存在」）：

```
cmd /c "set XIANYU_APP_DIR=C:\Users\Administrator\AppData\Local\Temp\xy_audit_items&& set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe C:\Users\Administrator\AppData\Local\Temp\xy_audit_items\introspect.py"

DB: C:\Users\Administrator\AppData\Local\Temp\xy_audit_items\data\app.db
module-level get_client: None
  client has list_items = False
  client has fetch_items = False
  client has get_items = False
  client has my_items = False
  client has downshelf = False
  client has reshelf = False
  client has set_status = False
acc_with_cookie -> backend.app.xianyu.client.XianyuClient | err = None
acc_no_cookie -> None | err = {'success': False, 'message': '账号 acc_no_cookie 还没有 Cookie，请先在「账号」页完成扫码登录', 'code': 'xianyu_not_ready'}
cookie_id(unb) = 2200000001
  call_method sync: status=missing value=None
```

### 根因（`文件:行`）

- `backend/app/api/items.py:161-164` 只定义了 `GET /{account_id}/{item_id}`，**没有** `GET /{account_id}`；`backend/app/api/items.py:185-187` 的 `GET /{item_id}` 兜住了两段路径里的一段。
- `backend/app/services/items.py:464-466`：
  ```python
  status, value = common.call_method(
      client, ('list_items', 'fetch_items', 'get_items', 'my_items'), account_id,
  )
  ```
  而 `backend/app/xianyu/client.py` 里 `XianyuClient` **这四个方法一个都没有**（`hasattr` 全为 False；整个 `backend/app/xianyu/` 目录 grep `list_items|fetch_items|get_items|my_items` 零命中）。
- `backend/app/services/common.py:250-257` 的 `call_method` 找不到方法就返回 `CALL_MISSING`；`backend/app/services/items.py:467-470` 用 `common.call_failed` 把它翻成 `not_ready('协议层尚未实现该方法')` → 503。

### 建议最小修法

1. 在 `backend/app/xianyu/client.py` 给 `XianyuClient` 补一个 `async def list_items(self, account_id=None)`（真实接口族在文件头常量区已有先例，形如 `mtop.taobao.idle.pc.…`，需要新增一个 `ITEM_LIST_API` 常量），返回 `{'items': [...]}` 即可被 `common.extract_list` 直接吃下。在补上之前，`sync_items` 应该返回**明确说「尚未实现」的 503**——现在已经是这个行为，属于诚实失败，不算假成功。
2. 列表接口：在 `backend/app/api/items.py` 里**不要**新增 `GET /{account_id}`（会和 `GET /{item_id}` 冲突，除非把 `GET /{item_id}` 改成更长的字面前缀）。建议只修文档/CONTRACTS，明确「列表 = `GET /api/items?account_id=`」。
3. 若确实要保留 `GET /api/items/{account_id}`，须把 `backend/app/api/items.py:161` 与 `backend/app/api/items.py:185` 的路由合并成 `GET /{account_id}` + `GET /{account_id}/{item_id}` 两条，并删掉 `GET /{item_id}` 或改挂到 `/api/items/detail/{item_id}`。

---

## 2. `common.get_client` 生产路径到底拿到了什么（历史缺陷 11 复核）

### 结论

**缺陷 11 已修好，当前不存在「悄悄退回假 client」的情况。**

- `backend/app/xianyu/client.py` **没有**模块级 `get_client` 工厂（自省：`module-level get_client: None`），所以 `common.get_client` 会走 `cls(...)` 分支，且**先取 Cookie 再用 Cookie 构造**——顺序正确。
- 有 Cookie → 返回真 `XianyuClient`，`cookie_id` 正确解析为 `2200000001`。
- 无 Cookie / Cookie 缺 `unb` → 结构化 `xianyu_not_ready` 信封，不抛异常、不返回假对象（仓库自带回归锁 `backend/tests/test_protocol_wiring.py` 8/8 通过，`exitcode=0`）。

### 证据

```
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe backend\tests\test_protocol_wiring.py"
...
Ran 8 tests in 0.102s
OK
exitcode=0
```

代码原文 `backend/app/services/common.py:195-213`：

```python
module = protocol_module('client')
if module is None:
    return None, not_ready()
factory = getattr(module, 'get_client', None)
if callable(factory):
    try:
        return _bind_im_service(account_id, factory(account_id)), None
    except Exception as exc:
        return None, not_ready(f'协议层客户端创建失败: {exc}')
cls = getattr(module, 'XianyuClient', None)
...
cookie = _cookie_for(account_id)
if not cookie:
    return None, not_ready(f'账号 {account_id} 还没有 Cookie，请先在「账号」页完成扫码登录')
try:
    return _bind_im_service(account_id, cls(cookie)), None
```

### 根因（`文件:行`）

`backend/app/services/common.py:198-211`。对比历史写法（`cls(account_id)`）——当前代码已是正确顺序，**本项无缺陷**。`backend/tests/test_protocol_wiring.py:1-12` 的模块注释记录了这次修复的来龙去脉。

### 建议最小修法

无需修复。**但**：`backend/tests/test_protocol_wiring.py` 只锁了「客户端能建出来」，**没锁任何一条真实商品操作**。建议在该文件补 4 条断言：对真 `XianyuClient` 调用 `common.call_method` 走 `sync/edit/delist/publish` 的候选名字，断言**不得**出现 `CALL_ERROR`（TypeError）——第 3 节的缺陷正是从这个缺口漏出去的。

---

## 3. 发布 / 编辑 / 上下架：拿到了真 client，但调用全错

### 结论

真 client 拿到了，但服务层调用协议层时**方法名与参数个数都不对**：

| 操作 | 服务层实际调用 | 真 client 的签名 | 结果 |
|---|---|---|---|
| 发布 | `publish_item(account_id, payload)` | `publish_item(self, payload)` | `TypeError` → **502 `protocol_error`** |
| 编辑 | `edit_item(account_id, item_id, resolved)` | `edit_item(self, item_id, payload)` | `TypeError` → warning → **local_only** |
| 上架 | `reshelf(account_id, item_id)` | **不存在** | `CALL_MISSING` → **local_only** |
| 下架 | `downshelf(account_id, item_id)` | **不存在** | `CALL_MISSING` → **local_only** |

失败发生在构造参数阶段，**没有任何网络出站**——这也是本次审计能安全跑通的前提。

### 证据

```
--- 2b POST /api/items/acc_with_cookie/publish
HTTP 502 :: {"success":false,"message":"协议层调用失败: XianyuClient.publish_item() takes 2 positional arguments but 3 were given","code":"protocol_error"}
--- 2c POST /api/items/publish (扁平路径)
HTTP 502 :: {"success":false,"message":"协议层调用失败: XianyuClient.publish_item() takes 2 positional arguments but 3 were given","code":"protocol_error"}
--- 3a POST /api/items/acc_with_cookie/itemA1/edit
HTTP 200 :: {"success":true,"data":{"item":{...,"price":123.0,...},"synced":false,"updated_fields":["price"],"warning":"协议层修改失败，改动仅本地生效: XianyuClient.edit_item() takes 3 posi...
     DB after edit: (123.0, 'on_sale', '2026-10-05 20:28:26')      # 本地确实改了
--- 3b POST /api/items/acc_with_cookie/itemA1/delist
HTTP 200 :: {"success":true,"data":{"total":1,"ok":0,"synced":0,"local_only":1,"warnings":[{"id":"itemA1","code":"not_pushed","message":"协议层尚未实现该操作，仅本地生效"}],"failed":[],"skipped":[],"status":"off_shelf"}}
     DB after delist: (123.0, 'off_shelf', '2026-10-05 20:28:26')  # 本地确实改了
--- 3c POST /api/items/acc_with_cookie/itemA1/relist
HTTP 200 :: {"success":true,"data":{"total":1,"ok":0,...,"local_only":1,...,"status":"on_sale"}}
```

进程内 `call_method` 逐个复核：

```
  call_method sync:    status=missing value=None
  call_method edit:    status=error   value=TypeError('XianyuClient.edit_item() takes 3 positional arguments but 4 were given')
  call_method delist:  status=missing value=None
  call_method publish: status=error   value=TypeError('XianyuClient.publish_item() takes 2 positional arguments but 3 were given')
```

### 根因（`文件:行`）

- 发布：`backend/app/services/items.py:521` `common.call_method(client, ('publish_item','create_item','publish'), account_id, payload)` vs `backend/app/xianyu/client.py:237` `async def publish_item(self, payload: Dict[str, Any])`。
- 编辑：`backend/app/services/items.py:260-263` `common.call_method(client, ('edit_item','update_item','modify_item','edit'), account_id, item_id, resolved)` vs `backend/app/xianyu/client.py:225` `async def edit_item(self, item_id: str, payload: Dict[str, Any])`。
- 上下架：`backend/app/services/items.py:428-430` `common.call_method(client, (action, f'{action}_item', 'set_status'), item['account_id'], item['item_id'])`，而 `downshelf`/`reshelf`/`set_status` 在 `backend/app/xianyu/client.py` 中均不存在（`hasattr` 全 False）。

### 建议最小修法

统一成「服务层只传协议层真正需要的参数」：

1. `backend/app/services/items.py:521` 改为 `common.call_method(client, ('publish_item','create_item','publish'), payload)`（去掉 `account_id`）。
2. `backend/app/services/items.py:260-263` 改为 `common.call_method(client, ('edit_item','update_item','modify_item','edit'), item_id, resolved)`（去掉 `account_id`）。
3. `backend/app/xianyu/client.py` 补 `async def downshelf(self, item_id)` / `async def reshelf(self, item_id)`（或让服务层改调已存在的 `edit_item(item_id, {'itemStatus': ...})`）。**注意**：修好 arity 后暴露的下一个风险是**字段名映射**——`resolved` 里的键是本地列名（`price`/`title`/`stock`），而 mtop 编辑接口吃的是平台字段名（如 `soldPrice`/`itemTitle`），这层映射目前不存在。该风险**未验证**（本审计不打真实平台，无法确认平台字段名），修 arity 时必须一并处理，否则会从「TypeError 假通」变成「平台真实报错假通」。

---

## 4. `batch-edit` / `batch-publish` 的结果口径在真机上是否可靠

### 结论

**后端口径是可靠的、诚实的**：协议层没推成功时 `ok`/`synced` 恒为 0，逐条进 `warnings`（`code=not_pushed`）或 `failed`，`skipped` = `failed` 的 id 列表。**没有发现「双向假成功」**（本节范围内）。

但发现**前端把单条 `/edit` 的返回体误当成 BatchResult**，导致「仅本地生效」的警告在 UI 上被吞掉，用户看到的是一句「没有可处理的商品（共 0 个）」。

### 证据（后端口径可靠）

```
--- 5a batch-edit 有Cookie账号 itemA1,itemA2 price_delta +1
HTTP 200 :: {"success":true,"data":{"total":2,"ok":0,"synced":0,"local_only":2,"warnings":[{"id":"itemA1","code":"not_pushed","message":"协议层修改失败，改动仅本地生效: XianyuClient.edit_item() takes 3 positional arguments but 4 were given"},{"id":"itemA2","code":"not_pushed","message":"协议层修改失败，改动仅本地生效: ..."}],"failed":[],"skipped":[],"updated":["itemA1","it...
     DB after batch: (124.0, 'on_sale', '2026-10-05 20:28:26') (201.0, 'on_sale', '2026-10-05 20:28:26')   # 本地改了，ok 依然是 0 -> 口径正确
--- 6a POST /api/items/acc_with_cookie/batch-publish
HTTP 200 :: {"success":true,"data":{"total":2,"ok":0,"failed":[{"id":"np1","message":"协议层调用失败: XianyuClient.publish_item() takes 2 positional arguments but 3 were given"},{"id":"np2","message":"协议层调用失败: ..."}]}}
--- 7a GET /api/items/acc_with_cookie/operation-logs
{"logs":[{"id":16,...,"action":"publish_item","success":false,"detail":"协议层调用失败: ..."},{"id":14,...,"action":"downshelf","success":true,"detail":"{\"status\": \"off_shelf\", \"warning\": \"协议层尚未实现该操作，仅本地生效\"}"},...]}
```

### 证据（前端吞 warning）

`3a` 的真实响应体是：

```json
{"success":true,"data":{"item":{...},"synced":false,"updated_fields":["price"],
 "warning":"协议层修改失败，改动仅本地生效: XianyuClient.edit_item() takes 3 positional arguments but 4 were given"}}
```

它**没有** `ok` / `warnings` / `failed` / `total` 字段。而前端 `frontend/src/pages/Items.tsx:408-414` 按 `BatchResult` 读它：

```tsx
// 单条编辑返回 {ok, warnings, failed}：ok 才算真的改到闲鱼上了；
const result = await api.post<BatchResult>(`/api/items/.../edit`, patch)
showBatchNotice(toast, result, '修改', '商品')
```

`frontend/src/pages/Items.tsx:26-39` 的 `showBatchNotice` 在三个数组全空时落到最后一句：

```tsx
const text = `${label}${verb}：${parts.join('；') || `没有可处理的商品（共 ${Number(result?.total) || 0} 个）`}`
...
else toast.info(text)
```

按此计算，保存成功后用户看到的是 **「商品修改：没有可处理的商品（共 0 个）」**，而真正的原因（协议层没推成功）被丢弃。同一错误也存在于非统一补丁的批量路径 `frontend/src/pages/Items.tsx:605-618`（`merged.warnings.push(...(result?.warnings ?? []))` 恒为空）。

### 根因（`文件:行`）

- 后端：`backend/app/services/items.py:306` `result: Dict[str, Any] = {'item': updated, 'synced': warning is None, 'updated_fields': list(resolved)}` —— 单条 `update_item` 返回的是 `{item, synced, updated_fields, warning?}`，**不是**批量形状。
- 前端：`frontend/src/pages/Items.tsx:410-414`、`frontend/src/pages/Items.tsx:605-611` 按 `BatchResult`（`ok/warnings/failed`）消费。
- `docs/CONTRACTS.md:176` 只规定「**所有 `batch-*` 接口**」用批量形状，单条 `/edit` 的形状没有写进契约 —— 契约缺口导致两边各自假设。

### 建议最小修法

二选一，**最小**的是改前端：

- 前端：把 `frontend/src/pages/Items.tsx:410-414` 改成读 `{synced, warning}`：`synced===true` → `toast.success('已保存并同步')`；否则 → `toast.info('已保存到本地，但未同步到闲鱼：' + (result.warning ?? '未知原因'))`。`frontend/src/pages/Items.tsx:605-611` 同理。
- 或后端：让 `update_item` 也返回 `_batch_summary(...)` 形状（`total:1, ok, local_only, warnings[], failed[]`），并同步 `docs/CONTRACTS.md` 写清单条 `/edit` 的形状。**推荐后端统一**——MCP 的 `items_edit` 直连同一条接口，前端改一次只能修一半。

---

## 5. 商品详情 `GET /api/items/{account_id}/{item_id}`

### 结论

**可用，但只是本地快照，永远不会去平台刷新。** 跨账号查询正确地 404（不串号）。协议层其实有 `XianyuClient.get_item`（`backend/app/xianyu/client.py:216`），但服务层**没有任何调用点**。

### 证据

```
--- 4a GET /api/items/acc_with_cookie/itemA1
HTTP 200 :: {"success":true,"data":{"id":1,"account_id":"acc_with_cookie","item_id":"itemA1","title":"有Cookie账号的商品","price":123.0,...,"synced_at":null}}
--- 4b GET /api/items/acc_other/itemA1 (跨账号详情)
HTTP 404 :: {"success":false,"message":"商品不存在: acc_other/itemA1","code":"not_found"}
--- 4c GET /api/items/itemA1 (通用单条)
HTTP 200 :: {"success":true,"data":{...,"account_id":"acc_with_cookie",...}}
```

`synced_at: null`（该商品从未从平台同步过）依然 200 返回，说明接口不校验新鲜度。

### 根因（`文件:行`）

`backend/app/services/items.py:144-154` `get_item_by_account` 只做 `_find_row_for_account`（纯 SQLite 查询，见 `backend/app/services/items.py:80-93`），无协议层调用；`backend/app/xianyu/client.py:216-223` 的 `get_item` 在整个 `backend/app/services/` 中零调用。

### 建议最小修法

保持「详情=本地缓存」的语义（MCP `items_get` 依赖它的低延迟与不触发风控），但：

1. 在响应里加入 `stale: bool`（`synced_at` 为空或超过 N 分钟）；**注意** 这会改对外结构，需同步 `docs/CONTRACTS.md` 与 MCP schema。
2. 不要在这个接口里默认打平台——`backend/app/xianyu/client.py:216` 会走真实 mtop，详情页会被用户刷成风控目标。若需要刷新，另加 `POST /api/items/{account_id}/{item_id}/refresh`。

---

## 6. 批量修改的账号隔离（跨账号必须 `failed` `code=account_mismatch`）

### 结论

**正确，且真机验证通过**：属别人的商品进 `failed`，`code=account_mismatch`，`skipped` 同步带上 id，**数据库里的商品一个字节都没动**，整批 HTTP 仍是 200（不整批 400）。单条上下架路径也同样正确。

### 证据

```
--- 5b batch-edit 跨账号：路径=acc_other 商品=itemA1
HTTP 200 :: {"success":true,"data":{"total":1,"ok":0,"synced":0,"local_only":0,"warnings":[],"failed":[{"id":"itemA1","message":"商品不属于账号 acc_other，拒绝跨账号批改","code":"account_mismatch"}],"skipped":["itemA1"],"updated":[],"fields":{"price_set":7}}}
     DB itemA1 (必须没被改): (124.0, 'on_sale', '2026-10-05 20:28:26')     # 与 5a 后一致，未被改动
--- 5c batch-edit 不存在商品
HTTP 200 :: {"success":true,"data":{"total":1,...,"failed":[{"id":"no_such_item","message":"商品不存在","code":"not_found"}],"skipped":["no_such_item"],...}}
```

### 根因（`文件:行`）

`backend/app/services/items.py:364-374`（`batch_update` 逐条 `_find_row_for_account`，未命中时再 `_find_row(item_id)` 区分 `not_found` 与 `account_mismatch`）；配对查询实现在 `backend/app/services/items.py:80-93`；上下架同款逻辑在 `backend/app/services/items.py:411-419`。

### 建议最小修法

无需修复。当前实现与 `docs/CONTRACTS.md:176-186` 的口径一致，建议补一条回归用例把 `5b` 这个场景锁死（现有 `backend/tests/test_items_batch_warning.py` 未覆盖跨账号）。

---

## 7. 为什么 297 个用例全绿却漏掉了第 3 节

### 结论

仓库的测试**自注入了一个假协议客户端，其方法签名恰好和服务层的错误调用方式一致**，于是把「服务层参数传错」这条真实缺陷整体绕过了。这正是缺陷 11 当年能存活的原因，也是它被修复后**第二层缺陷立刻顶上来**的原因。

### 证据

`backend/tests/test_services_items.py:28-55` 的 `FakeClient`：

```python
class FakeClient:
    def __init__(self, account_id: str): ...
    def list_items(self, account_id): ...                                    # 真类没有这个方法
    def edit_item(self, account_id, item_id, fields): ...                    # 真类签名是 edit_item(item_id, payload)
    def publish_item(self, account_id, payload): ...                         # 真类签名是 publish_item(payload)
    def downshelf(self, account_id, item_id): ...                            # 真类没有这个方法
    def reshelf(self, account_id, item_id): ...                              # 真类没有这个方法
```

`backend/tests/test_services_items.py:58-63`：`module.get_client = lambda account_id: client`，直接替换掉 `backend/app/xianyu/client.py` 模块。同样手法见 `backend/tests/test_items_batch_warning.py:50,65`、`backend/tests/test_services_chats.py:43`、`backend/tests/test_im_loop.py:134`、`backend/tests/test_services_orders.py:55`。

真机（不注入任何东西）：

```
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe -m unittest backend.tests.test_services_items backend.tests.test_items_batch_warning"
exitcode=0
Ran 30 tests in 0.455s
OK
```

——**30 个全绿**，而同一份代码在真后端上发布 502、编辑/上下架只是写本地。

### 根因（`文件:行`）

`backend/tests/test_services_items.py:28-63`（FakeClient 签名顺从了实现而非真类）、`backend/tests/test_items_batch_warning.py:50`。
`backend/tests/test_protocol_wiring.py:3-10` 的注释已经点明了这个模式，但该文件只覆盖了 `common.get_client` 本身。

### 建议最小修法

1. 把 `FakeClient` 的签名改成**真类的签名**（`edit_item(self, item_id, payload)`、`publish_item(self, payload)`、去掉 `list_items/downshelf/reshelf` 或按真类补齐），让测试重新变红——这才能对齐。
2. 更好的做法：不要在服务层测试里自造 client，而是用 `unittest.mock.patch.object(XianyuClient, 'mtop', ...)` 只拦网络层，保留真实方法名与签名。
3. 抽一条**签名一致性守卫**（无需网络）：对 `XianyuClient` 上每个被 `common.call_method` 候选名引用到的方法，用 `inspect.signature` 断言服务层调用点传的位置参数个数与之一致。这是唯一能在离线 CI 里挡住「又一处 TypeError 假通」的办法。

---

## 8. 未验证项

- **平台真实接口名与字段名**：本次不打真实闲鱼平台，`list_items` 到底该调哪个 mtop 接口、编辑 payload 需要哪些平台字段（`soldPrice` 等），**未验证**。
- **有真实 Cookie 时平台侧行为**：上面的「有 Cookie」用的是形状合法但伪造的 Cookie（`unb=2200000001`）。它足以走到「构造真实 XianyuClient 并调用方法」这一步（`cookie_id` 解析成功可证），但真实 Cookie 会走完 mtop 签名并被平台校验，**未验证**。不过这不影响本报告结论：所有失败都发生在**发请求之前**。
- **前端实际渲染文本**：`Items.tsx` 的「商品修改：没有可处理的商品（共 0 个）」是按 `showBatchNotice` 源码逐行推演的，未在浏览器里点一次（`scripts/ui_smoke.py` 覆盖的是页面能否打开，未覆盖编辑弹窗的 toast 文案）。**未验证**。
- **MCP `items_edit` 的真实表现**：`mcp_server/client.py:138-147` 直连同一条 `/edit`，推测会拿到同样的 `{item, synced, warning}` 形状；未起 MCP 实测。**未验证**。

## 9. 复现清单

```powershell
# 1) 起后端（临时数据目录，绝不碰真实 %APPDATA%）
$repo='C:\Users\Administrator\Projects\xianyu-ai-workbench'
$appdir='C:\Users\Administrator\AppData\Local\Temp\xy_audit_items'
New-Item -ItemType Directory -Force -Path $appdir | Out-Null
$cmd = 'cmd /c "set XIANYU_APP_DIR=' + $appdir + '&& set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe -m backend.app.main --port 18091 > ' + $appdir + '\backend.log 2>&1"'
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$cmd; CurrentDirectory=$repo}

# 2) 打全套探针（第 1~7 节的 HTTP 证据都出自这条命令）
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe C:\Users\Administrator\AppData\Local\Temp\xy_audit_items\probe_items.py 18091"

# 3) 进程内自省（证明 get_client 拿的是真 client、方法确实不存在）
cmd /c "set XIANYU_APP_DIR=C:\Users\Administrator\AppData\Local\Temp\xy_audit_items&& set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe C:\Users\Administrator\AppData\Local\Temp\xy_audit_items\introspect.py"

# 4) 仓库自带回归锁
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe backend\tests\test_protocol_wiring.py"

# 5) 收尾：结束审计后端（进程号见第 1 步输出）
Get-CimInstance Win32_Process -Filter "ProcessId = 52400" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

## 10. 缺陷清单（按严重度）

| # | 严重度 | 缺陷 | 位置 | 用户可见后果 |
|---|---|---|---|---|
| A | 阻断 | 协议层没有 `list_items` 族方法，同步永远 `CALL_MISSING` | `backend/app/services/items.py:464-466` + `backend/app/xianyu/client.py` | 「同步商品」任何账号都 503，「拉取不到」 |
| B | 阻断 | 发布调用多传一个 `account_id`，真类只收 `payload` | `backend/app/services/items.py:521` vs `backend/app/xianyu/client.py:237` | 「发布」必然 502 |
| C | 高（假通） | 编辑多传一个 `account_id`；上下架方法根本不存在 | `backend/app/services/items.py:260-263`、`428-430` vs `backend/app/xianyu/client.py:225` | 编辑/上下架只写本地却报「已下架/已保存」 |
| D | 中 | 前端把单条 `/edit` 的返回体当 BatchResult，吞掉 warning | `frontend/src/pages/Items.tsx:410-414`、`605-611` vs `backend/app/services/items.py:306` | 保存后提示「没有可处理的商品（共 0 个）」 |
| E | 中 | 测试自注入假 client，签名顺从实现，掩盖 A/B/C | `backend/tests/test_services_items.py:28-63` | 测试全绿 → 缺陷长期存活 |
| F | 低 | 商品详情只读本地，无新鲜度标记，协议层 `get_item` 零调用 | `backend/app/services/items.py:144-154` | 详情是旧快照，用户以为是最新的 |
| G | 低 | `GET /api/items/{account_id}` 不存在，被 `GET /{item_id}` 吞 | `backend/app/api/items.py:161-164,185-187` | 按文档调用得到 404「商品不存在: <账号id>」 |

**已复核无缺陷**：`common.get_client` 的 Cookie 构造顺序（`backend/app/services/common.py:195-213`）、批量结果口径 `ok/local_only/failed/skipped`（`backend/app/services/items.py:312-336`）、跨账号隔离 `account_mismatch`（`backend/app/services/items.py:364-374`）、操作日志落库（`backend/app/services/common.py:81-96`）。
