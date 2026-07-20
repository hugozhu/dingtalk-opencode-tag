#!/usr/bin/env python3
"""test_ack_capability.py — 回执能力（已读 + 状态「文字表情」时间线）单测（custom）

覆盖：
- 时间线解析 `_parse_stages`（delay:表情:文字，`|` 分隔；排序/非法跳过/空回退；文字含冒号逗号）
- 完成/失败解析 `_parse_status`
- 触发范围 `_should_ack` + on_inbound 非消费型 + 自过滤 + msgId 去重
- 文字表情模板缓存 `_emotion_id`（首次 create，之后复用）
- 状态切换 `_set_status`（移除旧 + 贴新；相同 noop；None 只移除）
- 收到阶段 `_do_processing` + 收尾 `_finalize`（完成/失败/超时）
- 生命周期 worker：随时间升级文字表情 → reply-sent 提前收尾 / 超时兜底
- best-effort：CLI 失败不抛
- core dispatch_reply_sent 广播到 on_reply_sent
"""

import os
import sys
import time
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom.capabilities import ack
from core.inbound import InboundMessage, KIND_TEXT, KIND_IMAGE


def _msg(user="hugozhu", conv_type="1", conv_id="cidO2O==", msg_id="msg1==",
         kind=KIND_TEXT, extra=None):
    return InboundMessage(user=user, text="hi", conv_type=conv_type,
                          conv_id=conv_id, msg_id=msg_id, kind=kind,
                          extra=extra if extra is not None else {})


def _wait_gone(conv_id, tries=100, interval=0.02):
    for _ in range(tries):
        with ack._pending_lock:
            if conv_id not in ack._pending:
                return True
        time.sleep(interval)
    return False


class TestParseStages(unittest.TestCase):
    def test_sorts_and_fields(self):
        self.assertEqual(
            ack._parse_stages("300:咖啡:仍在处理|0:收到:已收到|5:稍等:处理中"),
            [(0.0, "收到", "已收到"), (5.0, "稍等", "处理中"), (300.0, "咖啡", "仍在处理")])

    def test_text_may_contain_colon_and_comma(self):
        self.assertEqual(ack._parse_stages("0:收到:已收到：请稍候,马上"),
                         [(0.0, "收到", "已收到：请稍候,马上")])

    def test_skips_invalid(self):
        self.assertEqual(ack._parse_stages("bad|0:收到|0:收到:文字|x:y:z"),
                         [(0.0, "收到", "文字")])

    def test_empty_falls_back(self):
        self.assertEqual(ack._parse_stages(""), [(0.0, "稍等", "正在处理…")])


class TestParseStatus(unittest.TestCase):
    def test_parse_and_default(self):
        self.assertEqual(ack._parse_status("OK:好了", "x", "y"), ("OK", "好了"))
        self.assertEqual(ack._parse_status("", "OK", "完成"), ("OK", "完成"))
        self.assertEqual(ack._parse_status("noColon", "OK", "完成"), ("OK", "完成"))


class TestShouldAck(unittest.TestCase):
    def test_o2o_triggers(self):
        self.assertTrue(ack._should_ack(_msg(conv_type="1")))

    def test_group_no_at_gated_by_o2o_only(self):
        # 普通群消息（未被@）：ACK_O2O_ONLY=1 不回执；=0 回执（逃生口）
        with patch.object(ack, "_O2O_ONLY", True), patch.object(ack, "_AT_MENTION", True):
            self.assertFalse(ack._should_ack(_msg(conv_type="2")))
        with patch.object(ack, "_O2O_ONLY", False):
            self.assertTrue(ack._should_ack(_msg(conv_type="2")))

    def test_group_at_mention_triggers(self):
        # 群里被@：ACK_AT_MENTION 开 → 回执（即便 ACK_O2O_ONLY=1）
        with patch.object(ack, "_O2O_ONLY", True), patch.object(ack, "_AT_MENTION", True):
            self.assertTrue(ack._should_ack(
                _msg(conv_type="2", extra={"at_mention": True})))

    def test_group_at_mention_flag_off(self):
        # ACK_AT_MENTION 关 + 普通只单聊 → 被@的群消息也不回执
        with patch.object(ack, "_O2O_ONLY", True), patch.object(ack, "_AT_MENTION", False):
            self.assertFalse(ack._should_ack(
                _msg(conv_type="2", extra={"at_mention": True})))

    def test_missing_ids(self):
        self.assertFalse(ack._should_ack(_msg(conv_id="")))
        self.assertFalse(ack._should_ack(_msg(msg_id="")))

    def test_image_kind_also_acked(self):
        self.assertTrue(ack._should_ack(_msg(kind=KIND_IMAGE)))


