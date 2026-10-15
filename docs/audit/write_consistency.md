# 写接口六面一致性独立审计（共享任务 task-7）

- 审计员：`write-audit`（独立审计员，**只审不动手**）
- 审计时间：2026-10-15 12:42–12:58（Asia/Shanghai）
- 被审对象：`C:\Users\Administrator\Projects\xianyu-ai-workbench` @ `ebdd568`（**工作区非干净**）
- 边界遵守：未修改任何源码、未跑任何写操作、未打真实闲鱼平台、未 git commit
- 证据来源：源码逐层静态核对（协议层/服务层/路由/AI 工具/MCP/测试）+ 进程内自省（`app.openapi()` / `app.agents.tools.TOOL_SPECS` / `mcp_server.client.TOOL_ROUTES`）+ 实跑 `scripts/reconcile_tools.py` + 单独跑纯 AST 的 `backend/tests/test_write_routes_have_gates.py`（4 项 OK，0.396s，零业务代码执行）
- 审计起点工作区已有 5 个未提交改动：`backend/app/services/orders.py`、`backend/app/xianyu/client.py`、`backend/tests/test_services_orders.py`、`backend/tests/test_xianyu_orders.py`、`scripts/build_exe.ps1` ⇒ **本报告结论针对工作区当前内容，不等于 `ebdd568` 提交的结论**（订单族尤其）。

---

## 0. 直接结论（先看这段）

1. **六面在「已接入的 72 条写能力」上是一致的**：写工具全部有 MCP 路由、全部声明 `confirm`、全部在 `_HANDLERS`/`_SERVICE_TARGETS` 有落点、72/72 在测试源码里至少被提及一次，无孤儿。
2. **但「六面」只覆盖 123 条写路由中的 81 条**。42 条写路由**没有 AI/MCP 面** —— 对它们来说只有四面（协议/服务/路由/测试）。其中约 9 条是**真会打平台**的商品/订单写路径（见问题 9）。
3. **§6 能力清单有 4 处与代码相反**：财务「充值/提现」、营销「活动配置」、风控「熔断参数」、通知「已读」都被标为已接 AI+MCP，实际分别是「只读」「不存在」「无工具」「无写路由」（问题 1–4）。
4. **confirm 闸门本身是可靠的**：HTTP 侧有 AST 回归锁（`test_write_routes_have_gates.py`，含白名单过期检查），MCP 侧在参数拼装前拦（`mcp_server/client.py:1018-1026`）。风险不在漏拦，而在**判据被判了 13+ 份**（问题 7）与**两族更严口径让 §5.2 不成立**（问题 6）。
5. **`confirm_required` 的实际 HTTP 状态码是 400，不是文档写的 409**（问题 5）。
6. **写操作没有幂等键**：重放 = 二次提交（问题 8）。
7. 鉴权：123 条写路由里 120 条带 `Depends(current_user)`，例外 3 条是 `/api/auth/{login,desktop,logout}` 自身（合理，不算缺口）。
8. 分页：65 条 GET 无分页参数，绝大多数是单资源/字典/统计端点（不需要分页）；**真正的集合型缺口只有 4 条**（问题 10）。

---

## 1. reconcile 五方数字（实跑，exit 0）

命令：`cd <repo> && PYTHONPATH=. <python> scripts/reconcile_tools.py`

```
specs=148 master=147 routes=149 targets=136 handlers=148
specs-not-in-routes=[]
routes-not-in-specs(allow=['agents_messages', 'master_chat'])=[]
write_tools=72
route_checked=149
ALIASES=22
RECON OK：五方一致，且每条路由的 path/query/body 都被真实端点接受。
```

五方口径：**TOOL_SPECS 148 / MASTER_TOOLS 147 / HTTP 路由 149 / MCP targets 136 / handlers 148**；`write_tools=72`（带 `confirm` 且落在 `_WRITE_HINT` 覆盖内的写工具），`route_checked=149`，别名 22 条。差异全部有解释，无孤儿、无未登记路由。

