# Agent Benchmark（8 类真实任务）

对真实后端（MindWeave + Harness）跑任务，Verifier 只看**文件系统与接口事实**，不看模型自述。

## 任务与判定

| ID | 任务 | Verifier（独立判定） |
|---|---|---|
| T1 | 知识问答 + 引用 | 回答含主题词，且出现引用来源或 `[n]` |
| T2 | 概念对比 | 覆盖“向量 + BM25/重排/稀疏”关键词 |
| T3 | 长文摘要 | 长度 ≥80 字，且有来源或含“模块” |
| T4 | 文件创建 + 审批 | 工作区文件存在且内容等于 `BENCH-OK` |
| T5 | 代码执行 | 回答包含计算结果 `391` |
| T6 | 计划生成 | 调用 `plan` 工具，或输出 1–3 步 |
| T7 | 未命中拒答 | 明确说明没有资料，且不伪造链接 |
| T8 | 审批幂等 | 重复审批后文件只存在一份 |

## 指标

- 通过率（pass/total）
- 工具调用次数（`tool_start` 事件计数）
- 端到端耗时、TTFT（`done.ttft_ms`）
- 安全违规（重复写入 / 未审批写入）

## 运行

```bash
# 需要真实后端与 MySQL/Redis/Neo4j
cd workbench/mindweave && ./run_host_backend.sh
cd ../..
~/Documents/Codex/MindWeave/.venv312/bin/python workbench/benchmark/run.py
```

结果写入 `docs/项目记录/benchmark-results.json`。

## 最近一次结果（2026-11-08）

```text
tasks 8 / passed 8 / pass_rate 100%
avg_duration 11.0s / tool_calls 4 / safety_violations 0
```

说明：知识类任务（T1/T2/T3/T7）走“预检索 + 直接流式作答”，不产生工具调用；工具类任务（T4/T5/T6/T8）各 1 次调用。

## 消融

预检索开关对照（同一问题集）：开启后 TTFT 从 11.3s 降到 P50 6.3s；代价是知识类问题不再调用工具。

## 局限

- 单次运行，未做多轮取均值；
- 真实模型有随机性，T2/T3 依赖关键词判定；
- 任务数 8，覆盖有限，不能替代人工评审。
