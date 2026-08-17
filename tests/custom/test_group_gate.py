#!/usr/bin/env python3
"""group_gate — 群消息闸门单测。

覆盖：单聊放行 / 群里没 @ 我压下 / 被 @ 放行 / 双流重复投递只放行一份（两种到达顺序）/
无 msgId 兜底 / 记忆表有界 / 挂点顺序（晚于 ack，早于会开口的能力）。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from core.inbound import InboundMessage  # noqa: E402
from custom.capabilities import group_gate as gg  # noqa: E402


def _msg(text="你好", conv_type="2", msg_id="m1", at=False, user="张三",
         conv_id="cidGroup"):
    extra = {"at_mention": True} if at else {}
    return InboundMessage(user=user, text=text, conv_type=conv_type,
                          conv_id=conv_id, msg_id=msg_id, extra=extra)


class TestGate(unittest.TestCase):
    def setUp(self):
        gg._reset()

    def test_o2o_always_passes(self):
        """单聊不归本能力管（只有一条流，也不存在 @ 一说）。"""
        self.assertFalse(gg.on_inbound(_msg(conv_type="1")))

    def test_group_without_at_is_swallowed(self):
        """群里没 @ 数字员工 → 吞掉，不进大脑、不回复。"""
        self.assertTrue(gg.on_inbound(_msg(at=False)))

    def test_group_with_at_passes(self):
        """被 @ → 放行给后续能力。"""
        self.assertFalse(gg.on_inbound(_msg(at=True)))


class TestDualStreamDedup(unittest.TestCase):
    """同一条 @ 消息被群流 + @ 流投两次，只能有一份往下走。"""

    def setUp(self):
        gg._reset()

    def test_at_copy_first_then_group_copy(self):
        """@ 流先到：放行第一份，后到的群流那份吞掉（否则 text_reply 会绕过审核再回一条）。"""
        self.assertFalse(gg.on_inbound(_msg(msg_id="dup", at=True)))
        self.assertTrue(gg.on_inbound(_msg(msg_id="dup", at=False)))

    def test_group_copy_first_then_at_copy(self):
        """群流先到（那份**没有** atMention 标记）：先压下，等带标记的那份到了再放行。

        这一条是本能力存在的理由 —— 若"见过就丢"，被 @ 的消息反而永远没人答。
        """
        self.assertTrue(gg.on_inbound(_msg(msg_id="dup", at=False)))
        self.assertFalse(gg.on_inbound(_msg(msg_id="dup", at=True)))

    def test_third_copy_after_pass_is_swallowed(self):
        """放行过一份之后，同 msgId 再来多少份都吞掉。"""
        gg.on_inbound(_msg(msg_id="dup", at=True))
        self.assertTrue(gg.on_inbound(_msg(msg_id="dup", at=True)))
        self.assertTrue(gg.on_inbound(_msg(msg_id="dup", at=False)))

    def test_distinct_msgids_independent(self):
        """不同 msgId 互不影响。"""
        self.assertFalse(gg.on_inbound(_msg(msg_id="a", at=True)))
        self.assertTrue(gg.on_inbound(_msg(msg_id="b", at=False)))
        self.assertFalse(gg.on_inbound(_msg(msg_id="c", at=True)))


class TestBounds(unittest.TestCase):
    def setUp(self):
        gg._reset()

    def test_missing_msgid_falls_back_to_at_flag(self):
        """没 msgId 就无从去重（也就不会有双份），只按有没有 @ 我判。"""
        self.assertTrue(gg.on_inbound(_msg(msg_id="", at=False)))
        self.assertFalse(gg.on_inbound(_msg(msg_id="", at=True)))

    def test_seen_table_is_bounded(self):
        """记忆表有界，长跑不涨内存。"""
        for i in range(gg._SEEN_MAX + 50):
            gg.on_inbound(_msg(msg_id=f"m{i}", at=True))
        self.assertLessEqual(len(gg._seen), gg._SEEN_MAX)


class TestAwaitingReply(unittest.TestCase):
    """数字员工自己在群里问了话，对方回一句不会 @ 它 —— 闸门必须先让"正等回答"的能力过目。

    回归的是真事故：群里发「🔐 需要授权」后，用户回「同意」被闸门吞掉，审批只能等到
    超时自动拒绝（e2e_permission_test 的 V2 因此恒 ❌，耗时 63s）。
    """

    def setUp(self):
        gg._reset()
        from core.builtin_caps import permission
        self.permission = permission
        permission._reset()

    def tearDown(self):
        self.permission._reset()
        gg._reset()

    def _arm_pending(self, conv_id="cidG"):
        """在该群挂一条待审批。"""
        with self.permission._pending_lock:
            self.permission._pending["req1"] = {
                "sid": "s1", "conv_id": conv_id, "conv_type": "2",
                "action": "bash", "api": "v1", "timer": None,
            }

    def test_approval_reply_reaches_permission(self):
        """有待审批时，群里没 @ 的「同意」要被 permission 收走，而不是被闸门吞掉。"""
        self._arm_pending()
        with patch.object(self.permission, "on_inbound", return_value=True) as oi:
            self.assertTrue(gg.on_inbound(_msg(text="同意", at=False, conv_id="cidG")))
        oi.assert_called_once()

    def test_unrelated_chatter_still_swallowed(self):
        """但闲聊仍要吞掉 —— permission 对不认识的文本返回 False，放行会让数字员工接话。"""
        self._arm_pending()
        with patch.object(self.permission, "on_inbound", return_value=False) as oi:
            self.assertTrue(gg.on_inbound(_msg(text="今晚吃啥", at=False, conv_id="cidG")))
        oi.assert_called_once()      # 问过了
        # 返回 True = 吞掉，不会走到 text_reply

    def test_no_pending_does_not_probe(self):
        """没有待答请求时不该去打扰任何能力。"""
        with patch.object(self.permission, "on_inbound") as oi:
            self.assertTrue(gg.on_inbound(_msg(text="闲聊", at=False, conv_id="cidG")))
        oi.assert_not_called()

    def test_pending_in_another_group_does_not_leak(self):
        """待审批挂在别的群 → 本群的消息照旧吞掉。"""
        self._arm_pending(conv_id="cid其他群")
        with patch.object(self.permission, "on_inbound") as oi:
            self.assertTrue(gg.on_inbound(_msg(text="同意", at=False, conv_id="cidG")))
        oi.assert_not_called()

    def test_probe_failure_does_not_break_gate(self):
        """探测出错不能把闸门带走（best-effort）。"""
        self._arm_pending()
        with patch.object(self.permission, "on_inbound", side_effect=RuntimeError("boom")):
            self.assertTrue(gg.on_inbound(_msg(text="同意", at=False, conv_id="cidG")))


class TestWiring(unittest.TestCase):
    def test_priority_after_ack_before_talkers(self):
        """必须晚于 trace(0)/ack(1)——群消息照常记账+标已读；早于任何会开口/认命令的能力。"""
        from custom.capabilities import ack, cancel
        from core.builtin_caps import text_reply
        self.assertGreater(gg.CAPABILITY.priority, ack.CAPABILITY.priority)
        self.assertLess(gg.CAPABILITY.priority, cancel.CAPABILITY.priority)
        self.assertLess(gg.CAPABILITY.priority, text_reply.CAPABILITY.priority)

    def test_dedup_off(self):
        """core 的按能力 dedup 必须关：本能力要看到每一份投递才能合并双流。"""
        self.assertFalse(gg.CAPABILITY.dedup)

    def test_handles_all_kinds(self):
        """群里没 @ 我的图片/文件/合并转发同样不该触发处理。"""
        self.assertFalse(gg.CAPABILITY.handles_kinds)


if __name__ == "__main__":
    unittest.main()
