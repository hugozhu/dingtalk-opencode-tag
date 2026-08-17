#!/usr/bin/env python3
"""supervisor_review — 主管审核回路单测。

覆盖：拦截非主管单聊 / 主管单聊放行 / 三种裁决（同意·改写·忽略）/ 编号对应 /
并发不串 / 超时兜底 / 群聊同样送审（#107，含主管自己在群里提问）/ 学习只在改写时发生。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from core.inbound import InboundMessage  # noqa: E402
from custom.capabilities import supervisor_review as sr  # noqa: E402


def _msg(user, text, conv_id="cidAsker", conv_type="1", msg_id="m1"):
    return InboundMessage(user=user, text=text, conv_type=conv_type,
                          conv_id=conv_id, msg_id=msg_id)


class _Base(unittest.TestCase):
    def setUp(self):
        sr._reset()
        self.tmpdir = tempfile.mkdtemp()
        self.kf = os.path.join(self.tmpdir, "qa.jsonl")
        # 模块属性在 import 时定型，测试直接 patch 属性（同 brain 知识注入测试）
        self.patches = [
            patch.object(sr, "_KNOWLEDGE_FILE", self.kf),
            patch.object(sr, "_TIMEOUT", 0),        # 0=不起定时器，超时单独测
            patch.object(sr, "_O2O_ONLY", False),   # 默认：单聊 + 群聊都审（#107）
            # 反查卡片 msgId 会真的 shell 出去调 dws —— 单测里替换成确定值
            patch.object(sr, "_locate_card_msg_id", lambda seq: f"cardMsg{seq}"),
            patch.object(sr, "_REACTION_POLL", 0),  # 默认不起轮询线程，贴表情单独测
        ]
        for p in self.patches:
            p.start()
        os.environ["AGENT_SUPERVISOR_USER_ID"] = "sup123"
        os.environ["AGENT_SUPERVISOR_NAME"] = "boss"
        os.environ["AGENT_SUPERVISOR_ALIASES"] = "老板"

    def tearDown(self):
        for p in self.patches:
            p.stop()
        sr._reset()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for k in ("AGENT_SUPERVISOR_USER_ID", "AGENT_SUPERVISOR_NAME",
                  "AGENT_SUPERVISOR_ALIASES"):
            os.environ.pop(k, None)

    def _escalate(self, user="张三", text="问题", conv_id="cidZhang", msg_id="m1",
                  draft="AI草稿", conv_type="1"):
        """走一遍拦截 → 草稿 → 转交，返回 (send_to_sup mock, send_reply mock)。"""
        with patch.object(sr, "generate_reply_ex", return_value=(draft, "ok")), \
             patch.object(sr, "_send_to_supervisor", return_value=True) as sup, \
             patch.object(sr, "send_reply") as rep:
            sr._draft_and_forward(user, text, conv_type, conv_id, msg_id)
        return sup, rep


class TestInterception(_Base):
    def test_non_supervisor_o2o_intercepted(self):
        """非主管单聊被消费（不落到 text_reply），并提交后台生成草稿。"""
        with patch.object(sr, "submit_reply") as sub:
            self.assertTrue(sr.on_inbound(_msg("张三", "你好")))
            sub.assert_called_once()

    def test_group_message_intercepted(self):
        """群聊同样送审（#107）：群里的回答是公开发言，比单聊更该先过主管。"""
        with patch.object(sr, "submit_reply") as sub:
            self.assertTrue(sr.on_inbound(_msg("张三", "你好", conv_type="2")))
            sub.assert_called_once()

    def test_group_message_from_supervisor_intercepted(self):
        """主管在群里提问也送审（#107）—— 闸门管的是"数字员工说什么"，不是"谁在问"。"""
        with patch.object(sr, "submit_reply") as sub:
            self.assertTrue(sr.on_inbound(_msg("boss", "季度目标是啥", conv_type="2")))
            sub.assert_called_once()

    def test_group_message_from_supervisor_is_not_a_verdict(self):
        """裁决只认主管单聊：主管在群里说「同意」是新提问，不能放行别人的待审。"""
        self._escalate(user="张三", conv_id="cidZhang")
        with patch.object(sr, "submit_reply") as sub, \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(_msg("boss", "同意", conv_type="2",
                                               conv_id="cidGroup", msg_id="m9")))
        sub.assert_called_once()          # 当成新提问送审
        rep.assert_not_called()           # 张三的待审没被放行
        self.assertEqual(len(sr._pending), 1)

    def test_group_passes_through_when_o2o_only(self):
        """SUPERVISOR_REVIEW_O2O_ONLY=1 → 回到只审单聊的老行为。"""
        with patch.object(sr, "_O2O_ONLY", True), \
             patch.object(sr, "submit_reply") as sub:
            self.assertFalse(sr.on_inbound(_msg("张三", "你好", conv_type="2")))
            sub.assert_not_called()

    def test_group_card_marks_public_scene(self):
        """卡片必须标出群聊 —— 主管得知道这条答案会公开发出去。"""
        sup, _ = self._escalate(conv_type="2", conv_id="cidGroup")
        self.assertIn("群聊", sup.call_args[0][0])
        sup2, _ = self._escalate(conv_type="1", msg_id="m2")
        self.assertIn("单聊", sup2.call_args[0][0])

    def test_supervisor_without_pending_passes_through(self):
        """主管没有待审时正常对话 —— 必须放行给 text_reply，否则主管没法用数字员工。"""
        with patch.object(sr, "submit_reply") as sub:
            self.assertFalse(sr.on_inbound(_msg("boss", "今天天气如何")))
            sub.assert_not_called()

    def test_supervisor_alias_recognized(self):
        """别名也认作主管（bridge 只传显示名，可能是姓名或花名）。"""
        self.assertTrue(sr._is_supervisor("老板"))
        self.assertTrue(sr._is_supervisor("boss"))
        self.assertFalse(sr._is_supervisor("张三"))

    def test_no_supervisor_configured_passes_through(self):
        """没配主管 → 本能力等于关闭，不能把消息吞掉。"""
        for k in ("AGENT_SUPERVISOR_USER_ID", "AGENT_SUPERVISOR_NAME",
                  "AGENT_SUPERVISOR_ALIASES"):
            os.environ.pop(k, None)
        self.assertFalse(sr.on_inbound(_msg("张三", "你好")))

    def test_draft_not_sent_to_asker(self):
        """核心契约：草稿只发主管，**不发提问者**。"""
        sup, rep = self._escalate()
        sup.assert_called_once()
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 1)

    def test_card_contains_question_and_draft(self):
        sup, _ = self._escalate(text="报销怎么走", draft="找财务")
        card = sup.call_args[0][0]
        self.assertIn("报销怎么走", card)
        self.assertIn("找财务", card)
        self.assertIn("#1", card)

    def test_forward_failure_falls_back_to_asker(self):
        """转交主管失败不能把提问者永久挂着 → 回退直接回复。"""
        with patch.object(sr, "generate_reply_ex", return_value=("草稿", "ok")), \
             patch.object(sr, "_send_to_supervisor", return_value=False), \
             patch.object(sr, "send_reply") as rep:
            sr._draft_and_forward("张三", "问题", "1", "cidZhang", "m1")
        rep.assert_called_once()
        self.assertEqual(len(sr._pending), 0)

    def test_draft_failure_still_escalates(self):
        """模型挂了也要转交 —— 主管仍可手写答案，问题不能丢。"""
        with patch.object(sr, "generate_reply_ex", return_value=("", "failed")), \
             patch.object(sr, "_send_to_supervisor", return_value=True) as sup, \
             patch.object(sr, "send_reply") as rep:
            sr._draft_and_forward("张三", "问题", "1", "cidZhang", "m1")
        sup.assert_called_once()
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 1)