> 注：该脚本只验「声明了 confirm 的是不是写工具 / 路由清单是否对得上」，**不验闸门是否存在、不验错误语义** —— 这部分由 `test_write_routes_have_gates.py`（正面清单）与本报告补齐。

---

## 2. 抽检表（18 条：交易 6 / 财务 3 / 营销 3 / 其它 6）

图例：✓ 该面存在且已核；— 该面不存在；⚠ 存在但口径/覆盖有问题；列内给 `文件:行`。

| # | 能力 | 协议层 | 服务层（闸门） | 路由 | AI 工具 | MCP | 测试 | 结论 / 缺口 |
|---|---|---|---|---|---|---|---|---|
| T1 | 交易-改价 | ✓ `backend/app/xianyu/trade_write.py:333 adjust_price_request` | ✓ `backend/app/services/trade_write.py:169`（首行 `if not _truthy(confirm)`） | ✓ `backend/app/api/trade_write.py` POST `/{account_id}/orders/{order_id}/adjust-price` | ✓ `trade_write_adjust_price`（confirm required） | ✓ body 含 confirm | ✓ `test_trade_write_api.py` | 六面齐，闸门在三道顺序第一道 |
| T2 | 交易-同意退款 | ✓ `xianyu/trade_write.py:344 refund_agree_request` | ✓ `services/trade_write.py:255 refund_agree` | ✓ `api/trade_write.py` | ✓ `trade_write_refund_agree` | ✓ | ✓ | 六面齐 |
| T3 | 交易-关单 | ✓ `xianyu/trade_write.py` 族 | ✓ `services/trade_write.py:732 close_order` | ✓ `api/trade_write.py` | ✓ `trade_close_order` | ✓ | ✓ | 六面齐 |
| T4 | 交易-真发货 | ✓ | ✓ `backend/app/services/orders.py:29/32/46`（闸门在 29-46 区段） | ✓ PUT `/api/orders/{order_id}/shipment` | ✓ `orders_deliver` | ✓ | ✓ `test_services_orders.py` | ⚠ 旧路径 `/api/orders/{order_id}/deliver` 仍在且不在 MCP 面；且 `services/orders.py` 工作区有未提交改动，结论以工作区为准 |
| T5 | 交易-加备注 | ✓ `xianyu/trade_write.py:535 add_memo_request` | ✓ `services/trade_write.py:830 add_memo` | ✓ `api/trade_write.py:490-506`（`confirm=body.get('confirm')` 原样透传） | ✓ `trade_add_memo` | ✓ | ✓ | ✓ 且**不谎报成功**：平台无回执时只加 `warnings[].code=trade_memo_no_echo` + 中文提示「无法确认备注是否已生效」 |
| T6 | 交易-线下发货 | ✓ `xianyu/trade_write.py:308 ship_offline_request` | ✓ `services/trade_write.py:134 ship_offline` | ✓ `api/trade_write.py:34-47` | ✓ `trade_write_ship_offline` | ✓ | ✓ | 六面齐；路由层确认「原样透传」，符合 §5.2 |
| F1 | 财务-收入/支出/发票/采购者 | — 只读 | — | ✓ 但全是 GET：`backend/app/api/finance.py:41 / 57 / 73 / 86` | — | — | — | ⚠ **只有读面**，无任何写能力；AST 扫描 `finance.py` 的 post/put/patch/delete = 0 条 |
| F2 | 财务-充值 / 提现（§6 记为写能力） | — | — | ✓ 但是**只读查询**：`backend/app/api/promotion.py:43 GET /{account_id}/recharge/{order_id}`、`:47 GET /{account_id}/withdraw/{order_id}` | — | — | — | ✗ **§6 表记错**：全仓无充值/提现写路由、无对应工具，六面里写面**五面皆无** |
| F3 | 财务-族写路由总数 | — | — | 0 条 | — | — | — | ✗ 财务分类整体无写能力可审（§6 的 ✓ 不成立） |
| M1 | 营销-商品/任务/竞争力/记录 | — 只读 | — | ✓ 但全是 GET：`backend/app/api/marketing.py:44 / 54 / 66 / 78` | — | — | — | ⚠ 只有读面 |
| M2 | 营销-活动配置（§6 记为写能力） | — | — | ✗ `marketing.py` 零写路由 | — | — | — | ✗ **§6 表记错**；唯一沾「营销/推广写」的是 `promotion_write_execute`（72 条名单内，走 `/api/promotion` 族，与 `marketing.py` 不是同一模块） |
| M3 | 营销-记录查询 | — 只读 | — | ✓ GET `marketing.py:78 /{account_id}/records` 无分页参数 | — | — | — | ⚠ 集合型只读接口无分页（见问题 10） |
| O1 | 风控-熔断参数 / 清冷却 | — | ✓ 但**纯本地**，白名单登记理由「改本机风控参数/清本机风控冷却」 | ✓ `backend/app/api/risk.py:49 PUT /settings`、`:55 POST /clear`、`:65 POST /{account_id}/clear` | — | — | — | ✗ **§6 记 ✓ 不成立**：无 risk 写工具/MCP 路由（`risk_clear` 工具指向 `/{account_id}/clear` 但**不带 confirm 参数**，见问题 11）；业务上纯本地，风险低 |
| O2 | 通知-已读（§6 记为写能力） | — | — | ✗ `backend/app/api/notify.py:27` 只有 1 条 `GET /{account_id}` | ⚠ `im_clear_unread` 存在但指会话/系统通知，**不是通知中心** | ⚠ 同上 | ⚠ | ✗ **§6 记 ✓ 不成立**：缺 mark-read 写路由；AI 侧那条是另一条路径，不能当同一能力 |
| O3 | 商品-发布 / 批量编辑 | ✓ `xianyu/client.py:704 publish_item`、`xianyu/item_write.py` | ✓ `services/items.py:1407 publish_item`、`:773 batch_update`（均含 `_confirm_gate`） | ✓ POST `/{account_id}/publish`、`/batch-edit` | ✓ `items_publish` / `items_batch_edit` | ✓ | ✓ | 正式路径六面齐；⚠ 同时并行保留 **一代扁平写路径** `POST /api/items/{batch,publish,downshelf,reshelf}`（`api/items.py:90/98/105/111`），它们过同一服务函数（闸门有效）但不在 AI/MCP 面 |
| O4 | IM-手动发消息 | ✓ | ✓ `services/chats.py:665 send_manual` 首行 `_confirm_gate('给买家发消息')` | ✓ `api/chats.py` | ✓ `chats_send`（薄包装 `send_from_args` @`services/chats.py:1764` 自身无标记，confirm 透传下层） | ✓ `mcp_server/client.py:1018-1026` 闸门早于参数拼装 | ✓ `test_mcp_server.py` | ✓ 三层闸门齐全 |
| O5 | 资质-提交打包 / OSS 凭据 | ✓ `xianyu/qualification.py` | ⚠ `services/qualification.py:278-281` 与 `:297-300` 用 `if confirm is not True`（**只认布尔 True**） | ✓ `api/qualification.py` | ✓ `qualification_submit_packages` / `qualification_oss_credential` | ✓ | ✓ `test_qualification_api.py:359-386` 把该严格口径固定为预期 | ⚠ 比 §5.2 更严（`1`/`'true'` 一律拒），**不是漏洞**，但让 §5.2 全站口径不成立 |
| O6 | 广告-写入 | ✓ `xianyu/ad_write.py` | ⚠ `services/ad_write.py:75-78` 同款 `confirm is not True` 严格版 | ✓ `api/ad_write.py:35-48` POST `/{account_id}/{action}`（docstring 明确严格判定） | ✗ **无 ad 写工具** | ✗ | ✓ `test_ad_write_api.py:166-172` 断言 `{'confirm':1}`/`{'confirm':'true'}` 都回 `confirm_required` | ⚠ 属 42 条未接 AI/MCP 的写路由之一；严格口径与 §5.2 冲突 |

