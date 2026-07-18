#!/usr/bin/env python3
"""test_capabilities.py — 能力注册表单测（core）

覆盖：注册/清空、CAP_*_ENABLED 开关、按 priority 排序、kind 路由、短路消费、
classify_line 认领、异常隔离。
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from core import capabilities as C
from core.inbound import InboundMessage, KIND_TEXT, KIND_FORWARD, KIND_IMAGE


class TestRegistry(unittest.TestCase):
    def setUp(self):
        C.clear()
        # 清掉可能残留的开关 env
        for k in list(os.environ):
            if k.startswith("CAP_"):
                del os.environ[k]

    def tearDown(self):
        C.clear()

    def test_kind_routing_and_shortcircuit(self):
        seen = []
        C.register(C.Capability(name="fwd",
                                on_inbound=lambda m: (seen.append("fwd"), True)[1],
                                handles_kinds={KIND_FORWARD}, priority=10))
        C.register(C.Capability(name="text",
                                on_inbound=lambda m: (seen.append("text"), True)[1],
                                handles_kinds={KIND_TEXT}, priority=100))
        self.assertTrue(C.dispatch_inbound(InboundMessage(kind=KIND_TEXT)))
        self.assertEqual(seen, ["text"])          # 只有 text 能力收到
        seen.clear()
        self.assertTrue(C.dispatch_inbound(InboundMessage(kind=KIND_FORWARD)))
        self.assertEqual(seen, ["fwd"])

    def test_priority_order_first_consumer_wins(self):
        seen = []
        # 两个都吃 text，priority 小的先；第一个返回 True 就短路
        C.register(C.Capability(name="a", on_inbound=lambda m: (seen.append("a"), True)[1],
                                handles_kinds={KIND_TEXT}, priority=10))
        C.register(C.Capability(name="b", on_inbound=lambda m: (seen.append("b"), True)[1],
                                handles_kinds={KIND_TEXT}, priority=20))
        C.dispatch_inbound(InboundMessage(kind=KIND_TEXT))
        self.assertEqual(seen, ["a"])             # b 不被调用

    def test_first_returns_false_falls_through(self):
        seen = []
        C.register(C.Capability(name="a", on_inbound=lambda m: (seen.append("a"), False)[1],
                                handles_kinds={KIND_TEXT}, priority=10))
        C.register(C.Capability(name="b", on_inbound=lambda m: (seen.append("b"), True)[1],
                                handles_kinds={KIND_TEXT}, priority=20))
        self.assertTrue(C.dispatch_inbound(InboundMessage(kind=KIND_TEXT)))
        self.assertEqual(seen, ["a", "b"])        # a 放行 → b 消费

    def test_switch_off_via_env(self):
        seen = []
        C.register(C.Capability(name="text", on_inbound=lambda m: (seen.append("t"), True)[1],
                                handles_kinds={KIND_TEXT}, priority=100))
        os.environ["CAP_TEXT_ENABLED"] = "0"
        self.assertFalse(C.dispatch_inbound(InboundMessage(kind=KIND_TEXT)))
        self.assertEqual(seen, [])                # 关掉不参与

    def test_default_enabled_false_needs_switch_on(self):
        seen = []
        C.register(C.Capability(name="agg", on_inbound=lambda m: (seen.append("agg"), True)[1],
                                handles_kinds={KIND_TEXT}, priority=100, default_enabled=False))
        self.assertFalse(C.dispatch_inbound(InboundMessage(kind=KIND_TEXT)))  # 默认关
        os.environ["CAP_AGG_ENABLED"] = "1"
        self.assertTrue(C.dispatch_inbound(InboundMessage(kind=KIND_TEXT)))

    def test_empty_handles_kinds_matches_all(self):
        seen = []
        C.register(C.Capability(name="all", on_inbound=lambda m: (seen.append(m.kind), True)[1],
                                priority=100))
        C.dispatch_inbound(InboundMessage(kind=KIND_IMAGE))
        self.assertEqual(seen, [KIND_IMAGE])

    def test_classify_line_first_non_none_wins(self):
        C.register(C.Capability(name="fwd",
                                classify_line=lambda ln: InboundMessage(kind=KIND_FORWARD)
                                if "chatRecord" in ln else None, priority=10))
        self.assertIsNone(C.classify_line("normal line"))
        m = C.classify_line('... msgtype="chatRecord" ...')
        self.assertIsNotNone(m)
        self.assertEqual(m.kind, KIND_FORWARD)

    def test_exception_isolated(self):
        seen = []

        def boom(m):
            raise RuntimeError("boom")

        C.register(C.Capability(name="bad", on_inbound=boom, handles_kinds={KIND_TEXT}, priority=10))
        C.register(C.Capability(name="good", on_inbound=lambda m: (seen.append("g"), True)[1],
                                handles_kinds={KIND_TEXT}, priority=20))
        # bad 抛异常被隔离，good 仍收到
        self.assertTrue(C.dispatch_inbound(InboundMessage(kind=KIND_TEXT)))
        self.assertEqual(seen, ["g"])

    def test_sse_and_cleanup_dispatch(self):
        C.register(C.Capability(name="s", on_sse_event=lambda e, p, w: e.get("hit", False),
                                on_cleanup=lambda e, st, lk: e.get("chit", False), priority=10))
        self.assertTrue(C.dispatch_sse({"hit": True}, 1, "p"))
        self.assertFalse(C.dispatch_sse({"hit": False}, 1, "p"))
        self.assertTrue(C.dispatch_cleanup({"chit": True}, {}, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
