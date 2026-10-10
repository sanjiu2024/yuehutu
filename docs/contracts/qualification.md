# 资质管理契约（超级擦亮 `/seller-ad/qualification`）

> 本文件是资质管理接口的**唯一对外口径**：HTTP 路径、mtop 名、请求/响应字段、错误码，
> 以及**每条结论的证据来源**。
> 实现：`backend/app/xianyu/qualification.py`（协议层）、`backend/app/services/qualification.py`
> （服务层）、`backend/app/api/qualification.py`（HTTP 层）、
> `backend/tests/test_qualification_api.py`（单测）。

## 0. 页面与证据来源

| 结论 | 证据 |
|---|---|
| 页面存在，路由 `#/seller-ad/qualification` | `logs/seller_discover/ad_pages.json:32` |
| bundle 为 `fish-ad-pc@0.0.12`（`https://g.alicdn.com/idle-pc/fish-ad-pc/0.0.12/`） | 任务书（Lead 从官方工作台静态挖出） |
| 7 个 mtop 接口名 | 任务书（Lead 从 bundle 静态挖出）；**本任务未独立复核**，但已由 `test_mtop_names_are_frozen` 冻结 |
| 调用形状 `data: {requestJson: JSON.stringify(...)}` | 与本仓库已交付的 `idle.ad.*` 同形状（`backend/app/xianyu/client.py:932 _ad_json`），非本页独有 |

**证据缺口（必读）**：仓库 `logs/seller_discover/` 下**没有** `fish-ad-pc` 的 JS 抓取物，
所以**「`requestJson` 里业务对象的键名」一条都没有调用点证据**。本契约因此规定：
协议层与服务层**只做 `requestJson` 序列化，不做任何字段翻译或补默认键**；
业务对象由调用方原样给出。凡是下面标了「⚠ 证据缺口」的键名，都是**沿用同仓库其它
`idle.ad.*` 接口的命名习惯**（`client.py:905-930`），不是已证实的事实。

## 1. 只读接口

| # | HTTP | mtop 接口 | 请求 `requestJson` | 响应 |
|---|---|---|---|---|
| 1 | `GET /api/qualification/{account_id}/detail` | `mtop.taobao.idle.ad.qualification.detail.query` | `{}`（⚠ 证据缺口：字段名未知，本页真机资质为空，无法反推） | `detail`: 平台 `data` 原样（dict）；平台没给 → `null` |
| 2 | `GET /api/qualification/{account_id}/submitted-records?page=&size=` | `...qualification.submittedRecords.batchQuery` | `{pageNum:int, pageSize:int}`（⚠ 证据缺口：键名沿用 `ad_campaigns`；`page` 默认 1、`size` 默认 20，上限 50） | `records`: 列表或 `null`；`total`: 整数或 `null` |
| 3 | `GET /api/qualification/{account_id}/categories` | `...qualification.allowAuditCategories.query` | `{}`（⚠ 证据缺口） | `categories`: 列表或 `null` |
| 4 | `GET /api/qualification/{account_id}/templates` | `...qualification.templates.queryv2` | `{}`（⚠ 证据缺口） | `templates`: 列表或 `null` |
| 5 | `GET /api/qualification/{account_id}/reusable` | `...qualification.reusableQualification.query` | `{}`（⚠ 证据缺口） | `qualifications`: 列表或 `null` |

五个只读接口的响应**共同字段**：

```json
{ "account_id": "2217970177348", "source": "platform", "fetched_at": "2026-10-11 04:20:00" }
```

`source` 恒为 `platform`（本仓库统一口径，表示数据来自平台而非本地缓存）；
`fetched_at` 是服务端 UTC 时间字符串（`%Y-%m-%d %H:%M:%S`）。

**信封**：上面这段是**内层**。成功响应整体再套一层 `{"success": true, "data": {...}}`
（`backend/app/services/common.py:82`）；失败信封是**平铺**的
`{"success": false, "code": "...", "message": "..."}`（`common.py:81`）。前端取数据走
`resp.data`。

**列表/总数的取值规则**（`services/qualification.py::_first_list` / `_first_total`）：

- 平台返回了**空列表** → `[]`（「确实一条都没有」是有效答复，真机该账号就是「暂无数据」）；
- 平台**没有这个键** → `null`（前端显示「-」）；
- **禁止 0 顶替**：`total` 认不出就是 `null`；平台给了 `total: 0` 才返回 `0`。

## 2. 写接口（不可逆，必须显式确认）

