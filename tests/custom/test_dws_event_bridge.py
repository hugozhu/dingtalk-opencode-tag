#!/usr/bin/env python3
"""test_dws_event_bridge.py — dws_event_bridge NDJSON → connect-log 转换单测（custom）

重点覆盖 @我(at) 事件的订阅链路末端：dws event consume user_im_message_receive_at
的 NDJSON 被 bridge 正确转成 event_watcher 能解析的 "[connect] 收到 @user: text
(convType=2 ...)" 行。group/o2o 一并回归，防 convType 映射漂移。
"""

import importlib.util
import json
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BRIDGE_PATH = os.path.join(PROJECT_ROOT, "bin", "custom", "dws_event_bridge.py")

# bridge 是脚本（非包内模块），按路径动态加载
_spec = importlib.util.spec_from_file_location("dws_event_bridge", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def _event(etype, sender="hugozhu", content="hi", conv="cidABC==",
           msg="msg123==", wrap_data=True):
    """构造一个 dws event consume NDJSON 事件对象（data 是二层 JSON 字符串）。"""
    body = {
        "sender": sender,
        "content": content,
        "openConversationId": conv,
        "openMessageId": msg,
        "createTime": "1700000000000",
    }
    data = {"payload": {"body": body}}
    return {
        "type": "event",
        "event_type": etype,
        "event_id": "ev-1",
        "data": json.dumps(data, ensure_ascii=False) if wrap_data else data,
    }


class TestToConnectLine(unittest.TestCase):
    def test_at_event_maps_to_group_convtype(self):
        """@我(at) 事件 → convType=2（群语境），字段齐全。"""
        line = bridge._to_connect_line(
            _event("user_im_message_receive_at",
                   sender="hugozhu", content="@Claude Code 帮我看下",
                   conv="cidAT==", msg="msgAT=="))
        self.assertIsNotNone(line)
        self.assertIn("[connect] 收到 @hugozhu: @Claude Code 帮我看下", line)
        self.assertIn("convType=2", line)
        self.assertIn("convId=cidAT==", line)
        self.assertIn("msgId=msgAT==", line)

    def test_at_line_is_parseable_by_inbound(self):
        """bridge 产出的 @我 行必须能被 core.inbound.parse_line 解析（契约对齐）。"""
        import sys
        src = os.path.join(PROJECT_ROOT, "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from core import inbound
        line = bridge._to_connect_line(
            _event("user_im_message_receive_at", sender="u", content="1+1",
                   conv="cidX==", msg="msgY=="))
        m = inbound.parse_line(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.user, "u")
        self.assertEqual(m.text, "1+1")
        self.assertEqual(m.conv_type, "2")
        self.assertEqual(m.conv_id, "cidX==")
        self.assertEqual(m.msg_id, "msgY==")
        self.assertEqual(m.kind, inbound.KIND_TEXT)

    def test_group_and_o2o_convtype(self):
        g = bridge._to_connect_line(_event("user_im_message_receive_group"))
        self.assertIn("convType=2", g)
        o = bridge._to_connect_line(_event("user_im_message_receive_o2o"))
        self.assertIn("convType=1", o)

    def test_unknown_event_defaults_group(self):
        line = bridge._to_connect_line(_event("some_future_event"))
        self.assertIn("convType=2", line)

    def test_newlines_collapsed(self):
        line = bridge._to_connect_line(
            _event("user_im_message_receive_at", content="line1\nline2"))
        self.assertIn("line1 line2", line)
        self.assertNotIn("\n", line.rstrip("\n"))

    def test_empty_content_dropped(self):
        self.assertIsNone(bridge._to_connect_line(
            _event("user_im_message_receive_at", content="")))
        self.assertIsNone(bridge._to_connect_line(
            _event("user_im_message_receive_at", content="   ")))

    def test_no_data_returns_none(self):
        self.assertIsNone(bridge._to_connect_line(
            {"type": "event", "event_type": "user_im_message_receive_at"}))

    def test_bad_inner_json_returns_none(self):
        evt = {"type": "event", "event_type": "user_im_message_receive_at",
               "event_id": "x", "data": "{not-json"}
        self.assertIsNone(bridge._to_connect_line(evt))

    def test_event_key_fallback(self):
        """有的 dws 版本用 event_key 而非 event_type，映射仍需生效。"""
        evt = _event("ignored")
        del evt["event_type"]
        evt["event_key"] = "user_im_message_receive_o2o"
        line = bridge._to_connect_line(evt)
        self.assertIn("convType=1", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
