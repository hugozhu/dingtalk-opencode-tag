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


class TestDeclarativeGuards(unittest.TestCase):
    """dedup / loop_guard 声明式预处理（能力零样板，#52 P1）。"""
    def setUp(self):
        C.clear()
        os.environ["AGENT_SELF_NAMES"] = "opencode,数字员工"

    def tearDown(self):
        C.clear()
        os.environ.pop("AGENT_SELF_NAMES", None)

    def _m(self, user="hugozhu", mid="m1"):
        return InboundMessage(user=user, text="x", conv_type="1",
                              conv_id="c", msg_id=mid, kind=KIND_TEXT)

    def test_loop_guard_skips_self(self):
        seen = []
        C.register(C.Capability(name="t", loop_guard=True,
                                on_inbound=lambda m: (seen.append(m.user), True)[1],
                                handles_kinds={KIND_TEXT}, priority=10))
        C.dispatch_inbound(self._m(user="opencode"))   # 自己发的 → 跳过
        C.dispatch_inbound(self._m(user="hugozhu"))    # 他人 → 命中
        self.assertEqual(seen, ["hugozhu"])

    def test_dedup_skips_duplicate_msgid(self):
        seen = []
        C.register(C.Capability(name="t", dedup=True,
                                on_inbound=lambda m: (seen.append(m.msg_id), True)[1],
                                handles_kinds={KIND_TEXT}, priority=10))
        C.dispatch_inbound(self._m(mid="a"))
        C.dispatch_inbound(self._m(mid="a"))   # 重复 → 跳过
        C.dispatch_inbound(self._m(mid="b"))
        self.assertEqual(seen, ["a", "b"])

    def test_dedup_namespace_per_capability(self):
        # 不同能力的去重互不影响：同一 msgId 两个能力都能各看一次
        s1, s2 = [], []
        C.register(C.Capability(name="c1", dedup=True,
                                on_inbound=lambda m: (s1.append(m.msg_id), False)[1],
                                handles_kinds={KIND_TEXT}, priority=5))
        C.register(C.Capability(name="c2", dedup=True,
                                on_inbound=lambda m: (s2.append(m.msg_id), False)[1],
                                handles_kinds={KIND_TEXT}, priority=10))
        C.dispatch_inbound(self._m(mid="z"))
        self.assertEqual(s1, ["z"])
        self.assertEqual(s2, ["z"])

    def test_no_flags_no_guard(self):
        # 不声明则不预处理（保持旧默认行为）
        seen = []
        C.register(C.Capability(name="t",
                                on_inbound=lambda m: (seen.append(m.msg_id), True)[1],
                                handles_kinds={KIND_TEXT}, priority=10))
        C.dispatch_inbound(self._m(user="opencode", mid="a"))
        C.dispatch_inbound(self._m(user="opencode", mid="a"))
        self.assertEqual(seen, ["a", "a"])   # 无 dedup/loop_guard → 都命中


class TestReplyOutcomeSignal(unittest.TestCase):
    """core.replier：send_reply 的 outcome_ok 覆盖广播给 on_reply_sent 的业务成败（#59）。"""

    def setUp(self):
        from core import replier as R
        self.R = R
        R._impl = lambda conv_id, conv_type, text, *, at_user_id=None: True  # 投递恒成功
        self.signals = []
        self._orig = R.dispatch_reply_sent
        R.dispatch_reply_sent = lambda cid, ct, ok: self.signals.append(ok)

    def tearDown(self):
        self.R.dispatch_reply_sent = self._orig
        self.R._impl = None

    def test_default_uses_delivery_result(self):
        self.R.send_reply("cid", "1", "hi")
        self.assertEqual(self.signals, [True])   # 未传 outcome_ok → 用投递结果

    def test_outcome_ok_false_overrides_successful_delivery(self):
        # 投递成功但业务失败（兜底提示）→ 广播 False，让 ack 落失败终态
        self.R.send_reply("cid", "1", "兜底", outcome_ok=False)
        self.assertEqual(self.signals, [False])


if __name__ == "__main__":
    unittest.main(verbosity=2)
