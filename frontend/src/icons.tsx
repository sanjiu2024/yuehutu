/**
 * 全站图标集：全部为内联手绘 SVG（stroke = currentColor），不引第三方图标库、不联网。
 *
 * 为什么不用 emoji：emoji 的字形随系统/字体变化，在 WebView2、打包版、不同 Windows 版本上
 * 大小与基线都会漂，颜色也无法跟随主题（深色/浅色切换时最明显），看起来廉价且不整齐。
 *
 * 约定：
 *   - 每个图标是一个组件，统一接受 `size`（默认 18）与 `className`，其余 props 透传到 <svg>。
 *   - 线宽 1.7，`strokeLinecap="round"`，视觉重量与 13.5~14px 的中文正文匹配。
 *   - 需要「实心强调」时传 `filled`（少数图标支持，如 star/heart）。
 *   - 新增图标请保持 24×24 viewBox，并在 `ICONS` 里登记，方便 grep。
 */

import type { CSSProperties, SVGProps } from 'react'

export interface IconProps extends Omit<SVGProps<SVGSVGElement>, 'children'> {
  size?: number
  /** 需要实心填充时用（仅部分图标支持）。 */
  filled?: boolean
  style?: CSSProperties
}

function Svg({ size = 18, children, ...rest }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  )
}

/* ---------------- 导航图标 ---------------- */

/** 概览：仪表盘。 */
export const IconOverview = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 14.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z" />
    <path d="M20.4 15.6A9 9 0 1 1 8.4 3.6" />
    <path d="M12 12l4.2-4.2" />
  </Svg>
)

/** 账号：人形。 */
export const IconUser = (p: IconProps) => (
  <Svg {...p}>
    <path d="M20 21v-1.8a4.2 4.2 0 0 0-4.2-4.2H8.2A4.2 4.2 0 0 0 4 19.2V21" />
    <circle cx="12" cy="7.5" r="4" />
  </Svg>
)

/** 商品：包裹。 */
export const IconBox = (p: IconProps) => (
  <Svg {...p}>
    <path d="M20.5 7.8v8.4a1.6 1.6 0 0 1-.85 1.42l-6.9 3.6a1.6 1.6 0 0 1-1.5 0l-6.9-3.6A1.6 1.6 0 0 1 3.5 16.2V7.8" />
    <path d="M3.72 7.1l7.53-3.8a1.6 1.6 0 0 1 1.5 0l7.53 3.8-8.28 4.3-8.28-4.3Z" />
    <path d="M12 11.4V21" />
  </Svg>
)

/** 会话：对话气泡。 */
export const IconChat = (p: IconProps) => (
  <Svg {...p}>
    <path d="M20.5 12.4c0 4.1-3.8 7.4-8.5 7.4a9.9 9.9 0 0 1-2.6-.34L4.6 21l1.15-3.6A7 7 0 0 1 3.5 12.4C3.5 8.3 7.3 5 12 5s8.5 3.3 8.5 7.4Z" />
    <path d="M8.8 12h.01M12 12h.01M15.2 12h.01" />
  </Svg>
)

/** AI 沟通：两个 AI 握手机器人（用双向箭头+圆点表达「互相沟通」）。 */
export const IconAgents = (p: IconProps) => (
  <Svg {...p}>
    <rect x="3.5" y="6.5" width="6" height="6" rx="1.8" />
    <rect x="14.5" y="11.5" width="6" height="6" rx="1.8" />
    <path d="M9.5 9.5h3.2a2 2 0 0 1 2 2v.5" />
    <path d="M12.05 10.6l-1.4-1.4M12.05 10.6l-1.4 1.4" />
    <path d="M6.5 6.5V4.5M17.5 17.5v2" />
  </Svg>
)

/** 主管 AI：芯片/大脑。 */
export const IconMaster = (p: IconProps) => (
  <Svg {...p}>
    <rect x="5.5" y="5.5" width="13" height="13" rx="2.6" />
    <path d="M9.6 9.6h4.8v4.8H9.6z" />
    <path d="M9.5 2.8v2.7M14.5 2.8v2.7M9.5 18.5v2.7M14.5 18.5v2.7M2.8 9.5h2.7M2.8 14.5h2.7M18.5 9.5h2.7M18.5 14.5h2.7" />
  </Svg>
)

