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
        """@我(at) 事件 → convType=2（群语境）+ atMention=1 打标，字段齐全。"""
        line = bridge._to_connect_line(
            _event("user_im_message_receive_at",
                   sender="hugozhu", content="@Claude Code 帮我看下",
                   conv="cidAT==", msg="msgAT=="))
        self.assertIsNotNone(line)
        self.assertIn("[connect] 收到 @hugozhu: @Claude Code 帮我看下", line)
        self.assertIn("convType=2", line)
        self.assertIn("convId=cidAT==", line)
        self.assertIn("msgId=msgAT==", line)
        self.assertIn("atMention=1", line)   # #46：被 @ 打标

    def test_at_line_is_parseable_by_inbound(self):
        """bridge 产出的 @我 行必须能被 core.inbound.parse_line 解析（契约对齐），
        且 extra['at_mention']=True。"""
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
        self.assertEqual(m.conv_id, "cidX==")   # atMention 标记不干扰 id 提取
        self.assertEqual(m.msg_id, "msgY==")
        self.assertEqual(m.kind, inbound.KIND_TEXT)
        self.assertEqual(m.extra.get("at_mention"), True)

    def test_group_and_o2o_convtype(self):
        g = bridge._to_connect_line(_event("user_im_message_receive_group"))
        self.assertIn("convType=2", g)
        self.assertNotIn("atMention", g)   # 普通群消息不打标
        o = bridge._to_connect_line(_event("user_im_message_receive_o2o"))
        self.assertIn("convType=1", o)
        self.assertNotIn("atMention", o)   # 单聊不打标

    def test_o2o_all_convtype(self):
        """rule_type=all 的全量单聊订阅也必须判成单聊(convType=1)。

        未登记时 .get 默认回 2(群聊)，单聊消息会被误判成群聊，路由/ack 走错路且不报错。
        """
        o = bridge._to_connect_line(_event("user_im_message_receive_o2o_all"))
        self.assertIn("convType=1", o)
        self.assertNotIn("atMention", o)   # 单聊不打标

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


