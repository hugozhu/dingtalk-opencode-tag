#!/usr/bin/env python3
"""test_forward_capability.py — 合并转发能力单测（custom）

覆盖：摘要检测、list-by-ids 反查解析、sender 补齐、假阳性回退、防回环、去重、
优先级放行给 text_reply。用 mock _run_cli，不依赖网络/钉钉。

样本取自真实链路（树莓派群 combine-forward 后 list-by-ids 的响应结构）。
"""

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom.capabilities import forward
from core.inbound import InboundMessage, KIND_TEXT

# 真实结构：outer content 摘要 + forwardMessages（sender 可能已解析或为 "null"）
_FWD_RESPONSE = json.dumps({
    "result": {"messages": [{
        "openMessageId": "msgFWD==",
        "sender": "hugozhu",
        "content": "群聊的聊天记录\nhugozhu:[消息]\nopencode:[消息]",
        "forwardMessages": [
            {"sender": "hugozhu", "content": "probe-fulldata",
             "createTime": "2026-07-18 22:45:29", "openMessageId": "msgIN0=="},
            {"sender": "opencode", "content": "未找到相关代码",
             "createTime": "2026-07-18 22:45:40", "openMessageId": "msgIN1=="},
        ],
    }]}
}, ensure_ascii=False)

_NORMAL_RESPONSE = json.dumps({
    "result": {"messages": [{
        "openMessageId": "msgN==", "sender": "hugozhu", "content": "普通消息",
        # 无 forwardMessages
    }]}
}, ensure_ascii=False)


class TestForwardDetection(unittest.TestCase):
    def test_summary_patterns_match(self):
        self.assertTrue(forward._looks_like_forward("群聊的聊天记录 a:[消息]"))
        self.assertTrue(forward._looks_like_forward("hugozhu与opencode的聊天记录"))

    def test_normal_text_not_matched(self):
        self.assertFalse(forward._looks_like_forward("1+1"))
        self.assertFalse(forward._looks_like_forward(""))


class TestForwardRouting(unittest.TestCase):
    def setUp(self):
        forward._seen.clear()

    def _msg(self, text, user="hugozhu", mid="msgFWD=="):
        return InboundMessage(user=user, text=text, conv_type="2",
                              conv_id="cid==", msg_id=mid, kind=KIND_TEXT)

    def test_forward_claimed_and_dispatched(self):
        calls = []
        with patch.object(forward, "submit_handler",
                          side_effect=lambda fn, *a: calls.append(a)):
            consumed = forward.on_inbound(self._msg("群聊的聊天记录 x:[消息]"))
        self.assertTrue(consumed)
        self.assertEqual(len(calls), 1)

    def test_non_forward_passed_through(self):
        # 普通文本不认领（return False）→ 交给 text_reply
        self.assertFalse(forward.on_inbound(self._msg("1+1")))

    def test_self_sent_forward_filtered(self):
        with patch.object(forward, "_SELF_NAMES", {"opencode"}), \
             patch.object(forward, "submit_handler") as sh:
            consumed = forward.on_inbound(self._msg("群聊的聊天记录", user="opencode"))
        self.assertTrue(consumed)          # 消费掉
        sh.assert_not_called()             # 但不处理

    def test_dedup(self):
        calls = []
        with patch.object(forward, "submit_handler",
                          side_effect=lambda fn, *a: calls.append(a)):
            forward.on_inbound(self._msg("群聊的聊天记录", mid="dup=="))
            forward.on_inbound(self._msg("群聊的聊天记录", mid="dup=="))
        self.assertEqual(len(calls), 1)


class TestHandleForward(unittest.TestCase):
    def test_parses_and_replies_to_group(self):
        with patch.object(forward, "_run_cli", return_value=(0, _FWD_RESPONSE)), \
             patch.object(forward, "fetch_attachments",
                          side_effect=lambda fms, lookup_convs=None: [
                              {"type": "text", "text": fm["content"], "time": fm["createTime"]}
                              for fm in fms]), \
             patch.object(forward, "generate_reply", return_value="总结：两条消息") as gen, \
             patch.object(forward, "send_reply", return_value=True) as snd:
            forward.handle_forward("hugozhu", "群聊的聊天记录", "msgFWD==", "cid==", "2")
        gen.assert_called_once()
        prompt = gen.call_args[0][1]
        # prompt 里应包含解析出的内层消息内容
        self.assertIn("probe-fulldata", prompt)
        self.assertIn("未找到相关代码", prompt)
        # 回复发回来源群
        snd.assert_called_once()
        self.assertEqual(snd.call_args[0][0], "cid==")
        self.assertEqual(snd.call_args[0][2], "总结：两条消息")

    def test_false_positive_falls_back_to_text_reply(self):
        # content 像转发但反查无 forwardMessages → 回退普通文本回复
        with patch.object(forward, "_run_cli", return_value=(0, _NORMAL_RESPONSE)), \
             patch.object(forward, "generate_reply", return_value="普通回复") as gen, \
             patch.object(forward, "send_reply", return_value=True) as snd:
            forward.handle_forward("hugozhu", "假的聊天记录文本", "msgN==", "cid==", "2")
        gen.assert_called_once_with("hugozhu", "假的聊天记录文本")  # 用原始 text 回退
        snd.assert_called_once()

    def test_lookup_failure_no_crash(self):
        with patch.object(forward, "_run_cli", return_value=(1, "")), \
             patch.object(forward, "generate_reply", return_value="") as gen, \
             patch.object(forward, "send_reply") as snd:
            forward.handle_forward("u", "群聊的聊天记录", "m==", "c==", "2")
        # rc!=0 → 无 forwardMessages → 回退；大脑空回复 → 不发
        snd.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
