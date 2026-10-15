# 第 61 批 · 工作流 B：UI 视觉与人类使用习惯打磨

- 仓库：`C:/Users/Administrator/Projects/xianyu-ai-workbench`，基线 HEAD `2f9e9bd`（340 提交）
- 写盘范围：`frontend/src/**` + 本文件。未新增 npm 依赖，未改 `api.ts` 请求路径/字段，未 git add/commit。
- 原则：第 59 批刚统一过设计令牌与共享组件，本轮**只补缺口，不推倒重来**——所有改动复用既有 `--sp-*` / `--radius*` / `--fs-*` / 语义色与 `AsyncBox` / `ConfirmDialog` / `format.ts`。

## 0. 先读现有设计语言（改动的前提）

| 层 | 位置 | 现状 |
| --- | --- | --- |
| 设计令牌 | `frontend/src/styles.css:12-115` | `--bg/--panel/--border/--text*/--accent` + 成套语义色三件套、`--radius(10)/--radius-sm(6)/--radius-lg(14)`、`--sp-1..6`、`--shadow-sm/md/lg`、`--fs-11..20` |
| 浅色主题 | `frontend/src/styles.css:118-172` | `html[data-theme='light']` 只覆盖变量，不改选择器（对比度已按 `logs/contrast_audit` 实测调过） |
| 主滚动容器 | `frontend/src/styles.css:326-356` | `.content{flex:1;overflow-y:auto;padding:18px 20px 40px;min-height:0}` |
| 表格 | `frontend/src/styles.css:560-603` | `.table-wrap` 已有圆角+横向滚动；`th{position:sticky;top:0}` **表头已全局吸顶**；`td.num` 右对齐 + `tabular-nums` |
| 四态 | `styles.css:430-481`、`styles.css:1451-1469` | `.btn:hover:not(:disabled)` / `.btn:disabled{opacity:.45}` / `:focus-visible{outline:2px solid var(--focus-color)}` 齐全 |
| 共享件 | `frontend/src/components.tsx:275-354,365-476,480-620` | `AsyncBox`（空/骨架/刷新保留旧内容）、`Pager`（跳页支持 Enter+blur）、`Modal`（Esc 关、锁滚动、焦点收束与归还）、`ConfirmDialog`（危险框默认聚焦「取消」） |

摸底结论：**吸顶表头、排序可见性、危险确认焦点、金额右对齐、四态、失败 toast 常驻**这些第 59 批已经做过了，本轮不重复做；真正的缺口只有三处（见下）。

## 1. 逐条：问题 → 证据 → 改法 → 结果

### 1.1 列表时间全是 19 字符绝对时间，卖家要自己做减法

- **问题**：消息、运行记录、通知三类时间线每行铺满 `2026-10-16 01:53:14`。看「这条是不是刚发的」得心算一次减法，长列表里几乎不可用。
- **证据**：`frontend/src/pages/Agents.tsx:497`、`Agents.tsx:610`、`frontend/src/pages/Chats.tsx:1619`、`frontend/src/pages/Notifications.tsx:206` 全是裸 `formatTime(...)`；全仓 `grep -E "刚刚|分钟前|小时前|天前"` **命中 0**，即项目此前没有任何相对时间实现。
- **改法**：
  1. `frontend/src/format.ts`：把 `formatTime` 里那套「unix 秒 / 毫秒 / 无时区 UTC 串补 Z / 认不出原样返回」的解析抽成私有 `function toDate(value?: string | number | null): Date | null`（失败返回 `null`），`formatTime` 改为调它，行为不变；
  2. 新增 `export function formatRelativeTime(value?: string | number | null, now: Date = new Date()): string`：`<60s`→「刚刚」、`<60min`→「N 分钟前」、同一自然日→「N 小时前」、昨天→「昨天 HH:mm」、同年→「MM-DD HH:mm」、跨年→`formatTime`。**解析失败或 `diff < 0`（未来时间 / 时钟漂移）一律回落 `formatTime`**，绝不出现「-3 分钟前」或 1970。
  3. 四个调用点改成相对时间，并把绝对时间放进 `title`——鼠标悬停仍是精确值，信息不丢：
     - `frontend/src/pages/Agents.tsx:498` `<td className="ag-nowrap dim" title={formatTime(message.created_at)}>{formatRelativeTime(message.created_at)}</td>`
     - `frontend/src/pages/Agents.tsx:611` 对 `run.created_at` 同样处理
     - `frontend/src/pages/Chats.tsx:1619` 气泡时间：`<span title={formatTime(message.created_at)}>{formatRelativeTime(message.created_at)}</span>`
     - `frontend/src/pages/Notifications.tsx:207`：`<span className="nt-time" title={channel.time ? formatTime(when(channel.time)) : ''}>{channel.time ? formatRelativeTime(when(channel.time)) : ''}</span>`
  4. **刻意只用在列表/气泡**，详情页与审计字段仍用 `formatTime`（已在函数注释里写明）——审计要的是可核对的绝对时刻。
