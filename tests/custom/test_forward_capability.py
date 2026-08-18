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

    def test_english_summary_matches(self):
        # 英文客户端摘要头（实测 msg98nDkoiY... 同一条转发英文 locale）
        self.assertTrue(forward._looks_like_forward("Group Chat History\nhugozhu:[Image]"))
        self.assertTrue(forward._looks_like_forward("Chat History\na:hi"))
        self.assertTrue(forward._looks_like_forward("chat history"))  # 大小写不敏感

    def test_other_languages_via_structural_fallback(self):
        # 繁体「聊天記錄」(記錄≠记录)、日语等快路径没枚举的语言，靠 ≥2 行 `名字:内容` 结构兜底
        self.assertTrue(forward._looks_like_forward(
            "群組聊天記錄\nhugozhu:[圖片]\n冬翔:@朱鴻 主動性"))
        self.assertTrue(forward._looks_like_forward(
            "グループチャットの履歴\nhugozhu:[画像]\n冬翔:主動性"))

    def test_normal_text_not_matched(self):
        self.assertFalse(forward._looks_like_forward("1+1"))
        self.assertFalse(forward._looks_like_forward(""))
        self.assertFalse(forward._looks_like_forward("history of the project"))
        # 单行 `label:value` 不够阈值（<2 行），且自然语言 `label: value` 带空格不匹配
        self.assertFalse(forward._looks_like_forward("note:hi"))
        self.assertFalse(forward._looks_like_forward("时间: 10:30\n地点: 北京"))


class TestSendersFromSummary(unittest.TestCase):
    """外层摘要解析发送人——转发内层 sender 常为 "null" 且反查不到，摘要是唯一可靠来源。"""

    # 真实样本（取自 msghynJd0G+sjOyX6XESiuFXg== 的 mget content）
    _REAL = ("群聊的聊天记录\n"
             "hugozhu:[图片]\n"
             "hugozhu:传统播报机器人 真人 数字员工 在一个群里协同\n"
             "hugozhu:@夏东翔(冬翔) FDE教练要拟人化 要主动做这个岗位该做的事情 能力要全面胜任岗位\n"
             "冬翔:@朱鸿(hugozhu) 主动性 就是主动感知和触发action的能力")

    def test_real_sample_aligns(self):
        self.assertEqual(forward._senders_from_summary(self._REAL, 4),
                         ["hugozhu", "hugozhu", "hugozhu", "冬翔"])

    def test_chinese_colon(self):
        self.assertEqual(
            forward._senders_from_summary("X与Y的聊天记录\n张三：你好\n李四：在", 2),
            ["张三", "李四"])

    def test_count_mismatch_returns_none(self):
        # 行数与 n 不一致（内层含换行会错位）→ 不猜，返回 None 交回兜底
        self.assertIsNone(forward._senders_from_summary(self._REAL, 3))

    def test_colon_in_content_not_captured_as_sender(self):
        # 正文深处的冒号（URL）不应被当成发送人分隔——取第一个冒号前即可
        out = forward._senders_from_summary("群聊的聊天记录\nhugozhu:见 http://a.com/x", 1)
        self.assertEqual(out, ["hugozhu"])

    def test_no_header_still_parses(self):
        self.assertEqual(forward._senders_from_summary("a:1\nb:2", 2), ["a", "b"])

    def test_english_header_stripped(self):
        # 英文摘要头也要被识别并丢掉，剩余行与 n 对齐
        english = ("Group Chat History\n"
                   "hugozhu:[Image]\n"
                   "冬翔:@朱鸿(hugozhu) 主动性")
        self.assertEqual(forward._senders_from_summary(english, 2), ["hugozhu", "冬翔"])

    def test_empty_or_zero(self):
        self.assertIsNone(forward._senders_from_summary("", 3))
        self.assertIsNone(forward._senders_from_summary("群聊的聊天记录\na:1", 0))


