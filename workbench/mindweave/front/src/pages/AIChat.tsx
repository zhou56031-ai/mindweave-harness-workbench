import { useState, useRef, useEffect, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Send, Bot, User, Loader2, Activity, X } from 'lucide-react'
import BrandMark from '../components/common/BrandMark'
import ReactMarkdown from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import rehypeRaw from 'rehype-raw'
import remarkGfm from 'remark-gfm'
import { useSSE } from '../hooks/useSSE'
import { sessionsApi } from '../api/sessions'
import { useThemeStore } from '../stores/useThemeStore'
import { endpoints } from '../api/endpoints'
import type { ApprovalPayload, SSEMessage, SourceItem, UsageInfo } from '../types/api'
import { isEvidenceEvent, isRetrievalStage, isVisibleThinkingStage, mergeThinkingStep, toEvidence } from '../utils/thinkingTrace'
import type { ThinkingStep } from '../utils/thinkingTrace'

const STAGE_META: Record<string, { label: string; dot: string }> = {
  agentic_plan: { label: '规划', dot: 'bg-sky-500' },
  local_retrieval: { label: '检索', dot: 'bg-blue-500' },
  web_search: { label: '联网检索', dot: 'bg-cyan-500' },
  supplemental_retrieval: { label: '补充检索', dot: 'bg-cyan-400' },
  tool_start: { label: '调用工具', dot: 'bg-emerald-500' },
  tool_end: { label: '工具返回', dot: 'bg-emerald-400' },
  tool_execution: { label: '工具执行', dot: 'bg-emerald-500' },
  answerability: { label: '资料研判', dot: 'bg-amber-500' },
  evidence_fusion: { label: '证据整理', dot: 'bg-violet-500' },
}

function stageMeta(stage: string) {
  return STAGE_META[stage] ?? { label: stage, dot: 'bg-[var(--color-text-tertiary)]' }
}

interface Message {
  role: 'user' | 'assistant'
  content: string
  thinking?: string
  steps?: string[]
  sources?: SourceItem[]
  usage?: UsageInfo
}

function mergeSources(previous: SourceItem[], next?: SourceItem[]): SourceItem[] {
  if (!next?.length) return previous
  const map = new Map(previous.map((item) => [item.id, item]))
  for (const item of next) {
    if (item?.id) map.set(item.id, item)
  }
  return [...map.values()]
}

const quickQuestions = [
  '帮我解释一下量子计算',
  '写一首关于春天的诗',
  '推荐几本提升思维的书',
]

const evidenceSourceLabels: Record<string, string> = {
  note: '笔记',
  knowledge_base: '知识库',
  graph: '知识图谱',
  web: 'Web',
  web_search: 'Web search',
}


const approvalToolLabels: Record<string, string> = {
  create_note: '创建笔记',
  create_note_tool: '创建笔记',
  mark_reviewed: '完成回顾',
}

const APPROVAL_PREVIEW_LIMIT = 240
const APPROVAL_ARG_LIMIT = 200

function truncateText(text: string, limit: number): string {
  if (text.length <= limit) return text
  return `${text.slice(0, limit)}…`
}

