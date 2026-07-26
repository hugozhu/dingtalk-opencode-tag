#!/usr/bin/env python3
"""test_reply_chunk.py — 长回复分片发送单测（custom，#79）

覆盖 custom/replier.py：
  1. _split_text —— 按 \\n 段落边界优先切、每片 ≤ 有效上限、单行超长硬切
  2. _dingtalk_send —— 短文本单发（无「（i/n）」前缀）、长文本多发（每片带前缀、顺序正确）、
     at_user_id 只带第 1 片、某片失败提前返回 False

不依赖网络/钉钉：把 dws 子进程调用（subprocess.run）mock 掉。
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom import replier


def _ok(*a, **k):
    """伪 subprocess.run：返回成功。"""
    m = MagicMock()
    m.returncode = 0
    m.stderr = ""
    return m


class TestSplitText(unittest.TestCase):
    def test_short_returns_single(self):
        self.assertEqual(replier._split_text("hello", 100), ["hello"])

    def test_effective_limit_accounts_for_prefix(self):
        # 刚好等于有效上限（size - margin）时不分片
        size = 50
        limit = size - replier._PREFIX_MARGIN
        self.assertEqual(replier._split_text("x" * limit, size), ["x" * limit])
        # 超过 1 个字符即分片
        self.assertGreater(len(replier._split_text("x" * (limit + 1), size)), 1)

    def test_splits_on_newline_boundary(self):
        size = 40
        limit = size - replier._PREFIX_MARGIN
        lines = ["line-%02d" % i for i in range(20)]
        text = "\n".join(lines)
        chunks = replier._split_text(text, size)
        self.assertGreater(len(chunks), 1)
        for ch in chunks:
            self.assertLessEqual(len(ch), limit)
        # 无损：重组后行序一致（分片以 \n 断开，拼回等价原文的行集合）
        rejoined = "\n".join(chunks).split("\n")
        self.assertEqual(rejoined, lines)

    def test_oversized_single_line_hard_split(self):
        size = 40
        limit = size - replier._PREFIX_MARGIN
        text = "y" * (limit * 3 + 5)  # 一整行、无换行
        chunks = replier._split_text(text, size)
        self.assertGreater(len(chunks), 1)
        for ch in chunks:
            self.assertLessEqual(len(ch), limit)
        self.assertEqual("".join(chunks), text)  # 硬切无损


class TestDingtalkSendChunking(unittest.TestCase):
    def setUp(self):
        # user 模式 + 有效 PROFILE，走真发路径（但 subprocess 被 mock）
        self._patches = [
            patch.object(replier, "_REPLY_MODE", "user"),
            patch.object(replier, "PROFILE", "corp:userid"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _texts_sent(self, run_mock):
        """从 mock 的每次调用里抽出 --text 的值，按调用顺序返回。"""
        out = []
        for call in run_mock.call_args_list:
            cmd = call.args[0]
            out.append(cmd[cmd.index("--text") + 1])
        return out

    def test_short_single_send_no_prefix(self):
        with patch.object(replier.subprocess, "run", side_effect=_ok) as run:
            ok = replier._dingtalk_send("cid123", 1, "简短回复")
        self.assertTrue(ok)
        texts = self._texts_sent(run)
        self.assertEqual(len(texts), 1)
        self.assertEqual(texts[0], "简短回复")
        self.assertNotIn("（1/", texts[0])

    def test_long_multi_send_with_prefix_and_order(self):
        with patch.object(replier, "_CHUNK_CHARS", 40):
            text = "\n".join("行内容-%02d" % i for i in range(30))
            with patch.object(replier.subprocess, "run", side_effect=_ok) as run:
                ok = replier._dingtalk_send("cid123", 1, text)
        self.assertTrue(ok)
        texts = self._texts_sent(run)
        self.assertGreater(len(texts), 1)
        n = len(texts)
        for i, body in enumerate(texts, 1):
            self.assertTrue(body.startswith(f"（{i}/{n}）\n"), body[:20])
        # 去掉前缀重组 → 行序还原
        stripped = [b.split("\n", 1)[1] for b in texts]
        self.assertEqual("\n".join(stripped).split("\n"),
                         text.split("\n"))

    def test_at_user_only_on_first_chunk(self):
        # bot 模式才会带 --at-user-ids
        with patch.object(replier, "_REPLY_MODE", "bot"), \
             patch.object(replier, "ROBOT_CODE", "robot-xyz"), \
             patch.object(replier, "_CHUNK_CHARS", 40):
            text = "\n".join("行-%02d" % i for i in range(30))
            with patch.object(replier.subprocess, "run", side_effect=_ok) as run:
                ok = replier._dingtalk_send("cid123", 2, text, at_user_id="u999")
        self.assertTrue(ok)
        calls = run.call_args_list
        self.assertGreater(len(calls), 1)
        self.assertIn("--at-user-ids", calls[0].args[0])
        for c in calls[1:]:
            self.assertNotIn("--at-user-ids", c.args[0])

    def test_stops_on_first_failure(self):
        # 第 2 片失败 → 返回 False 且不再发后续
        seq = [_ok(), _fail(), _ok(), _ok()]

        def _side(*a, **k):
            return seq.pop(0)

        with patch.object(replier, "_CHUNK_CHARS", 40):
            text = "\n".join("行-%02d" % i for i in range(30))
            with patch.object(replier.subprocess, "run", side_effect=_side) as run:
                ok = replier._dingtalk_send("cid123", 1, text)
        self.assertFalse(ok)
        # 发到第 2 片就停：总调用数 == 2
        self.assertEqual(run.call_count, 2)

    def test_log_mode_never_splits(self):
        with patch.object(replier, "_REPLY_MODE", "log"), \
             patch.object(replier.subprocess, "run", side_effect=_ok) as run:
            ok = replier._dingtalk_send("cid123", 1, "x" * 99999)
        self.assertTrue(ok)
        run.assert_not_called()


def _fail(*a, **k):
    m = MagicMock()
    m.returncode = 1
    m.stderr = "boom"
    return m


if __name__ == "__main__":
    unittest.main(verbosity=2)
