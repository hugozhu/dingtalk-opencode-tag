#!/usr/bin/env python3
"""test_brain.py — core/brain.py 的在跑任务登记表（协议层）单测

覆盖 register_inflight / list_inflight —— core 只定义协议，实现由 custom 注册。
重点是**降级行为**：这条路径唯一的调用方是 /reboot 通知，任何情况下都不能抛异常，
否则可观测性会挡住重启本身。

不依赖网络、不依赖 custom 实现：用 patch.object 直接注入假实现。
"""

import os
import sys
import time
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from core import brain as core_brain


class TestListInflight(unittest.TestCase):
    """Test list_inflight — 归一 + 排序 + 全方位降级。"""

    @patch.object(core_brain, "_inflight_impl", new=None)
    def test_unregistered_returns_empty(self):
        self.assertEqual(core_brain.list_inflight(), [])

    def test_impl_raising_returns_empty(self):
        def boom():
            raise RuntimeError("backend exploded")
        with patch.object(core_brain, "_inflight_impl", new=boom):
            self.assertEqual(core_brain.list_inflight(), [])

    def test_impl_returning_none_returns_empty(self):
        with patch.object(core_brain, "_inflight_impl", new=lambda: None):
            self.assertEqual(core_brain.list_inflight(), [])

    def test_sorted_by_started_ascending(self):
        now = time.time()
        recs = [
            {"conv_id": "c2", "sid": "s2", "title": "新的", "started": now - 10},
            {"conv_id": "c1", "sid": "s1", "title": "最久的", "started": now - 600},
            {"conv_id": "c3", "sid": "s3", "title": "中间", "started": now - 60},
        ]
        with patch.object(core_brain, "_inflight_impl", new=lambda: recs):
            out = core_brain.list_inflight()
        # 跑得最久的排最前 —— 重启时最该被看见的就是它
        self.assertEqual([r["sid"] for r in out], ["s1", "s3", "s2"])
        self.assertGreater(out[0]["elapsed"], out[-1]["elapsed"])

    def test_title_normalized(self):
        recs = [{"conv_id": "c", "sid": "s", "title": "a\n\nb   c", "started": time.time()}]
        with patch.object(core_brain, "_inflight_impl", new=lambda: recs):
            self.assertEqual(core_brain.list_inflight()[0]["title"], "a b c")

    def test_missing_fields_tolerated(self):
        # 实现少给字段也不能炸；elapsed 归 0 而不是「从 1970 年跑到现在」
        with patch.object(core_brain, "_inflight_impl", new=lambda: [{}]):
            out = core_brain.list_inflight()
        self.assertEqual(out[0]["sid"], "")
        self.assertEqual(out[0]["title"], "")
        self.assertEqual(out[0]["elapsed"], 0.0)

    def test_garbage_started_does_not_raise(self):
        recs = [{"conv_id": "c", "sid": "s", "title": "t", "started": "not-a-number"}]
        with patch.object(core_brain, "_inflight_impl", new=lambda: recs):
            self.assertEqual(core_brain.list_inflight()[0]["elapsed"], 0.0)

    def test_register_inflight_installs_impl(self):
        try:
            core_brain.register_inflight(lambda: [
                {"conv_id": "c", "sid": "sid1", "title": "t", "started": time.time()}])
            self.assertEqual(core_brain.list_inflight()[0]["sid"], "sid1")
        finally:
            core_brain._inflight_impl = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
