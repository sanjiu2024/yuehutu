/**
 * 交易 → **投诉**（列表只读；驳回 / 举证在页内做，要二次确认）。
 * 契约 `docs/contracts/trade.md` §2.5、写接口 `docs/contracts/trade_write.md`。
 *
 * 这一页和前面几页有个本质区别，先写清楚免得后面踩坑：
 *  1. **没有真机样本** —— 真机上平台回 `NO_PERMISSION::用户没有权限` → 后端 502 `protocol_error`。
 *     所以元素是**原样透传**，字段长什么样契约没有保证，下面全部走防御式取值。
 *  2. 因为读不到数据，这里**绝不能**用空列表假装「没有投诉」：拿不到就是错误态，照实显示。
 *  3. 那个 ret **不写 `need_relogin`**，所以「去重新扫码」是错误引导 —— 这不是掉线。
 *
 * 第 41 批（task-94）在这一页加了「联系纠纷小二」面板：纠纷协商留言 / 发图举证 / 催办 / 标记已读。
 * 面板的读接口是 `/api/disputes/*`（契约见任务卡），**写操作全部走 `runDisputeWrite` 唯一出口**。
 */

import { useState } from 'react'
import { api, errorText } from '../api'
import { Badge, ConfirmDialog, Field, Loading, Modal, Notice, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import {
  IconChat,
  IconCheckCircle,
  IconClock,
  IconImage,
  IconSend,
  IconShield,
} from '../icons'
import './complaints.css'
import {
  AccountBar,
  TradeHead,
  TradeListView,
  TradeWritePanel,
  dayOf,
  ellipsis,
  isNeedRelogin,
  isPermissionDenied,
  money,
  num,
  useTradeAccounts,
  when,
  type TradeColumn,
  type TradeEnvelope,
  type WriteDraft,
} from './Orders'

/**
 * 平台原样透传，没有冻结的字段表 —— 用索引签名而不是编一套 interface。
 * 想读的字段一律经 `pick()` / 本地 `read()`，读不到给 `null`，渲染成 `-`。
 */
type RawComplaint = Record<string, unknown>

interface ComplaintsResult extends TradeEnvelope<RawComplaint> {
  complaints?: RawComplaint[]
  /** 状态计数，平台给什么键就是什么键，不猜中文。 */
  status_count_map?: Record<string, number | null> | null
}

function read(row: RawComplaint, ...keys: string[]): unknown {
  for (const key of keys) {
    const value = row[key]
    if (value !== undefined && value !== null && value !== '') return value
  }
  return null
}

function toneForStatus(status: string): 'ok' | 'warn' | 'danger' | 'muted' {
  if (status.includes('完成') || status.includes('关闭') || status.includes('结束')) return 'ok'
  if (status.includes('处理') || status.includes('进行') || status.includes('待')) return 'warn'
  if (status.includes('失败') || status.includes('拒绝') || status.includes('超时')) return 'danger'
  return 'muted'
}

/* ---------------- 纠纷会话（task-94，契约已冻结） ---------------- */

interface DisputeConversation {
  account_id: string
  conversation_id: string | null
  found: boolean
  source?: string | null
  fetched_at?: string | null
  warnings?: string[]
}

interface DisputeImage {
  url?: string | null
  ext?: string | null
  alt_text?: string | null
  width?: number | null
  height?: number | null
  thumb_url?: string | null
}

interface DisputeMessage {
  msg_id?: string | number | null
  sequence?: number | string | null
  /** `'USER'` = 自己（官方就是拿这个判「你」）。 */
  sender_role?: string | null
  sender_name?: string | null
  sender_avatar?: string | null
  gmt_create?: unknown
  tips?: string | null
  content_type?: string | null
  text?: string | null
  image?: DisputeImage | null
  raw?: unknown
}

interface DisputeMessagesResult {
  messages?: DisputeMessage[] | null
  current_time?: unknown
  has_more?: boolean | null
  count?: number | null
  source?: string | null
  fetched_at?: string | null
  warnings?: string[]
}

interface DisputeUnreadResult {
  messages?: DisputeMessage[] | null
  count?: number | null
}

interface DisputeWriteResult {
  action?: string
  saved?: boolean
  msg_id?: string | number | null
  sequence?: number | string | null
  tips?: string | null
  warnings?: string[]
}

/** 打开面板要带上的三件套：三个 id 至少有一个非空，否则后端找不到会话。 */
interface DisputeTarget {
  orderId: string
  refundId: string
  disputeId: string
  /** 展示用（工单号），不参与请求。 */
  label: string
}

function idOf(value: unknown): string {
  if (value === null || value === undefined) return ''
  return String(value).trim()
}

/**
 * 投诉行的字段表**没有冻结**（见文件头第 1 条），所以这里和列表一样走防御式取值：
 * 三个 id 全拿不到就禁用按钮并说明原因，而不是拿一个空 id 去请求。
 */
function disputeRefs(row: RawComplaint): DisputeTarget {
  const orderId = idOf(read(row, 'order_id', 'oid'))
  const refundId = idOf(read(row, 'refund_id', 'refund_no', 'rid'))
  const disputeId = idOf(read(row, 'dispute_id', 'complaint_id', 'complain_id', 'case_id', 'id'))
  return { orderId, refundId, disputeId, label: disputeId || orderId }
}

function isMine(message: DisputeMessage): boolean {
  return String(message.sender_role || '').toUpperCase() === 'USER'
}

/** 对方名字：会话响应里没有对方昵称字段，只能从消息里倒着找最后一条非自己的名字。 */
function peerOf(messages: DisputeMessage[] | null): string {
  if (!messages) return '-'
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i]
    if (!message || isMine(message)) continue
    const name = String(message.sender_name || '').trim()
    if (name) return name
  }
  return '-'
}

