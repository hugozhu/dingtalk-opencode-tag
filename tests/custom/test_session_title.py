#!/usr/bin/env python3
"""test_session_title.py — serve session title 生成单测（#89）

覆盖 custom/brain.py 的 _session_title：让 opencode 后台会话列表可辨识来源，
不再全是 "agent-textreply"。校验：
  1. 群聊带 [群] 标记 + 发送者 + 摘要。
  2. 单聊带 [私] 标记。
  3. 摘要去掉 "user：" 冗余前缀。
  4. 多行/多空白折叠为单空格。
  5. 超长摘要截断并加省略号。
  6. 无 ctx / 无摘要回退到 default（向后兼容 e2e 直调）。
  7. _create_session 缺省 title 仍回退旧值 "agent-textreply"。

不依赖网络。
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom import brain


class TestSessionTitle(unittest.TestCase):
    def test_group_marker_user_summary(self):
        t = brain._session_title({"conv_type": "2", "user": "张三"}, "看下这个报错")
        self.assertEqual(t, "[群] 张三 · 看下这个报错")

    def test_o2o_marker(self):
        t = brain._session_title({"conv_type": "1", "user": "李四"}, "image")
        self.assertEqual(t, "[私] 李四 · image")

    def test_strips_user_prefix(self):
        # prompt 常形如 "user：text"，摘要里去掉冗余前缀
        t = brain._session_title({"conv_type": "2", "user": "张三"}, "张三：你好")
        self.assertEqual(t, "[群] 张三 · 你好")

    def test_collapses_whitespace(self):
        t = brain._session_title({"conv_type": "1", "user": "李四"}, "a\n\nb   c")
        self.assertEqual(t, "[私] 李四 · a b c")

    def test_truncates_long_summary(self):
        t = brain._session_title({"conv_type": "2", "user": "王五"}, "很" * 40)
        self.assertTrue(t.endswith("…"))
        # 头部 "[群] 王五 · " + 24 字 + 省略号
        self.assertIn("[群] 王五 · ", t)
        self.assertEqual(t.count("很"), 24)

    def test_fallback_default_when_empty(self):
        self.assertEqual(brain._session_title({}, ""), "agent-textreply")
        self.assertEqual(brain._session_title(None, "", default="x"), "x")

    def test_unknown_conv_type_no_marker(self):
        t = brain._session_title({"conv_type": "", "user": "赵六"}, "hi")
        self.assertEqual(t, "赵六 · hi")

    def test_create_session_default_title_backward_compat(self):
        captured = {}

        def fake_serve(method, port, pwd, path, body=None, timeout=8):
            captured["body"] = body
            return {"id": "sid_x"}

        with patch.object(brain, "_serve_request", side_effect=fake_serve):
            with patch.object(brain, "_OPENCODE_PERMISSION", None):
                sid = brain._create_session("p", "w")  # 不传 title
        self.assertEqual(sid, "sid_x")
        self.assertEqual(captured["body"]["title"], "agent-textreply")

    def test_create_session_uses_given_title(self):
        captured = {}

        def fake_serve(method, port, pwd, path, body=None, timeout=8):
            captured["body"] = body
            return {"id": "sid_y"}

        with patch.object(brain, "_serve_request", side_effect=fake_serve):
            with patch.object(brain, "_OPENCODE_PERMISSION", None):
                sid = brain._create_session("p", "w", "[群] 张三 · 看下")
        self.assertEqual(sid, "sid_y")
        self.assertEqual(captured["body"]["title"], "[群] 张三 · 看下")


if __name__ == "__main__":
    unittest.main()