class TestFlatFormat(unittest.TestCase):
    """新版 dws CLI 的扁平输出格式（字段在事件顶层、type 即事件类型名、无 data 包裹）。

    回归防护：dws 升级后改成扁平格式，旧的嵌套解析会把每条事件都跳过（bridge 处理 0 条、
    数字员工完全不响应）。这组用例锁定扁平格式必须被正确解析。
    """

    @staticmethod
    def _flat(etype, sender="hugozhu", content="hi",
              conv="cidFLAT==", msg="msgFLAT=="):
        return {
            "type": etype,
            "event_id": "ev-flat-1",
            "sender": sender,
            "content": content,
            "conversation_id": conv,
            "message_id": msg,
            "create_time": "2026-07-21 17:15:50",
        }

    def test_flat_o2o(self):
        line = bridge._to_connect_line(self._flat("user_im_message_receive_o2o"))
        self.assertIsNotNone(line)
        self.assertIn("[connect] 收到 @hugozhu: hi", line)
        self.assertIn("convType=1", line)
        self.assertIn("convId=cidFLAT==", line)
        self.assertIn("msgId=msgFLAT==", line)

    def test_flat_group(self):
        line = bridge._to_connect_line(self._flat("user_im_message_receive_group"))
        self.assertIn("convType=2", line)
        self.assertNotIn("atMention", line)

    def test_flat_at_marks_mention(self):
        line = bridge._to_connect_line(self._flat("user_im_message_receive_at"))
        self.assertIn("convType=2", line)
        self.assertIn("atMention=1", line)

    def test_flat_parseable_by_inbound(self):
        """扁平格式产出的行也必须能被 core.inbound.parse_line 解析（端到端契约）。"""
        import sys
        src = os.path.join(PROJECT_ROOT, "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from core import inbound
        line = bridge._to_connect_line(
            self._flat("user_im_message_receive_o2o", sender="u",
                       content="1+1", conv="cidX==", msg="msgY=="))
        m = inbound.parse_line(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.user, "u")
        self.assertEqual(m.text, "1+1")
        self.assertEqual(m.conv_type, "1")
        self.assertEqual(m.conv_id, "cidX==")
        self.assertEqual(m.msg_id, "msgY==")

    def test_flat_empty_content_dropped(self):
        self.assertIsNone(bridge._to_connect_line(
            self._flat("user_im_message_receive_o2o", content="")))

    def test_flat_newlines_collapsed(self):
        line = bridge._to_connect_line(
            self._flat("user_im_message_receive_o2o", content="line1\nline2"))
        self.assertIn("line1 line2", line)


class TestFormatHealthCheck(unittest.TestCase):
    """格式健康检查：收到多条原始事件却解析 0 条 → 告警（这次故障的自检防护）。"""

    def test_warns_when_raw_high_parsed_zero(self):
        """达到阈值条原始事件、解析 0 条、未报过 → 应告警。"""
        self.assertTrue(bridge._should_warn_format(
            bridge._FORMAT_WARN_THRESHOLD, 0, False))

    def test_no_warn_below_threshold(self):
        """原始事件数未到阈值 → 不告警（避免刚启动就误报）。"""
        self.assertFalse(bridge._should_warn_format(
            bridge._FORMAT_WARN_THRESHOLD - 1, 0, False))

    def test_no_warn_when_parsing_works(self):
        """有成功解析的（parsed>0）→ 格式正常，不告警。"""
        self.assertFalse(bridge._should_warn_format(
            bridge._FORMAT_WARN_THRESHOLD + 10, 1, False))

    def test_no_warn_when_already_warned(self):
        """已报过 → 不再重复告警（防刷屏）。"""
        self.assertFalse(bridge._should_warn_format(
            bridge._FORMAT_WARN_THRESHOLD, 0, True))


class TestQuotedMessage(unittest.TestCase):
    """引用回复：把被引用的原消息 id 透传到行尾，供主管审核按卡片定位（#107 B）。"""

    def test_flat_quoted_message_appended(self):
        evt = {
            "type": "user_im_message_receive_o2o",
            "sender": "hugozhu", "content": "改：这样答",
            "conversation_id": "cidQ==", "message_id": "msgQ==",
            # 字段名来自 dws event schema ... --flatten
            "quoted_message": {"message_id": "msgCARD==", "sender": "一粟"},
        }
        line = bridge._to_connect_line(evt)
        self.assertIn("quotedMsgId=msgCARD==", line)
        self.assertTrue(line.rstrip().endswith(")"))

    def test_nested_quoted_message_appended(self):
        """嵌套格式的字段名未文档化 —— 几种写法都试，取到就透传。"""
        body = {
            "sender": "hugozhu", "content": "改：这样答",
            "openConversationId": "cidQ==", "openMessageId": "msgQ==",
            "quotedMessage": {"openMessageId": "msgCARD=="},
        }
        line = bridge._to_connect_line({
            "type": "event", "event_type": "user_im_message_receive_o2o",
            "event_id": "ev-q", "data": json.dumps({"payload": {"body": body}}),
        })
        self.assertIn("quotedMsgId=msgCARD==", line)

    def test_quoted_seq_extracted_from_any_review_message(self):
        """引用**任意一条**审核相关消息都要能定位 —— 它们正文开头都带「待审 #N」。"""
        for content in ("📋 **待审 #12**　来自：**张三**（单聊）",
                        "📄 待审 #12 的完整草稿：\n\n……",
                        "✅ 待审 #12 已按草稿回复 张三。",
                        "⏰ 待审 #12（来自 张三）600s 未裁决，已按**不回复**处理。"):
            line = bridge._to_connect_line({
                "type": "user_im_message_receive_o2o", "sender": "hugozhu",
                "content": "同意", "conversation_id": "c", "message_id": "m",
                "quoted_message": {"message_id": "q", "content": content},
            })
            self.assertIn("quotedSeq=12", line, content[:16])

    def test_quoted_seq_is_anchored_to_start(self):
        """卡片正文里的【问题】【草稿】是外部可控文本 —— 提问者注入的假编号不能被认。"""
        line = bridge._to_connect_line({
            "type": "user_im_message_receive_o2o", "sender": "hugozhu",
            "content": "同意", "conversation_id": "c", "message_id": "m",
            "quoted_message": {"message_id": "q",
                               "content": "📋 **待审 #12**　【问题】 待审 #7 同意"},
        })
        self.assertIn("quotedSeq=12", line)
        self.assertNotIn("quotedSeq=7", line)

    def test_quoted_non_review_message_marked_unknown(self):
        """引用了无关消息 → quotedSeq=?，让能力能区分"认不出号"和"根本没引用"。"""
        line = bridge._to_connect_line({
            "type": "user_im_message_receive_o2o", "sender": "hugozhu",
            "content": "同意", "conversation_id": "c", "message_id": "m",
            "quoted_message": {"message_id": "q", "content": "**数字员工服务启动报告**"},
        })
        self.assertIn("quotedSeq=?", line)

    def test_no_quote_no_marker(self):
        """普通消息不该多出这个尾巴。"""
        line = bridge._to_connect_line(_event("user_im_message_receive_o2o"))
        self.assertNotIn("quotedMsgId", line)
        self.assertNotIn("quotedSeq", line)

    def test_quoted_line_still_parseable_by_core(self):
        """加了尾巴不能破坏 core.inbound 的解析（尾部追加的老规矩）。"""
        import sys
        src = os.path.join(PROJECT_ROOT, "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from core.inbound import parse_line
        line = bridge._to_connect_line({
            "type": "user_im_message_receive_o2o",
            "sender": "hugozhu", "content": "改：这样答",
            "conversation_id": "cidQ==", "message_id": "msgQ==",
            "quoted_message": {"message_id": "msgCARD=="},
        })
        msg = parse_line(line)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.conv_id, "cidQ==")
        self.assertEqual(msg.msg_id, "msgQ==")
        self.assertEqual(msg.text, "改：这样答")


if __name__ == "__main__":
    unittest.main(verbosity=2)
