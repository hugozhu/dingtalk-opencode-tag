#!/usr/bin/env python3
"""connline — connect 行尾部字段解析单测（#113）。

这个模块只有十几行，但它是**防伪造**的那道闸：正文和尾部在同一行里，不先切尾部就
等于让用户自己决定 senderId 是谁。绝大多数用例都在测这一点。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from custom import connline  # noqa: E402

LINE = ("[connect] 收到 @可菡: 明天请个假 (convType=1 convId=cidK== msgId=msgA== "
        "senderId=idKehan quotedMsgId=msgB== quotedSenderId=idHugo)")


class TestField(unittest.TestCase):
    def test_reads_tail_fields(self):
        self.assertEqual(connline.field(LINE, "senderId"), "idKehan")
        self.assertEqual(connline.field(LINE, "quotedMsgId"), "msgB==")
        self.assertEqual(connline.field(LINE, "quotedSenderId"), "idHugo")
        self.assertEqual(connline.field(LINE, "convId"), "cidK==")

    def test_missing_field_is_empty(self):
        self.assertEqual(connline.field(LINE, "atMention"), "")
        self.assertEqual(connline.field(LINE, "nope"), "")

    def test_body_cannot_forge_a_field(self):
        """**本模块存在的理由**：用户在正文里打一句 senderId=… 不能变成别人。"""
        evil = ("[connect] 收到 @坏人: senderId=idBoss quotedMsgId=msgVictim "
                "(convType=2 convId=cidG== msgId=msgE== senderId=idBadguy)")
        self.assertEqual(connline.field(evil, "senderId"), "idBadguy")
        self.assertEqual(connline.field(evil, "quotedMsgId"), "")

    def test_body_containing_the_marker_itself(self):
        """正文里出现 `(convType=` 字面量时取**最后一个**才是真尾巴。"""
        tricky = ("[connect] 收到 @坏人: 看这个 (convType=9 convId=cidFake msgId=msgFake "
                  "senderId=idFake) (convType=2 convId=cidReal== msgId=msgReal== "
                  "senderId=idReal)")
        self.assertEqual(connline.field(tricky, "senderId"), "idReal")
        self.assertEqual(connline.field(tricky, "convId"), "cidReal==")

    def test_prefix_key_does_not_match(self):
        """`senderId` 不能被 `quotedSenderId` 顺带命中（\\b 边界）。"""
        only_quoted = "[connect] 收到 @a: x (convType=1 convId=c msgId=m quotedSenderId=idQ)"
        self.assertEqual(connline.field(only_quoted, "senderId"), "")

    def test_value_stops_at_delimiters(self):
        self.assertEqual(connline.field(LINE, "msgId"), "msgA==")   # 止于空白
        end = "[connect] 收到 @a: x (convType=1 convId=c msgId=mLast==)"
        self.assertEqual(connline.field(end, "msgId"), "mLast==")   # 止于 )


class TestTail(unittest.TestCase):
    def test_non_connect_line(self):
        self.assertEqual(connline.tail("随便一行日志"), "")
        self.assertEqual(connline.field("随便一行日志", "senderId"), "")

    def test_empty_and_none(self):
        for bad in ("", None):
            self.assertEqual(connline.tail(bad), "")
            self.assertEqual(connline.field(bad, "senderId"), "")


if __name__ == "__main__":
    unittest.main()