function Warnings({ list }: { list?: string[] }) {
  if (!Array.isArray(list) || list.length === 0) return null
  return (
    <>
      {list.map((text, index) => (
        <span className="cp-warn" key={`${index}-${text}`}>平台提示：{text}</span>
      ))}
    </>
  )
}

export function ComplaintsPage() {
  const { accounts, picked, setPicked, accountId } = useTradeAccounts()
  const [page, setPage] = useState(1)
  const [write, setWrite] = useState<WriteDraft | null>(null)
  const [talk, setTalk] = useState<DisputeTarget | null>(null)
  const pageSize = 10

  const state = useAsync<ComplaintsResult>(
    () => api.get<ComplaintsResult>(`/api/trade/${encodeURIComponent(accountId)}/complaints`, { page, size: pageSize }),
    [accountId, page],
    Boolean(accountId),
    ['accounts.changed'],
  )

  const data = state.data
  const rows = data?.complaints ?? []
  const permission = isPermissionDenied(state.error)
  const relogin = isNeedRelogin(state.error)
  const counts = data?.status_count_map && typeof data.status_count_map === 'object'
    ? Object.entries(data.status_count_map)
    : []

  const columns: Array<TradeColumn<RawComplaint>> = [
    {
      key: 'complaint_id',
      label: '投诉单',
      sortValue: (row) => {
        const value = read(row, 'complaint_id', 'id', 'case_id', 'cid')
        const n = Number(value)
        return Number.isFinite(n) ? n : value === null ? null : String(value)
      },
      render: (row) => {
        const id = read(row, 'complaint_id', 'id', 'case_id', 'cid')
        const order = read(row, 'order_id', 'oid')
        return (
          <div className="tr-cell">
            <span className="tr-mono">{money(id)}</span>
            <span className="tr-sub">订单 {money(order)}</span>
          </div>
        )
      },
    },
    {
      key: 'status',
      label: '状态',
      sortValue: (row) => String(read(row, 'status_text', 'status', 'complaint_status') ?? ''),
      render: (row) => {
        const text = String(read(row, 'status_text', 'status', 'complaint_status') ?? '')
        return (
          <div className="tr-cell">
            <Badge tone={text ? toneForStatus(text) : 'muted'}>{text || '-'}</Badge>
            <span className="tr-sub">状态码 {money(read(row, 'status_code', 'state'))}</span>
          </div>
        )
      },
    },
    {
      key: 'item',
      label: '商品 / 对方',
      sortValue: (row) => String(read(row, 'item_title', 'title', 'item_name') ?? ''),
      render: (row) => (
        <div className="tr-cell">
          <span className="tr-name" title={String(read(row, 'item_title', 'title', 'item_name') ?? '')}>
            {ellipsis(String(read(row, 'item_title', 'title', 'item_name') ?? ''), 26) || '-'}
          </span>
          <span className="tr-sub">{money(read(row, 'buyer_nick', 'other_nick', 'nick'))}</span>
        </div>
      ),
    },
    {
      key: 'reason',
      label: '原因',
      sortValue: (row) => String(read(row, 'reason', 'complaint_reason', 'type', 'reason_text') ?? ''),
      render: (row) => {
        const text = String(read(row, 'reason', 'complaint_reason', 'type', 'reason_text') ?? '')
        return <span className="tr-name" title={text}>{text || '-'}</span>
      },
    },
    {
      key: 'money',
      label: '金额（元）',
      num: true,
      sortValue: (row) => {
        const value = read(row, 'amount', 'money', 'complaint_amount', 'refund_fee')
        if (value === null) return null
        const n = Number(value)
        return Number.isFinite(n) ? n : null
      },
      render: (row) => (
        <span className="tr-money">{money(read(row, 'amount', 'money', 'complaint_amount', 'refund_fee'))}</span>
      ),
    },
    {
      key: 'time',
      label: '时间',
      sortValue: (row) => {
        const n = Number(read(row, 'gmt_create', 'create_time', 'gmt_modified', 'apply_time'))
        return Number.isFinite(n) && n > 0 ? n : null
      },
      render: (row) => <span>{when(read(row, 'gmt_create', 'create_time', 'gmt_modified', 'apply_time'))}</span>,
    },
    {
      key: 'write',
      label: '处理',
      render: (row) => {
        const complainId = String(read(row, 'complaint_id', 'complain_id', 'id') ?? '')
        const refs = disputeRefs(row)
        const hasRef = Boolean(refs.orderId || refs.refundId || refs.disputeId)
        return (
          <span className="tr-writes" onClick={(event) => event.stopPropagation()}>
            <button
              className="btn sm danger"
              disabled={!accountId || !complainId}
              title={complainId
                ? '驳回 / 举证：会真的改动平台数据，要二次确认'
                : '这一行没有投诉单号，处理不了'}
              onClick={() => setWrite({ kind: 'complain-refuse', complainId })}
            >
              处理投诉
            </button>
            <button
              className="btn sm"
              disabled={!accountId || !hasRef}
              title={hasRef
                ? '纠纷协商留言 / 发图举证 / 催办 / 标记已读，写操作都要二次确认'
                : '这一行没有订单号 / 退款号 / 纠纷号，找不到纠纷会话'}
              onClick={() => setTalk(refs)}
            >
              <IconChat size={13} /> 联系小二
            </button>
          </span>
        )
      },
    },
  ]

  return (
    <>
      <TradeHead
        icon={<IconShield size={18} />}
        title="投诉"
        desc={
          <>
            买家投诉工单 · 列表只读；驳回 / 举证在每行的「处理投诉」里做，要二次确认
            {data?.fetched_at ? ` · 更新于 ${when(data.fetched_at)}` : ''}
            {data?.api ? ` · 平台接口 ${data.api}` : ''}
          </>
        }
        onRefresh={state.reload}
        loading={state.loading}
      />

      <AccountBar accounts={accounts} picked={picked} setPicked={(id) => { setPicked(id); setPage(1) }} />

      {!accountId && <Notice kind="info">先在上方选一个账号，没有账号就没投诉工单可看。</Notice>}

      {permission && (
        <Notice kind="warn">
          平台回了 <code>NO_PERMISSION::用户没有权限</code>，这个账号读不到投诉工单。
          这是平台给的业务结果，<strong>不是掉线、不用重新扫码</strong> ——
          要能读得换一个有卖家工作台会话的账号。下面照实显示错误，不会拿空列表冒充「没有投诉」。
        </Notice>
      )}
      {relogin && !permission && (
        <Notice kind="warn">
          这个账号的登录态过期了，平台不让查投诉 —— 这不是接口坏掉。
          <a className="btn sm" href="#/accounts" style={{ marginLeft: 8 }}>去账号页重新扫码</a>
        </Notice>
      )}

      {counts.length > 0 && (
        <section className="card tr-toolbar">
          <div className="tr-tabs">
            {counts.map(([key, value]) => (
              <div className="tr-tab" key={key} title={`平台原样返回的状态键 ${key}，不擅自翻译成中文`}>
                <span>{key}</span>
                <b>{num(value)}</b>
              </div>
            ))}
          </div>
        </section>
      )}

      <TradeListView<RawComplaint>
        rows={rows}
        columns={columns}
        loading={state.loading}
        error={state.error}
        notReady={state.notReady}
        onRetry={state.reload}
        rowKey={(row, index) => String(read(row, 'complaint_id', 'id', 'case_id') ?? index)}
        emptyTitle="这个账号还没有投诉工单"
        emptyDesc="平台一条投诉都没返回。拿不到的金额一律显示 -，不会用 0 冒充。"
        detail={(row) => <ComplaintDetail row={row} />}
        statusFilter={{
          label: '状态',
          options: Array.from(new Set(rows
            .map((row) => String(read(row, 'status_text', 'status', 'complaint_status') ?? '')).filter(Boolean))),
          value: (row) => String(read(row, 'status_text', 'status', 'complaint_status') ?? ''),
        }}
        keywordOf={(row) => Object.values(row)
          .filter((value) => typeof value === 'string')
          .slice(0, 12)
          .join(' ')}
        dateOf={(row) => dayOf(read(row, 'gmt_create', 'create_time', 'gmt_modified'))}
        page={page}
        pageSize={pageSize}
        total={data?.paging?.total_count ?? null}
        hasPrev={page > 1}
        hasNext={Number(data?.paging?.next_page ?? 0) > page}
        onPage={setPage}
      />

      {write && (
        <TradeWritePanel
          accountId={accountId}
          draft={write}
          onClose={() => setWrite(null)}
          onChanged={state.reload}
        />
      )}

      {talk && accountId && (
        <DisputeTalkPanel accountId={accountId} target={talk} onClose={() => setTalk(null)} />
      )}
    </>
  )
}