/** AI 配置：钥匙。 */
export const IconKey = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="8" cy="16" r="3.5" />
    <path d="M10.6 13.6 20 4.2M17.2 7l1.8 1.8M14.6 9.6l1.8 1.8" />
  </Svg>
)

/** 提示词：带文本的纸。 */
export const IconPrompts = (p: IconProps) => (
  <Svg {...p}>
    <path d="M14.5 3.5H7.8A2.3 2.3 0 0 0 5.5 5.8v12.4a2.3 2.3 0 0 0 2.3 2.3h8.4a2.3 2.3 0 0 0 2.3-2.3V7.5l-4-4Z" />
    <path d="M14.2 3.7V7.6h4" />
    <path d="M8.8 12h6.4M8.8 15.6h4.4" />
  </Svg>
)

/** 风控：盾牌。 */
export const IconShield = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 2.8 5 5.4v5.9c0 4.3 2.9 7.9 7 9.9 4.1-2 7-5.6 7-9.9V5.4l-7-2.6Z" />
    <path d="M9.2 11.8l2 2 3.6-3.8" />
  </Svg>
)

/* ---------------- 操作图标 ---------------- */

export const IconRefresh = (p: IconProps) => (
  <Svg {...p}>
    <path d="M20.2 11.2A8.2 8.2 0 0 0 6.9 6.1L3.8 8.9" />
    <path d="M3.8 12.8a8.2 8.2 0 0 0 13.3 5.1l3.1-2.8" />
    <path d="M3.8 4.6v4.3h4.3M20.2 19.4v-4.3h-4.3" />
  </Svg>
)

export const IconPlus = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 5v14M5 12h14" />
  </Svg>
)

export const IconEdit = (p: IconProps) => (
  <Svg {...p}>
    <path d="M16.6 3.9a2.1 2.1 0 0 1 3 3L8.3 18.2 4 19.6l1.4-4.3L16.6 3.9Z" />
    <path d="M14.8 5.7l3 3" />
  </Svg>
)

export const IconTrash = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4.5 6.8h15M9.4 6.8V4.9c0-.6.5-1.1 1.1-1.1h3c.6 0 1.1.5 1.1 1.1v1.9" />
    <path d="M6.4 6.8l.9 12c.05.7.6 1.2 1.3 1.2h6.8c.7 0 1.25-.5 1.3-1.2l.9-12" />
    <path d="M10.4 10.6v5.6M13.6 10.6v5.6" />
  </Svg>
)

export const IconUpload = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 16.2V4.6M7.6 8.8 12 4.4l4.4 4.4" />
    <path d="M4.5 15.4v2.7a1.9 1.9 0 0 0 1.9 1.9h11.2a1.9 1.9 0 0 0 1.9-1.9v-2.7" />
  </Svg>
)

export const IconDownload = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 4.6v11.6M7.6 12l4.4 4.4 4.4-4.4" />
    <path d="M4.5 15.4v2.7a1.9 1.9 0 0 0 1.9 1.9h11.2a1.9 1.9 0 0 0 1.9-1.9v-2.7" />
  </Svg>
)

export const IconImage = (p: IconProps) => (
  <Svg {...p}>
    <rect x="3.6" y="4.6" width="16.8" height="14.8" rx="2.2" />
    <circle cx="9" cy="10" r="1.6" />
    <path d="M4.4 16.8l4.3-4.3a1.7 1.7 0 0 1 2.4 0l5.8 5.9" />
    <path d="M14.6 15.2l1.4-1.4a1.7 1.7 0 0 1 2.4 0l1.9 1.9" />
  </Svg>
)

export const IconClose = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6.2 6.2l11.6 11.6M17.8 6.2 6.2 17.8" />
  </Svg>
)

