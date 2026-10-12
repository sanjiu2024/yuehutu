# 草稿箱（官方 `seller-item` 的「草稿箱」tab）

> 证据来源：本地副本 `C:\Users\Administrator\.dsh-tmp\adapp\item-main.js`（官方 `seller-item/0.0.20` 的
> 打包产物，4,402,065 字符**单行**压缩 JS，所以只能给**字符偏移**，行号没有意义）。
> **一次真机请求都没发过** —— 本机账号缺平台业务权限，且删草稿不可撤销，不拿线上草稿试。
> 所以下面每一条都标注了「证据来自哪里」与「置信度」。

## §1 已实现（读 + 写）

| 能力 | HTTP | 平台接口 | 说明 |
| --- | --- | --- | --- |
| 草稿列表 | `GET /api/drafts/{account_id}?page=&size=` | `mtop.alibaba.idle.item.pc.draft.search` v1.0 | 分页只允许 10 / 20 / 50 |
| 草稿详情 | `GET /api/drafts/{account_id}/{draft_id}` | `mtop.alibaba.idle.item.pc.draft.get` v1.0 | `fields` 是平台原样 payload，键名不翻译 |
| 删除单条草稿 | `POST /api/drafts/{account_id}/{draft_id}/delete` | `mtop.alibaba.idle.item.pc.draft.delete` v1.0 | body `{confirm}`，**不可撤销** |
| 批量删除草稿 | `POST /api/drafts/{account_id}/batch-delete` | `mtop.alibaba.idle.idle.pc.draft.batch.delete` v1.0 | body `{draft_ids, confirm}`，`draft_ids` 收**数组或逗号串** |

工具链：`backend/app/xianyu/draft.py`（协议层，纯函数）→ `backend/app/services/drafts.py` →
`backend/app/api/drafts.py`（前缀 `/api/drafts`，在 `backend/app/main.py:35` 的 `ROUTE_MODULES` 里注册为 `drafts`）。

### 1.1 为什么单开 `/api/drafts` 而不是并进 `/api/items`

`/api/items/{account_id}/{item_id}` 是**两段路径**，`drafts` 会被当成 `item_id` 吃掉。这与
`/api/item-templates` 当初单开是同一个原因（见 `docs/contracts/item_templates.md`）。

### 1.2 参数形状（逐字）

```python
# 列表：bundle @4079050 原文 s = {pageNo: l || 1, pageSize: c || 20}
{'pageNo': 1, 'pageSize': 20}

# 详情 / 单删：@3063462 与 @1806143 原文 data:{draftId: e, sourceId: PC.getState().sourceId}
{'draftId': 'd1'}          # sourceId 由客户端统一补，不由调用方传

# 批删：@4098620 原文 q0({draftIds: T.join(",")})
{'draftIds': 'd1,d2'}      # 逗号串，不是数组
```

- `pageNo` 从 1 起：`0` / 负数 / 非数字一律纠正成 1（不把参数错误推给平台）。
- `pageSize` **只接受 10 / 20 / 50**（官方草稿 tab 的分页器就这三档，商品 tab 只有 10 / 20）：
  别的值在**本地**就报 `invalid_fields`，一个平台请求都不发。
- `draftId` / `draftIds` 为空 → 协议层抛 `ValueError`（服务层的 `_missing` 是第一道，协议层是最后一站）。

### 1.3 成功判据

官方对这四个接口的响应处理**极简**：`draft.search` 读 `e.data.data.itemSearchResponseList` 与
`e.data.data.total`；`draft.delete` / `batch.delete` **根本没读响应体**；`batch.delete` 只读
`e.data.sucCount` / `e.data.failCount`（原文 `"".concat(e?.data?.sucCount,"个草稿删除成功")`）。

所以我们**不能**用「业务体为真」当判据 —— 空草稿箱返回 `[]`，那是**正常结果**。判据是：

```
客户端没报错（raw['success'] is not False）
且 平台业务码 code == 'success'
（信封里没有 code 时，要求它带了 data 键）
```

### 1.4 诚实口径（不造数据的地方）

| 情形 | 我们的行为 |
| --- | --- |
| 草稿箱一件都没有 | `items: []`、`total: 0`、**无 warning** —— 正常结果，不是错误 |
| 平台回了成功但**没有**列表字段 | `items: []` + `total: null` + warning `list_key_missing`（明写「这不等于 0 条」） |
| 列表项缺标题 / 图 / 价格 / 库存 | 对应字段 `null`（不造空串、不造 `0`） |
| 详情没有内容 | `fields: null` + warning `platform_returned_nothing`（不是 `{}`） |
| `failCount > 0` 但平台不说是哪条 | `failed: []` + warning `no_per_item_report`（绝不编造明细） |
| 平台连 `sucCount`/`failCount` 都没给 | `ok: null` + warning `no_counts`（不是 0，也不是「全部成功」） |
| 平台拒绝（无权限 / 业务错） | 502 `protocol_error` + `platform_errors{api, code, message}`，**不碰登录态** |