class TestVerdict(_Base):
    def test_approve_sends_draft(self):
        self._escalate(draft="AI草稿")
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss")))
        rep.assert_called_once()
        self.assertEqual(rep.call_args[0][0], "cidZhang")   # 发给提问者
        self.assertEqual(rep.call_args[0][2], "AI草稿")
        self.assertEqual(len(sr._pending), 0)

    def test_approve_group_pending_replies_to_group(self):
        """群里提的问题，主管在单聊放行后答案要发回**那个群**（#107）。"""
        self._escalate(conv_id="cidGroup", conv_type="2", draft="AI草稿")
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss")))
        self.assertEqual(rep.call_args[0][0], "cidGroup")
        self.assertEqual(rep.call_args[0][1], "2")     # 按群聊发，不是单聊
        self.assertEqual(rep.call_args[0][2], "AI草稿")

    def test_approve_does_not_learn(self):
        """同意 = AI 本来就对，没有新知识，不该写知识库。"""
        self._escalate()
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss"))
        self.assertFalse(os.path.exists(self.kf))

    def test_rewrite_sends_supervisor_answer_and_learns(self):
        """长答案直接写就行，不用前缀。"""
        self._escalate(text="报销怎么走", draft="AI瞎猜")
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr.on_inbound(_msg("boss", "#1 找财务小王签字，走 OA 审批", conv_id="cidBoss"))
        self.assertEqual(rep.call_args[0][2], "找财务小王签字，走 OA 审批")
        with open(self.kf, encoding="utf-8") as f:
            recs = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["question"], "报销怎么走")
        self.assertEqual(recs[0]["answer"], "找财务小王签字，走 OA 审批")

    def test_ignore_sends_nothing_to_asker(self):
        self._escalate()
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(_msg("boss", "#1 忽略", conv_id="cidBoss")))
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 0)

    def test_verdict_without_seq_uses_latest(self):
        """不带编号 → 对应最近一条待审（单人场景零心智负担）。"""
        self._escalate(user="张三", conv_id="cidZhang", msg_id="m1")
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr.on_inbound(_msg("boss", "同意", conv_id="cidBoss"))
        rep.assert_called_once()
        self.assertEqual(rep.call_args[0][0], "cidZhang")

    def test_unknown_seq_reports_and_consumes(self):
        """指名一个不存在的编号 → 告知主管，但不能放行去当普通对话。"""
        with patch.object(sr, "_send_to_supervisor") as sup:
            self.assertTrue(sr.on_inbound(_msg("boss", "#99 同意", conv_id="cidBoss")))
        self.assertIn("#99", sup.call_args[0][0])

    def test_concurrent_askers_do_not_cross(self):
        """两个提问者并发：#1/#2 各自对应正确，答案不能发错人。"""
        self._escalate(user="张三", text="Q1", conv_id="cidZhang", msg_id="m1")
        self._escalate(user="李四", text="Q2", conv_id="cidLi", msg_id="m2")
        self.assertEqual(len(sr._pending), 2)

        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr.on_inbound(_msg("boss", "#1 改：答张三", conv_id="cidBoss"))
            sr.on_inbound(_msg("boss", "#2 改：答李四", conv_id="cidBoss"))

        calls = {c[0][0]: c[0][2] for c in rep.call_args_list}
        self.assertEqual(calls["cidZhang"], "答张三")
        self.assertEqual(calls["cidLi"], "答李四")

    def test_rewrite_empty_answer_rejected(self):
        """空答案不发出（避免给提问者发一条空消息）。"""
        self._escalate()
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr.on_inbound(_msg("boss", "#1   ", conv_id="cidBoss"))
        rep.assert_not_called()