export const IconCheck = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4.8 12.6l4.6 4.6 9.8-10.4" />
  </Svg>
)

export const IconChevronDown = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6.4 9.4 12 15l5.6-5.6" />
  </Svg>
)

export const IconChevronRight = (p: IconProps) => (
  <Svg {...p}>
    <path d="M9.4 6.4 15 12l-5.6 5.6" />
  </Svg>
)

export const IconChevronLeft = (p: IconProps) => (
  <Svg {...p}>
    <path d="M14.6 6.4 9 12l5.6 5.6" />
  </Svg>
)

export const IconArrowRight = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4.6 12h14.2M13.4 6.6 18.8 12l-5.4 5.4" />
  </Svg>
)

export const IconSearch = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="10.8" cy="10.8" r="6.3" />
    <path d="M15.4 15.4 20.4 20.4" />
  </Svg>
)

export const IconFilter = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3.8 5.6h16.4l-6.4 7.5v5.2l-3.6 1.9v-7.1L3.8 5.6Z" />
  </Svg>
)

export const IconSort = (p: IconProps) => (
  <Svg {...p}>
    <path d="M8.4 4.6v14.8M5 8l3.4-3.4L11.8 8" />
    <path d="M15.6 19.4V4.6M12.2 16l3.4 3.4 3.4-3.4" />
  </Svg>
)

export const IconExternal = (p: IconProps) => (
  <Svg {...p}>
    <path d="M14.2 4.6h5.2v5.2" />
    <path d="M19.4 4.6 11.6 12.4" />
    <path d="M18.4 14v4.2a2 2 0 0 1-2 2H6.6a2 2 0 0 1-2-2V8.4a2 2 0 0 1 2-2H11" />
  </Svg>
)

export const IconLogout = (p: IconProps) => (
  <Svg {...p}>
    <path d="M14.6 4.6h3.2a2 2 0 0 1 2 2v10.8a2 2 0 0 1-2 2h-3.2" />
    <path d="M10 8.2 6 12l4 3.8M6 12h8.6" />
  </Svg>
)

export const IconSun = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="4.1" />
    <path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4M5.6 5.6l1.7 1.7M16.7 16.7l1.7 1.7M18.4 5.6l-1.7 1.7M7.3 16.7l-1.7 1.7" />
  </Svg>
)

export const IconMoon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M20.2 14.4A8.6 8.6 0 0 1 9.6 3.8a8.5 8.5 0 1 0 10.6 10.6Z" />
  </Svg>
)

export const IconInfo = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="8.4" />
    <path d="M12 11v5.4M12 7.9h.01" />
  </Svg>
)

export const IconWarn = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3.6 21 19.4H3l9-15.8Z" />
    <path d="M12 9.4v4.4M12 16.6h.01" />
  </Svg>
)

export const IconCheckCircle = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="8.4" />
    <path d="M8.4 12.2l2.5 2.5 4.7-5" />
  </Svg>
)

export const IconXCircle = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="8.4" />
    <path d="M9.4 9.4l5.2 5.2M14.6 9.4l-5.2 5.2" />
  </Svg>
)

export const IconEye = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2.8 12S6.2 5.8 12 5.8 21.2 12 21.2 12 17.8 18.2 12 18.2 2.8 12 2.8 12Z" />
    <circle cx="12" cy="12" r="3.1" />
  </Svg>
)

export const IconClock = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="8.4" />
    <path d="M12 7.4V12l3.2 2" />
  </Svg>
)

export const IconTag = (p: IconProps) => (
  <Svg {...p}>
    <path d="M11.4 3.8H5.6a1.8 1.8 0 0 0-1.8 1.8v5.8c0 .5.2 1 .53 1.3l7.5 7.5a1.8 1.8 0 0 0 2.55 0l5.6-5.6a1.8 1.8 0 0 0 0-2.55l-7.5-7.5a1.8 1.8 0 0 0-1.28-.55Z" />
    <path d="M8.2 8.2h.01" />
  </Svg>
)

