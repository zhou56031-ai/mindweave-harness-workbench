import { useTranslation } from 'react-i18next'
import { ArrowUpRight, GitFork } from 'lucide-react'
import BrandMark from '../components/common/BrandMark'

const STATS = [
  { value: '117', unit: '篇', label: '知识库文档', hint: '全部建完索引' },
  { value: '2,770', unit: '片', label: '检索切片', hint: '向量 + 图谱' },
  { value: '6.3', unit: 's', label: '首字延迟 P50', hint: '基线 11.3s' },
  { value: '428', unit: '个', label: '单测通过', hint: 'tsc / build 全绿' },
]

const IMPROVEMENTS = [
  {
    tag: 'Agent',
    title: '原生 Tool Calling 循环',
    body: '不依赖 LangChain Agent：模型自己决定查几次资料，工具结果回到同一条消息链，支持审批中断、结构化摘要与原文归档。',
  },
  {
    tag: '检索',
    title: '融合、配额与重排',
    body: '向量 + 全文 RRF 融合；每路限额避免单源挤占，同一 chunk 去重；Lucene 特殊字符转义、CJK 分析器，再交给模型分级重排。',
  },
  {
    tag: '数据',
    title: '父子块与证据预算',
    body: '子块负责召回，父块负责回填上下文；证据总量按字符封顶，长对话通过归档 + read_artifact 分页读回。',
  },
  {
    tag: '可观测',
    title: 'Trace、引用与成本',
    body: '每次请求记录 span、token 与工具序列；前端展示思考步骤、引用来源，以及按会话聚合的 token / 耗时统计。',
  },
  {
    tag: '评测',
    title: '两套评测闭环',
    body: 'AutoRAG 120 条（80 调参 / 40 测试）比较召回、重排与 prompt；仓库内 eval harness 覆盖检索、引用捏造与拒答。',
  },
  {
    tag: '扩展',
    title: '记忆、技能与 MCP',
    body: '长期记忆走审批后写入；load_skill 只读加载技能说明；stdio MCP 服务把技能目录暴露给外部客户端。',
  },
]

const STACK = [
  'React 19', 'FastAPI', 'SQLAlchemy 2', 'MySQL 8', 'Redis 7', 'Neo4j 5',
  'DeepSeek', 'text-embedding-v4', 'AntV G6', 'Tailwind',
]

export default function AboutUs() {
  const { t } = useTranslation()

  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <section className="relative overflow-hidden rounded-[6px] border border-[var(--color-border)] bg-[var(--color-card)] p-8">
        <div className="grid-backdrop pointer-events-none absolute inset-0" aria-hidden />
        <div className="relative">
        <div className="flex items-center gap-3 text-[11px] uppercase tracking-[0.18em] text-[var(--color-text-tertiary)]">
          <BrandMark size={22} className="text-[var(--color-text)]" />
          <span>MindWeave Harness 智能工作台</span>
        </div>
        <h1 className="mt-6 font-heading text-[30px] font-semibold leading-[1.2] tracking-[-0.02em] text-[var(--color-text)]">
          把笔记、文档与知识图谱
          <br />
          织成一张可检索的网
        </h1>
        <p className="mt-4 max-w-2xl text-sm leading-relaxed text-[var(--color-text-secondary)]">
          笔记、文档与知识图谱共享同一次检索；Agent 能查资料、读文件、执行代码，写操作先经过你审批。
          每次回答都能看到它查了什么、引用了哪几段、执行了什么、花了多少 token。
        </p>
        <div className="mt-6 flex flex-wrap items-center gap-2 text-xs text-[var(--color-text-secondary)]">
          <span className="rounded-full border border-[var(--color-border)] px-3 py-1">检索 + 引用可回跳</span>
          <span className="rounded-full border border-[var(--color-border)] px-3 py-1">文件 / 代码执行 · 人工审批</span>
          <span className="rounded-full border border-[var(--color-border)] px-3 py-1">可回放的请求 trace</span>
        </div>
        </div>
      </section>

      <section className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        {STATS.map((stat) => (
          <div
            key={stat.label}
            className="group relative overflow-hidden rounded-[4px] border border-[var(--color-border)] bg-[var(--color-card)] px-4 py-4 transition-colors hover:border-[var(--color-text-tertiary)]"
          >
            <div className="flex items-baseline gap-1">
              <span className="font-heading text-[30px] font-semibold leading-none tracking-[-0.02em] text-[var(--color-text)]">
                {stat.value}
              </span>
              <span className="text-xs text-[var(--color-text-tertiary)]">{stat.unit}</span>
            </div>
            <div className="mt-2 text-xs font-medium text-[var(--color-text-secondary)]">{stat.label}</div>
            <div className="mt-0.5 text-[10px] text-[var(--color-text-tertiary)]">{stat.hint}</div>
          </div>
        ))}
      </section>

      <section className="mt-10">
        <div className="flex items-end justify-between">
          <h2 className="font-heading text-lg font-semibold text-[var(--color-text)]">这个项目做了什么</h2>
          <span className="text-xs text-[var(--color-text-tertiary)]">从检索到执行的完整链路</span>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          {IMPROVEMENTS.map((item, index) => (
            <article
              key={item.title}
              className="group rounded-[4px] border border-[var(--color-border)] bg-[var(--color-card)] p-5 transition-colors hover:border-[var(--color-text-tertiary)]"
            >
              <div className="flex items-center justify-between">
                <span className="text-[10px] uppercase tracking-[0.16em] text-[var(--color-text-tertiary)]">
                  {String(index + 1).padStart(2, '0')} / {item.tag}
                </span>
              </div>
              <h3 className="mt-3 text-sm font-medium text-[var(--color-text)]">{item.title}</h3>
              <p className="mt-2 text-xs leading-relaxed text-[var(--color-text-secondary)]">{item.body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="mt-10">
        <h2 className="font-heading text-lg font-semibold text-[var(--color-text)]">{t('about.techStack')}</h2>
        <div className="mt-4 flex flex-wrap gap-2">
          {STACK.map((tech) => (
            <span
              key={tech}
              className="rounded-[2px] border border-[var(--color-border)] bg-[var(--color-bg-secondary)] px-2.5 py-1 text-xs text-[var(--color-text-secondary)]"
            >
              {tech}
            </span>
          ))}
        </div>
      </section>

      <section className="mt-10 rounded-[4px] border border-[var(--color-border)] bg-[var(--color-card)] px-5 py-4">
        <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
          <a
            href="https://github.com/RMA-MUN/RAGNotebook"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-2 text-[var(--color-text)] hover:underline"
          >
            <GitFork size={15} />
            上游项目 RAGNotebook
            <ArrowUpRight size={13} className="text-[var(--color-text-tertiary)]" />
          </a>
          <span className="text-xs text-[var(--color-text-tertiary)]">
            MIT License · 文档与评测报告见仓库 docs/
          </span>
        </div>
      </section>
    </div>
  )
}