class TestParseVerdict(_Base):
    def test_parse_variants(self):
        self.assertEqual(sr._parse_verdict("#3 同意"), (3, "approve", ""))
        self.assertEqual(sr._parse_verdict("#12 忽略"), (12, "ignore", ""))
        self.assertEqual(sr._parse_verdict("同意"), (None, "approve", ""))
        self.assertEqual(sr._parse_verdict("#3 改：你应该这样答"), (3, "rewrite", "你应该这样答"))
        self.assertEqual(sr._parse_verdict("直接写一段够长的答案给提问者"),
                         (None, "rewrite", "直接写一段够长的答案给提问者"))

    def test_parse_is_case_insensitive(self):
        self.assertEqual(sr._parse_verdict("#1 OK")[1], "approve")
        self.assertEqual(sr._parse_verdict("#1 Yes")[1], "approve")


class TestTimeout(_Base):
    def test_timeout_does_not_release_draft(self):
        """主管一直不回 → **按不回复处理**，绝不自动放行没人看过的草稿。

        没人管不等于默认同意：自动放行等于把一条从没被人审过的草稿发出去（群里还是
        公开发言），审核闸门就白设了。
        """
        self._escalate(draft="AI草稿")
        seq = max(sr._pending)
        with patch.object(sr, "_send_to_supervisor") as sup, \
             patch.object(sr, "send_reply") as rep:
            sr._timeout(seq)
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 0)
        self.assertIn("不回复", sup.call_args[0][0])

    def test_timeout_archives_for_later(self):
        """超时的问题没被丢掉 —— 归档，主管事后引用卡片还能补裁。"""
        self._escalate(draft="AI草稿")
        seq = max(sr._pending)
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr._timeout(seq)
        self.assertIn("cardMsg1", sr._archive)

    def test_quoting_archived_card_still_works(self):
        """核心：超时之后引用那张卡片回「同意」，草稿照样发给提问者。"""
        self._escalate(user="张三", conv_id="cidZhang", draft="AI草稿")
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr._timeout(1)
        msg = _msg("boss", "同意", conv_id="cidBoss", msg_id="m9")
        msg.extra["quoted_msg_id"] = "cardMsg1"
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(msg))
        self.assertEqual(rep.call_args[0][0], "cidZhang")
        self.assertEqual(rep.call_args[0][2], "AI草稿")
        self.assertNotIn("cardMsg1", sr._archive)      # 补裁完就不该再被翻出来

    def test_archived_rewrite_is_learned(self):
        """事后补写的答案同样进知识库。"""
        self._escalate(text="报销怎么走", draft="AI瞎猜")
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr._timeout(1)
        msg = _msg("boss", "改：找财务小王", conv_id="cidBoss", msg_id="m9")
        msg.extra["quoted_msg_id"] = "cardMsg1"
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr.on_inbound(msg)
        self.assertEqual(rep.call_args[0][2], "找财务小王")
        with open(self.kf, encoding="utf-8") as f:
            recs = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(recs[0]["answer"], "找财务小王")

    def test_archive_is_bounded(self):
        """归档有界，长跑不涨内存。"""
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            for i in range(sr._ARCHIVE_MAX + 10):
                self._escalate(conv_id=f"cid{i}", msg_id=f"m{i}")
                sr._timeout(max(sr._pending))
        self.assertLessEqual(len(sr._archive), sr._ARCHIVE_MAX)

    def test_timeout_after_verdict_is_noop(self):
        """已裁决后定时器才触发 → 不能重复发第二条。"""
        self._escalate()
        seq = max(sr._pending)
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss"))
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr._timeout(seq)
        rep.assert_not_called()