export const IconGrip = (p: IconProps) => (
  <Svg {...p}>
    <path d="M9 6.5h.01M15 6.5h.01M9 12h.01M15 12h.01M9 17.5h.01M15 17.5h.01" strokeWidth={2.2} />
  </Svg>
)

export const IconWifi = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4.2 9.4a11.4 11.4 0 0 1 15.6 0" />
    <path d="M7.4 12.9a7 7 0 0 1 9.2 0" />
    <path d="M10.4 16.2a2.6 2.6 0 0 1 3.2 0" />
    <path d="M12 19.6h.01" strokeWidth={2.2} />
  </Svg>
)

export const IconChart = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4.6 19.4V4.6" />
    <path d="M4.6 19.4h14.8" />
    <path d="M8.6 19.4v-5.2M12.6 19.4v-8.6M16.6 19.4V8.2" />
  </Svg>
)

export const IconUsers = (p: IconProps) => (
  <Svg {...p}>
    <path d="M16.4 20.4v-1.6a3.6 3.6 0 0 0-3.6-3.6H7.2a3.6 3.6 0 0 0-3.6 3.6v1.6" />
    <circle cx="10" cy="8.4" r="3.4" />
    <path d="M20.4 20.4v-1.6a3.6 3.6 0 0 0-2.7-3.47" />
    <path d="M15.4 5.2a3.4 3.4 0 0 1 0 6.4" />
  </Svg>
)

export const IconSend = (p: IconProps) => (
  <Svg {...p}>
    <path d="M20.6 3.4 3.9 10.2l6.5 2.5 2.5 6.5 7.7-15.8Z" />
    <path d="M10.4 12.7l4.3-4.3" />
  </Svg>
)

export const IconSparkle = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3.6l1.7 4.6 4.6 1.7-4.6 1.7L12 16.2l-1.7-4.6L5.7 9.9l4.6-1.7L12 3.6Z" />
    <path d="M18.6 15.4l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8.8-2Z" />
  </Svg>
)

export const IconBell = (p: IconProps) => (
  <Svg {...p}>
    <path d="M18.4 16.4V11a6.4 6.4 0 1 0-12.8 0v5.4l-1.6 2.2h16l-1.6-2.2Z" />
    <path d="M10.2 21a2 2 0 0 0 3.6 0" />
  </Svg>
)

export const IconDatabase = (p: IconProps) => (
  <Svg {...p}>
    <ellipse cx="12" cy="6.2" rx="7.4" ry="2.8" />
    <path d="M4.6 6.2v11.6c0 1.55 3.3 2.8 7.4 2.8s7.4-1.25 7.4-2.8V6.2" />
    <path d="M4.6 12c0 1.55 3.3 2.8 7.4 2.8s7.4-1.25 7.4-2.8" />
  </Svg>
)

export const IconSettings = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="2.9" />
    <path d="M19.2 14.4a1.6 1.6 0 0 0 .32 1.76l.06.06a1.9 1.9 0 1 1-2.7 2.7l-.06-.06a1.6 1.6 0 0 0-1.76-.32 1.6 1.6 0 0 0-.97 1.46v.17a1.9 1.9 0 1 1-3.8 0v-.09a1.6 1.6 0 0 0-1.05-1.46 1.6 1.6 0 0 0-1.76.32l-.06.06a1.9 1.9 0 1 1-2.7-2.7l.06-.06a1.6 1.6 0 0 0 .32-1.76 1.6 1.6 0 0 0-1.46-.97h-.17a1.9 1.9 0 1 1 0-3.8h.09a1.6 1.6 0 0 0 1.46-1.05 1.6 1.6 0 0 0-.32-1.76l-.06-.06a1.9 1.9 0 1 1 2.7-2.7l.06.06a1.6 1.6 0 0 0 1.76.32h.08A1.6 1.6 0 0 0 10 4.03V3.9a1.9 1.9 0 1 1 3.8 0v.09a1.6 1.6 0 0 0 .97 1.46 1.6 1.6 0 0 0 1.76-.32l.06-.06a1.9 1.9 0 1 1 2.7 2.7l-.06.06a1.6 1.6 0 0 0-.32 1.76v.08a1.6 1.6 0 0 0 1.46.97h.17a1.9 1.9 0 1 1 0 3.8h-.09a1.6 1.6 0 0 0-1.46.97Z" />
  </Svg>
)

