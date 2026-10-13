# 话术（官方 IM 工作台的「快捷回复」）契约（读 + 写）

来源：官方 IM 工作台 bundle `js_008_vendors.js`（本地副本 5,857,322 字符单行）静态提取，
**零平台请求**。这是官方「快捷回复」功能的完整读 + 写复刻：分组与短语的增删改 + 列表。

> 与「话术自己填」（总目标第 ③ 条）的关系：那条讲的是**给 AI 用的助词/话术**（本地提示词层，
> 见 `docs/CONTRACTS.md` 的提示词三级拼接）。本页复刻的是**平台侧的快捷回复配置**，
> 两者同名不同物，别混。

## 1. 四个平台接口

| 动作 | mtop | v | 入参（逐字） | 响应 |
|---|---|---|---|---|
| 列表 | `mtop.taobao.idlemessage.quickreply.list.get.v1` | 1.0 | **不带参数** | `resp.data.groups` |
| 分组增删改 | `mtop.taobao.idlemessage.quickreply.group.operate` | 1.0 | create `{groupName,operateType:'create'}`；update `{groupId,groupName,operateType:'update'}`；delete `{groupId,operateType:'delete'}` | `resp.data.groupInfo` |
| 短语增删改 | `mtop.taobao.idlemessage.quickreply.content.operate` | 1.0 | create `{groupId,content,operateType:'create'}`；update `{groupId,contentId,content,operateType:'update'}`；delete `{groupId,contentId,operateType:'delete'}` | `resp.data.contentInfo` |

后两个接口的客户端包装带 `needLoginPC:true`。成功判据是**平台回显 id**：
`groupInfo.groupId` / `contentInfo.contentId` 存在才算成功；失败文案取 `data.error`。

## 1.1 HTTP 面（7 条，单开 `/api/quick-reply`）

```text
GET  /api/quick-reply/{account_id}/groups                      -- 读
POST /api/quick-reply/{account_id}/groups                      {group_name,confirm}
POST /api/quick-reply/{account_id}/groups/{group_id}           {group_name,confirm}
POST /api/quick-reply/{account_id}/groups/{group_id}/delete    {confirm}
POST /api/quick-reply/{account_id}/phrases                     {group_id,content,confirm}
POST /api/quick-reply/{account_id}/phrases/{content_id}        {group_id,content,confirm}
POST /api/quick-reply/{account_id}/phrases/{content_id}/delete {group_id,confirm}
```

**为什么单开前缀而不并进 `/api/chats`**：`/api/chats` 承载的是本地会话与消息
（`conversations` / `messages` 两张表），而话术是**平台侧配置**，跟本地数据没有关系；
路径分开，AI 工具与前端一眼能对上。

## 1.2 参数与上限

- 官方上限照抄：分组名 ≤10 字（输入框 `maxLength:10`）、短语内容 ≤200 字
  （`maxLength:200` 且 onChange 里再 `slice(0,200)`）。本地超限 → 400 `invalid_fields`，**零平台调用**。
- `group_id` / `content_id` 收字符串或数字串（内部规范化成 int 后交给平台），空值报错。
- 列表**不分页**（官方调用点 `b$({})` 就是不带参数，响应一次性给全部分组与短语）。

## 1.3 成功判据（与「不造数据」的对照）

| 平台情况 | 我们回什么 | 前端显示 |
|---|---|---|
| 正常返回 `groups` | `groups: [...]`、`count` | 分组 + 短语列表 |
| 没有 `groups` 键 | `groups: null` + warning `list_key_missing` | 「平台没给列表字段」≠「一条话术都没有」 |
| `groups: []` | `groups: []`、`count: 0` | 「还没有分组」 |
| 写入成功且回显 id | `saved: true`、`submitted`（实际发出的 payload） | 成功提示 |
| 写入平台收了但**没回显 id** | `saved: false` + warning `no_group_id` / `no_content_id` | 「提交已经发出，平台没确认，请点刷新看平台当前值」 |
| 删除动作 | 另给 `deleted`（= `saved`） | 删除成功/未确认 |
| 平台拒绝（无权限等） | 502 `protocol_error` + `platform_errors{api,code,message}`，**不碰 `login_state`** | 人话提示（「不是登录掉线」） |

`total` 只在平台给了数值时用它，否则用**数出来的条数**并置 `total_from_platform: false`（不编数字）。

## 1.4 三步顺序

**确认 → 字段校验（协议层构造 payload）→ 建客户端**。确认在最前：否则用户会以为「填对了就能发」；
字段校验在建客户端之前，因为它是纯本地、免费的。未确认时**一次平台请求都不发**
（`test_quick_reply_api.py` 的 `ConfirmGateTest` 用「假客户端的调用记录为空」钉住）。

`confirm` 只认字面 `True`（`_truthy`：`True`/`'true'`/`1`/`'1'`）——路由层**不许** `bool(body.get('confirm'))`，
因为 `bool('false')` 是 `True`，会把用户明确的拒绝当成同意。

## 2. 测试

`backend/tests/test_quick_reply_api.py`（58 条）：协议层 26 条纯函数（接口名逐字、白名单、
`confirm` 身份判定、6 个请求形状逐字、两个上限、id 规范化、解析三态、`is_success` 五态）+
`QuickReplyPageTest` 基类下的读 / 闸门 / 校验 / happy path / 失败路径 / 路由六组。

## 3. AI 侧

7 个工具：`quick_reply_list`（只读、无 `confirm`）+ `quick_reply_group_create` /
`quick_reply_group_update` / `quick_reply_group_delete` / `quick_reply_phrase_create` /
`quick_reply_phrase_update` / `quick_reply_phrase_delete`（六条写、`required` 含 `confirm`、
description 带中文警示）。MCP 路由 7 条（1 条 GET + 6 条 POST）。

## 4. 未验证（诚实边界）

- 7 个写接口**一次真机请求都没发过**（只验到：路由注册、body 被端点接受、`confirm` 闸门拦得住、
  界面上不点最终确认就零写请求）。
- 列表接口的真机返回值没见过（账号 `2217970177348` 当时的登录态是 `need_relogin`）。
- 「平台收了但不回显 id」这条分支（`no_group_id` / `no_content_id`）没有真实响应触发过。
- 分组/短语在真机上的表格渲染、批量选择、删除确认框没有人工鼠标点过。