---

## 3. 问题清单

### P1（与文档/宣称相反，会误导使用者）

1. **§6 表把财务「充值/提现」记为已接 AI+MCP，实际不存在写能力。**
   `docs/交接文档.md` §6 财务行 ↔ `backend/app/api/finance.py:41/57/73/86`（全 GET）、`backend/app/api/promotion.py:43/47`（充值/提现是只读查询）、全仓无 finance/promotion 充值提现写工具。
2. **§6 表把营销「活动配置」记为已接，实际 `marketing.py` 零写路由。**
   `docs/交接文档.md` §6 营销行 ↔ `backend/app/api/marketing.py:44/54/66/78`（全 GET）。
3. **§6 表把风控「熔断参数」记为已接，实际无 risk 写工具/MCP 路由。**
   `docs/交接文档.md` §6 风控行 ↔ `backend/app/api/risk.py:49/55/65`（路由存在）+ `TOOL_SPECS` 无 risk 写工具。
4. **§6 表把通知「已读」记为已接，实际 `notify.py` 无写路由。**
   `docs/交接文档.md` §6 通知行 ↔ `backend/app/api/notify.py:27`（仅 GET）；AI 侧 `im_clear_unread` 打的是会话/系统通知，是另一条能力。

### P2（口径/健壮性，会让人按错判据写新代码）