`failed[]` 的元素形状目前**恒为空数组**：官方不逐条回报，我们没有证据编。

### 1.5 三步顺序：确认 → 字段校验 → 建客户端

写路径（`services/drafts.py` 的 `delete_draft` / `batch_delete_drafts`）严格按这个顺序：

1. **确认**：`confirm is True`（`_truthy` 只认 `True` / `'true'` / `1`）否则 400 `confirmation_required`，
   **一个平台请求都不发**；
2. **字段校验**：`draft_ids` 非空、去重后 ≤ `BATCH_WRITE_LIMIT=200`、页面大小合法 —— 纯本地，免费；
3. **建客户端**：这时候才可能因为 Cookie / 账号不存在回 503 `xianyu_not_ready`，或登录态过期回 409。

理由：把凭据检查放前面，用户会先被叫去「重新扫码」，扫完回来才发现草稿 ID 是空的。

## §2 故意不实现

| 接口 | 为什么不做 |
| --- | --- |
| `mtop.alibaba.idle.item.pc.draft.update` @1805953 | payload 是**整份发布表单**的 `{inputJson: JSON.stringify({...e, draftId, sourceId, uniqueCode})}`；表单字段形状没有证据，猜一个 `inputJson` 去打平台等于拿用户的草稿做实验。已登记在 `draft.NOT_IMPLEMENTED`，测试断言它**既不在** `READ_APIS` 也**不在** `WRITE_API_SET`。 |
| `mtop.idle.idleitem.draft.{publish,edit}` @4142350/@4142467 | 与「发布商品」族重叠（带 `uniqueCode`），属于发布流程，另案处理。 |

## §3 测试

`backend/tests/test_drafts_api.py` —— **46 条**，三个类：

- `DraftProtocolTest`（约 16 条）：API 名逐字、写白名单只放两条、`draft.update` 登记但不可调用、
  `draftIds` 两种入参、分页只有三档、`confirm` 五种假值全抛、空 ID 抛、`is_success` 把空草稿箱当成功、
  `parse_list` 不造值 / `parse_detail` 原样 / `parse_process` 的 `0` 与 `None` 之分。
- `DraftApiTest`（约 27 条）：只读 happy path（含**只发一次**请求、`v='1.0'`）、空草稿箱、列表键缺失、
  非法 `size` 零平台调用、平台拒绝 502 且 `login_state` 仍 `active`、详情两种 warning、读路径不碰写接口、
  无 Cookie 503 零调用、两个写端点的确认闸门（含 8 种假值）、**字段校验先于凭据检查**、超限、
  单删 / 批删 happy path（数组与逗号串）、`failed[]` 恒空、`no_counts`、平台拒绝、`SessionExpiredError`
  409 + 回写 `need_relogin`、未登录 401。
- `DraftRouteTest`（3 条）：四条路由的方法、`batch-delete` **声明在** `{draft_id}/delete` 之前、
  `main.ROUTE_MODULES` 含 `drafts`。

跑法（仓库根）：

```
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe backend\tests\test_drafts_api.py"
→ Ran 46 tests in 0.683s OK
```

全量回归：`backend\tests\run_all.py` → `TOTAL 50  PASS 50  FAIL 0  CASES 1587  SKIPPED 2`。

## §4 AI 侧

| AI 工具 | 平台 | 需要 confirm |
| --- | --- | --- |
| `drafts_list` | `GET /api/drafts/{account_id}` | 否 |
| `drafts_get` | `GET /api/drafts/{account_id}/{draft_id}` | 否 |
| `drafts_delete` | `POST /api/drafts/{account_id}/{draft_id}/delete` | **是** |
| `drafts_batch_delete` | `POST /api/drafts/{account_id}/batch-delete` | **是** |

两个写工具的 description 里都写了「会真的删除闲鱼上的草稿，不可撤销」。MCP 侧同样：
`mcp_server/client.py` 的 `_dispatch_route` 在**最前面**判 `confirm`，`"confirm": "false"` 之类不算点头。
`scripts/reconcile_tools.py` 的 `WRITE_PREFIX` 里加的是 `drafts_delete` / `drafts_batch_delete`
（**故意不加 `drafts_` 前缀** —— 否则只读的 `drafts_list` / `drafts_get` 会被误判成写工具）。

## §5 未验证（诚实边界）

1. **真机一次都没打过**：上面所有形状都来自 bundle 反推 + 假协议层测试，平台是否接受
   `{'draftId': …}`（不带 `sourceId`）**未验证**。真机第一次调用请做好「平台报参数错」的准备，
   报错会原样透出。
2. 草稿**详情**的 `fields` 键名未钉住（`lC` 解析器只证明「平台 payload 直接进表单」），所以前端把它
   摊平成「字段 / 值」两列展示而不是硬编码列。
3. 列表项的 `imageInfoDOList[0].url` / `gmtCreateStr` 是**从 bundle 的读法**反推的，没有响应样本。
