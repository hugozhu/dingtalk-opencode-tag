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
        self.journal = os.path.join(self.tmpdir, "reviews.jsonl")
        # 模块属性在 import 时定型，测试直接 patch 属性（同 brain 知识注入测试）
        self.patches = [
            patch.object(sr, "_KNOWLEDGE_FILE", self.kf),
            # 审核流水必须指到 tmpdir —— 否则测试会写进真实的 knowledge/ 并且短号
            # 从上一次跑测试的高水位续起，用例之间互相串味
            patch.object(sr, "_JOURNAL_FILE", self.journal),
            patch.object(sr, "_TIMEOUT", 0),        # 0=不起定时器，超时单独测
            patch.object(sr, "_O2O_ONLY", False),   # 默认：单聊 + 群聊都审（#107）
            # 贴表情裁决要反查被贴消息的正文 —— 单测里给确定值，别 shell 出去调 dws
            patch.object(sr, "_seq_of_message", lambda mid: 1 if mid else None),
            patch.object(sr, "_REACTION_DEBOUNCE", 0),   # 单测不等宽限期
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
        """超时的问题没被丢掉 —— 流水里记为 expired，主管事后引用任意相关消息都能补裁。"""
        self._escalate(draft="AI草稿")
        seq = max(sr._pending)
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr._timeout(seq)
        self.assertEqual(sr._history[seq]["state"], "expired")

    def test_quoting_expired_review_still_works(self):
        """核心：超时之后引用那次审核的消息回「同意」，草稿照样发给提问者。"""
        self._escalate(user="张三", conv_id="cidZhang", draft="AI草稿")
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr._timeout(1)
        msg = _msg("boss", "同意", conv_id="cidBoss", msg_id="m9")
        msg.extra.update({"quoted": True, "quoted_seq": 1})
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(msg))
        self.assertEqual(rep.call_args[0][0], "cidZhang")
        self.assertEqual(rep.call_args[0][2], "AI草稿")
        self.assertEqual(sr._history[1]["state"], "answered")

    def test_archived_rewrite_is_learned(self):
        """事后补写的答案同样进知识库。"""
        self._escalate(text="报销怎么走", draft="AI瞎猜")
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr._timeout(1)
        msg = _msg("boss", "改：找财务小王", conv_id="cidBoss", msg_id="m9")
        msg.extra.update({"quoted": True, "quoted_seq": 1})
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr.on_inbound(msg)
        self.assertEqual(rep.call_args[0][2], "找财务小王")
        with open(self.kf, encoding="utf-8") as f:
            recs = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(recs[0]["answer"], "找财务小王")

    def test_history_is_bounded(self):
        """历史有界，长跑不涨内存。"""
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            for i in range(sr._HISTORY_MAX + 10):
                self._escalate(conv_id=f"cid{i}", msg_id=f"m{i}")
                sr._timeout(max(sr._pending))
        self.assertLessEqual(len(sr._history), sr._HISTORY_MAX)

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
    """不发消息的出口必须显式收尾 ack（#109）。

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
    """贴表情裁决改为**事件驱动**（#108）：不再轮询，且不再只限卡片、不再只限待审期间。"""

    _LINE = ("[connect] 表情回应 @boss: {emoji} (convType=1 convId=cidBoss "
             "reactedMsgId={mid} reactionOp={op} eventId={eid})")

    def _reaction(self, emoji="赞", mid="msgCARD", op="add", eid="ev1"):
        return sr.classify_line(self._LINE.format(emoji=emoji, mid=mid, op=op, eid=eid))

    def test_classify_line_parses_reaction(self):
        msg = self._reaction()
        self.assertEqual(msg.kind, sr.KIND_REACTION)
        self.assertEqual(msg.user, "boss")
        self.assertEqual(msg.extra["reaction"], "赞")
        self.assertEqual(msg.extra["reacted_msg_id"], "msgCARD")
        self.assertEqual(msg.msg_id, "ev1")   # msg_id=event_id → 复用 core 的 dedup

    def test_reaction_line_is_not_a_normal_message(self):
        """这条行绝不能被 core 当成普通消息 —— 否则能力关掉时会被喂给大脑。"""
        from core.inbound import parse_line
        self.assertIsNone(parse_line(self._LINE.format(
            emoji="赞", mid="m", op="add", eid="e")))

    def test_thumbs_up_approves(self):
        self._escalate(draft="AI草稿")
        with patch.object(sr, "submit_reply", lambda fn, *a: fn(*a)), \
             patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(self._reaction("赞")))
        self.assertEqual(rep.call_args[0][2], "AI草稿")
        self.assertEqual(len(sr._pending), 0)

    def test_cross_ignores(self):
        self._escalate()
        with patch.object(sr, "submit_reply", lambda fn, *a: fn(*a)), \
             patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(self._reaction("❌")))
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 0)

    def test_reaction_on_non_review_message_ignored(self):
        """贴在无关消息上（反查不出编号）→ 什么都不做，绝不改判到别的待审。"""
        self._escalate()
        with patch.object(sr, "submit_reply", lambda fn, *a: fn(*a)), \
             patch.object(sr, "_seq_of_message", lambda mid: None), \
             patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr.on_inbound(self._reaction("赞", mid="msg无关"))
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 1)

    def test_non_supervisor_reaction_ignored(self):
        """别人贴的表情不是裁决，更不能被当成新提问送审。"""
        line = self._LINE.format(emoji="赞", mid="m", op="add", eid="e").replace(
            "@boss:", "@张三:")
        with patch.object(sr, "submit_reply") as sub, \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(sr.classify_line(line)))
        sub.assert_not_called()      # 没有生成草稿、没造假待审
        rep.assert_not_called()

    def test_remove_within_debounce_cancels(self):
        """贴错了立刻撤 → 取消裁决。事件驱动后每次贴都是不可撤的即时裁决，要留反悔窗口。"""
        self._escalate(draft="AI草稿")
        with patch.object(sr, "_REACTION_DEBOUNCE", 5), \
             patch.object(sr, "submit_reply") as sub:
            sr.on_inbound(self._reaction("赞", eid="e1"))
            sr.on_inbound(self._reaction("赞", op="remove", eid="e2"))
        sub.assert_not_called()
        self.assertEqual(len(sr._pending), 1)

    def test_emoji_mapping(self):
        self.assertEqual(sr._emoji_action("赞"), "approve")
        self.assertEqual(sr._emoji_action("❌"), "ignore")
        self.assertIsNone(sr._emoji_action("🐶"))

    def test_unknown_emoji_asks(self):
        self._escalate()
        with patch.object(sr, "submit_reply", lambda fn, *a: fn(*a)), \
             patch.object(sr, "_send_to_supervisor") as sup, \
             patch.object(sr, "send_reply") as rep:
            sr.on_inbound(self._reaction("🐶"))
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 1)
        self.assertIn("🐶", sup.call_args[0][0])

    def test_reaction_works_after_review_expired(self):
        """核心诉求：待审早就结束了，事后贴表情照样能补裁（以前轮询线程已退出，无人读）。"""
        self._escalate(user="张三", conv_id="cidZhang", draft="AI草稿")
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr._timeout(1)
        self.assertEqual(len(sr._pending), 0)
        with patch.object(sr, "submit_reply", lambda fn, *a: fn(*a)), \
             patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr.on_inbound(self._reaction("赞"))
        self.assertEqual(rep.call_args[0][0], "cidZhang")
        self.assertEqual(rep.call_args[0][2], "AI草稿")


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

    def test_classify_line_ignores_forged_body_markers(self):
        """正文里打 quotedSeq=9 不能伪造出引用 —— 字段只从行尾段抽。

        提问者的正文同样会原样进 connect 行，这不只是主管能触发。
        """
        line = ("[connect] 收到 @hugozhu: 我打个 quotedMsgId=x quotedSeq=9 试试 "
                "(convType=1 convId=cidBoss msgId=m5)")
        msg = sr.classify_line(line)
        self.assertIsNone(msg, "正文里的假标记被当真了")

    def test_classify_line_reads_quoted_seq(self):
        line = ("[connect] 收到 @hugozhu: 同意 (convType=1 convId=cidBoss "
                "msgId=m5 quotedMsgId=q1 quotedSeq=12)")
        msg = sr.classify_line(line)
        self.assertEqual(msg.extra.get("quoted_seq"), 12)
        self.assertTrue(msg.extra.get("quoted"))

    def test_classify_line_marks_unresolvable_quote(self):
        """引用了无关消息：quoted=True 但 quoted_seq=None —— 两者必须能区分。"""
        line = ("[connect] 收到 @hugozhu: 同意 (convType=1 convId=cidBoss "
                "msgId=m5 quotedMsgId=q1 quotedSeq=?)")
        msg = sr.classify_line(line)
        self.assertTrue(msg.extra.get("quoted"))
        self.assertIsNone(msg.extra.get("quoted_seq"))

    def test_classify_line_ignores_normal_lines(self):
        """没有引用信息的行交回 core 标准解析。"""
        line = "[connect] 收到 @hugozhu: 同意 (convType=1 convId=cidBoss msgId=m5)"
        self.assertIsNone(sr.classify_line(line))

    def test_quoted_card_wins_over_latest(self):
        """引用了 #1 就裁 #1，哪怕 #2 才是最近一条。"""
        self._escalate(user="张三", conv_id="cidZhang", msg_id="m1", draft="草稿1")
        self._escalate(user="李四", conv_id="cidLi", msg_id="m2", draft="草稿2")
        msg = _msg("boss", "同意", conv_id="cidBoss")
        msg.extra.update({"quoted": True, "quoted_seq": 1, "quoted_msg_id": "q1"})
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(msg))
        self.assertEqual(rep.call_args[0][0], "cidZhang")   # 不是最近的李四
        self.assertEqual(rep.call_args[0][2], "草稿1")

    def test_quoted_unrelated_message_never_retargets(self):
        """**最高危的一条**：引用了无关消息，绝不能改判到"最近一条"。

        那会把裁决落到另一个提问者头上，而引用恰恰是主管指向性最明确的动作。
        """
        self._escalate(user="张三", conv_id="cidZhang")
        msg = _msg("boss", "同意", conv_id="cidBoss")
        msg.extra["quoted_msg_id"] = "msg-不相干"      # 有引用但无 quotedSeq
        with patch.object(sr, "_send_to_supervisor") as sup, \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(msg))
        rep.assert_not_called()                        # 张三没有被误答
        self.assertEqual(len(sr._pending), 1)
        self.assertIn("对不上号", sup.call_args[0][0])

    def test_quoting_an_answered_review_refuses_by_default(self):
        """答案已经发出去了，再引用回来改口 → 默认拒绝（数字员工不能自己推翻自己）。

        群聊场景更要紧：两条互相矛盾的公开发言比不改口糟得多。
        """
        self._escalate(user="张三", conv_id="cidZhang", msg_id="m1")
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss"))   # #1 已答复
        self._escalate(user="李四", conv_id="cidLi", msg_id="m2")       # #2 成为最近一条
        msg = _msg("boss", "忽略", conv_id="cidBoss", msg_id="m9")
        msg.extra.update({"quoted": True, "quoted_seq": 1})
        with patch.object(sr, "_send_to_supervisor") as sup, \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(msg))
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 1)          # 李四的待审没被误判
        self.assertIn("已经答复过", sup.call_args[0][0])

    def test_amend_prefix_overrides_answered_guard(self):
        """主管明说「更正：」就放行改口 —— 默认保守，但要有逃生口。"""
        self._escalate(user="张三", conv_id="cidZhang", msg_id="m1")
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss"))
        msg = _msg("boss", "更正：应该找财务小王", conv_id="cidBoss", msg_id="m9")
        msg.extra.update({"quoted": True, "quoted_seq": 1})
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(msg))
        self.assertEqual(rep.call_args[0][0], "cidZhang")
        self.assertEqual(rep.call_args[0][2], "应该找财务小王")

    def test_quoting_any_related_message_locates_the_review(self):
        """核心诉求：引用那次审核的**任意一条**消息（不只是卡片）都能定位。

        bridge 从被引用消息的正文开头抽「待审 #N」，8 种消息正文里全都带它。
        """
        self._escalate(user="张三", conv_id="cidZhang", draft="AI草稿")
        msg = _msg("boss", "同意", conv_id="cidBoss", msg_id="m9")
        msg.extra.update({"quoted": True, "quoted_seq": 1})   # 引用的是「完整草稿」那条
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(msg))
        self.assertEqual(rep.call_args[0][2], "AI草稿")


