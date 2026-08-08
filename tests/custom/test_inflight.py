#!/usr/bin/env python3
"""test_inflight.py — custom/brain.py 在跑任务登记表单测

覆盖 _mark_inflight / _clear_inflight / list_inflight。三个关键不变量：

1. **started 跨轮内重建保留** —— 同一轮里 404 失效重建、回空重试都会再 mark 一次；
   若每次重置 started，「已跑 8 分钟」会显示成刚开始，正好在最该看清耗时时骗人。
2. **凭据不外泄** —— list_inflight 是给通知渲染用的，port/pwd 不能跟着流出去。
3. **_clear_inflight 的 sid 匹配** —— 上一轮的 finally 不能清掉新一轮的登记。
"""

import os
import sys
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom import brain


class TestInflight(unittest.TestCase):

    def setUp(self):
        brain._inflight.clear()

    def tearDown(self):
        brain._inflight.clear()

    def test_mark_records_title_and_started(self):
        brain._mark_inflight("cid1", "sid1", 4096, "pw", "[私] 张三 · 你好")
        rec = brain._inflight["cid1"]
        self.assertEqual(rec["sid"], "sid1")
        self.assertEqual(rec["title"], "[私] 张三 · 你好")
        self.assertLessEqual(abs(rec["started"] - time.time()), 5)

    def test_empty_conv_or_sid_skipped(self):
        brain._mark_inflight("", "sid", 1, "p", "t")
        brain._mark_inflight("cid", "", 1, "p", "t")
        self.assertEqual(brain._inflight, {})

    def test_rebuild_preserves_started(self):
        # 模拟：POST 前 mark → 404 失效 → 重建新 sid 再 mark（同一轮对话）
        brain._mark_inflight("cid1", "sid_old", 4096, "pw", "标题")
        original = brain._inflight["cid1"]["started"]
        brain._inflight["cid1"]["started"] = original - 480   # 假装已跑 8 分钟
        brain._mark_inflight("cid1", "sid_new", 4096, "pw", "标题")
        rec = brain._inflight["cid1"]
        self.assertEqual(rec["sid"], "sid_new")          # sid 换新
        self.assertEqual(rec["started"], original - 480)  # 起始时间保留
        self.assertGreater(time.time() - rec["started"], 400)

    def test_rebuild_with_empty_title_keeps_previous(self):
        brain._mark_inflight("cid1", "sid1", 4096, "pw", "原标题")
        brain._mark_inflight("cid1", "sid2", 4096, "pw", "")
        self.assertEqual(brain._inflight["cid1"]["title"], "原标题")

    def test_list_inflight_does_not_leak_credentials(self):
        brain._mark_inflight("cid1", "sid1", 4096, "s3cret-password", "t")
        out = brain.list_inflight()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["conv_id"], "cid1")
        self.assertNotIn("pwd", out[0])
        self.assertNotIn("port", out[0])
        self.assertNotIn("s3cret-password", repr(out))

    def test_list_inflight_empty_when_idle(self):
        self.assertEqual(brain.list_inflight(), [])

    def test_clear_with_matching_sid(self):
        brain._mark_inflight("cid1", "sid1", 1, "p", "t")
        brain._clear_inflight("cid1", "sid1")
        self.assertEqual(brain._inflight, {})

    def test_clear_with_stale_sid_keeps_newer_record(self):
        # 旧一轮的 finally 迟到时，不能把新一轮的登记清掉
        brain._mark_inflight("cid1", "sid_new", 1, "p", "t")
        brain._clear_inflight("cid1", "sid_old")
        self.assertIn("cid1", brain._inflight)

    def test_cancel_inflight_misses_when_empty(self):
        self.assertFalse(brain.cancel_inflight("cid_unknown"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