class TestOnInbound(unittest.TestCase):
    def setUp(self):
        ack._seen.clear()
        with ack._pending_lock:
            ack._pending.clear()

    def test_returns_false_and_begins(self):
        with patch.object(ack, "_begin") as beg:
            self.assertFalse(ack.on_inbound(_msg()))
            beg.assert_called_once()

    def test_self_message_skipped(self):
        with patch.object(ack, "_SELF_NAMES", {"数字员工"}), patch.object(ack, "_begin") as beg:
            self.assertFalse(ack.on_inbound(_msg(user="数字员工")))
            beg.assert_not_called()

    def test_dedup(self):
        with patch.object(ack, "_begin") as beg:
            ack.on_inbound(_msg(msg_id="d=="))
            ack.on_inbound(_msg(msg_id="d=="))
            self.assertEqual(beg.call_count, 1)

    def test_out_of_scope_skipped(self):
        with patch.object(ack, "_O2O_ONLY", True), patch.object(ack, "_AT_MENTION", True), \
             patch.object(ack, "_begin") as beg:
            self.assertFalse(ack.on_inbound(_msg(conv_type="2")))
            beg.assert_not_called()


class TestRaceUpgrade(unittest.TestCase):
    """群里被@时 group+at 双投同一 msgId、行序不定 → 恰好启动一次回执（#46）。"""
    def setUp(self):
        ack._seen.clear()

    def _grp(self, mid, at):
        return _msg(conv_type="2", conv_id="cidG==", msg_id=mid,
                    extra={"at_mention": True} if at else {})

    def test_untagged_then_tagged_begins_once(self):
        with patch.object(ack, "_O2O_ONLY", True), patch.object(ack, "_AT_MENTION", True), \
             patch.object(ack, "_begin") as beg:
            ack.on_inbound(self._grp("m==", at=False))   # 群投递（未打标）先到
            ack.on_inbound(self._grp("m==", at=True))    # @我投递（打标）后到 → 升级启动
            self.assertEqual(beg.call_count, 1)

    def test_tagged_then_untagged_begins_once(self):
        with patch.object(ack, "_O2O_ONLY", True), patch.object(ack, "_AT_MENTION", True), \
             patch.object(ack, "_begin") as beg:
            ack.on_inbound(self._grp("m==", at=True))    # 打标先到 → 启动
            ack.on_inbound(self._grp("m==", at=False))   # 未打标后到 → 不重复启动
            self.assertEqual(beg.call_count, 1)

    def test_both_untagged_group_never_begins(self):
        with patch.object(ack, "_O2O_ONLY", True), patch.object(ack, "_AT_MENTION", True), \
             patch.object(ack, "_begin") as beg:
            ack.on_inbound(self._grp("m==", at=False))
            ack.on_inbound(self._grp("m==", at=False))
            beg.assert_not_called()


class TestEmotionCache(unittest.TestCase):
    def setUp(self):
        with ack._emotion_lock:
            ack._emotion_cache.clear()

    def test_creates_once_and_caches(self):
        calls = []
        def fake_cli(args, timeout=15):
            calls.append(args)
            return 0, '{"result": {"emotionId": "42", "backgroundId": "im_bg_3"}}'
        with patch.object(ack, "_run_cli", fake_cli):
            self.assertEqual(ack._emotion_id("稍等", "处理中"), ("42", "im_bg_3"))
            self.assertEqual(ack._emotion_id("稍等", "处理中"), ("42", "im_bg_3"))  # 缓存
        self.assertEqual(len(calls), 1)   # 只 create 一次

    def test_create_failure_returns_none(self):
        with patch.object(ack, "_run_cli", lambda a, timeout=15: (1, "boom")):
            self.assertEqual(ack._emotion_id("x", "y"), (None, None))


class TestAddRemove(unittest.TestCase):
    def test_add_resolves_id_and_passes_args(self):
        seen = {}
        def fake_cli(args, timeout=15):
            seen["args"] = args
            return 0, "{}"
        with patch.object(ack, "_emotion_id", lambda e, t: ("42", "im_bg_3")), \
             patch.object(ack, "_run_cli", fake_cli):
            self.assertTrue(ack._add_text_emotion("c==", "m==", "稍等", "处理中"))
        a = seen["args"]
        self.assertIn("add-text-emotion", a)
        for tok in ("--emotion-id", "42", "--emotion-name", "稍等", "--text", "处理中",
                    "--background-id", "im_bg_3", "--msg-id", "m=="):
            self.assertIn(tok, a)

    def test_add_skips_when_no_emotion_id(self):
        with patch.object(ack, "_emotion_id", lambda e, t: (None, None)), \
             patch.object(ack, "_run_cli") as cli:
            self.assertFalse(ack._add_text_emotion("c==", "m==", "x", "y"))
            cli.assert_not_called()


