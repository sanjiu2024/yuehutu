/**
 * 数据组三页（数据总览 / 粉丝数据 / 客服数据）的**共享内核**。
 *
 * 这里只有纯工具函数 + 一个无副作用的日期模块；三页的状态与副作用（`useState`、
 * `useAsync`）各自保留在页面里，避免一个共享 hook 把三条不同的接口路径揉成一团。
 *
 * 契约（`docs/contracts/datacenter.md`，task-52 冻结）：
 *  - `GET /api/datacenter/{account_id}/{overview|fans|customer-service}?start=&end=`
 *  - `start`/`end` 是 `YYYYMMDD`；缺省后端补「今天往前 7 天」。非法值后端回 `400 invalid_fields`，
 *    所以这里在发请求前先校验，别把一个用户看得懂的输入变成一句后端报错。
 *
 * 两条不能破的口径：
 *  1. **平台没给的指标是 `null`，显示 `-`，绝不补 0**。只有平台真的返回 0 才显示 0 ——
 *     0 是「真的没人看」，null 是「没权限 / 没取到」，画成 0 用户会以为生意崩了。
 *  2. **无权限 ≠ 掉线**：`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 只是这个账号没有「数据」权限，
 *     后端给 `null` + 中文 warning，不会把账号打成 `need_relogin`，页面别提示重新扫码。
 *  3. 本页无写操作，刷新一律走 `reload()`，不轮询。
 */

/** 今天往前 n 天，格式 `YYYYMMDD`（n=6 就是含今天的 7 天）。 */
export function daysAgo(n: number): string {
  const d = new Date()
  d.setDate(d.getDate() - n)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}${m}${day}`
}

/** 今天，格式 `YYYYMMDD`。 */
export function today(): string {
  return daysAgo(0)
}

/** `YYYYMMDD` 才合法；形如 `2026-10-04` / 位数不对 / 非数字一律拒绝（后端也会拒，这里提前拦）。 */
export function isYmd(value: string): boolean {
  return /^\d{8}$/.test(value)
}

/** 区间合法 = 两头都是 `YYYYMMDD` 且开始不晚于结束。 */
export function isRangeOk(start: string, end: string): boolean {
  return isYmd(start) && isYmd(end) && start <= end
}

/** 计数：`null` / 缺失 / 非数字一律 `-`，真实 0 原样保留。 */
export function num(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  const n = Number(value)
  if (!Number.isFinite(n)) return '-'
  return n.toLocaleString('zh-CN')
}

/** 百分比：平台给的就是百分数（如 `33.33`），原样显示并补 `%`；`null` → `-`。 */
export function pct(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  const n = Number(value)
  if (!Number.isFinite(n)) return '-'
  return `${n}%`
}

/** 秒：同 `num`，只是补单位。 */
export function secs(value: unknown): string {
  const s = num(value)
  return s === '-' ? '-' : `${s} 秒`
}

/** 平台回的真实区间认不出时是 `null`；此时不写，别假装知道。 */
export function realRangeText(real: unknown, begin: unknown): string {
  const realText = real ? String(real) : ''
  const beginText = begin ? String(begin) : ''
  const parts: string[] = []
  if (realText) parts.push(`平台回的真实区间：${realText}`)
  if (beginText) parts.push(`数据起始日：${beginText}`)
  return parts.join(' · ')
}

/** 一个指标的展示形态：怎么把值变成字符串。 */
export interface MetricDef {
  key: string
  label: string
  format?: (value: unknown) => string
  tip?: string
}