- **结果**：`npx esbuild src/format.ts --format=cjs` 后跑探针（临时产物已删），固定 `now = 2026-10-16T10:00:00+08:00`：

  ```text
  '2026-10-16 01:59:30' (UTC->本地 09:59:30) -> "刚刚"      title= 2026-10-16 09:59:30
  '2026-10-16 01:48:00'                        -> "12 分钟前"  title= 2026-10-16 09:48:00
  '2026-10-16 00:00:00'                        -> "2 小时前"   title= 2026-10-16 08:00:00
  '2026-10-14 06:30:00'                        -> "10-14 14:30"
  '2025-12-01 02:00:00'                        -> "2025-12-01 10:00"
  '不是时间'                                    -> "不是时间"   (回落 formatTime)
  null                                          -> "-"
  '2026-12-01 00:00:00' (未来)                  -> "2026-12-01 08:00:00" (回落全量)
  unix 秒 (本地 09:30)                          -> "30 分钟前"
  ```

  冒烟无回归：解析失败、空值、未来时间三条边界都走回落分支，未出现负数或 1970。

### 1.2 切页面后主内容区停在半空（滚动位置不重置）

- **问题**：商品列表 4000 行，滚到底部改完价再点左侧「订单」，新页面直接停在半空——顶部标题和筛选栏都在屏幕上方看不见，用户第一反应是「页面是不是坏了」。这是最典型的桌面端习惯缺口：换页 = 换上下文，视口应回到顶部。
- **证据**：`frontend/src/App.tsx:359` 是 `<div className="content">`，而 `.content` 是主滚动容器（`frontend/src/styles.css:326-356` `overflow-y:auto`）；`grep scrollTop|scrollTo` 在 `App.tsx` **命中 0**，全仓只有 `Chats.tsx:1020-1040`（贴底）、`Chats.tsx:1558`（`stickRef`）、`Messages.tsx:288`（保留高度）三处会话内的滚动处理，没有任何页面级重置。
- **改法**：`frontend/src/App.tsx` react import 加 `useRef`；新增 `const contentRef = useRef<HTMLDivElement>(null)` 与 `useEffect(() => { contentRef.current?.scrollTo({ top: 0 }) }, [page])`；`:359` 改为 `<div className="content" ref={contentRef}>`。依赖只有 `page`，同页内刷新（`AsyncBox` 保留旧内容）不会把用户滚回去。
- **结果**：`tsc` / `build` 均 0；该行为在无浏览器环境下靠代码路径核对（`page` 状态是唯一入口，`App.tsx:270-273`），未做 DOM 实测——见「未完成/未验证」。

### 1.3 危险操作的确认按钮不写动词，看不出点下去会发生什么

