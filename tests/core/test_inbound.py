#!/usr/bin/env python3
"""test_inbound.py — InboundMessage 解析 + kind 分类单测（core）"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from core import inbound
from core.inbound import KIND_TEXT, KIND_IMAGE, KIND_REBOOT


class TestParseLine(unittest.TestCase):
    def _line(self, user, text, conv="cidABC==", mid="msgXYZ==", ctype=2):
        return f"[connect] 收到 @{user}: {text} (convType={ctype} convId={conv} msgId={mid})"

    def test_text_message_full_fields(self):
        m = inbound.parse_line(self._line("hugozhu", "1+1"))
        self.assertEqual(m.user, "hugozhu")
        self.assertEqual(m.text, "1+1")
        self.assertEqual(m.conv_type, "2")
        self.assertEqual(m.conv_id, "cidABC==")
        self.assertEqual(m.msg_id, "msgXYZ==")
        self.assertEqual(m.kind, KIND_TEXT)

    def test_image_kind(self):
        m = inbound.parse_line(self._line("u", "[图片]", ctype=1))
        self.assertEqual(m.kind, KIND_IMAGE)

    def test_at_message_parsed_as_group_text(self):
        # @我(at) 事件经 bridge 产出 convType=2 的普通文本行，与群消息同路
        m = inbound.parse_line(self._line("hugozhu", "@Claude Code 帮我看下", ctype=2))
        self.assertEqual(m.user, "hugozhu")
        self.assertEqual(m.text, "@Claude Code 帮我看下")
        self.assertEqual(m.conv_type, "2")
        self.assertEqual(m.kind, KIND_TEXT)

    def test_image_marker_kind(self):
        # event-consume 格式：[图片消息](mediaId=...)，可带 caption
        m = inbound.parse_line(self._line("u", "[图片消息](mediaId=$abc)"))
        self.assertEqual(m.kind, KIND_IMAGE)
        m2 = inbound.parse_line(self._line("u", "[图片消息](mediaId=@x)看这里"))
        self.assertEqual(m2.kind, KIND_IMAGE)

    def test_reboot_kind_case_insensitive(self):
        self.assertEqual(inbound.parse_line(self._line("u", "/reboot")).kind, KIND_REBOOT)
        self.assertEqual(inbound.parse_line(self._line("u", "/REBOOT")).kind, KIND_REBOOT)

    def test_non_inbound_line_returns_none(self):
        self.assertIsNone(inbound.parse_line("[connect] agent 已生成回复 (x 1.2s): hi"))
        self.assertIsNone(inbound.parse_line("random log line"))
        self.assertIsNone(inbound.parse_line(""))

    def test_text_stripped(self):
        m = inbound.parse_line(self._line("u", "  hi there  "))
        self.assertEqual(m.text, "hi there")

    def test_classify_direct(self):
        self.assertEqual(inbound.classify("/reboot"), KIND_REBOOT)
        self.assertEqual(inbound.classify("[图片]"), KIND_IMAGE)
        self.assertEqual(inbound.classify("hello"), KIND_TEXT)

    def test_extra_defaults_empty(self):
        m = inbound.parse_line(self._line("u", "hi"))
        self.assertEqual(m.extra, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