class TestAckClosure(_Base):
    """不发消息的出口必须显式收尾 ack（#108）。

    ack 的「处理中→完成」只由 send_reply 广播的 reply-sent 驱动。本能力有几条路径
    压根不产生 send_reply，不收尾 → 那条消息的 ack worker 每 5 分钟往会话播一条
    「仍在处理中」直到 65 分钟超时（群聊里尤其刺眼：主管都决定忽略了还在报进度）。
    """

    def test_ignore_closes_asker_ack_silently(self):
        """忽略 → 提问者那条静默收尾（ok=None）：没答就别贴「完成」。"""
        self._escalate(conv_id="cidGroup", conv_type="2")
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"), \
             patch.object(sr, "dispatch_reply_sent") as disp:
            sr.on_inbound(_msg("boss", "#1 忽略", conv_id="cidBoss"))
        self.assertIn(("cidGroup", "2", None), [c[0] for c in disp.call_args_list])

    def test_verdict_message_closes_its_own_ack(self):
        """主管的裁决消息自己也挂着 worker：回执裸发不经 send_reply，必须显式收尾。"""
        self._escalate()
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"), \
             patch.object(sr, "dispatch_reply_sent") as disp:
            sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss"))
        self.assertIn(("cidBoss", "1", True), [c[0] for c in disp.call_args_list])

    def test_approve_does_not_double_close_asker(self):
        """同意 → 提问者那条由 send_reply 自带的 reply-sent 收尾，不该再手动发一次。"""
        self._escalate(conv_id="cidZhang")
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"), \
             patch.object(sr, "dispatch_reply_sent") as disp:
            sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss"))
        self.assertEqual([c[0][0] for c in disp.call_args_list], ["cidBoss"])

    def test_supervisor_passthrough_does_not_close_ack(self):
        """主管无待审时是普通对话 → 交给 text_reply 回，提前收尾会把「处理中」掐掉。"""
        with patch.object(sr, "submit_reply"), patch.object(sr, "dispatch_reply_sent") as disp:
            self.assertFalse(sr.on_inbound(_msg("boss", "今天天气如何", conv_id="cidBoss")))
        disp.assert_not_called()

    def test_forward_failure_without_draft_closes_ack_failed(self):
        """草稿也没有、主管也没转成 → 什么都发不出去，落失败终态而非挂死。"""
        with patch.object(sr, "generate_reply_ex", return_value=("", "failed")), \
             patch.object(sr, "_send_to_supervisor", return_value=False), \
             patch.object(sr, "send_reply") as rep, \
             patch.object(sr, "dispatch_reply_sent") as disp:
            sr._draft_and_forward("张三", "问题", "1", "cidZhang", "m1")
        rep.assert_not_called()
        disp.assert_called_once_with("cidZhang", "1", False)

    def test_timeout_closes_ack_silently(self):
        """超时 = 按不回复处理 → 静默收尾（与「忽略」一致），提问者那条不贴终态。"""
        self._escalate(draft="AI草稿")
        seq = max(sr._pending)
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply") as rep, \
             patch.object(sr, "dispatch_reply_sent") as disp:
            sr._timeout(seq)
        rep.assert_not_called()
        disp.assert_called_once_with("cidZhang", "1", None)

    def test_close_ack_survives_dispatch_failure(self):
        """收尾是 best-effort —— 广播抛错不能拖垮审核主流程。"""
        def boom(*a):
            raise RuntimeError("dispatch down")
        with patch.object(sr, "dispatch_reply_sent", boom):
            sr._close_ack("cidZhang", "1", True)   # 不应抛


