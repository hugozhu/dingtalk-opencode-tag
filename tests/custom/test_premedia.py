#!/usr/bin/env python3
"""premedia — 群图预识别单测（#112 步骤 3）。

这个能力的危险之处不是"漏识别"，而是**乱花钱**和**挡路**：它为没人跟它说话的消息
发起视觉调用。所以测的重点是限流、恒不消费、异常不外溢，以及默认必须是关的。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from core.inbound import InboundMessage, KIND_IMAGE  # noqa: E402
from custom import mediadesc  # noqa: E402
from custom.capabilities import premedia  # noqa: E402

IMG = "[图片消息](mediaId=$abc)"


def _msg(msg_id="m1", conv_id="cid群==", text=IMG, conv_type="2"):
    return InboundMessage(user="张三", text=text, conv_type=conv_type,
                          conv_id=conv_id, msg_id=msg_id, kind=KIND_IMAGE)


class _Base(unittest.TestCase):
    def setUp(self):
        premedia._reset()

    def tearDown(self):
        premedia._reset()


class TestNeverConsumes(_Base):
    def test_returns_false_so_group_gate_still_runs(self):
        """**恒 False**：它只是预热，消费掉这条消息会让 group_gate/image 全失效。"""
        with patch.object(mediadesc, "describe", return_value=("", "pending")):
            self.assertFalse(premedia.on_inbound(_msg()))

    def test_exception_does_not_escape(self):
        """预热失败绝不能影响这条消息的正常处理。"""
        with patch.object(mediadesc, "describe", side_effect=RuntimeError("vision down")):
            self.assertFalse(premedia.on_inbound(_msg()))


class TestScope(_Base):
    def test_group_image_triggers(self):
        with patch.object(mediadesc, "describe", return_value=("", "pending")) as d:
            premedia.on_inbound(_msg())
        d.assert_called_once()
        self.assertIsNone(d.call_args.kwargs["wait"])       # 只发起、不等
        self.assertEqual(d.call_args.kwargs["by"], "premedia")

    def test_o2o_skipped(self):
        """单聊的图 image 能力总会识别，不用预热。"""
        with patch.object(mediadesc, "describe") as d:
            premedia.on_inbound(_msg(conv_type="1"))
        d.assert_not_called()

    def test_non_media_skipped(self):
        with patch.object(mediadesc, "describe") as d:
            premedia.on_inbound(_msg(text="[图片消息](没有mediaId)"))
        d.assert_not_called()

    def test_does_not_look_at_at_mention(self):
        """**不判 at_mention**：它是每份投递的属性，群流那份没有标记，靠它分流必错。

        被 @ 的图照样预识别；重复由 mediadesc 的单飞在下游解决。
        """
        m = _msg()
        m.extra = {"at_mention": True}
        with patch.object(mediadesc, "describe", return_value=("", "pending")) as d:
            premedia.on_inbound(m)
        d.assert_called_once()


class TestRateLimit(_Base):
    def test_burst_is_capped(self):
        """有人一口气贴 20 张截图 —— 这些图没人在等，不该排在真活前面。"""
        with patch.object(mediadesc, "describe", return_value=("", "pending")) as d:
            for i in range(20):
                premedia.on_inbound(_msg(msg_id=f"m{i}"))
        self.assertEqual(d.call_count, premedia._RATE)

    def test_limit_is_per_conversation(self):
        """一个群刷屏不该让别的群跟着哑掉。"""
        with patch.object(mediadesc, "describe", return_value=("", "pending")) as d:
            for i in range(20):
                premedia.on_inbound(_msg(msg_id=f"a{i}", conv_id="cidA=="))
            premedia.on_inbound(_msg(msg_id="b1", conv_id="cidB=="))
        self.assertEqual(d.call_count, premedia._RATE + 1)

    def test_window_expiry_restores_budget(self):
        with patch.object(mediadesc, "describe", return_value=("", "pending")) as d, \
             patch.object(premedia, "_WINDOW", 0.0):     # 窗口立刻过期
            for i in range(10):
                premedia.on_inbound(_msg(msg_id=f"m{i}"))
        self.assertEqual(d.call_count, 10)

    def test_rate_zero_disables(self):
        with patch.object(premedia, "_RATE", 0), \
             patch.object(mediadesc, "describe") as d:
            premedia.on_inbound(_msg())
        d.assert_not_called()


class TestWiring(_Base):
    def test_default_off(self):
        """**默认必须关**：全仓唯一"为没人跟它说话的消息花钱"的能力。"""
        self.assertFalse(premedia.CAPABILITY.default_enabled)

    def test_runs_before_group_gate_after_msgstore(self):
        """图要先落盘（msgstore -10）才能预识别；又必须早于 group_gate(2) 把它吞掉。"""
        from custom.capabilities import group_gate, msgstore_cap
        self.assertLess(msgstore_cap.CAPABILITY.priority, premedia.CAPABILITY.priority)
        self.assertLess(premedia.CAPABILITY.priority, group_gate.CAPABILITY.priority)

    def test_declares_dedup_and_loop_guard(self):
        self.assertTrue(premedia.CAPABILITY.dedup)
        self.assertTrue(premedia.CAPABILITY.loop_guard)
        self.assertEqual(premedia.CAPABILITY.handles_kinds, {KIND_IMAGE})


if __name__ == "__main__":
    unittest.main()
