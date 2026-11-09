# 合并工作台功能清单与测评

> 日期：2026-11-07 ｜ 前端：MindWeave 原版（3100）｜ 后端：`workbench/mw_server.py`（8100）
> 结论：**核心链路（登录 → 检索 → 对话 → 引用 → 知识库详情/上传/删除）已跑通，9 个自动化测试全过。**
> 未完成：笔记编辑闭环、复习、模板、图谱、用量详情。

---

## 1. 本轮修了什么

| 问题 | 现象 | 修法 |
|---|---|---|
| 知识库文档“看不到内容” | 列表能出，点开详情是空/报错 | 补 `GET /knowledge/detail`：返回正文、切片数、创建时间 |
| 上传没有反应 | 选文件后一直 pending / 失败 | 补 `POST /knowledge/add/multiple/stream`（SSE：processing→completed→finish）+ 动态入索引 |
| 删除无效 | 删了还在 | 补 `DELETE /knowledge/delete/filename` 与 `/knowledge/clean`，删除后重建索引 |
| 切片看不到 | 详情抽屉里的切片为空 | 补 `GET /knowledge/chunks?filename=` |
| 回答是“第一段” | 代码/表格全进答案 | 之前已改为“模型生成 + 抽取式回退” |

---

## 2. 功能矩阵

图例：✅ 可用且有测试 ｜ ⚠️ 可用但数据为空 ｜ ❌ 未实现

| 页面/功能 | 依赖接口 | 状态 | 说明 |
|---|---|---|---|
| 登录 / 注册 | `/user/login/`、`/user/register/`、`/user/detail/` | ✅ | 演示模式，任意账号 |
| AI 对话 | `/chat/agent/query/stream` | ✅ | 真实模型（deepseek-flash）+ 引用；模型失败回退抽取式 |
| 对话思考步骤 | 同上 SSE | ✅ | `agentic_plan` / `local_retrieval`（searching / evidence） |
| 会话列表 | `/chat/sessions/{user}` | ✅ | 按更新时间倒序 |
| 会话详情（历史） | `/chat/session/{id}` | ✅ | 返回 user/assistant 历史 |
| 删除会话 | `DELETE /chat/session/{id}` | ✅ | |
| 知识库列表 | `/knowledge/list` | ✅ | 117 篇 / 2,473 片 |
| 知识库详情 | `/knowledge/detail` | ✅ | 正文 + 切片数 |
| 知识库切片 | `/knowledge/chunks` | ✅ | 按 doc 过滤 |
| 上传文档 | `/knowledge/add/multiple/stream` | ✅ | 落盘 + 入索引 + 立即可检索 |
| 删除文档 | `/knowledge/delete/filename` | ✅ | 删除后重建索引 |
| 清空向量库 | `/knowledge/clean` | ✅ | 清 uploads + 重建 |
| MD5 列表 | `/knowledge/md5/list` | ✅ | 文档清单 |
| 笔记列表 | `/note/list` | ✅ | 本地 JSON |
| 新建笔记 | `/note/create` | ✅ | |
| 笔记详情/编辑/删除 | `/note/{id}` | ❌ | 下一步 |
| 笔记搜索/统计 | `/note/search`、`/note/stats` | ⚠️ | 统计有总数，搜索未实现 |
| 每日复习 | `/review/today` | ⚠️ | 返回空列表 |
| 笔记模板 | `/note-template/list` | ⚠️ | 返回空列表 |
| 知识图谱 | `/api/graph/overview` | ⚠️ | 返回空图，页面不崩 |
| 用量统计 | `/chat/usage` | ⚠️ | 返回空数据 |

---

## 3. 测评结果

### 3.1 自动化测试（9/9 通过）

```text
workbench.test_workbench
  RetrievalQualityTest
    test_expected_doc_in_top3      6 个自然语言问题 → 期望文档在 top3   ✅
    test_results_are_diverse       结果来自 ≥2 篇文档                  ✅
  AnswerQualityTest
    test_answer_shape              结论/来源/引用、无代码块、无表格      ✅
    test_unknown_topic_is_explicit 无资料时明确说“没有检索到”            ✅
  ApiContractTest
    test_login                     登录返回 token                     ✅
    test_knowledge_list            文档数 > 100                       ✅
    test_knowledge_detail          正文含“缓存击穿”                     ✅
    test_upload_then_search_then_delete  上传→可检索→删除→数量回落     ✅
    test_chat_stream_contract      SSE：thinking→response→done         ✅
```

### 3.2 检索质量（6 题）

| 问题 | 期望文档 | Top3 是否命中 |
|---|---|---|
| 缓存击穿怎么处理 | 缓存与检索 | ✅ |
| 缓存穿透和雪崩的区别 | 缓存与检索 | ✅ |
| RRF 融合为什么只比较名次 | 缓存与检索 | ✅ |
| 混合检索为什么需要重排 | 缓存与检索 | ✅ |
| 知识图谱怎么做实体抽取 | AI-Agent功能全景说明 | ✅ |
| 审批流怎么防止重复执行 | AI-Agent功能全景说明 | ✅ |

### 3.3 回答质量（真实模型样例）

问：`混合检索为什么需要重排？`

```text
**结论**：因为 RRF 融合结果只反映两路各自的名次，无法判断候选是否真能回答问题；
因此必须再加一步重排，对候选逐条判断[1]。

**要点**：…（3 条，均带 [n]）
**适用边界**：资料没说明重排器选型、候选截断数量与评测方式；需另查 Q23 正文。

> 回答由 deepseek-flash 基于检索结果生成。
```

### 3.4 上传闭环

```text
上传 测试上传.md → 列表 117 → 118
检索“帆船测试专用段落” → Top1 = 测试上传 / 唯一章节
删除 → 列表回到 117
```

---

## 4. 待优化（按优先级）

| 优先级 | 事项 | 说明 |
|---|---|---|
| P0 | 笔记编辑闭环 | `/note/{id}` 的 GET/PUT/DELETE、搜索 |
| P0 | 知识库详情里的图片 | PDF 页面图未实现（当前只支持文本） |
| P1 | 真实 RAG | 向量 + 图谱 + 重排（需要 Neo4j / embedding） |
| P1 | 会话续跑与审批 | 写操作 pending→executing→completed |
| P2 | 复习 / 模板 / 图谱 | 目前是空实现，页面不崩但没有内容 |
| P2 | 用量统计 | 记录每次对话的 token |

---

## 5. 复跑

```bash
# 测试
./.venv/bin/python -m unittest workbench.test_workbench -v

# 后端
./.venv/bin/python -m workbench.mw_server

# 前端
cd workbench/mindweave/front && npm run dev
```