/* ---------------- 「联系纠纷小二」面板（task-94） ---------------- */

/**
 * 面板里四个写操作的待确认项。
 * **必须二次确认**：这四个按钮点下去就是一条真到平台的写请求，页面文案（列表里的入口提示）
 * 也写着「写操作都要二次确认」，所以确认这一步不能省。
 */
interface DisputePending {
  kind: 'text' | 'image' | 'urge' | 'markRead'
  title: string
  message: string
  ok: string
}

function DisputeTalkPanel({
  accountId,
  target,
  onClose,
}: {
  accountId: string
  target: DisputeTarget
  onClose: () => void
}) {
  const toast = useToast()
  const [text, setText] = useState('')
  const [imageUrl, setImageUrl] = useState('')
  /** 正在进行的写操作 path；空串 = 空闲。所有写按钮共用它做禁用。 */
  const [busy, setBusy] = useState('')
  /** 待确认的写操作；非空时弹确认框，用户点「确认」才真的发。 */
  const [pending, setPending] = useState<DisputePending | null>(null)

  // ① 先拿纠纷会话：found:false 就是「这个订单还没有纠纷会话」，不要拿空消息列表冒充
  const conversation = useAsync<DisputeConversation>(
    () => api.get<DisputeConversation>(`/api/disputes/${encodeURIComponent(accountId)}/conversation`, {
      order_id: target.orderId || undefined,
      refund_id: target.refundId || undefined,
      dispute_id: target.disputeId || undefined,
    }),
    [accountId, target.orderId, target.refundId, target.disputeId],
    true,
    ['disputes.changed'],
  )

  const conversationId = idOf(conversation.data?.conversation_id)
  const found = conversation.data?.found === true

  // ② 拿到 conversation_id 才拉消息；两个只读接口都订阅 `disputes.changed` 静默重拉（不轮询）
  const stream = useAsync<DisputeMessagesResult>(
    () => api.get<DisputeMessagesResult>(
      `/api/disputes/${encodeURIComponent(accountId)}/${encodeURIComponent(conversationId)}/messages`,
    ),
    [accountId, conversationId],
    Boolean(conversationId),
    ['disputes.changed'],
  )

  const unread = useAsync<DisputeUnreadResult>(
    () => api.get<DisputeUnreadResult>(
      `/api/disputes/${encodeURIComponent(accountId)}/${encodeURIComponent(conversationId)}/unread`,
    ),
    [accountId, conversationId],
    Boolean(conversationId),
    ['disputes.changed'],
  )

  /**
   * 三态**必须分开**（后端没给 `messages` 字段 / 不是数组 = 没拿到）：
   *   `null` → 没拿到，照实说；`[]` → 平台真的没有；有数据 → 渲染。
   */
  const messages: DisputeMessage[] | null = Array.isArray(stream.data?.messages)
    ? stream.data.messages
    : null
  const last = messages && messages.length > 0 ? messages[messages.length - 1] : null
  /**
   * 「标记已读」的目标：优先用未读接口回的最后一条未读消息（那才是语义上要标的），
   * 未读接口没给消息就退回消息流里的最后一条。
   */
  const unreadList = Array.isArray(unread.data?.messages) ? unread.data.messages : null
  const markTarget = unreadList && unreadList.length > 0 ? unreadList[unreadList.length - 1] : last
  const readDone = !stream.loading && !stream.notReady && !stream.error

  const base = `/api/disputes/${encodeURIComponent(accountId)}/${encodeURIComponent(conversationId)}`
  const locked = !conversationId || busy !== ''

  /**
   * 面板里写操作的**唯一出口**（照 `Chats.tsx::runImWrite`）：
   * ① 一律 `confirm: true`（后端只认显式确认，字符串 `'false'` 会被判成没确认）；
   * ② `saved === false` 严格判等，不拿 `Boolean(...)` 猜；
   * ③ 失败原样把后端的中文原因弹出来 —— 平台没权限 ≠ 掉线，不能笼统说「请重新登录」。
   */
  const runDisputeWrite = async (
    path: string,
    body: Record<string, unknown>,
    okText: string,
  ): Promise<boolean> => {
    setBusy(path)
    try {
      const result = await api.post<DisputeWriteResult>(path, { ...body, confirm: true })
      const saved = !(result && result.saved === false)
      if (saved) {
        toast.success(result?.tips ? String(result.tips) : okText)
      } else {
        toast.error((result?.warnings || [])[0] || '平台没有确认这次操作')
      }
      conversation.reload()
      stream.reload()
      unread.reload()
      return saved
    } catch (err) {
      toast.error(errorText(err))
      return false
    } finally {
      setBusy('')
    }
  }

  const askSendText = () => {
    const body = text.trim()
    if (!body) {
      toast.info('先写点内容再发')
      return
    }
    setPending({
      kind: 'text',
      title: '确认给纠纷小二留言',
      message: `这条留言会真的发到闲鱼平台：\n\n${body}`,
      ok: '发送留言',
    })
  }

  const askSendImage = () => {
    const url = imageUrl.trim()
    if (!url) {
      toast.info('先填图片 URL 再发')
      return
    }
    setPending({
      kind: 'image',
      title: '确认发图举证',
      message: `会把这张图片链接发给纠纷小二（平台只收链接，本机不上传文件）：\n\n${url}`,
      ok: '发送图片',
    })
  }

  const askUrge = () => {
    setPending({
      kind: 'urge',
      title: '确认催办',
      message: '会给纠纷小二发一条「催促处理维权」，会真的发到闲鱼平台。',
      ok: '催办',
    })
  }

  /** 标记已读要 `{msg_id, sequence}`：拿最后一条（未读优先）；平台没给 msg_id 就直说标不了。 */
  const askMarkRead = () => {
    if (!markTarget) {
      toast.info('还没有消息，没有可标记的已读')
      return
    }
    const msgId = markTarget.msg_id
    if (idOf(msgId) === '') {
      toast.error('这条消息平台没有给 msg_id，标不了已读')
      return
    }
    setPending({
      kind: 'markRead',
      title: '确认标记已读',
      message: `会把消息 ${idOf(msgId)}（序号 ${num(markTarget.sequence)}）在平台上标记为已读。`,
      ok: '标记已读',
    })
  }

  /** 确认框点「确认」后才走到这里 —— 仍然是唯一写出口 `runDisputeWrite`。 */
  const confirmPending = () => {
    const item = pending
    if (!item) return
    setPending(null)
    if (item.kind === 'text') {
      const body = text.trim()
      void runDisputeWrite(`${base}/messages`, { text: body }, '留言已发出').then((saved) => {
        if (saved) setText('')
      })
      return
    }
    if (item.kind === 'image') {
      const url = imageUrl.trim()
      void runDisputeWrite(`${base}/messages/image`, { url }, '图片已发出').then((saved) => {
        if (saved) setImageUrl('')
      })
      return
    }
    if (item.kind === 'urge') {
      void runDisputeWrite(`${base}/urge`, {}, '已催办')
      return
    }
    const msgId = markTarget?.msg_id
    const body: Record<string, unknown> = { msg_id: msgId }
    const sequence = markTarget?.sequence
    if (sequence !== null && sequence !== undefined && sequence !== '') {
      body.sequence = sequence
    }
    void runDisputeWrite(`${base}/mark-read`, body, '已标记已读')
  }

  return (
    <Modal
      wide
      title={<span className="cp-title">联系纠纷小二{target.label ? ` · 工单 ${target.label}` : ''}</span>}
      onClose={onClose}
    >
      <div className="cp-panel">
        <div className="cp-head">
          <div className="cp-head-row">
            <IconChat size={15} />
            <span className="cp-sub">会话 id</span>
            <span className="cp-mono">{conversationId || '-'}</span>
            <span className="cp-grow" />
            <Badge tone={unread.data && Number(unread.data.count) > 0 ? 'warn' : 'muted'}>
              未读 {num(unread.data?.count)}
            </Badge>
          </div>
          <div className="cp-head-row">
            <span className="cp-sub">对方</span>
            <span>{peerOf(messages)}</span>
            {stream.data?.source ? <span className="cp-sub">来源 {stream.data.source}</span> : null}
            {stream.data?.fetched_at ? <span className="cp-sub">更新于 {when(stream.data.fetched_at)}</span> : null}
          </div>
          {unread.error ? <span className="cp-warn">未读条数没拿到：{unread.error}</span> : null}
          <Warnings list={conversation.data?.warnings} />
          <Warnings list={stream.data?.warnings} />
        </div>

        {conversation.loading && <Loading text="正在找这个订单的纠纷会话…" />}

        {!conversation.loading && conversation.notReady && (
          <Notice kind="info">纠纷会话接口还没就绪（后端未实现或服务没起），这里不假装「没有纠纷会话」。</Notice>
        )}

        {!conversation.loading && !conversation.notReady && conversation.error && (
          <Notice kind="warn">
            纠纷会话没拿到：{conversation.error}
            <button className="btn sm" style={{ marginLeft: 8 }} onClick={conversation.reload}>重试</button>
          </Notice>
        )}

        {!conversation.loading && !conversation.notReady && !conversation.error && !found && (
          <Notice kind="info">
            这个订单还没有纠纷会话（后端回 <code>found: false</code>）——
            没有会话就没法给小二留言，这里不会拿空消息列表冒充。
          </Notice>
        )}

        {found && (
          <>
            <div className="cp-stream">
              {stream.loading && <Loading text="正在拉纠纷消息…" />}

              {!stream.loading && stream.notReady && (
                <Notice kind="info">消息接口还没就绪（后端未实现或服务没起）。</Notice>
              )}

              {readDone && stream.error && (
                <Notice kind="warn">
                  纠纷消息没拿到：{stream.error}
                  <button className="btn sm" style={{ marginLeft: 8 }} onClick={stream.reload}>重试</button>
                </Notice>
              )}

              {readDone && messages === null && (
                <div className="cp-empty">
                  {conversationId
                    ? <>消息列表没拿到（响应里没有 <code>messages</code> 字段）—— 照实说，不拿空列表冒充「没有消息」。</>
                    : <>后端说找到了纠纷会话，却没给 <code>conversation_id</code>，没法拉消息。</>}
                </div>
              )}

              {readDone && messages !== null && messages.length === 0 && (
                <div className="cp-empty">这个纠纷会话里一条消息都没有。</div>
              )}

              {messages !== null && messages.map((message, index) => (
                <DisputeMessageRow
                  message={message}
                  key={String(message.msg_id ?? `${index}-${String(message.sequence ?? '')}`)}
                />
              ))}
            </div>

            <div className="cp-sub">
              本页 {num(stream.data?.count)} 条 ·
              {' '}还有更多：{stream.data?.has_more === true ? '有' : stream.data?.has_more === false ? '没有' : '-'}
              {stream.data?.current_time !== undefined && stream.data?.current_time !== null
                ? ` · 平台时间 ${when(stream.data.current_time)}`
                : ''}
            </div>

            <div className="cp-foot">
              <textarea
                className="cp-textarea"
                value={text}
                disabled={locked}
                placeholder="给纠纷小二留言（会真的发到平台，写操作都带 confirm: true）"
                onChange={(event) => setText(event.target.value)}
              />
              <div className="cp-actions">
                <button className="btn sm" disabled={locked} onClick={askSendText}>
                  <IconSend size={13} /> {busy.endsWith('/messages') ? '发送中…' : '发送'}
                </button>
                <button className="btn sm" disabled={locked || !markTarget} onClick={askMarkRead} title={markTarget ? '把最后一条（优先未读）消息标记为已读' : '还没有消息可标记'}>
                  <IconCheckCircle size={13} /> 标记已读
                </button>
                <button className="btn sm" disabled={locked} onClick={askUrge} title="催办：请小二尽快处理">
                  <IconClock size={13} /> 催办
                </button>
                <span className="cp-grow" />
                <span className="cp-sub">
                  待标记 msg_id {money(markTarget?.msg_id)} · 序号 {num(markTarget?.sequence)}
                </span>
              </div>
              <div className="cp-img-row">
                <input
                  className="cp-input"
                  value={imageUrl}
                  disabled={locked}
                  placeholder="图片 URL（发图举证；平台只收链接，本机不上传文件）"
                  onChange={(event) => setImageUrl(event.target.value)}
                />
                <button className="btn sm" disabled={locked} onClick={askSendImage}>
                  <IconImage size={13} /> {busy.endsWith('/messages/image') ? '发送中…' : '发送图片'}
                </button>
              </div>
              <span className="cp-sub">
                写操作都要先在确认框里过一道；确认后才带 <code>confirm: true</code> 发出去，
                平台拒绝的原因原样显示，不笼统说「请重新登录」。
              </span>
            </div>
          </>
        )}
      </div>
      {pending ? (
        <ConfirmDialog
          title={pending.title}
          message={<span style={{ whiteSpace: 'pre-wrap' }}>{pending.message}</span>}
          confirmText={pending.ok}
          busy={busy !== ''}
          onConfirm={confirmPending}
          onCancel={() => setPending(null)}
        />
      ) : null}
    </Modal>
  )
}