| # | HTTP | mtop 接口 | 请求体 | 响应 |
|---|---|---|---|---|
| 6 | `POST /api/qualification/{account_id}/packages/submit` | `mtop.taobao.idle.ad.qualification.packages.submit` | `{"confirm": true, "request": {...}}` | `submitted`: 平台 `data` 原样或 `null` |
| 7 | `POST /api/qualification/{account_id}/oss-credential` | `mtop.taobao.idle.ad.qualification.get.oss.credential` | `{"confirm": true, "request": {...}}` | `credential`: 平台 `data` 原样或 `null` |

**红线**（双层锁，测试逐条钉住）：

1. `confirm` **必须显式为 JSON 布尔 `true`**；缺省 `false`、字符串 `"true"` 一律拒绝
   （400 `confirm_required`），**服务层在查账号、查库、发请求之前就返回**；
   协议层函数自身再有一道 `ValueError` 硬闸（`xianyu/qualification.py::_require_confirm`）。
2. `request` 必须是**非空 JSON 对象**，且**原样**进 `requestJson`：服务层不补键、不改名
   （没有 bundle 证据，补键等于编协议）。空对象 / 非对象 → 400 `invalid_fields`。
3. `request` 里的业务字段**本契约不定义**（⚠ 证据缺口）：请以官方页面实际请求为准，
   拿到调用点证据后再回填本节。

## 3. 错误码

| HTTP | `code` | 含义 |
|---|---|---|
| 400 | `invalid_fields` | 分页/业务参数不合法（含空 `request`） |
| 400 | `confirm_required` | 写接口没显式确认（本项目自造码，平台侧不存在） |
| 404 | `not_found` | 账号不在本地 `accounts` 表里 |
| 409 | `verification_required` | **登录问题**：Cookie 没了 / 库里已标 `need_relogin`（带 `login_state: need_relogin`） |
| 502 | `protocol_error` | 协议层调用失败（含**无权限** `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`） |
| — | `xianyu_not_ready` | 协议层未就绪（`common.not_ready`），不冒充「资质为空」 |

**无权限 ≠ 掉线**：平台返回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 时给 502 `protocol_error`
如实透出，**绝不写 `need_relogin`** —— 那是账号没有资质业务权限，重扫码也没用。
只有第 409 行那两种**登录**情况才写 `need_relogin`。

## 4. 未验证项（诚实边界）

1. **7 个 mtop 接口名未由本任务独立复核**（来自任务书）；调用形状（`requestJson` +
   `v='1.0'` + `spm_cnt='a21ybx.im.0.0'`）由单测钉住，但**没有真机响应样本**。
2. **所有 `requestJson` 业务键名都是证据缺口**（见 §0），包括第 2 行的 `pageNum/pageSize`。
3. **真机只读探针已执行，但没拿到响应结构**（2026-10-11 04:18，Lead 批准的单次探针）：
   打 `mtop.taobao.idle.ad.qualification.templates.queryv2`（`requestJson: '{}'`，`v='1.0'`，
   `spm_cnt='a21ybx.im.0.0'`）**直接被平台挡在登录态**：
   `FAIL_SYS_SESSION_EXPIRED::Session过期`（`SessionExpiredError`，客户端日志
   `backend.app.xianyu.client:_check_response:292`）。
   原文落盘：`logs/seller_discover/qualification_probe.json`；探针脚本
   `logs/seller_discover/qualification_probe.py`（只读、单次、不重试，按 Lead 要求失败即停）。
   **账号 `2217970177348` 的 Cookie 已过期，需要重新扫码登录** —— 在此之前任何真机核对
   （包括 §1 的响应键名）都做不了。所以 §1 的 `templates` / `categories` /
   `qualifications` 键名**仍无真机样本**，是靠别名表（`_LIST_KEYS`）兜的：真机若返回别的
   键名，接口会如实返回 `null`（不会伪造数据），但前端就看不到列表。
   （注意：这次探针**绕过了** `common.call_method`，所以没有回写 `need_relogin`；
   真机走正常链路时，同样的过期会被翻成 409 + `need_relogin`。）
4. **写接口一次都没真调**（设计如此）：`packages.submit` / `get.oss.credential` 的真机
   行为（限流、幂等、失败码）完全未知。
5. 该账号（`2217970177348`）真机资质为「暂无数据」，所以 `detail` 即使探针成功也只能
   验证「空态」的响应形状，验证不了有资质时的字段。

## 5. 复现与验证

```powershell
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe backend\tests\test_qualification_api.py"
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe backend\tests\run_all.py"
```

单测**不打真实平台**：协议层用假模块注入（`install_fakes`），HTTP 层用
`app.dependency_overrides` 绕开登录。