5. **`confirm_required` 状态码：代码 400，文档 409。**
   `backend/app/services/common.py:41-50` 的 `FAIL_STATUS` **没有** `confirm_required`，`backend/app/services/common.py:88` `FAIL_STATUS.get(code, 400)` ⇒ 实际 400；注释也承认：`backend/app/services/chats.py:637`、`backend/app/services/disputes.py:35`、`backend/app/services/drafts.py:33`、`backend/app/services/fans_price.py:35`。而 `docs/交接文档.md` §5.3 表写 409。**二者必须统一**（前端/MCP 若按 409 判会漏判）。
6. **§5.2「`'true'`/`1` 也算确认」不是全站口径。**
   `backend/app/services/ad_write.py:75-78`、`backend/app/services/qualification.py:278-281`、`:297-300` 用 `if confirm is not True`，只认布尔 `True`；`backend/tests/test_ad_write_api.py:166-172`、`backend/tests/test_qualification_api.py:359-386` 把它锁定成预期。更严不是漏洞，但 §5.2 应写成「默认宽松判据（`_truthy`），广告与资质两族为严格判据」。
7. **同一条 confirm 判据在 13+ 个服务模块各复制一份（漂移风险）。**
   `backend/app/services/chats.py:638 CONFIRM_CODE='confirmation_required'`、`:641 _truthy`、`:655 _confirm_gate`，同款复制于 `disputes.py:36/83/92`、`drafts.py:34/57/66`、`fans_price.py:36/70/79`、`im_actions.py:36/97/106`、`item_templates.py:146/152/159/242`、`items.py:947/954/967`、`logistics.py:29/66/75`、`orders.py:29/32/46`、`quick_reply.py:35/72/81`、`security_write.py:30/33/40`、`shop.py:360`、`shop_profile.py:46`、`sub_accounts.py:37`，`trade_write.py` 另自带一套。改判据要改 13+ 处，漏一处就是 §5.2 那类真实事故的翻版。建议收敛到 `services/common` 单点。
8. **写操作无幂等键：重放等于二次提交。**
   全仓在服务层搜 `idempot` / `request_id` / `dedup` / 重复提交 = 零命中；`services/common.py` 里的「重试」只是 HTTP 层重试语义。AI/前端一旦超时重试（或用户连点两次带 confirm），平台侧会二次执行（关单、退款同意、发货尤其敏感）。api-review 第 2 项（幂等）**不满足**。