class TestJournal(_Base):
    """审核流水：短号 #N 必须跨重启唯一（#108）。

    以前 _seq_counter 是纯内存的，重启归零 → 主管会话里同时存在多张「待审 #3」，
    已经造成过一次静默事故（反查卡片匹配到上一轮的同号卡片，贴表情从此无效）。
    """

    def _write(self, *lines):
        with open(self.journal, "w", encoding="utf-8") as f:
            f.write("".join(ln + "\n" for ln in lines))

    def test_seq_continues_after_restart(self):
        """核心：重启（=重新 replay）后短号从高水位续起，不回到 #1。"""
        self._escalate(msg_id="m1")
        self._escalate(msg_id="m2")
        self.assertEqual(max(sr._pending), 2)
        sr._reset()                       # 模拟进程重启：内存全丢，流水还在
        self.assertEqual(sr._seq_counter, 0)
        sup, _ = self._escalate(msg_id="m3")
        self.assertEqual(max(sr._pending), 3, "重启后短号撞号了")
        self.assertIn("待审 #3", sup.call_args[0][0])

    def test_open_and_decision_are_journaled(self):
        self._escalate(text="报销怎么走")
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss"))
        recs, bad = sr._journal_tail(self.journal)
        self.assertEqual(bad, 0)
        kinds = [r.get("t") for r in recs]
        self.assertEqual(kinds, ["seq", "open", "done"])
        self.assertEqual(recs[1]["q"], "报销怎么走")
        self.assertEqual(recs[2]["action"], "answered")

    def test_history_records_state(self):
        """裁决后历史里留下状态，供事后引用旧消息时分辨"答过"与"没人理"。"""
        self._escalate()
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr.on_inbound(_msg("boss", "#1 忽略", conv_id="cidBoss"))
        self.assertEqual(sr._history[1]["state"], "ignored")

    def test_replay_survives_garbage(self):
        """坏行/半行/空行都不能让 replay 抛异常 —— 它跑在能力注册链上。"""
        self._write('{"t":"seq","n":7}', "这不是 JSON", '{"t":"seq","n":9}',
                    '{"t":"open","n":8,', "", "[1,2,3]")
        recs, bad = sr._journal_tail(self.journal)
        self.assertEqual(bad, 3)          # 坏行被计数而不是静默吞掉
        sr._ensure_replayed()
        self.assertEqual(sr._seq_counter, 9)

    def test_replay_on_missing_file_is_noop(self):
        sr._ensure_replayed()             # journal 尚不存在
        self.assertEqual(sr._seq_counter, 0)

    def test_replay_on_empty_file(self):
        self._write()
        sr._ensure_replayed()
        self.assertEqual(sr._seq_counter, 0)

    def test_tail_reads_only_last_bytes(self):
        """只读尾部：流水跑几个月会有几十 MB，全量解析不可接受。"""
        self._write(*[f'{{"t":"seq","n":{i}}}' for i in range(1, 501)])
        recs, _ = sr._journal_tail(self.journal, max_bytes=200)
        self.assertLess(len(recs), 500)
        self.assertEqual(recs[-1]["n"], 500)      # 尾部一定读到

    def test_tail_drops_partial_first_line(self):
        """从中间切进去时，被切半的那行必须丢掉而不是当坏行。"""
        self._write(*[f'{{"t":"seq","n":{i}}}' for i in range(1, 51)])
        recs, bad = sr._journal_tail(self.journal, max_bytes=64)
        self.assertEqual(bad, 0)

    def test_unwritable_journal_does_not_block_escalation(self):
        """落盘失败要告警但**不能丢掉提问者的问题**（丢问题比重号更糟）。"""
        with patch.object(sr, "_JOURNAL_FILE", "/proc/nonexistent/x.jsonl"):
            sup, rep = self._escalate()
        sup.assert_called_once()                  # 卡片照发
        self.assertEqual(len(sr._pending), 1)

    def test_reset_does_not_touch_journal_file(self):
        """_reset 只清内存 —— 单测 setUp 先 _reset 后 patch 路径，读盘会读到真实文件。"""
        self._escalate()
        sr._reset()
        self.assertTrue(os.path.exists(self.journal))