/** 一条纠纷消息：自己的靠右、对方靠左；拿不到的字段显示 `-`，不补 0。 */
function DisputeMessageRow({ message }: { message: DisputeMessage }) {
  const mine = isMine(message)
  const image = message.image || null
  const url = idOf(image?.url)
  const name = String(message.sender_name || '').trim()
  const altText = String(image?.alt_text || '').trim()
  const content = message.text === null || message.text === undefined ? '' : String(message.text)
  return (
    <div className={`cp-msg${mine ? ' mine' : ''}`}>
      {idOf(message.sender_avatar)
        ? <img className="cp-avatar" src={String(message.sender_avatar)} alt={name || '头像'} loading="lazy" />
        : <span className="cp-avatar-fallback" title={name || '平台没有给头像'}>{(name || (mine ? '我' : '对')).slice(0, 1)}</span>}
      <div className="cp-body">
        <span className="cp-name">
          {mine ? '我' : (name || '对方')}
          <span className="cp-meta">{when(message.gmt_create)}</span>
        </span>
        <div className="cp-bubble">
          {url ? <img className="cp-img" src={url} alt={altText || '纠纷举证图片'} loading="lazy" /> : null}
          {content ? <span>{content}</span> : (url ? null : <span className="cp-sub">内容：-</span>)}
        </div>
        {message.tips ? <span className="cp-tips">平台提示：{String(message.tips)}</span> : null}
        <span className="cp-meta">
          msg_id {money(message.msg_id)} · 序号 {num(message.sequence)} · 类型 {money(message.content_type)}
        </span>
      </div>
    </div>
  )
}

function ComplaintDetail({ row }: { row: RawComplaint }) {
  let raw = ''
  try {
    raw = JSON.stringify(row, null, 2)
  } catch {
    raw = ''
  }
  return (
    <div className="tr-detail">
      <div className="tr-detail-grid">
        <Field label="已识别的字段">
          <span>投诉单 <b>{money(read(row, 'complaint_id', 'id', 'case_id'))}</b></span>
          <span className="tr-sub">订单 {money(read(row, 'order_id', 'oid'))}</span>
          <span className="tr-sub">状态 {money(read(row, 'status_text', 'status', 'complaint_status'))}</span>
          <span className="tr-sub">时间 {when(read(row, 'gmt_create', 'create_time', 'gmt_modified'))}</span>
        </Field>
        <Field label="为什么这里显示原始 JSON" tip="">
          <span className="tr-sub">
            这个接口没有真机样本，契约只写了「元素原样透传」，字段表没冻结。
            下面是平台原样回给你的内容 —— 不替你猜字段名，也不删字段。
          </span>
        </Field>
      </div>
      <pre className="tr-raw">{raw || '平台没有返回任何内容'}</pre>
    </div>
  )
}