- **问题**：确认框标题已经写清后果（「不可撤销」「会真的改到闲鱼上」），但按钮只写「确认执行」/「确认提交」——用户在最后一步要点的是**动作**，不是「确认」。措辞与后果脱节，是危险操作最容易误点的地方。
- **证据**：`frontend/src/pages/Items.tsx:1207` 与 `frontend/src/pages/Promotion.tsx:545` 都是 `confirmText="确认执行"`；`Items.tsx:3148`、`Items.tsx:3487` 都是 `confirmText="确认提交"`。对照同仓已有写法：`Orders.tsx:2079` `` confirmText={`确认${spec.action}`} ``、`ReturnAddresses.tsx:571`、`Complaints.tsx:756`、`DraftBox.tsx:558`、`QuickReply.tsx:579`、`Master.tsx:544` 都已按动作写文案——**是这两页没跟上，不是新风格**。
- **改法**（不改任何请求，只改 `ConfirmDialog` 的 `confirmText`）：
  - `Items.tsx:1207` → `confirmText={confirmAction.title}`：7 个 `setConfirmAction` 的 `title` 本身就是动词短语（批量上架 / 批量下架 / 批量删除 / 删除商品 / 只从本地列表移除 / 下架商品 / 重新上架），直接复用，零新增文案。
  - `Items.tsx:3148` → `confirmText={priceChanged && quantityChanged ? '确认改价改库存' : priceChanged ? '确认改价' : '确认改库存'}`（`priceChanged` 见 `Items.tsx:3000-3001`、`quantityChanged` 见 `Items.tsx:3002-3005`）：点开哪个框就说哪个动作，不必猜。
  - `Items.tsx:3487` → `confirmText="确认保存粉丝价"`（与标题 `title="确认保存粉丝价"` 一致）。
  - `Promotion.tsx:545` → `` confirmText={`确认${label(draft.action)}`} ``（`label()` 见 `Promotion.tsx:88`，`ACTION_LABELS` 里是「暂停 / 提现」这类动词），与 `Orders.tsx:2079` 完全同款。
- **结果**：全部按钮变成「确认 + 动词」。危险框仍然默认聚焦「取消」（`components.tsx:582-620`），误点成本没变高。

## 2. 有意不做（附理由）

| 项 | 理由 |
| --- | --- |
| 给会话 / 通知加批量选择 | 这两页是**流水**不是**清单**：消息只能逐条已读，通知只有「全部已读」一个真实语义。加复选框只会让用户以为能批量删除平台通知。已有批量能力的地方（`Items.tsx` 批量上下架/删除、`DraftBox.tsx` 批量删除）已经齐全，且 `Items.tsx:706,802,845,852` 已把「一次只能管一个账号」「只作用于看得见的商品」写清楚。 |
| 加 `Cmd/Ctrl+K` 全局命令面板 | 主人原话要求的是「符合人类使用习惯」，不是「加特效」。全仓快捷键目前只有 `Master.tsx:523` 的 `Ctrl/Cmd+Enter` 发送，加一个无处可搜的命令面板属于新增能力而非打磨；没有搜索索引支撑，它只会变成一个更慢的导航。**不新增**。 |
| 给其余页面的表格加排序/筛选 | `Orders.tsx:279-476`（三态排序 + `aria-sort` + `.tr-sort` ↑↓↕ + 「只作用于当前页」说明 + 空筛选态自带「清空筛选」）与 `CommodityData.tsx:154-199,398-421` 已经做全。其余页面的表格是**详情展示**（字段-值、逐条结果），排序没有业务含义，加了反而是噪音。 |
| 重做配色 / 圆角 / 阴影 / 令牌 | 第 59 批刚统一（`styles.css:12-172`），深浅两套主题对比度按实测调过。本轮**没有改动 `styles.css` 一个字节**——改动全部落在行为与文案层，避免把已验收的视觉基线推倒重来。 |
| 把相对时间用在详情页 / 审计 / 导出 | 审计要的是可核对的绝对时刻。`formatRelativeTime` 的注释已写明适用范围（列表与气泡），详情一律 `formatTime`。 |
| 给相对时间做「自动刷新」定时器 | 会让每行每秒重渲染，且卖家工作台不是实时看板。悬停 `title` 给绝对时间已经够用。 |

## 3. 验收判据（真实输出）