class TestSetStatus(unittest.TestCase):
    def test_add_swap_remove(self):
        rec = ack._Pending("c==", "1", "m==")
        with patch.object(ack, "_add_text_emotion") as add, \
             patch.object(ack, "_remove_text_emotion") as rm:
            ack._set_status(rec, ("稍等", "已收到"))
            add.assert_called_once_with("c==", "m==", "稍等", "已收到")
            rm.assert_not_called()
            self.assertEqual(rec.cur, ("稍等", "已收到"))

            add.reset_mock()
            ack._set_status(rec, ("咖啡", "还在处理"))   # 升级
            rm.assert_called_once_with("c==", "m==", "稍等", "已收到")
            add.assert_called_once_with("c==", "m==", "咖啡", "还在处理")

            add.reset_mock(); rm.reset_mock()
            ack._set_status(rec, None)                    # 收尾只移除
            rm.assert_called_once_with("c==", "m==", "咖啡", "还在处理")
            add.assert_not_called()
            self.assertIsNone(rec.cur)

    def test_same_status_noop(self):
        rec = ack._Pending("c==", "1", "m==")
        rec.cur = ("稍等", "处理中")
        with patch.object(ack, "_add_text_emotion") as add, \
             patch.object(ack, "_remove_text_emotion") as rm:
            ack._set_status(rec, ("稍等", "处理中"))
            add.assert_not_called(); rm.assert_not_called()


class TestProcessingAndFinalize(unittest.TestCase):
    def test_do_processing_marks_read_and_first_status(self):
        rec = ack._Pending("c==", "1", "m==")
        with patch.object(ack, "_MARK_READ", True), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "已收到"), (5.0, "稍等", "处理中")]), \
             patch.object(ack, "_mark_read") as mr, patch.object(ack, "_add_text_emotion") as add:
            ack._do_processing(rec)
            mr.assert_called_once_with("c==", "m==")
            add.assert_called_once_with("c==", "m==", "收到", "已收到")
            self.assertEqual(rec.cur, ("收到", "已收到"))

    def test_do_processing_mark_read_off(self):
        rec = ack._Pending("c==", "1", "m==")
        with patch.object(ack, "_MARK_READ", False), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "已收到")]), \
             patch.object(ack, "_mark_read") as mr, patch.object(ack, "_add_text_emotion") as add:
            ack._do_processing(rec)
            mr.assert_not_called(); add.assert_called_once()

    def test_finalize_ok(self):
        rec = ack._Pending("c==", "1", "m=="); rec.cur = ("稍等", "处理中")
        with patch.object(ack, "_DONE", ("OK", "完成")), \
             patch.object(ack, "_remove_text_emotion") as rm, \
             patch.object(ack, "_add_text_emotion") as add:
            ack._finalize(rec, True)
            rm.assert_called_once_with("c==", "m==", "稍等", "处理中")
            add.assert_called_once_with("c==", "m==", "OK", "完成")

    def test_finalize_error(self):
        rec = ack._Pending("c==", "1", "m=="); rec.cur = ("稍等", "处理中")
        with patch.object(ack, "_ERROR", ("疑问", "失败")), \
             patch.object(ack, "_remove_text_emotion") as rm, \
             patch.object(ack, "_add_text_emotion") as add:
            ack._finalize(rec, False)
            add.assert_called_once_with("c==", "m==", "疑问", "失败")

    def test_finalize_none_only_removes(self):
        rec = ack._Pending("c==", "1", "m=="); rec.cur = ("咖啡", "还在处理")
        with patch.object(ack, "_remove_text_emotion") as rm, \
             patch.object(ack, "_add_text_emotion") as add:
            ack._finalize(rec, None)
            rm.assert_called_once_with("c==", "m==", "咖啡", "还在处理")
            add.assert_not_called()