class TestForwardMainSession(unittest.TestCase):
    """转发回复必须发进来源会话的**主 session**（ctx 带 conv_id），复用多轮上下文——
    与 image/file/text_reply 一致。图片识别用的临时 session 是另一回事（只做 vision 转写）。"""

    def setUp(self):
        forward._seen.clear()

    def test_main_path_passes_conv_id_ctx(self):
        fms = [{"content": "hi", "createTime": "t", "openMessageId": "m1"}]
        body = {"sender": "hugozhu", "content": "群聊的聊天记录\nhugozhu:hi", "messages": fms}
        with patch.object(forward, "_fetch_forward_body", return_value=(body, fms)), \
             patch.object(forward, "_fetch_senders", return_value=["hugozhu"]), \
             patch.object(forward, "fetch_attachments", return_value=[{"text": "hi", "time": "t"}]), \
             patch.object(forward, "send_reply"), \
             patch.object(forward, "generate_reply", return_value="ok") as gr:
            forward.handle_forward("hugozhu", "txt", "msgX", "cidMAIN==", "2")
        kwargs = gr.call_args.kwargs
        self.assertEqual(kwargs.get("ctx", {}).get("conv_id"), "cidMAIN==")
        self.assertTrue(kwargs.get("raw"))

    def test_false_positive_fallback_also_passes_ctx(self):
        # 假阳性回退（无 forwardMessages）也要进主 session，不能退化成无状态
        with patch.object(forward, "_fetch_forward_body", return_value=(None, [])), \
             patch.object(forward, "send_reply"), \
             patch.object(forward, "generate_reply", return_value="ok") as gr:
            forward.handle_forward("hugozhu", "txt", "msgX", "cidMAIN==", "2")
        self.assertEqual(gr.call_args.kwargs.get("ctx", {}).get("conv_id"), "cidMAIN==")


class TestForwardImageEntry(unittest.TestCase):
    """转发内层图片识别——必须走 image._recognize（serve+gemini 优先），与独立图片一致，
    而非旧的直连 _proxy_vision（实测转发内图 Connection refused）。"""

    def test_uses_image_recognize(self):
        from custom import handler
        from custom.capabilities import image
        with patch.object(handler, "_download_image_to_path", return_value="/tmp/fake.png"), \
             patch.object(image, "_recognize", autospec=True, side_effect=lambda *a, **k: "GEMINI识别文本") as rec:
            out = handler._fetch_image_entry("[图片消息](mediaId=$abc123)", "msg1", "cid1")
        rec.assert_called_once_with("/tmp/fake.png")   # 走 image 能力的统一识别
        self.assertIn("GEMINI识别文本", out)
        self.assertIn("识别内容", out)

    def test_no_media_id(self):
        from custom import handler
        self.assertEqual(handler._fetch_image_entry("无 mediaId", "m", "c"),
                         "[图片消息，未提取到 mediaId]")

    def test_download_failed(self):
        from custom import handler
        with patch.object(handler, "_download_image_to_path", return_value=None):
            self.assertEqual(handler._fetch_image_entry("[图片消息](mediaId=$x)", "m", "c"),
                             "[图片，下载失败]")

    def test_recognize_empty_marks_failed(self):
        from custom import handler
        from custom.capabilities import image
        with patch.object(handler, "_download_image_to_path", return_value="/tmp/fake.png"), \
             patch.object(image, "_recognize", autospec=True, side_effect=lambda *a, **k: ""):
            out = handler._fetch_image_entry("[图片消息](mediaId=$abc)", "m", "c")
        self.assertEqual(out, "[图片，识别失败]")   # 留标记，不静默丢弃


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
        # 语境头 + 末句指令都在（明确这是合并转发聊天记录）
        self.assertIn("转发了一段聊天记录", prompt)
        self.assertIn("合并转发", prompt)
        # raw=True：brain 不再拼 "{user}：" 前缀
        self.assertTrue(gen.call_args.kwargs.get("raw"))
        self.assertFalse(prompt.startswith("hugozhu："))
        # 回复发回来源群
        snd.assert_called_once()
        self.assertEqual(snd.call_args[0][0], "cid==")
        self.assertEqual(snd.call_args[0][2], "总结：两条消息")

    def test_false_positive_falls_back_to_text_reply(self):
        # content 像转发但反查无 forwardMessages → 回退普通文本回复（仍进主 session：ctx 带 conv_id）
        with patch.object(forward, "_run_cli", return_value=(0, _NORMAL_RESPONSE)), \
             patch.object(forward, "generate_reply", return_value="普通回复") as gen, \
             patch.object(forward, "send_reply", return_value=True) as snd:
            forward.handle_forward("hugozhu", "假的聊天记录文本", "msgN==", "cid==", "2")
        gen.assert_called_once_with("hugozhu", "假的聊天记录文本", ctx={
            "conv_id": "cid==", "conv_type": "2", "msg_id": "msgN==", "user": "hugozhu",
        })
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