class TestParseSafety(_Base):
    """#107 D：认不出就问，绝不把主管随口说的话当答案发给提问者。"""

    def test_short_unknown_is_unclear_not_rewrite(self):
        """核心：老逻辑会把「这个不太对」当答案公开发出去。"""
        for text in ("这个不太对", "等一下", "嗯？", "再想想"):
            seq, action, _ = sr._parse_verdict(text)
            self.assertEqual(action, "unclear", f"{text!r} 不该被当成答案")

    def test_expanded_approve_words(self):
        for text in ("可以发", "就这样", "发吧", "没问题", "通过"):
            self.assertEqual(sr._parse_verdict(text)[1], "approve", text)

    def test_rewrite_prefix_is_unambiguous(self):
        """带前缀 = 主管明说"下面是答案"，再短也照发。"""
        self.assertEqual(sr._parse_verdict("改：短答"), (None, "rewrite", "短答"))
        self.assertEqual(sr._parse_verdict("#2 答:好的"), (2, "rewrite", "好的"))

    def test_long_text_still_rewrite(self):
        long_answer = "这个问题要找财务小王签字，然后走 OA 审批流程"
        self.assertEqual(sr._parse_verdict(long_answer), (None, "rewrite", long_answer))

    def test_unclear_keeps_pending_and_sends_nothing(self):
        """认不出时：待审保留、提问者零消息、主管收到澄清。"""
        self._escalate()
        with patch.object(sr, "_send_to_supervisor") as sup, \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(_msg("boss", "这个不太对", conv_id="cidBoss")))
        rep.assert_not_called()                      # ← 老代码会把这四个字发给提问者
        self.assertEqual(len(sr._pending), 1)        # 待审还在，主管可以重裁
        self.assertIn("没听懂", sup.call_args[0][0])


class TestCardRendering(_Base):
    """#107 C：卡片瘦身 + 长草稿折叠。"""

    def test_actions_collapsed_to_one_line(self):
        card = sr._render_card(1, "张三", "问题", "草稿")
        self.assertEqual(card.count("\n#") + card.count("回「"), 0)
        self.assertIn("「#1 同意」放行", card)

    def test_long_draft_folded_with_full_text_separate(self):
        draft = "\n".join(f"第{i}行" for i in range(30))
        with patch.object(sr, "_CARD_DRAFT_MAX_LINES", 5):
            shown, full = sr._fold_draft(draft)
            card = sr._render_card(1, "张三", "问题", draft)
        self.assertIn("共 30 行", shown)
        self.assertEqual(full, draft)                 # 全文原样另发
        self.assertNotIn("第29行", card)              # 卡片里不再塞全文

    def test_short_draft_not_folded(self):
        shown, full = sr._fold_draft("一行草稿")
        self.assertEqual(shown, "一行草稿")
        self.assertIsNone(full)

    def test_full_draft_sent_as_second_message(self):
        draft = "\n".join(f"第{i}行" for i in range(30))
        with patch.object(sr, "_CARD_DRAFT_MAX_LINES", 5):
            sup, _ = self._escalate(draft=draft)
        sent = [c[0][0] for c in sup.call_args_list]
        self.assertEqual(len(sent), 2)
        self.assertIn("完整草稿", sent[1])
        self.assertIn("第29行", sent[1])

    def test_card_marker_does_not_prefix_collide(self):
        """#1 的前缀不能命中 #11，否则反查 msgId 会张冠李戴。"""
        self.assertFalse(sr._card_marker(11).startswith(sr._card_marker(1)))


