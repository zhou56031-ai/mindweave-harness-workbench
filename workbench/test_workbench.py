"""合并切片的质量与接口测试。"""

from __future__ import annotations

import json
import unittest

from unittest.mock import patch

from fastapi.testclient import TestClient

from workbench.answer import compose_answer, is_grounded
from workbench.knowledge import get_index
from workbench.mw_server import app


class RetrievalQualityTest(unittest.TestCase):
    """自然语言问题应该命中正确的文档。"""

    CASES = [
        ("缓存击穿怎么处理", "缓存与检索"),
        ("缓存穿透和雪崩的区别", "缓存与检索"),
        ("RRF 融合为什么只比较名次", "缓存与检索"),
        ("混合检索为什么需要重排", "缓存与检索"),
        ("知识图谱怎么做实体抽取", "AI-Agent功能全景说明"),
        ("审批流怎么防止重复执行", "AI-Agent功能全景说明"),
    ]

    def test_expected_doc_in_top3(self) -> None:
        index = get_index()
        for query, expected in self.CASES:
            with self.subTest(query=query):
                docs = [hit.doc_id for hit in index.search(query, 3)]
                self.assertIn(expected, docs, f"{query} → {docs}")

    def test_results_are_diverse(self) -> None:
        index = get_index()
        hits = index.search("知识图谱怎么做实体抽取", 5)
        self.assertGreaterEqual(len({hit.doc_id for hit in hits}), 2)


class AnswerQualityTest(unittest.TestCase):
    """回答必须是可读的抽取式答案，而不是代码/表格倾倒。"""

    QUERIES = ["缓存击穿怎么处理？", "混合检索为什么需要重排？", "审批流怎么防止重复执行"]

    def test_answer_shape(self) -> None:
        index = get_index()
        for query in self.QUERIES:
            with self.subTest(query=query):
                answer = compose_answer(query, index.search(query, 5))
                self.assertIn("**结论**", answer)
                self.assertIn("**来源**", answer)
                self.assertRegex(answer, r"\[\d+\]")
                self.assertNotIn("```", answer)
                self.assertNotIn("|", answer)
                self.assertGreater(len(answer), 80)
                self.assertLess(len(answer), 900)

    def test_grounding_decision(self) -> None:
        index = get_index()
        self.assertTrue(is_grounded("缓存击穿怎么处理？", index.search("缓存击穿怎么处理？", 5)))
        self.assertFalse(is_grounded("帮我解释一下量子计算", index.search("帮我解释一下量子计算", 5)))

    def test_unknown_topic_is_explicit(self) -> None:
        answer = compose_answer("量子引力的弦论证明", [])
        self.assertIn("没有检索到", answer)


class ApiContractTest(unittest.TestCase):
    """前端契约：登录、知识库、SSE 三种事件。"""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_login(self) -> None:
        response = self.client.post("/user/login/", json={"username": "demo", "password": "x"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["token"])

    def test_knowledge_list(self) -> None:
        data = self.client.get("/knowledge/list").json()["data"]
        self.assertGreater(data["total_count"], 100)

    def test_knowledge_detail(self) -> None:
        data = self.client.get("/knowledge/detail", params={"filename": "缓存与检索.md"}).json()["data"]
        self.assertEqual(data["filename"], "缓存与检索.md")
        self.assertGreater(data["chunk_count"], 0)
        self.assertIn("缓存击穿", data["content"])

    def test_upload_then_search_then_delete(self) -> None:
        content = "# 上传测试\n\n## 唯一章节\n\n帆船测试专用段落，用于验证上传闭环。\n"
        files = {"files": ("上传测试.md", content.encode(), "text/markdown")}
        with self.client.stream("POST", "/knowledge/add/multiple/stream", files=files) as response:
            body = "".join(response.iter_text())
        self.assertIn("completed", body)

        before = self.client.get("/knowledge/list").json()["data"]["total_count"]
        self.assertGreaterEqual(before, 118)

        hits = get_index().search("帆船测试专用段落", 1)
        self.assertEqual(hits[0].doc_id, "上传测试")

        self.client.delete("/knowledge/delete/filename", params={"filename": "上传测试.md"})
        after = self.client.get("/knowledge/list").json()["data"]["total_count"]
        self.assertEqual(after, before - 1)

    def test_chat_stream_contract(self) -> None:
        # 固定走抽取式回退，避免测试依赖外网模型
        with patch("workbench.llm.stream_answer", return_value=None), \
                self.client.stream("POST", "/chat/agent/query/stream",
                                   json={"query": "缓存击穿怎么处理？"}) as response:
            self.assertEqual(response.status_code, 200)
            body = "".join(response.iter_text())

        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        types = [event["type"] for event in events]
        self.assertIn("thinking", types)
        self.assertIn("response", types)
        self.assertEqual(types[-1], "done")
        stages = [event.get("stage") for event in events if event["type"] == "thinking"]
        for stage in ("agentic_plan", "tool_start", "local_retrieval", "tool_end", "answerability"):
            self.assertIn(stage, stages)
        text = "".join(event.get("content", "") for event in events if event["type"] == "response")
        self.assertIn("**结论**", text)
        self.assertIn("[1]", text)


if __name__ == "__main__":
    unittest.main()