function stripMarkdown(text: string): string {
  return text
    .replace(/```[^\n]*\n?/g, '')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^>\s?/gm, '')
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+/gm, '')
    .replace(/(\*\*|__)(.*?)\1/g, '$2')
    .replace(/^---+\s*$/gm, '')
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '')
    .join('\n')
}

export default function AIChat() {
  const { sessionId } = useParams()
  const navigate = useNavigate()
  const { t } = useTranslation()
  const theme = useThemeStore((s) => s.theme)
  const { start, loading } = useSSE()
  const [pendingApproval, setPendingApproval] = useState<{
    runId: string
    payload: ApprovalPayload
    sessionId: string | null
  } | null>(null)
  const [approveSubmitting, setApproveSubmitting] = useState(false)
  const [approvalNotice, setApprovalNotice] = useState<string | null>(null)
  const [rejectReason, setRejectReason] = useState('')
  const [input, setInput] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [currentThinking, setCurrentThinking] = useState('')
  const [thinkingSteps, setThinkingSteps] = useState<ThinkingStep[]>([])
  const [showTrace, setShowTrace] = useState(false)
  const [activeSource, setActiveSource] = useState<SourceItem | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const traceEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const lastQueryRef = useRef('')
  const [expandedApproval, setExpandedApproval] = useState<Record<string, boolean>>({})
  const [, setShowThinking] = useState(true)
  const [loadingHistory, setLoadingHistory] = useState(false)
  // 会话切换时在 render 阶段同步重置审批与历史加载态（与原 effect 语义一致，避免 effect 内同步 setState）
  const [prevSessionId, setPrevSessionId] = useState<string | undefined | null>(null)
  if (sessionId !== prevSessionId) {
    setPrevSessionId(sessionId)
    setPendingApproval(null)
    setApprovalNotice(null)
    setRejectReason('')
    setLoadingHistory(!!sessionId)
  }
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef('')
  const rafRef = useRef<number | null>(null)
  const pendingThinkingRef = useRef<ThinkingStep[]>([])
  const sourcesRef = useRef<SourceItem[]>([])
  const usageRef = useRef<UsageInfo | undefined>(undefined)
  const thinkingTimerRef = useRef<number | null>(null)
  const thinkingGenerationRef = useRef(0)
  const thinkingManuallyCollapsedRef = useRef(false)
  const thinkingTerminalGenerationRef = useRef<number | null>(null)
  // 流式完成后跳转会话会触发历史加载；跳过这一次，避免覆盖 sources/usage。
  const skipHistoryRef = useRef<string | null>(null)

  const logicalThinkingKey = (step: ThinkingStep) => {
    if (step.stage !== 'supplemental_retrieval') return step.stage
    const query = step.details?.query
    return `${step.stage}:${typeof query === 'string' ? query : ''}`
  }

  const mergeIncomingThinkingStep = (steps: ThinkingStep[], next: ThinkingStep) => {
    const nextKey = logicalThinkingKey(next)
    const placeholderIndex = steps.findIndex((step) => (
      logicalThinkingKey(step) === nextKey && step.details?.placeholder
    ))
    if (placeholderIndex !== -1) {
      const replaced = [...steps]
      replaced[placeholderIndex] = next
      return replaced
    }

    if (isRetrievalStage(next.stage)) {
      const existingIdx = steps.findIndex((step) => logicalThinkingKey(step) === nextKey)
      if (existingIdx !== -1) {
        const merged = [...steps]
        merged[existingIdx] = mergeThinkingStep(merged[existingIdx], next)
        return merged
      }
    }

    return [...steps, next]
  }

  const flushContent = useCallback(() => {
    setMessages((prev) => {
      const newMsgs = [...prev]
      const last = newMsgs[newMsgs.length - 1]
      const patch = {
        content: contentRef.current,
        sources: sourcesRef.current,
        usage: usageRef.current,
      }
      if (last?.role === 'assistant') {
        newMsgs[newMsgs.length - 1] = { ...last, ...patch }
      } else {
        newMsgs.push({ role: 'assistant', ...patch })
      }
      return newMsgs
    })
  }, [])

  const cancelPendingThinking = useCallback(() => {
    thinkingGenerationRef.current += 1
    pendingThinkingRef.current = []
    if (thinkingTimerRef.current !== null) {
      clearTimeout(thinkingTimerRef.current)
      thinkingTimerRef.current = null
    }
  }, [])

  useEffect(() => {
    return () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current)
        rafRef.current = null
      }
      cancelPendingThinking()
    }
  }, [cancelPendingThinking])

  const fetchPendingApproval = useCallback(async () => {
    // 新会话 URL 可能没有 :sessionId 参数，用流里见过的会话 id 兜底
    const sid = sessionId ?? sessionStorage.getItem('lastSessionId')
    if (!sid) return
    try {
      const res = await sessionsApi.getPending(sid)
      const data = res.data as {
        run_id?: string
        payload?: ApprovalPayload
        query?: string
      } | null
      if (data?.run_id && data?.payload) {
        setPendingApproval({ runId: data.run_id, payload: data.payload, sessionId: sid })
        if (data.query) {
          setMessages((prev) => [...prev, { role: 'user', content: data.query ?? '' }])
        }
      } else {
        setPendingApproval(null)
      }
    } catch {
      // 静默失败：用户仍可重发消息触发后端守卫提示
    }
  }, [sessionId])

  useEffect(() => {
    if (sessionId) {
      if (skipHistoryRef.current === sessionId) {
        skipHistoryRef.current = null
        setLoadingHistory(false)
        return
      }
      sessionsApi.get(sessionId).then((res) => {
        const data = res.data as { history?: [string, string][]; pending_run_id?: string | null } | undefined
        if (data?.history) {
          const entries = data.history
          const roleFormat = entries.length > 0 && entries.every(([role]) =>
            role === 'user' || role === 'assistant' || role === 'system' || role === 'tool',
          )
          const mapped: Message[] = roleFormat
            ? entries
                .filter(([role]) => role === 'user' || role === 'assistant')
                .map(([role, content]) => ({ role: role as 'user' | 'assistant', content }))
            : entries.flatMap(([query, response]) => [
                { role: 'user' as const, content: query },
                { role: 'assistant' as const, content: response },
              ])
          setMessages(mapped)
        }
        if (data?.pending_run_id) {
          fetchPendingApproval()
        }
      }).catch(() => {}).finally(() => setLoadingHistory(false))
    }
  }, [sessionId, fetchPendingApproval])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, currentThinking])

  useEffect(() => {
    if (!sessionId) {
      const lastId = sessionStorage.getItem('lastSessionId')
      if (lastId) {
        navigate(`/chat/${lastId}`, { replace: true })
      }
    }
  }, [sessionId, navigate])

  const handleSend = useCallback(async (query: string) => {
    if (!query.trim() || loading || pendingApproval) return

    lastQueryRef.current = query
    const userMsg: Message = { role: 'user', content: query }
    cancelPendingThinking()
    const requestGeneration = thinkingGenerationRef.current
    thinkingManuallyCollapsedRef.current = false
    thinkingTerminalGenerationRef.current = null
    setMessages((prev) => [...prev, userMsg])
    setInput('')
    setCurrentThinking('')
    setThinkingSteps([])
    setShowThinking(true)
    setShowTrace(true)

    contentRef.current = ''
    sourcesRef.current = []
    usageRef.current = undefined
    let hasResponseStarted = false

    await start(
      '/chat/agent/query/stream',
      { query, session_id: sessionId },
      {
        onThinking: (stage, content, details) => {
          if (thinkingGenerationRef.current !== requestGeneration || thinkingTerminalGenerationRef.current === requestGeneration) return
          if (!isVisibleThinkingStage(stage)) return
          if (!thinkingManuallyCollapsedRef.current) setShowThinking(true)
          const results = isEvidenceEvent(stage, details) && Array.isArray(details?.results)
            ? details.results.map(toEvidence).filter((item) => item !== null)
            : []
          if (isEvidenceEvent(stage, details) && results.length === 0) return
          const step: ThinkingStep = {
            stage,
            content: content || '',
            details,
            at: Date.now(),
            ...(results.length > 0 ? { evidence: results } : {}),
          }
          if (details?.status === 'searching') {
            setThinkingSteps((prev) => {
              const nextKey = logicalThinkingKey(step)
              const existingIdx = prev.findIndex((item) => logicalThinkingKey(item) === nextKey)
              if (existingIdx === -1) return [...prev, step]
              const merged = [...prev]
              merged[existingIdx] = mergeThinkingStep(merged[existingIdx], step)
              return merged
            })
            return
          }
          const isPlaceholder = Boolean(details?.placeholder)
          if (isPlaceholder) {
            // 占位事件立即落地，让「正在规划」折叠框第一时间出现
            setThinkingSteps((prev) => [...prev, step])
            return
          }
          // 真实步骤加入待渲染队列，按 150ms 间隔逐个 flush，形成依次推进的节奏；
          // 若上一步是占位（同 stage），则替换而非新增，避免叠两条。
          pendingThinkingRef.current.push(step)
          setCurrentThinking((prev) => prev ? `${prev}\n${content}` : (content || ''))
          if (thinkingTimerRef.current !== null) return
          const flushOne = () => {
            if (thinkingGenerationRef.current !== requestGeneration) {
              thinkingTimerRef.current = null
              return
            }
            const next = pendingThinkingRef.current.shift()
            if (!next) {
              thinkingTimerRef.current = null
              return
            }
            setThinkingSteps((prev) => {
              return mergeIncomingThinkingStep(prev, next)
            })
            thinkingTimerRef.current = window.setTimeout(flushOne, 150)
          }
          thinkingTimerRef.current = window.setTimeout(flushOne, 150)
        },
        onResponse: (content, sessionId, meta) => {
          if (thinkingGenerationRef.current !== requestGeneration || thinkingTerminalGenerationRef.current === requestGeneration) return
          if (!hasResponseStarted) {
            hasResponseStarted = true
          }
          if (sessionId) {
            sessionStorage.setItem('lastSessionId', sessionId)
          }
          sourcesRef.current = mergeSources(sourcesRef.current, meta?.sources)
          if (meta?.usage) usageRef.current = meta.usage
          contentRef.current += content
          if (rafRef.current === null) {
            rafRef.current = requestAnimationFrame(() => {
              rafRef.current = null
              if (thinkingGenerationRef.current !== requestGeneration || thinkingTerminalGenerationRef.current === requestGeneration) return
              flushContent()
            })
          }
        },
        onInterrupt: (msg: SSEMessage) => {
          if (!msg.run_id || !msg.payload) return
          // interrupt 帧自带 session_id：新会话 URL 无参数时靠它恢复审批
          setPendingApproval({ runId: msg.run_id, payload: msg.payload, sessionId: msg.session_id ?? null })
        },
        onDone: (newSessionId) => {
          if (thinkingGenerationRef.current !== requestGeneration || thinkingTerminalGenerationRef.current === requestGeneration) return
          if (rafRef.current !== null) {
            cancelAnimationFrame(rafRef.current)
            rafRef.current = null
          }
          flushContent()
          const pending = pendingThinkingRef.current.splice(0)
          if (pending.length > 0) {
            setThinkingSteps((prev) => pending.reduce(mergeIncomingThinkingStep, prev))
          }
          cancelPendingThinking()
          if (!thinkingManuallyCollapsedRef.current) setShowThinking(true)
          if (newSessionId) {
            sessionStorage.setItem('lastSessionId', newSessionId)
          }
          if (newSessionId && newSessionId !== sessionId) {
            skipHistoryRef.current = newSessionId
            navigate(`/chat/${newSessionId}`, { replace: true })
          }
        },
        onError: (error) => {
          if (thinkingGenerationRef.current !== requestGeneration || thinkingTerminalGenerationRef.current === requestGeneration) return
          if (error === 'PENDING_EXISTS' && sessionId) {
            // 会话尚有未决审批：拉取 pending 恢复审批卡，不显示错误气泡
            fetchPendingApproval()
            return
          }
          const pending = pendingThinkingRef.current.splice(0)
          if (pending.length > 0) {
            setThinkingSteps((prev) => pending.reduce(mergeIncomingThinkingStep, prev))
          }
          if (rafRef.current !== null) {
            cancelAnimationFrame(rafRef.current)
            rafRef.current = null
          }
          thinkingTerminalGenerationRef.current = requestGeneration
          cancelPendingThinking()
          setMessages((prev) => [...prev, { role: 'assistant', content: `Error: ${error}` }])
        },
      }
    )
  }, [loading, sessionId, start, navigate, flushContent, cancelPendingThinking, pendingApproval, fetchPendingApproval])

  const handleApprove = async () => {
    if (!pendingApproval || approveSubmitting) return
    await resolveApproval([{ type: 'approve' }])
  }

  const handleReject = async () => {
    if (!pendingApproval || approveSubmitting) return
    await resolveApproval([{ type: 'reject', message: rejectReason.trim() || '用户拒绝了该操作' }])
    setRejectReason('')
  }

  const resolveApproval = async (decisions: Array<Record<string, unknown>>) => {
    const snapshot = pendingApproval
    if (!snapshot) return
    // session_id 多级兜底：审批卡自带的 > URL 参数 > 流里见过的（新会话 URL 无参数时前两者都可能为空）
    const sid = snapshot.sessionId ?? sessionId ?? sessionStorage.getItem('lastSessionId')
    if (!sid) {
      setMessages((prev) => [...prev, { role: 'assistant', content: 'Error: 无法确定会话，请刷新页面后重试' }])
      return
    }
    // 先关弹窗再跑流：resume 全程可能几十秒，弹窗不能一直卡着按钮
    const approved = decisions[0]?.type === 'approve'
    const actionCount = snapshot.payload.action_requests.length
    setPendingApproval(null)
    setApprovalNotice(approved ? `已同意 ${actionCount} 个操作，正在执行…` : '已拒绝该操作，正在处理…')
    setApproveSubmitting(true)
    contentRef.current = ''
    sourcesRef.current = []
    usageRef.current = undefined
    const requestGeneration = thinkingGenerationRef.current
    thinkingManuallyCollapsedRef.current = false
    setShowThinking(true)
    // onError 之后后端还会补一个 done 帧，用 failed 区分，避免 done 把错误现场清掉
    let failed = false
    try {
      await start(
        endpoints.agentResume,
        { session_id: sid, decisions },
        {
          onThinking: (stage, content, details) => {
            if (thinkingGenerationRef.current !== requestGeneration) return
            if (!isVisibleThinkingStage(stage)) return
            if (!thinkingManuallyCollapsedRef.current) setShowThinking(true)
            const step: ThinkingStep = {
              stage, content: content || '', details,
            }
            setThinkingSteps((prev) => mergeIncomingThinkingStep(prev, step))
          },
          onResponse: (content, _sid, meta) => {
            if (thinkingGenerationRef.current !== requestGeneration) return
            sourcesRef.current = mergeSources(sourcesRef.current, meta?.sources)
            if (meta?.usage) usageRef.current = meta.usage
            contentRef.current += content
            if (rafRef.current === null) {
              rafRef.current = requestAnimationFrame(() => {
                rafRef.current = null
                if (thinkingGenerationRef.current !== requestGeneration) return
                flushContent()
              })
            }
          },
          onInterrupt: (msg) => {
            if (!msg.run_id || !msg.payload) return
            setPendingApproval({ runId: msg.run_id, payload: msg.payload, sessionId: msg.session_id ?? null })
          },
          onDone: () => {
            if (rafRef.current !== null) {
              cancelAnimationFrame(rafRef.current)
              rafRef.current = null
            }
            if (!failed) {
              flushContent()
              cancelPendingThinking()
              if (!thinkingManuallyCollapsedRef.current) setShowThinking(true)
            }
            setApprovalNotice(null)
            setApproveSubmitting(false)
          },
          onError: (error) => {
            failed = true
            setApprovalNotice(null)
            setApproveSubmitting(false)
            setMessages((prev) => [...prev, { role: 'assistant', content: `Error: ${error}` }])
            // 后端出错保留 thread 与 pending 供重试：恢复审批卡；
            // 若中途已来新审批（二次中断）则不动；切走会话也不恢复旧卡
            if (sid === (sessionId ?? sessionStorage.getItem('lastSessionId'))) {
              setPendingApproval((prev) => prev ?? snapshot)
            }
          },
        },
      )
    } finally {
      setApproveSubmitting(false)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (!e.shiftKey || e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      handleSend(input)
    }
  }

  useEffect(() => {
    if (showTrace) traceEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [thinkingSteps.length, showTrace])

  useEffect(() => {
    if (!loading) {
      setElapsed(0)
      return
    }
    const started = Date.now()
    const id = window.setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 500)
    return () => window.clearInterval(id)
  }, [loading])

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setShowTrace(false)
        setActiveSource(null)
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        inputRef.current?.focus()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  const isLoading = loadingHistory || loading
  const hasStreamingAssistant = loading && messages.length > 0 && messages[messages.length - 1].role === 'assistant'

  return (
    <div className="h-full flex flex-col">
      {messages.length > 0 && (
        <div className="shrink-0 px-6 py-3 border-b border-[var(--color-border)] bg-[var(--color-bg)]">
          <div className="max-w-3xl mx-auto flex justify-end">
            <button
              onClick={() => {
                sessionStorage.removeItem('lastSessionId')
                setMessages([])
                navigate('/chat')
              }}
              className="px-3 py-1.5 text-xs rounded-md border border-[var(--color-border)] text-[var(--color-text-secondary)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)] transition-colors"
            >
              {t('chat.newSession')}
            </button>
          </div>
        </div>
      )}
      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="max-w-3xl mx-auto space-y-6">
          {messages.length === 0 && !isLoading && (
            <div className="py-20 text-center">
              <div className="mb-6 flex justify-center">
                <BrandMark size={40} className="text-[var(--color-text)]" />
              </div>
              <h2 className="font-heading text-[22px] leading-tight text-[var(--color-text)]">{t('chat.welcome')}</h2>
              <p className="mx-auto mt-2 max-w-sm text-[13px] leading-relaxed text-[var(--color-text-tertiary)]">
                {t('chat.welcomeSub')}
              </p>
              <div className="mx-auto mt-7 flex max-w-md flex-wrap justify-center gap-2">
                {quickQuestions.map((q) => (
                  <button
                    key={q}
                    onClick={() => handleSend(q)}
                    className="rounded-[3px] border border-[var(--color-border)] px-3.5 py-1.5 text-xs text-[var(--color-text-secondary)] hover:border-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-[var(--color-accent-contrast)]"
                  >
                    {q}
                  </button>
                ))}
              </div>
              <p className="mx-auto mt-8 max-w-sm text-[11px] leading-relaxed tracking-wide text-[var(--color-text-tertiary)]">
                {t('chat.emptyHint')}
              </p>
            </div>
          )}

          {loadingHistory && (
            <div className="flex justify-center py-4">
              <Loader2 size={20} className="animate-spin text-[var(--color-text-tertiary)]" />
            </div>
          )}

          {messages.map((msg, i) => (
            <div key={i} className={`flex gap-3 ${msg.role === 'user' ? 'justify-end' : ''}`}>
              {msg.role === 'assistant' && (
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[3px] border border-[var(--color-border)] bg-[var(--color-card)]">
                  <Bot size={15} className="text-[var(--color-text-secondary)]" />
                </div>
              )}
              <div className={`max-w-[75%] ${msg.role === 'user' ? 'order-first' : ''}`}>
                {msg.role === 'user' ? (
                  <div className="rounded-[10px] bg-[var(--color-accent)] px-4 py-2.5 text-sm text-[var(--color-accent-contrast)]">
                    {msg.content}
                  </div>
                ) : (
                  <>
                    {msg.content.startsWith('Error:') ? (
                      <div className="rounded-md border border-red-300/40 bg-red-500/5 px-3 py-2.5">
                        <p className="text-xs text-[var(--color-text-secondary)]">{msg.content.slice(6).trim()}</p>
                        <button
                          onClick={() => handleSend(lastQueryRef.current)}
                          disabled={loading}
                          className="mt-2 rounded border border-[var(--color-border)] px-2.5 py-1 text-[11px] text-[var(--color-text-secondary)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)] disabled:opacity-40"
                        >
                          重试
                        </button>
                      </div>
                    ) : (
                      <div className={`prose prose-sm max-w-none markdown-body${theme === 'dark' ? ' prose-invert' : ''}`}>
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          rehypePlugins={[rehypeHighlight, rehypeRaw]}
                          components={{
                            a: ({ href, children }) => {
                              if (href?.startsWith('#src-')) {
                                const index = Number(href.slice(5)) - 1
                                return (
                                  <button
                                    type="button"
                                    onClick={() => setActiveSource(msg.sources?.[index] ?? null)}
                                    className="mx-0.5 rounded bg-[var(--color-accent-bg)] px-1 text-[var(--color-accent)] hover:underline"
                                  >
                                    {children}
                                  </button>
                                )
                              }
                              return <a href={href} target="_blank" rel="noreferrer" className="text-[var(--color-accent)] underline">{children}</a>
                            },
                          }}
                        >
                          {msg.sources?.length ? msg.content.replace(/\[(\d+)\](?!\()/g, '[[$1]](#src-$1)') : msg.content}
                        </ReactMarkdown>
                      </div>
                    )}
                    {!!msg.sources?.length && (
                      <div className="mt-3 rounded-md border border-[var(--color-border)] bg-[var(--color-bg-secondary)] p-3">
                        <div className="mb-2 text-xs font-medium text-[var(--color-text-secondary)]">
                          引用来源（{msg.sources.length}）
                        </div>
                        <ol className="space-y-2">
                          {msg.sources.map((source, index) => (
                            <li key={source.id} className="text-xs leading-relaxed">
                              <span className="mr-1 text-[var(--color-text-tertiary)]">[{index + 1}]</span>
                              <span className="font-medium text-[var(--color-text)]">{source.title || source.id}</span>
                              {source.source && (
                                <span className="ml-2 text-[var(--color-text-tertiary)]">
                                  {evidenceSourceLabels[source.source] || source.source}
                                </span>
                              )}
                              {typeof source.score === 'number' && (
                                <span className="ml-2 text-[var(--color-text-tertiary)]">score {source.score}</span>
                              )}
                              {source.preview && (
                                <p className="mt-1 line-clamp-2 text-[var(--color-text-secondary)]">{source.preview}</p>
                              )}
                            </li>
                          ))}
                        </ol>
                      </div>
                    )}
                    {msg.usage && (
                      <div className="mt-2 text-[11px] text-[var(--color-text-tertiary)]">
                        tokens {msg.usage.input_tokens ?? 0} 输入 / {msg.usage.output_tokens ?? 0} 输出
                        {typeof msg.usage.duration_ms === 'number' && ` · 耗时 ${(msg.usage.duration_ms / 1000).toFixed(1)}s`}
                        {typeof msg.usage.calls === 'number' && ` · ${msg.usage.calls} 次模型调用`}
                      </div>
                    )}
                    {hasStreamingAssistant && i === messages.length - 1 && (
                      <div className="flex gap-1 mt-3">
                        <span className="w-2 h-2 rounded-full bg-[var(--color-accent)] animate-bounce" style={{ animationDelay: '0ms' }} />
                        <span className="w-2 h-2 rounded-full bg-[var(--color-accent)] animate-bounce" style={{ animationDelay: '150ms' }} />
                        <span className="w-2 h-2 rounded-full bg-[var(--color-accent)] animate-bounce" style={{ animationDelay: '300ms' }} />
                      </div>
                    )}
                  </>
                )}
              </div>
              {msg.role === 'user' && (
                <div className="w-8 h-8 rounded-lg bg-[var(--color-bg-tertiary)] flex items-center justify-center shrink-0">
                  <User size={16} className="text-[var(--color-text-secondary)]" />
                </div>
              )}
            </div>
          ))}

          {loading && !hasStreamingAssistant && (
            <div className="flex gap-3">
              <div className="w-8 h-8 rounded-lg bg-[var(--color-accent-bg)] flex items-center justify-center shrink-0">
                <Bot size={16} className="text-[var(--color-accent)]" />
              </div>
              <div className="space-y-2 flex-1">
                <div className="flex gap-1">
                  <span className="w-2 h-2 rounded-full bg-[var(--color-accent)] animate-bounce" style={{ animationDelay: '0ms' }} />
                  <span className="w-2 h-2 rounded-full bg-[var(--color-accent)] animate-bounce" style={{ animationDelay: '150ms' }} />
                  <span className="w-2 h-2 rounded-full bg-[var(--color-accent)] animate-bounce" style={{ animationDelay: '300ms' }} />
                </div>
              </div>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>
      </div>

      {!pendingApproval && approveSubmitting && approvalNotice && (
        <div className="max-w-3xl mx-auto px-6 pt-4">
          <div className="flex items-center gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] px-4 py-2.5 text-xs text-[var(--color-text-secondary)]">
            <Loader2 size={13} className="animate-spin shrink-0" />
            {approvalNotice}
          </div>
        </div>
      )}

      {pendingApproval && (
        <div className="max-w-3xl mx-auto px-6 pt-4">
          <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-4 space-y-3">
            <div className="text-sm font-medium text-[var(--color-text)]">
              需要确认以下操作
            </div>
            {pendingApproval.payload.action_requests.map((req, i) => {
              const expandKey = `${pendingApproval.runId}:${i}`
              const expanded = expandedApproval[expandKey] === true
              const toggleExpanded = () => setExpandedApproval((prev) => ({ ...prev, [expandKey]: !expanded }))
              const args = req.args ?? {}
              const title = typeof args.title === 'string' ? args.title : ''
              const content = typeof args.content === 'string' ? args.content : ''
              const isNote = (
                req.name === 'create_note' || req.name === 'create_note_tool'
              ) && (title !== '' || content !== '')
              const description = req.description ?? ''
              const descExpanded = expandedApproval[`${expandKey}:desc`] === true
              return (
                <div key={`${req.name}-${i}`} className="rounded-md bg-[var(--color-bg-secondary)] p-3 text-xs space-y-2">
                  <div className="font-medium text-[var(--color-text)] text-sm">
                    {approvalToolLabels[req.name] || req.name}
                    <span className="ml-2 font-normal text-[var(--color-text-tertiary)]">{req.name}</span>
                  </div>
                  {description !== '' && (
                    <p className={`leading-relaxed text-[var(--color-text-secondary)]${descExpanded ? ' max-h-96 overflow-y-auto pr-2' : ''}`}>
                      {descExpanded ? stripMarkdown(description) : truncateText(stripMarkdown(description), APPROVAL_PREVIEW_LIMIT)}
                      {description.length > APPROVAL_PREVIEW_LIMIT && (
                        <button
                          type="button"
                          onClick={() => setExpandedApproval((prev) => ({ ...prev, [`${expandKey}:desc`]: !descExpanded }))}
                          className="ml-1 text-[var(--color-accent)] hover:underline"
                        >
                          {descExpanded ? '收起' : '展开'}
                        </button>
                      )}
                    </p>
                  )}
                  {isNote ? (
                    <>
                      {title !== '' && (
                        <div className="flex gap-2">
                          <span className="shrink-0 text-[var(--color-text-tertiary)]">标题：</span>
                          <span className="font-medium text-[var(--color-text)]">{title}</span>
                        </div>
                      )}
                      {content !== '' && (
                        <div>
                          <div className="text-[var(--color-text-tertiary)]">正文预览：</div>
                          {expanded ? (
                            <div className={`prose prose-sm max-w-none markdown-body${theme === 'dark' ? ' prose-invert' : ''} max-h-96 overflow-y-auto pr-2`}>
                              <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight, rehypeRaw]}>
                                {content}
                              </ReactMarkdown>
                            </div>
                          ) : (
                            <p className="whitespace-pre-wrap break-all text-[var(--color-text-secondary)]">
                              {truncateText(stripMarkdown(content), APPROVAL_PREVIEW_LIMIT)}
                            </p>
                          )}
                          {content.length > APPROVAL_PREVIEW_LIMIT && (
                            <button
                              type="button"
                              onClick={toggleExpanded}
                              className="mt-1 text-[var(--color-accent)] hover:underline"
                            >
                              {expanded ? '收起' : '展开全文'}
                            </button>
                          )}
                        </div>
                      )}
                    </>
                  ) : (
                    <div className="space-y-1">
                      {Object.entries(args).map(([k, v]) => (
                        <div key={k} className="flex gap-2">
                          <span className="shrink-0 text-[var(--color-text-tertiary)]">{k}：</span>
                          <span className="whitespace-pre-wrap break-all text-[var(--color-text-secondary)]">
                            {truncateText(typeof v === 'string' ? v : JSON.stringify(v), APPROVAL_ARG_LIMIT)}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
            <div className="flex items-center gap-2">
              <button
                onClick={handleApprove}
                disabled={approveSubmitting}
                className="px-4 py-1.5 rounded-md bg-[var(--color-accent)] text-[var(--color-accent-contrast)] text-sm disabled:opacity-50"
              >
                同意
              </button>
              <button
                onClick={() => resolveApproval([{ type: 'approve_all' }])}
                disabled={approveSubmitting}
                className="px-3 py-1.5 rounded-md border border-[var(--color-border)] text-xs disabled:opacity-50"
                title="本会话内同类操作不再弹审批（1 小时）"
              >
                本会话都允许
              </button>
              <button
                onClick={handleReject}
                disabled={approveSubmitting}
                className="px-4 py-1.5 rounded-md border border-[var(--color-border)] text-sm disabled:opacity-50"
              >
                拒绝
              </button>
              <input
                value={rejectReason}
                onChange={(e) => setRejectReason(e.target.value)}
                placeholder="拒绝原因（可选）"
                className="flex-1 min-w-0 px-3 py-1.5 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] text-xs"
              />
            </div>
          </div>
        </div>
      )}

      <div className="border-t border-[var(--color-border)] bg-[var(--color-card)] px-6 py-4">
        <div className="max-w-3xl mx-auto flex gap-3">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={t('chat.input')}
            rows={1}
            className="flex-1 resize-none rounded-[3px] border border-[var(--color-border)] bg-[var(--color-bg)] px-4 py-2.5 text-sm text-[var(--color-text)] placeholder:text-[var(--color-text-placeholder)] focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
          />
          <button
            onClick={() => handleSend(input)}
            disabled={!input.trim() || loading || approveSubmitting}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[3px] bg-[var(--color-accent)] text-[var(--color-accent-contrast)] hover:bg-[var(--color-accent-hover)] disabled:opacity-40"
          >
            {loading ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
          </button>
        </div>
      </div>
      {thinkingSteps.length > 0 && !showTrace && (
        <button
          onClick={() => setShowTrace(true)}
          className="fixed bottom-24 right-5 z-40 flex items-center gap-1.5 rounded-full border border-[var(--color-border)] bg-[var(--color-card)] px-3.5 py-2 text-xs text-[var(--color-text-secondary)] shadow-lg hover:border-[var(--color-accent)] hover:text-[var(--color-accent)]"
        >
          <Activity size={13} /> 执行轨迹 · {thinkingSteps.length}
        </button>
      )}
      <aside
        className={`fixed right-0 top-0 z-50 flex h-full w-[400px] flex-col border-l border-[var(--color-border)] bg-[var(--color-card)] shadow-2xl transition-transform duration-300 ${showTrace ? 'translate-x-0' : 'translate-x-full'}`}
      >
        <div className="flex shrink-0 items-center justify-between border-b border-[var(--color-border)] px-4 py-3.5">
          <div className="flex items-center gap-2">
            <span className="flex h-7 w-7 items-center justify-center rounded-md bg-[var(--color-accent-bg)]">
              <Activity size={14} className="text-[var(--color-accent)]" />
            </span>
            <span className="text-sm font-medium">执行轨迹</span>
            <span className="rounded-full bg-[var(--color-bg-secondary)] px-2 py-0.5 text-[11px] text-[var(--color-text-tertiary)]">
              {thinkingSteps.length} 步
            </span>
            {loading && (
              <span className="flex items-center gap-1 text-[10px] text-[var(--color-text-tertiary)]">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--color-accent)]" />
                {elapsed}s
              </span>
            )}
          </div>
          <button aria-label="关闭执行轨迹" onClick={() => setShowTrace(false)} className="rounded p-1 text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-secondary)] hover:text-[var(--color-text)]">
            <X size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-4">
          {thinkingSteps.length === 0 && (
            <p className="text-xs leading-relaxed text-[var(--color-text-tertiary)]">
              发一条消息后，这里会显示模型每一轮的动作：规划、检索、工具调用与证据。
            </p>
          )}
          <ol className="relative ml-1 space-y-4 border-l border-[var(--color-border)] pl-5">
            {thinkingSteps.map((step, index) => {
              const meta = stageMeta(step.stage)
              return (
                <li key={`trace-${step.stage}-${index}`} className="relative">
                  <span className={`absolute -left-[25.5px] top-1.5 h-2.5 w-2.5 rounded-full ring-4 ring-[var(--color-card)] ${meta.dot}`} />
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-medium text-[var(--color-text)]">{meta.label}</span>
                    <span className="text-[10px] tabular-nums text-[var(--color-text-tertiary)]">
                      {step.at ? new Date(step.at).toLocaleTimeString('zh-CN', { hour12: false }) : ''}
                    </span>
                  </div>
                  {step.content && (
                    <p className="mt-1 text-xs leading-relaxed text-[var(--color-text-secondary)] whitespace-pre-wrap">{step.content}</p>
                  )}
                  {step.evidence && step.evidence.length > 0 && (
                    <div className="mt-2 space-y-1.5">
                      {step.evidence.map((item) => (
                        <div key={item.id} className="rounded-md border border-[var(--color-border)] bg-[var(--color-bg-secondary)] px-2.5 py-2">
                          <div className="flex items-center justify-between gap-2">
                            <span className="truncate text-[11px] text-[var(--color-text)]">{item.title}</span>
                            {typeof item.score === 'number' && (
                              <span className="shrink-0 text-[10px] text-[var(--color-text-tertiary)]">{item.score.toFixed(1)}</span>
                            )}
                          </div>
                          {item.preview && (
                            <p className="mt-1 line-clamp-2 text-[10px] leading-relaxed text-[var(--color-text-tertiary)]">{item.preview}</p>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                  {step.details && Object.keys(step.details).length > 0 && (
                    <details className="mt-2">
                      <summary className="cursor-pointer text-[10px] text-[var(--color-text-tertiary)] hover:text-[var(--color-text-secondary)]">原始事件</summary>
                      <pre className="mt-1 max-h-48 overflow-auto rounded-md bg-[var(--color-bg-secondary)] p-2 text-[10px] leading-relaxed text-[var(--color-text-tertiary)]">
                        {JSON.stringify(step.details, null, 2)}
                      </pre>
                    </details>
                  )}
                </li>
              )
            })}
            <div ref={traceEndRef} />
          </ol>
        </div>
      </aside>
      {activeSource && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 p-6" onClick={() => setActiveSource(null)}>
          <div className="max-h-[70vh] w-[560px] overflow-y-auto rounded-xl bg-[var(--color-card)] p-5 shadow-2xl" onClick={(event) => event.stopPropagation()}>
            <div className="mb-2 flex items-start justify-between gap-3">
              <div>
                <div className="text-sm font-medium text-[var(--color-text)]">{activeSource.title || activeSource.id}</div>
                <div className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">
                  {activeSource.source && (evidenceSourceLabels[activeSource.source] || activeSource.source)}
                  {typeof activeSource.score === 'number' && ` · score ${activeSource.score}`}
                </div>
              </div>
              <button aria-label="关闭来源卡片" onClick={() => setActiveSource(null)} className="rounded p-1 text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-secondary)]">
                <X size={16} />
              </button>
            </div>
            <p className="text-xs leading-relaxed text-[var(--color-text-secondary)] whitespace-pre-wrap">
              {activeSource.preview || '这条来源没有附带预览，可在知识库中打开原文。'}
            </p>
            {activeSource.source && /\.(md|txt|pdf|docx|pptx)$/i.test(activeSource.source) && (
              <button
                onClick={() => {
                  const filename = activeSource.source as string
                  setActiveSource(null)
                  navigate(`/knowledge?doc=${encodeURIComponent(filename)}&chunk=${encodeURIComponent(activeSource.id || '')}`)
                }}
                className="mt-3 rounded border border-[var(--color-border)] px-2.5 py-1 text-[11px] text-[var(--color-text-secondary)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)]"
              >
                在知识库打开原文
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