class TestReactionVerdict(_Base):
    """#107 A：贴表情裁决（轮询 list-emotion-replies）。"""

    @staticmethod
    def _emotion_payload(msg_id, emoji, users=("boss",)):
        return json.dumps({"result": {"messages": [
            {"openMessageId": msg_id,
             "emotionReplyList": [{"emoji": emoji, "replyUsers": list(users)}]},
        ]}})

    def test_emoji_mapping(self):
        self.assertEqual(sr._emoji_action("赞"), "approve")
        self.assertEqual(sr._emoji_action("❌"), "ignore")
        self.assertIsNone(sr._emoji_action("🐶"))     # 不认识 → None，不猜

    def test_only_supervisor_reactions_count(self):
        """别人贴的表情不是裁决。"""
        entries = [{"emoji": "赞", "replyUsers": ["张三"]}]
        self.assertEqual(sr._supervisor_emojis(entries), [])
        entries.append({"emoji": "❌", "replyUsers": ["老板"]})
        self.assertEqual(sr._supervisor_emojis(entries), ["❌"])

    def test_malformed_reply_users_do_not_crash(self):
        """钉钉 payload 形状没文档化 —— 万一 replyUsers 是对象数组也不能把轮询线程带走。"""
        self.assertEqual(sr._supervisor_emojis([{"emoji": "赞", "replyUsers": [{"n": "boss"}]}]), [])
        self.assertEqual(sr._supervisor_emojis([{"emoji": "赞", "replyUsers": "boss"}]), [])
        self.assertEqual(sr._supervisor_emojis(None), [])

    def test_thumbs_up_approves_draft(self):
        """主管贴 👍 → 提问者收到草稿，全程零打字。"""
        self._escalate(draft="AI草稿")
        with patch.object(sr, "_run_cli",
                          lambda a, timeout=60: (0, self._emotion_payload("cardMsg1", "赞"))), \
             patch.object(sr, "_send_to_supervisor") as sup, \
             patch.object(sr, "send_reply") as rep:
            self.assertEqual(sr._poll_reactions_once(), 1)
        self.assertEqual(rep.call_args[0][0], "cidZhang")
        self.assertEqual(rep.call_args[0][2], "AI草稿")
        self.assertEqual(len(sr._pending), 0)
        self.assertIn("✅", sup.call_args[0][0])

    def test_cross_ignores_without_replying(self):
        self._escalate()
        with patch.object(sr, "_run_cli",
                          lambda a, timeout=60: (0, self._emotion_payload("cardMsg1", "❌"))), \
             patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertEqual(sr._poll_reactions_once(), 1)
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 0)

    def test_unknown_emoji_asks_once_and_keeps_pending(self):
        """不认识的表情：待审保留、只提示一次（否则每轮都骚扰主管）。"""
        self._escalate()
        with patch.object(sr, "_run_cli",
                          lambda a, timeout=60: (0, self._emotion_payload("cardMsg1", "🐶"))), \
             patch.object(sr, "_send_to_supervisor") as sup, \
             patch.object(sr, "send_reply") as rep:
            sr._poll_reactions_once()
            sr._poll_reactions_once()
            sr._poll_reactions_once()
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 1)
        self.assertEqual(sup.call_count, 1)
        self.assertIn("🐶", sup.call_args[0][0])

    def test_sticky_reaction_acts_only_once(self):
        """表情是**状态**不是事件：贴上去就一直挂着，每轮都读得到，只能作用一次。

        没有这层记账时，「同意但没草稿」会把待审放回去 → 下一轮读到同一个 👍 → 再放回，
        每 5 秒给主管发一条同样的提示，且超时定时器被反复重置，提问者永远等不到兜底。
        """
        self._escalate(draft="")          # 模型挂了 → 无草稿（受支持的路径）
        with patch.object(sr, "_run_cli",
                          lambda a, timeout=60: (0, self._emotion_payload("cardMsg1", "赞"))), \
             patch.object(sr, "_send_to_supervisor") as sup, \
             patch.object(sr, "send_reply"):
            for _ in range(4):
                sr._poll_reactions_once()
        self.assertEqual(sup.call_count, 1, "同一个表情被重复处理了")
        self.assertEqual(len(sr._pending), 1)   # 待审留着让主管手写答案

    def test_unmapped_emoji_does_not_shadow_later_valid_one(self):
        """主管先误贴一个不认识的、再补贴 👍 —— 那个 👍 必须生效。"""
        self._escalate(draft="AI草稿")
        both = json.dumps({"result": {"messages": [{
            "openMessageId": "cardMsg1",
            "emotionReplyList": [{"emoji": "🐶", "replyUsers": ["boss"]},
                                 {"emoji": "赞", "replyUsers": ["boss"]}],
        }]}})
        with patch.object(sr, "_run_cli", lambda a, timeout=60: (0, both)), \
             patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertEqual(sr._poll_reactions_once(), 1)
        self.assertEqual(rep.call_args[0][2], "AI草稿")

    def test_repend_keeps_original_deadline(self):
        """放回待审要续原来的剩余时间，不能每次都续满 —— 否则兜底永远不触发。"""
        self._escalate(draft="")
        with patch.object(sr, "_TIMEOUT", 600), \
             patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply"):
            sr._pending[1]["ts"] = time.time() - 590      # 只剩 10s
            sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss"))
            timer = sr._pending[1]["timer"]
        self.assertLess(timer.interval, 30, "重新计时了，兜底被无限推后")
        timer.cancel()

    def test_no_pending_makes_no_cli_call(self):
        """没有待审就别去打扰 dws。"""
        calls = []
        with patch.object(sr, "_run_cli", lambda a, timeout=60: calls.append(a) or (0, "{}")):
            self.assertEqual(sr._poll_reactions_once(), 0)
        self.assertEqual(calls, [])

    def test_all_pending_polled_in_one_call(self):
        """N 条待审只发一次 CLI（--msg-ids 逗号分隔），不是 N 次。"""
        self._escalate(user="张三", conv_id="cidZhang", msg_id="m1")
        self._escalate(user="李四", conv_id="cidLi", msg_id="m2")
        calls = []
        with patch.object(sr, "_run_cli", lambda a, timeout=60: calls.append(a) or (0, "{}")):
            sr._poll_reactions_once()
        self.assertEqual(len(calls), 1)
        self.assertIn("cardMsg1,cardMsg2", " ".join(calls[0]))

    def test_cli_failure_is_survivable(self):
        self._escalate()
        with patch.object(sr, "_run_cli", lambda a, timeout=60: (1, "boom")), \
             patch.object(sr, "send_reply") as rep:
            self.assertEqual(sr._poll_reactions_once(), 0)
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 1)        # 拉不到表情 ≠ 裁决，待审不能丢


