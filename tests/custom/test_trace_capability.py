#!/usr/bin/env python3
"""test_trace_capability.py — 入站埋点能力单测（custom）

覆盖：所有 kind 都放行（return False，只观察不消费）、日志含 msgId/kind 字段、
Capability 配置（priority=0 最先跑、handles_kinds 空集=全 kind、不去重、不防回环）。
用 patch.object 捕获 log 输出，不依赖网络/钉钉。
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom.capabilities import trace
from core.inbound import (
    InboundMessage, KIND_TEXT, KIND_IMAGE, KIND_FILE, KIND_FORWARD, KIND_UNKNOWN,
)


def _msg(**kw):
    base = dict(user="hugozhu", text="hi", conv_type="2",
                conv_id="cidABCDEFGHIJKLMNOP==", msg_id="msgXYZ123", kind=KIND_TEXT)
    base.update(kw)
    return InboundMessage(**base)


class TestTracePassThrough(unittest.TestCase):
    def test_returns_false_for_all_kinds(self):
        """只观察不消费：任何 kind 都返回 False，放行给后续业务能力。"""
        for kind in (KIND_TEXT, KIND_IMAGE, KIND_FILE, KIND_FORWARD, KIND_UNKNOWN):
            with patch.object(trace, "log"):
                self.assertFalse(trace.on_inbound(_msg(kind=kind)), f"kind={kind} 应放行")

    def test_self_message_still_passes_through(self):
        """loop_guard=False：自己发的也记且放行（不在此处吞掉）。"""
        with patch.object(trace, "log"):
            self.assertFalse(trace.on_inbound(_msg(user="数字员工")))


class TestTraceLogFields(unittest.TestCase):
    def test_log_contains_id_and_kind(self):
        """日志行须含 msgId= 与 kind= 字段。"""
        with patch.object(trace, "log") as m:
            trace.on_inbound(_msg(msg_id="msgABC", kind=KIND_IMAGE))
        line = m.call_args[0][0]
        self.assertIn("inbound:", line)
        self.assertIn("msgId=msgABC", line)
        self.assertIn("kind=image", line)
        self.assertIn("user=hugozhu", line)
        self.assertIn("conv=2:", line)

    def test_empty_ids_render_placeholder(self):
        """缺 msgId/user 时用占位符，不抛异常。"""
        with patch.object(trace, "log") as m:
            trace.on_inbound(_msg(msg_id="", user="", conv_type="", conv_id=""))
        line = m.call_args[0][0]
        self.assertIn("msgId=-", line)
        self.assertIn("user=-", line)
        self.assertIn("conv=?:", line)

    def test_conv_id_truncated(self):
        """conv_id 只取前缀（16 字符），避免长 ID 撑爆日志。"""
        with patch.object(trace, "log") as m:
            trace.on_inbound(_msg(conv_id="c" * 40))
        line = m.call_args[0][0]
        self.assertIn("conv=2:" + "c" * 16, line)
        self.assertNotIn("c" * 17, line)

    def test_suspect_forward_marker(self):
        """合并转发以 kind=text 到达，摘要命中「聊天记录」→ 标注 suspect-forward。"""
        with patch.object(trace, "log") as m:
            trace.on_inbound(_msg(text="群聊的聊天记录\nhugozhu:[消息]", kind=KIND_TEXT))
        self.assertIn("kind=text(suspect-forward)", m.call_args[0][0])

    def test_plain_text_no_suspect_marker(self):
        with patch.object(trace, "log") as m:
            trace.on_inbound(_msg(text="1+1", kind=KIND_TEXT))
        line = m.call_args[0][0]
        self.assertIn("kind=text", line)
        self.assertNotIn("suspect-forward", line)


class TestTraceConfig(unittest.TestCase):
    def test_capability_wiring(self):
        cap = trace.CAPABILITY
        self.assertEqual(cap.name, "trace")
        self.assertEqual(cap.priority, 0)          # 最先跑，先于 ack(1)
        self.assertEqual(cap.handles_kinds, set())  # 空集 = 所有 kind
        self.assertFalse(cap.dedup)                 # 每次收到都记（含重投）
        self.assertFalse(cap.loop_guard)            # 自己发的也记
        self.assertTrue(cap.default_enabled)

    def test_runs_before_ack(self):
        """在启用能力列表里，trace 排在 ack 之前（priority 决定顺序）。"""
        from core.capabilities import enabled_capabilities
        names = [c.name for c in enabled_capabilities()]
        self.assertIn("trace", names)
        if "ack" in names:
            self.assertLess(names.index("trace"), names.index("ack"))


if __name__ == "__main__":
    unittest.main()
