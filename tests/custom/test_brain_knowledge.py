#!/usr/bin/env python3
"""brain.py 主管知识注入单测（配合 capabilities/supervisor_review 的学习闭环）。

注意：_KNOWLEDGE_FILE/_KNOWLEDGE_MAX 在 import 时读 env（与本仓库其它模块一致），
故测试用 patch.object 改**模块属性**，不是改 os.environ。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from custom import brain  # noqa: E402


class TestKnowledgeInjection(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.kf = os.path.join(self.tmpdir, "qa.jsonl")
        # 绝对路径 → _knowledge_path 直接返回它，不依赖 PROJECT_DIR
        self.p_file = patch.object(brain, "_KNOWLEDGE_FILE", self.kf)
        self.p_max = patch.object(brain, "_KNOWLEDGE_MAX", 3)
        self.p_file.start()
        self.p_max.start()
        # 清缓存，避免用例间串扰
        brain._knowledge_cache = {"mtime": None, "text": ""}

    def tearDown(self):
        self.p_file.stop()
        self.p_max.stop()
        brain._knowledge_cache = {"mtime": None, "text": ""}
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, records):
        with open(self.kf, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def test_no_file_returns_empty(self):
        """还没学到东西（文件不存在）→ 空串，system prompt 零变化。"""
        self.assertEqual(brain._load_knowledge(), "")

    def test_empty_file_returns_empty(self):
        open(self.kf, "w").close()
        self.assertEqual(brain._load_knowledge(), "")

    def test_loads_last_max_entries(self):
        """只取末 MAX 条（最近学到的优先）。"""
        self._write([{"question": f"Q{i}", "answer": f"A{i}"} for i in range(5)])
        out = brain._load_knowledge()
        for keep in ("Q2", "Q3", "Q4"):
            self.assertIn(keep, out)
        for drop in ("Q0", "Q1"):
            self.assertNotIn(drop, out)

    def test_cache_hit_when_unchanged(self):
        """文件没变 → 复用缓存，不重读盘。"""
        self._write([{"question": "Q1", "answer": "A1"}])
        self.assertIn("Q1", brain._load_knowledge())
        # 直接改缓存内容；文件 mtime 未变，应原样返回缓存
        brain._knowledge_cache["text"] = "CACHED"
        self.assertEqual(brain._load_knowledge(), "CACHED")

    def test_cache_invalidated_on_change(self):
        """文件变了 → 缓存失效重读。"""
        self._write([{"question": "Q1", "answer": "A1"}])
        self.assertIn("Q1", brain._load_knowledge())
        # 直接把 mtime 打回 None 模拟变更检测（避免依赖文件系统时间分辨率）
        brain._knowledge_cache["mtime"] = None
        self._write([{"question": "Q1", "answer": "A1"},
                     {"question": "Q2", "answer": "A2"}])
        out = brain._load_knowledge()
        self.assertIn("Q2", out)

    def test_max_zero_disables(self):
        """MAX=0 关闭注入。"""
        self._write([{"question": "Q1", "answer": "A1"}])
        with patch.object(brain, "_KNOWLEDGE_MAX", 0):
            self.assertEqual(brain._load_knowledge(), "")

    def test_malformed_line_skipped(self):
        """一行脏数据不该丢掉整个知识库。"""
        with open(self.kf, "w", encoding="utf-8") as f:
            f.write(json.dumps({"question": "Q1", "answer": "A1"}) + "\n")
            f.write("NOT JSON\n")
            f.write(json.dumps({"question": "Q2", "answer": "A2"}) + "\n")
        out = brain._load_knowledge()
        self.assertIn("Q1", out)
        self.assertIn("Q2", out)
        self.assertNotIn("NOT JSON", out)

    def test_incomplete_record_skipped(self):
        """缺 question 或 answer 的记录跳过。"""
        self._write([{"question": "Q1", "answer": ""},
                     {"question": "", "answer": "A2"},
                     {"question": "Q3", "answer": "A3"}])
        out = brain._load_knowledge()
        self.assertIn("Q3", out)
        self.assertNotIn("Q1", out)

    def test_effective_prompt_appends_knowledge(self):
        """有知识 → system prompt = 基础人设 + 知识段。"""
        self._write([{"question": "报销流程", "answer": "找财务小王"}])
        p = brain._effective_system_prompt()
        self.assertTrue(p.startswith(brain._SYSTEM_PROMPT))
        self.assertIn("报销流程", p)
        self.assertIn("找财务小王", p)

    def test_effective_prompt_unchanged_without_knowledge(self):
        """无知识 → 与原 _SYSTEM_PROMPT 完全一致（零行为变化）。"""
        self.assertEqual(brain._effective_system_prompt(), brain._SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
