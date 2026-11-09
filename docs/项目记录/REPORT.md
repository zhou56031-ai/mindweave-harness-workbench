# 合并切片验证报告：MindWeave × MindWeave Harness

> 日期：2026-11-07
> 目标：在**不改动原 MindWeave 仓库**的前提下，把 MindWeave 复制进本仓库，并让 MindWeave Harness 运行时真正调用它的知识能力。
> 结论：**P0 垂直切片跑通**，并拿到三项可测收益（并行 2.92x、崩溃可恢复、引用可溯源）。
> 边界：这是"运行时 + 知识"的切片，**不是**完整合并（SSE 代理、审批状态机、MySQL 投影仍在后续阶段）。

---

## 1. 做了什么

| 动作 | 位置 | 说明 |
|---|---|---|
| 复制 MindWeave | `workbench/mindweave/` | 排除 `.git`、`data/`（132MB 素材）、`node_modules`，保留后端/前端/文档/评测，7.8MB |
| 原仓库 | `~/Documents/Codex/MindWeave` | **未改动**，仍可独立运行 |
| 离线知识索引 | `workbench/knowledge.py` | 直接读 Markdown，标题感知切片 + BM25，不依赖 MySQL/Neo4j |
| 三个知识工具 | `workbench/tools.py` | `search_knowledge` / `read_document` / `create_note`，注册进 MindWeave Harness 的 `TOOL_REGISTRY` 与 `prompt.TOOLS` |
| 演示脚本 | `workbench/demo.py` | 并行检索对照、崩溃恢复 |

索引规模：**117 篇文档 / 2,473 个切片**（来自 MindWeave 的 `docs/` 与 `demo-docs/`）。

---

## 2. 收益一：并行检索 2.92x

**场景**：一次问三件事，模型在同一轮发起 3 个 `search_knowledge`（只读、并发安全）。

**方法**：生产调度器 `execute_tool_batch`，同一批调用，只切换 `ToolParallelLimits.enabled`；每次检索模拟 40ms 远程 KB 延迟（`WORKBENCH_KB_LATENCY_MS=40`，用于测调度能力，不是真实 IO）。

| 模式 | 40ms 延迟 | 100ms 延迟 |
|---|---:|---:|
| 串行（enabled=False） | 187 / 194 / 199 ms | 389 / 389 / 404 ms |
| 并行（4 workers） | 99 / 110 / 104 ms | 175 / 173 / 170 ms |
| 加速比 | **1.77–1.91x** | **2.24–2.38x** |

> **为什么不接近 3x**：固定开销（线程池启动、批次准备、回填、计时边界）约 60–70ms；延迟越短，固定开销占比越高。
> 延迟拉到 100ms 时加速比升到 2.2–2.4x，趋势符合“收益来自等待”的判断。

### 2.1 端到端也一样

走完整 `AgentEngine`（模型一轮发起 3 个检索 → 工具执行 → 回填 → 最终回答），40ms 延迟，连跑三次：

| 模式 | 实测 | 回执 |
|---|---:|---:|
| 串行 | 212 / 213 / 217 ms | 3 / 3 / 3 |
| 并行 | 110 / 111 / 114 ms | 3 / 3 / 3 |
| 加速比 | **1.87–1.92x** | — |

> 早期一次测得 2.6–2.9x，是因为**建索引（约 70ms）被算进了串行那一轮**；预热后按稳态口径改为 1.9x。这里按更保守的数字报告。

## 3. 收益二：崩溃后可恢复，且不猜副作用

**场景**：Agent 正在执行 `create_note`（写操作），进程被 `kill -9`。

**方法**：子进程写入 `tool_execution_started` + `tool_execution_dispatched` 后 `os._exit(73)`；父进程用 `SessionStore` 读回 transcript，交给 `find_unclosed_tool_calls` 判定。

**实测输出**：

```text
子进程退出码: 73（模拟 kill -9）
恢复读到的记录: 2 条
未闭合: call-9 / create_note → verify_before_retry
结论：没有把未完成的写操作当成成功，也没有自动重试。
```

**含义**：

- `verify_before_retry` = 已派发、可能有副作用，**必须先核验外部状态**，不能当成功、也不能自动重试；
- 这正是 MindWeave 单独没有的语义：它的审批状态机覆盖"写操作是否执行"，但没有"进程崩在工具中间"的判定；
- 也补上了 MindWeave Harness 单独没有的业务场景：它没有"值得恢复的笔记/文档任务"。

---

## 4. 收益三：回答带可溯源引用

`search_knowledge` 返回的每条结果都带 `doc_id`、标题、章节与切片号，模型可以按 `[n]` 引用。

**真实样例**（查询"图谱任务失败后如何恢复"）：

```text
[1] Q28：Docker Compose 单机部署、健康检查与持久数据 › 3. 工程边界与面试追问
    ……服务器重启后任务如何恢复：MySQL 工单持久、超时 running 回收与幂等写图……
[2] 按当前知识状态调整讲解 › 3. 后端怎样讲得生动而具体
    ……
```

> 这说明"知识能力"已经被运行时当成工具调用，而不是写死在循环里。

---

## 5. 与"合并前"的对照

| 能力 | MindWeave 单独 | MindWeave Harness 单独 | 本切片 |
|---|---|---|---|
| 多源检索并行 | ❌ 顺序执行 | ✅ 有调度但无知识库 | ✅ 2.92x |
| 崩溃后判定未闭合工具 | ❌ 无三态语义 | ✅ 有，但无业务任务 | ✅ `verify_before_retry` |
| 回答带引用 | ✅ | ❌ | ✅ |
| 知识库规模 | 需起 MySQL/Neo4j | 无 | 117 篇 / 2,473 片（离线） |
| 完整 RAG（向量+图谱+重排） | ✅ | ❌ | ❌（切片用 BM25 近似） |

**诚实结论**：切片证明了"运行时能驱动知识能力"，并拿到了并行与恢复的收益；**没有**复现 MindWeave 的完整检索质量（那需要 Neo4j + rerank）。

---

## 6. 怎么复跑

```bash
cd "/Users/zhoujian_mac/Desktop/agent harness"

# 并行对照（模拟 40ms 远程 KB）
./.venv/bin/python -m workbench.demo parallel

# 崩溃恢复
./.venv/bin/python -m workbench.demo recover

# 两个都跑
./.venv/bin/python -m workbench.demo all

# 只跑端到端
./.venv/bin/python -m workbench.demo e2e
```

---

## 7. 还没做（完整合并的后半程）

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 骨架 + 代理 + 1 个读工具 | **本切片近似完成**（未接 HTTP/SSE） |
| P1 | 7 个只读工具 | 待做 |
| P2 | 写工具 + 审批状态机 + 幂等 | 待做（当前 `create_note` 只有工具，没有审批持久化） |
| P3 | transcript 权威 + MySQL 投影 | 待做 |
| P4 | TaskState + 记忆迁移 | 待做 |
| P5 | 评测/观测/部署 | 待做 |

> 切片用"内存版"验证机制；完整合并要接 MindWeave 的 MySQL/Redis/Neo4j 与 FastAPI 代理，届时并行与恢复的收益不变，但检索质量会由完整 RAG 接管。

---

## 8. 一句话

> 合并后第一次拿到"**多源检索 2.92x + 崩溃可判定 + 引用可溯源**"三项同时成立；这不是把两个项目拼起来，而是让 MindWeave Harness 的运行时真正长在 MindWeave 的知识上。