9. **42 条写路由没有 AI/MCP 面（123 − 81），其中约 9 条是真会打平台的商品/订单写路径。**
   例：`backend/app/api/items.py:90 POST /batch`、`:98 POST /publish`、`:105 POST /downshelf`、`:111 POST /reshelf`、`:298 PUT /{item_id}`、`:214 DELETE /{account_id}/{item_id}`、`:233 /delist`、`:241 /relist`、`:140 /batch-publish`；`backend/app/api/orders.py` 的 `POST /api/orders/{order_id}/deliver`、`POST /api/orders/sync`。这些路径闸门本身有效（服务函数带 `_confirm_gate`），但**六面只落了四面**：AI 助手做不到这些动作，而文档 §6 声称「AI+MCP 全覆盖」。若属有意只走正式路径，应在 §6 注明；否则是覆盖缺口。

### P3（提示，不构成本轮缺陷判定）

10. **集合型端点缺分页上限**：`/api/promotion/write/actions`（写操作审计日志，会随使用无限增长）、`/api/trade/{account_id}/addresses`、`/api/quick-reply/{account_id}/groups`、`/api/im/{account_id}/blacklist`。其余 61 条无分页参数的 GET 都是单资源/字典/统计端点，不需要分页（api-review 第 4 项其余部分满足）。
11. **9 个 MCP 工具指向非 GET 路由却不带 `confirm` 参数** —— **已逐条核查，不构成闸门绕过**：`accounts_set_enabled`（`api/accounts.py:348` 纯本地改开关）、`accounts_relogin`、`items_sync`、`items_listing_precheck`（语义只读，只跑图片/违禁词/分类）、`chats_set_mode`、`risk_clear`、`providers_upsert`（本地 provider 配置，`api/providers.py:106`）、`master_chat`（LLM）、`security_appeal_form`（表单渲染）—— 对应端点均已在 `backend/tests/test_write_routes_have_gates.py:40 GATE_FREE_ALLOWLIST`（21 条）里带理由登记，或走纯本地 SQLite。**注意它们也因此永远不会返回 confirm_required，不要拿它们当闸门样例。**
12. **本轮未复现 §8.1 基线**（`TOTAL 63 PASS 63 FAIL 0 CASES 2737`）：任务边界只允许跑 reconcile，`run_all.py` 会起后端进程，故未跑。已单独复现的是 `test_write_routes_have_gates.py`：**4 项 OK，0.396s**（`test_scan_found_routes` / `test_每一条无闸门的写路由都在白名单里` / `test_白名单没有过期条目` / `test_白名单每条都写了理由`）。
13. **§5.5「平台错误就地翻人话」有 2 处偏离**：`backend/app/services/datacenter.py` 与 `backend/app/services/marketing.py` 里各自出现 `if 'UNAUTHORIZED' in ...`，未走 `backend/app/services/platform_errors.py:36 PLATFORM_ERROR_HINTS`（13 个码）。人话覆盖口径不完整，但属只读族。

---

## 4. 覆盖范围与未覆盖原因

- **已逐条抽检 18 条**（交易 6 / 财务 3 / 营销 3 / 其它 6），满足「≥12 条且三类各 ≥2」。
- **其余 54 条写工具**只做了机械核对：都在 `MASTER_TOOLS` 内、都有 MCP 路由、`confirm` 都在 `required` 里且 `type=boolean`、描述含 `confirm=true`、72/72 测试源码有提及。**未逐条读服务实现**。
- **协议层**只抽查了 `backend/app/xianyu/trade_write.py:308/333/344/477/535` 与 `backend/app/xianyu/client.py:704`；其余族的 request 构造器未读。
- **未做**：`backend/tests/run_all.py` 全量基线复跑、`docs/contracts/*.md` 与 `docs/CONTRACTS.md` 逐条比对、真机平台调用（禁止）、任何写操作（禁止）。
- 已知无法从静态证据判定的项：平台返回体「沉默 vs 0」在**每个**字段上的取舍（`null ≠ 0` 只在 `trade_write` 的 add_memo / 缺回显路径上取到正面证据），需要真机样本才能穷尽。