class TestQuotedVerdict(_Base):
    """#107 B：引用卡片回复，不用敲 #N。"""

    def test_classify_line_extracts_quoted_id(self):
        line = ("[connect] 收到 @hugozhu: 改：这样答 (convType=1 convId=cidBoss "
                "msgId=m5 quotedMsgId=cardMsg2)")
        msg = sr.classify_line(line)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.extra.get("quoted_msg_id"), "cardMsg2")
        self.assertEqual(msg.text, "改：这样答")
        self.assertEqual(msg.conv_id, "cidBoss")

    def test_classify_line_ignores_normal_lines(self):
        """没有引用信息的行交回 core 标准解析。"""
        line = "[connect] 收到 @hugozhu: 同意 (convType=1 convId=cidBoss msgId=m5)"
        self.assertIsNone(sr.classify_line(line))

    def test_quoted_card_wins_over_latest(self):
        """引用了 #1 就裁 #1，哪怕 #2 才是最近一条。"""
        self._escalate(user="张三", conv_id="cidZhang", msg_id="m1", draft="草稿1")
        self._escalate(user="李四", conv_id="cidLi", msg_id="m2", draft="草稿2")
        msg = _msg("boss", "同意", conv_id="cidBoss")
        msg.extra["quoted_msg_id"] = "cardMsg1"
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(msg))
        self.assertEqual(rep.call_args[0][0], "cidZhang")   # 不是最近的李四
        self.assertEqual(rep.call_args[0][2], "草稿1")

    def test_quoted_unknown_card_falls_back(self):
        """引用的不是待审卡片（比如引用了别的消息）→ 退回 #N/最近一条。"""
        self._escalate(user="张三", conv_id="cidZhang")
        msg = _msg("boss", "同意", conv_id="cidBoss")
        msg.extra["quoted_msg_id"] = "msg-不相干"
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(msg))
        self.assertEqual(rep.call_args[0][0], "cidZhang")

    def test_quoting_a_retired_card_does_not_retarget(self):
        """引用一张**已处理**的旧卡片 → 告知，绝不改判到另一个提问者头上。

        主管在这里的指向性最明确，猜错的代价是把裁决落到无关的人身上。
        """
        self._escalate(user="张三", conv_id="cidZhang", msg_id="m1")
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss"))   # #1 处理掉
        self._escalate(user="李四", conv_id="cidLi", msg_id="m2")       # #2 成为最近一条
        msg = _msg("boss", "忽略", conv_id="cidBoss", msg_id="m9")
        msg.extra["quoted_msg_id"] = "cardMsg1"                        # 引用已处理的 #1
        with patch.object(sr, "_send_to_supervisor") as sup, \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(msg))
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 1)          # 李四的待审没被误判
        self.assertIn("裁决过", sup.call_args[0][0])