class TestLifecycleWorker(unittest.TestCase):
    """驱动完整 worker 线程。CLI 全 mock，delay/timeout 设短。"""

    def setUp(self):
        with ack._pending_lock:
            ack._pending.clear()
        ack._seen.clear()

    def _record(self):
        calls = []
        return calls, {
            "_mark_read": lambda *a: calls.append(("read",) + a),
            "_add_text_emotion": lambda *a: calls.append(("add",) + a),
            "_remove_text_emotion": lambda *a: calls.append(("rm",) + a),
        }

    def test_escalates_then_done(self):
        calls, m = self._record()
        with patch.object(ack, "_mark_read", m["_mark_read"]), \
             patch.object(ack, "_add_text_emotion", m["_add_text_emotion"]), \
             patch.object(ack, "_remove_text_emotion", m["_remove_text_emotion"]), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "t0"), (0.05, "稍等", "t1"), (0.1, "咖啡", "t2")]), \
             patch.object(ack, "_DONE_TIMEOUT", 5), patch.object(ack, "_DONE", ("OK", "done")):
            ack._begin(_msg(conv_id="cE==", msg_id="mE=="))
            time.sleep(0.25)
            ack.on_reply_sent("cE==", "1", True)
            self.assertTrue(_wait_gone("cE=="))
        added = [(c[3], c[4]) for c in calls if c[0] == "add"]
        self.assertEqual(added[0], ("收到", "t0"))
        self.assertIn(("稍等", "t1"), added)
        self.assertIn(("咖啡", "t2"), added)
        self.assertEqual(added[-1], ("OK", "done"))
        self.assertIn(("read", "cE==", "mE=="), calls)

    def test_reply_before_escalation_skips(self):
        calls, m = self._record()
        with patch.object(ack, "_mark_read", m["_mark_read"]), \
             patch.object(ack, "_add_text_emotion", m["_add_text_emotion"]), \
             patch.object(ack, "_remove_text_emotion", m["_remove_text_emotion"]), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "t0"), (5.0, "稍等", "t1")]), \
             patch.object(ack, "_DONE_TIMEOUT", 30), patch.object(ack, "_DONE", ("OK", "done")):
            ack._begin(_msg(conv_id="cF==", msg_id="mF=="))
            time.sleep(0.05)
            ack.on_reply_sent("cF==", "1", True)
            self.assertTrue(_wait_gone("cF=="))
        added = [(c[3], c[4]) for c in calls if c[0] == "add"]
        self.assertNotIn(("稍等", "t1"), added)
        self.assertEqual(added[-1], ("OK", "done"))

    def test_timeout_only_removes(self):
        calls, m = self._record()
        with patch.object(ack, "_mark_read", m["_mark_read"]), \
             patch.object(ack, "_add_text_emotion", m["_add_text_emotion"]), \
             patch.object(ack, "_remove_text_emotion", m["_remove_text_emotion"]), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "t0")]), \
             patch.object(ack, "_DONE_TIMEOUT", 0.05):
            ack._begin(_msg(conv_id="cT==", msg_id="mT=="))
            self.assertTrue(_wait_gone("cT=="))
        added = [(c[3], c[4]) for c in calls if c[0] == "add"]
        rms = [c for c in calls if c[0] == "rm"]
        self.assertEqual(added, [("收到", "t0")])
        self.assertEqual(len(rms), 1)

    def test_best_effort_no_raise(self):
        def boom(*a):
            raise RuntimeError("cli down")
        rec = ack._Pending("c==", "1", "m==")
        with patch.object(ack, "_add_text_emotion", boom), patch.object(ack, "_mark_read", boom), \
             patch.object(ack, "_remove_text_emotion", boom), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "t0")]), \
             patch.object(ack, "_DONE_TIMEOUT", 0.01):
            ack._ack_worker(rec)  # 不应抛


class TestDispatchReplySent(unittest.TestCase):
    def test_dispatch_calls_on_reply_sent(self):
        import core.capabilities as C
        got = {}
        C.clear()
        C.register(C.Capability(name="probe",
                                on_reply_sent=lambda cid, ct, ok: got.update(cid=cid, ct=ct, ok=ok)))
        C.dispatch_reply_sent("cZ==", "1", True)
        self.assertEqual(got, {"cid": "cZ==", "ct": "1", "ok": True})

    def test_dispatch_isolates_exceptions(self):
        import core.capabilities as C
        C.clear()
        C.register(C.Capability(name="bad", on_reply_sent=lambda *a: (_ for _ in ()).throw(ValueError("x"))))
        hit = []
        C.register(C.Capability(name="good", on_reply_sent=lambda *a: hit.append(1)))
        C.dispatch_reply_sent("c==", "1", False)
        self.assertEqual(hit, [1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