export const IconLink = (p: IconProps) => (
  <Svg {...p}>
    <path d="M10.2 13.8a4 4 0 0 0 5.7 0l2.6-2.6a4 4 0 0 0-5.7-5.7l-1.3 1.3" />
    <path d="M13.8 10.2a4 4 0 0 0-5.7 0l-2.6 2.6a4 4 0 1 0 5.7 5.7l1.3-1.3" />
  </Svg>
)

export const IconCopy = (p: IconProps) => (
  <Svg {...p}>
    <rect x="8.6" y="8.6" width="11.8" height="11.8" rx="2.2" />
    <path d="M15.4 5.6V4.4a2 2 0 0 0-2-2H5.6a2 2 0 0 0-2 2v7.8a2 2 0 0 0 2 2h1.2" />
  </Svg>
)

export const IconQr = (p: IconProps) => (
  <Svg {...p}>
    <rect x="3.6" y="3.6" width="6.4" height="6.4" rx="1.4" />
    <rect x="14" y="3.6" width="6.4" height="6.4" rx="1.4" />
    <rect x="3.6" y="14" width="6.4" height="6.4" rx="1.4" />
    <path d="M14 14h2.6v2.6H14zM18.6 18.6h1.8v1.8h-1.8zM14 20.4h1.8M20.4 14v1.8" />
  </Svg>
)

/** 品牌标记：极简鱼形 + 渐变，替代原来的鱼形 emoji（全站零 emoji）。 */
export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id="xywb-logo-g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#38bdf8" />
          <stop offset="100%" stopColor="#6366f1" />
        </linearGradient>
      </defs>
      <rect x="0" y="0" width="32" height="32" rx="9" fill="url(#xywb-logo-g)" />
      <path
        d="M7 16.4c3.1-4 6.6-6 10.4-6 3.4 0 6.4 1.7 8.6 5-2.2 3.3-5.2 5-8.6 5-3.8 0-7.3-2-10.4-4Z"
        fill="rgba(255,255,255,.92)"
      />
      <circle cx="20.2" cy="15.2" r="1.25" fill="#1e3a8a" />
      <path d="M7 16.4 4.2 13.6v5.6L7 16.4Z" fill="rgba(255,255,255,.92)" />
    </svg>
  )
}

/** 所有图标的登记表，便于按名字查找/自检覆盖度。 */
export const ICONS = {
  overview: IconOverview,
  user: IconUser,
  box: IconBox,
  chat: IconChat,
  agents: IconAgents,
  master: IconMaster,
  key: IconKey,
  prompts: IconPrompts,
  shield: IconShield,
  refresh: IconRefresh,
  plus: IconPlus,
  edit: IconEdit,
  trash: IconTrash,
  upload: IconUpload,
  download: IconDownload,
  image: IconImage,
  close: IconClose,
  check: IconCheck,
  chevronDown: IconChevronDown,
  chevronRight: IconChevronRight,
  chevronLeft: IconChevronLeft,
  arrowRight: IconArrowRight,
  search: IconSearch,
  filter: IconFilter,
  sort: IconSort,
  external: IconExternal,
  logout: IconLogout,
  sun: IconSun,
  moon: IconMoon,
  info: IconInfo,
  warn: IconWarn,
  checkCircle: IconCheckCircle,
  xCircle: IconXCircle,
  eye: IconEye,
  clock: IconClock,
  tag: IconTag,
  grip: IconGrip,
  wifi: IconWifi,
  chart: IconChart,
  users: IconUsers,
  send: IconSend,
  sparkle: IconSparkle,
  bell: IconBell,
  database: IconDatabase,
  settings: IconSettings,
  link: IconLink,
  copy: IconCopy,
  qr: IconQr,
} as const

export type IconName = keyof typeof ICONS