class TestPendingNotLost(_Base):
    """裁决没能真正完成时，待审必须留着 —— 否则提问者永久挂起（并触发 #108 心跳）。"""

    def test_approve_without_draft_keeps_pending(self):
        self._escalate(draft="")
        with patch.object(sr, "_send_to_supervisor") as sup, \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss")))
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 1)
        self.assertIn("没有可放行的草稿", sup.call_args[0][0])

    def test_pending_registered_before_card_msgid_lookup(self):
        """反查卡片 msgId 是一次网络往返 —— 待审必须在它之前就登记好。

        否则主管秒回「同意」时会找不到待审，被当成普通对话落到 text_reply（提问者的
        ack 也就永远收不了尾，正是 #108 那类症状）。
        """
        seen = {}

        def _slow_lookup(seq):
            seen["pending_at_lookup"] = len(sr._pending)
            return f"cardMsg{seq}"

        with patch.object(sr, "_locate_card_msg_id", _slow_lookup), \
             patch.object(sr, "generate_reply_ex", return_value=("草稿", "ok")), \
             patch.object(sr, "_send_to_supervisor", return_value=True), \
             patch.object(sr, "send_reply"):
            sr._draft_and_forward("张三", "问题", "1", "cidZhang", "m1")
        self.assertEqual(seen["pending_at_lookup"], 1)
        self.assertEqual(sr._pending[1]["card_msg_id"], "cardMsg1")   # 回填成功

    def test_empty_rewrite_keeps_pending(self):
        self._escalate()
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr.on_inbound(_msg("boss", "#1 改：", conv_id="cidBoss"))
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 1)


class TestCapabilityWiring(_Base):
    def test_classify_line_registered(self):
        """没挂上 classify_line，引用回复裁决就是死代码。"""
        self.assertIs(sr.CAPABILITY.classify_line, sr.classify_line)

    def test_priority_between_forward_and_text_reply(self):
        """必须晚于 question(20)、早于 text_reply(100)，否则拦不住草稿外发。"""
        self.assertGreater(sr.CAPABILITY.priority, 20)
        self.assertLess(sr.CAPABILITY.priority, 100)

    def test_default_disabled(self):
        """改变默认回复行为的能力不该悄悄生效。"""
        self.assertFalse(sr.CAPABILITY.default_enabled)

    def test_loop_guard_and_dedup_on(self):
        self.assertTrue(sr.CAPABILITY.loop_guard)
        self.assertTrue(sr.CAPABILITY.dedup)


if __name__ == "__main__":
    unittest.main()
