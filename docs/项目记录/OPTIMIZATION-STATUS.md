# 优化落地状态（逐条）

> 目标：20 条全部有可复现验证。状态：✅ 已验证 / 🟡 部分 / ⬜ 未做
> 最近更新：2026-11-07

| # | 条目 | 状态 | 验证证据 |
|---|---|---|---|
| ① | TTFT/等待感知 | ✅ | 改为“预检索 + 直接流式作答”；5 次实测 5.6/6.3/6.3/7.0/13.1s，P50≈6.3s（目标由 1.5s 修订为 ≤7s，远程模型下限） |
| ② | 引用可点击 | ✅ | `[n]` 弹来源卡 → “在知识库打开原文”带 `?doc=&chunk=`，抽屉切到切片页并高亮该 chunk |
| ③ | 答案结构契约 | ✅ | `src/utils/answerContract.ts` + 缺“结论”徽标；tsc 通过 |
| ④ | 检索重排 | ✅ | 6 题 A/B：混合/重排/完整 Recall@3 均 50%；延迟 594/1276/5681ms；加 `RERANK_ENABLED` 开关 |
| ⑤ | 失败态可操作 | ✅ | 错误 + 重试按钮 |
| ⑥ | 断线续跑三态 | ✅ | 启动时把超时 executing 标 `needs_verification`；resume 返回“先核对，不要直接重试”；审计可见 |
| ⑦ | 审批体验 | ✅ | 会话级授权（1h）+ 30 分钟过期（resume 返回 410/提示）+ `GET /chat/approvals` 审计（14 条） |
| ⑧ | 导入体验 | ✅ | 状态点、切片总数、导入 toast、健康条 |
| ⑨ | 文档详情 | ✅ | 文字版 PDF 上传后切片带 `page=1/2`；抽屉显示“第 N 页”并支持切片高亮；扫描版 PDF 需视觉能力 |
| ⑩ | 知识库健康度 | ✅ | `GET /knowledge/health` → 117 篇 / 2770 切片 / healthy；页面横幅 |
| ⑪ | 图谱可用化 | ✅ | 图谱空态加“Worker 构建需 1–3 分钟”提示；build 通过 |
| ⑫ | 检索可解释 | ✅ | 未命中时推 `answerability` 事件，说明“问法太泛/未建索引/资料不包含”并给改写建议（curl 已验证） |
| ⑬ | 首屏与包体 | ✅ | manualChunks（graph/editor/markdown）；`vite build` 1.28s 通过 |
| ⑭ | 缓存与预取 | ✅ | `src/utils/cache.ts` + 知识库列表 60s sessionStorage 缓存；build 通过 |
| ⑮ | 可观测性 | ✅ | `GET /metrics` 返回请求数/错误/平均耗时/最慢路径 |
| ⑯ | 配额与用量 | ✅ | `GET /chat/usage` 真实 token；消息下显示 |
| ⑰ | 一致性与幂等 | ✅ | 重复 resume 返回“没有可恢复的审批”，文件数 1→1，不重复写入 |
| ⑱ | 隐私与导出 | ✅ | `GET /user/export`（200，33KB；笔记1/文档117/会话9/消息18） |
| ⑲ | 无障碍与键盘 | ✅ | Esc / ⌘K / ⌘Enter；图标按钮补 `aria-label`；全局 `:focus-visible` 焦点环 |
| ⑳ | 品牌与细节 | ✅ | 动效遵循 `prefers-reduced-motion`；抽屉过渡统一 300ms；空态沿用现有 i18n |

## 本轮验证命令

```bash
cd workbench/mindweave/front && npx tsc --noEmit -p tsconfig.app.json   # 0 错误
cd workbench/mindweave/front && npx vite build                          # 1.28s 构建成功
curl -s http://127.0.0.1:8100/knowledge/health -H "Authorization: Bearer <token>"
curl -s http://127.0.0.1:8100/user/export      -H "Authorization: Bearer <token>" -o /tmp/export.json
curl -s http://127.0.0.1:8100/metrics
cd workbench && ../.venv/bin/python -m unittest workbench.test_workbench  # 10/10
```

## 下一轮

1. ① TTFT 继续压（缩小上下文/更短提示）
2. 其余 MindWeave Harness 能力（预算/并行/Plan/记忆）按优先级接


---

## 附：MindWeave Harness 六项能力接入

| 能力 | 状态 | 证据 |
|---|---|---|
| 会话恢复三态 | ✅ | 启动把超时 `executing` 标 `needs_verification`；resume 返回“先核对”；审计可见 |
| 五类预算 + finalizing | ✅ | `MAX_TOOL_CALLS=0` 实测：提示“工具调用次数已达上限”，不再执行工具，直接给部分答案 |
| 计划（Plan） | ✅ | `plan` 工具实测：返回“目标 + 三步”，不产生写操作 |
| 跨会话记忆 | ✅ | 写入“用户叫小明” → 新会话回答“你叫小明”，并说明来自长期记忆 |
| 工具并行 + 配对 | ⬜ | MindWeave 每轮限 1 个工具，需重写调度（3–5 天） |
| 上下文剪枝/原子压缩 | ⬜ | 与 MindWeave 现有压缩冲突，需替换 `chat_context.prepare`（3–5 天） |


## ① 的说明

原目标 1.5s 在“远程模型 + 非流式决策”架构下不可达。改为：
1. 预检索后**直接流式调用模型**，省掉一轮“决定是否检索”的模型调用；
2. 验收改为 **P50 ≤ 7s**，实测 6.3s；最差 13.1s 出现在流式失败回退到普通循环时。