### 3.1 类型检查

```text
$ cd frontend && npx tsc --noEmit -p tsconfig.json
TSC_EXIT=0
(stderr 为空)
```

### 3.2 生产构建

```text
$ cd frontend && npm run build
> xianyu-ai-workbench-frontend@0.1.0 build
> tsc --noEmit && vite build

vite v5.4.21 building for production...
transforming...
✓ 97 modules transformed.
rendering chunks...
computing gzip size...
dist/index.html                   0.40 kB │ gzip:   0.30 kB
dist/assets/index-BpLeM6Kv.css  158.94 kB │ gzip:  24.97 kB
dist/assets/index-Bwt5EUxM.js   646.35 kB │ gzip: 205.13 kB
✓ built in 4.67s
BUILD_EXIT=0
```

产物：`dist/index.html` 0.40 kB、`dist/assets/index-BpLeM6Kv.css` 158.94 kB（gzip 24.97）、`dist/assets/index-Bwt5EUxM.js` 646.35 kB（gzip 205.13）。未新增任何 chunk。

> **第 61 批收尾追加（2026-10-16）**：本文件写作时只跑了 `vite build`；之后区域码三级级联又改了前端，
> **收尾已带前端重打包**：最终 `dist/闲鱼AI工作台/闲鱼AI工作台.exe` = **10,295,397 B / 02:22:40**，
> 打包内 `index-BhT_UXe6.js`（719,837 B，gzip 204,331 B）+ `index-BpLeM6Kv.css`（158,948 B），
> 五件套全过（`ASSETS_MATCH=True` / 29/29 页 / 对比度 dark 0/29、light 0/29 / a11y 三类 0）。
> 上面那次运行的 `index-Bwt5EUxM.js` 已被 `index-BhT_UXe6.js` 取代，不再是最新产物。

## 4. 改动清单与边界

```text
frontend/src/format.ts                  | 新增 toDate + formatRelativeTime（formatTime 行为不变）
frontend/src/App.tsx                    | 切页重置 .content 滚动位置
frontend/src/pages/Agents.tsx           | 消息表 / 运行表时间改相对时间 + title
frontend/src/pages/Chats.tsx            | 消息气泡时间改相对时间 + title
frontend/src/pages/Notifications.tsx    | 通知时间改相对时间 + title
frontend/src/pages/Items.tsx            | 3 处确认按钮动词化
frontend/src/pages/Promotion.tsx        | 1 处确认按钮动词化
frontend/src/pages/ReturnAddresses.tsx  | 区域码省 / 市 / 区三级级联（第 61 批收尾加入）
docs/audit/ui_polish61.md               | 本文件（新增）
```

- **未改**：`frontend/src/api.ts` 的请求路径与字段（本轮只动了 `ConfirmDialog` 的文案 prop）、`backend/**`、`mcp_server/**`、`scripts/**`、`docs/CONTRACTS.md`、`docs/HANDOFF.md`、`docs/交接文档.md`、`styles.css`。
- **未新增依赖**、未 `git add/commit`。**本文件写作时**未重打包、未跑 `scripts/build_exe.ps1`；
  第 61 批收尾已带前端重打包（见上文「收尾追加」）。

## 5. 未完成 / 未验证

1. **浏览器内行为未实测**：本环境无桌面壳与浏览器，`App.tsx` 的滚动重置、四处相对时间的实际渲染只做了代码路径核对 + `formatRelativeTime` 的 Node 探针，没有截图证据。建议在有壳的环境里点一次「商品滚到底 → 切订单」确认。
2. **未做相对时间的自动刷新**（理由见第 2 节）：页面停着不动时「刚刚」会一直是「刚刚」，刷新或切页后才更新。如果主人觉得别扭，最小改法是给 `AsyncBox` 的刷新加一个 60s 定时器，但那要动共享组件，本轮没做。
3. 表格密度、圆角、阴影本轮**没动**——判断是第 59 批的令牌层已经够用，改这些属于审美偏好而非「不足」，需要主人指名再动。