class TestQuotedJudge(_Base):
    """引用回复 = 正式作答：内容交大模型判能不能原样发给提问者（#107）。

    这是"用字数猜意图"的升级 —— 长度分不开「这个回答我觉得不太对，你再想想」（15 字，
    是评语）和「找财务小王签字」（7 字，是答案）。
    """

    def _quoted(self, text, seq=1):
        msg = _msg("boss", text, conv_id="cidBoss", msg_id="m9")
        msg.extra.update({"quoted": True, "quoted_seq": seq, "quoted_msg_id": "q1"})
        return msg

    def test_sendable_reply_goes_straight_to_asker(self):
        """判为可直发 → 原样发给提问者，不用敲「改：」。"""
        self._escalate(user="张三", conv_id="cidZhang")
        with patch.object(sr, "submit_reply", lambda fn, *a: fn(*a)), \
             patch.object(sr, "_judge_directly_sendable", return_value=True), \
             patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(self._quoted("找财务小王签字")))
        self.assertEqual(rep.call_args[0][0], "cidZhang")
        self.assertEqual(rep.call_args[0][2], "找财务小王签字")
        self.assertEqual(len(sr._pending), 0)

    def test_comment_is_held_back(self):
        """判为评语 → 一个字都不发给提问者，待审保留，回主管一句。"""
        self._escalate()
        with patch.object(sr, "submit_reply", lambda fn, *a: fn(*a)), \
             patch.object(sr, "_judge_directly_sendable", return_value=False), \
             patch.object(sr, "_send_to_supervisor") as sup, \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(self._quoted("这个回答我觉得不太对，你再想想")))
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 1)
        self.assertIn("没发", sup.call_args[0][0])

    def test_explicit_verdicts_skip_the_judge(self):
        """「同意」「忽略」「改：」是明示，不该白花一次模型往返。"""
        for text in ("同意", "忽略", "改：这样答"):
            self._escalate(msg_id=f"m-{text}")
            with patch.object(sr, "_judge_directly_sendable") as judge, \
                 patch.object(sr, "_send_to_supervisor"), \
                 patch.object(sr, "send_reply"):
                sr.on_inbound(self._quoted(text, seq=max(sr._pending)))
            judge.assert_not_called()

    def test_judge_failure_falls_back_to_heuristic(self):
        """模型判不了时回落长度启发式 —— 大模型挂了不该让正式通道整个失灵。"""
        self._escalate()
        with patch.object(sr, "submit_reply", lambda fn, *a: fn(*a)), \
             patch.object(sr, "_judge_directly_sendable", return_value=None), \
             patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr.on_inbound(self._quoted("需要部门经理先签字再交财务室，三个工作日到账"))
        self.assertEqual(rep.call_args[0][2], "需要部门经理先签字再交财务室，三个工作日到账")

    def test_judge_parses_model_output(self):
        """SEND/HOLD 解析：含糊或答非所问一律当"判不了"。"""
        cases = [("SEND", True), ("hold", False), ("SEND\n", True),
                 ("我觉得可以 SEND", True), ("SEND 还是 HOLD？", None), ("不知道", None)]
        for out, want in cases:
            with patch.object(sr, "generate_reply_ex", return_value=(out, "ok")):
                self.assertIs(sr._judge_directly_sendable("q", "d", "r"), want, out)

    def test_judge_prompt_defaults_to_send(self):
        """引用回复是主管特意选的"正式作答"通道 —— 判断必须**默认放行**。

        回归的是真事故：早期 prompt 把「疑问」也列进 HOLD，主管反问提问者一句
        「hello2 是啥意思」就被拦下（那明明是在跟提问者对话），另一条观点性答复
        也被当成"对草稿的评语"。主管的原话是"我觉得都可以啊"。
        """
        self.assertIn("默认是 SEND", sr._JUDGE_PROMPT)
        self.assertIn("拿不准", sr._JUDGE_PROMPT)          # 兜底方向写死在提示里
        self.assertNotIn("疑问", sr._JUDGE_PROMPT)         # 反问提问者不算 HOLD

    def test_judge_unavailable_returns_none(self):
        with patch.object(sr, "generate_reply_ex", return_value=("", "failed")):
            self.assertIsNone(sr._judge_directly_sendable("q", "d", "r"))

    def test_judge_can_be_disabled(self):
        """SUPERVISOR_JUDGE_QUOTED=0 → 退回纯启发式，不打模型。"""
        self._escalate()
        with patch.object(sr, "_JUDGE_QUOTED", False), \
             patch.object(sr, "_judge_directly_sendable") as judge, \
             patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply"):
            sr.on_inbound(self._quoted("这个不太对"))
        judge.assert_not_called()


class TestPendingNotLost(_Base):
    """裁决没能真正完成时，待审必须留着 —— 否则提问者永久挂起（并触发 #109 心跳）。"""

    def test_approve_without_draft_keeps_pending(self):
        self._escalate(draft="")
        with patch.object(sr, "_send_to_supervisor") as sup, \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss")))
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 1)
        self.assertIn("没有可放行的草稿", sup.call_args[0][0])

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
