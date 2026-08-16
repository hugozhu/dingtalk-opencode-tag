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


class TestShouldAckSupervisorOnly(unittest.TestCase):
    """只给主管贴状态表情（#106）。

    注意：上面 TestShouldAck 的用例跑在「未配主管」环境下（裸测试环境无
    AGENT_SUPERVISOR_* env），走的正是兜底那条路 —— 那些用例继续全绿本身就验证了
    「没配主管 → 保持原行为」。本类显式设置 env 覆盖新行为。
    """

    _KEYS = ("AGENT_SUPERVISOR_NAME", "AGENT_SUPERVISOR_ALIASES", "AGENT_SUPERVISOR_USER_ID")

    def setUp(self):
        self._orig = {k: os.environ.get(k) for k in self._KEYS}
        os.environ["AGENT_SUPERVISOR_NAME"] = "hugozhu"
        os.environ["AGENT_SUPERVISOR_ALIASES"] = "朱鸿"
        os.environ["AGENT_SUPERVISOR_USER_ID"] = "024083"

    def tearDown(self):
        for k, v in self._orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_supervisor_gets_status_emoji(self):
        with patch.object(ack, "_SUPERVISOR_ONLY", True):
            self.assertTrue(ack._should_ack(_msg(user="hugozhu")))

    def test_supervisor_alias_gets_status_emoji(self):
        with patch.object(ack, "_SUPERVISOR_ONLY", True):
            self.assertTrue(ack._should_ack(_msg(user="朱鸿")))

    def test_other_user_gets_no_status_emoji(self):
        """本 issue 的核心诉求：非主管的单聊不贴状态表情。"""
        with patch.object(ack, "_SUPERVISOR_ONLY", True):
            self.assertFalse(ack._should_ack(_msg(user="张三")))

    def test_other_user_in_group_at_mention_also_gated(self):
        """群里被 @ 的那一路（#46）同样只限主管。"""
        with patch.object(ack, "_SUPERVISOR_ONLY", True), \
             patch.object(ack, "_O2O_ONLY", True), patch.object(ack, "_AT_MENTION", True):
            self.assertFalse(ack._should_ack(
                _msg(user="张三", conv_type="2", extra={"at_mention": True})))
            self.assertTrue(ack._should_ack(
                _msg(user="hugozhu", conv_type="2", extra={"at_mention": True})))

    def test_switch_off_restores_ack_for_all(self):
        """ACK_SUPERVISOR_ONLY=0 → 逃生口，回到所有人都贴。"""
        with patch.object(ack, "_SUPERVISOR_ONLY", False):
            self.assertTrue(ack._should_ack(_msg(user="张三")))

    def test_unconfigured_supervisor_falls_back_to_ack_all(self):
        """未配主管 + 开关开 → 退化为原行为（都贴）。

        这条守着一个无声回退：若少了 has_supervisor() 前置，没配主管的部署会
        **谁都不贴表情**，而 CAP_ACK_ENABLED 默认是开的。
        """
        for k in self._KEYS:
            os.environ.pop(k, None)
        with patch.object(ack, "_SUPERVISOR_ONLY", True):
            self.assertTrue(ack._should_ack(_msg(user="张三")))
            self.assertTrue(ack._should_ack(_msg(user="hugozhu")))

    def test_mark_read_unaffected_by_supervisor_gate(self):
        """已读不受主管闸门影响：非主管的消息照常标已读（真人也会已读）。"""
        with patch.object(ack, "_SUPERVISOR_ONLY", True), \
             patch.object(ack, "_MARK_READ", True):
            self.assertTrue(ack._should_mark_read(_msg(user="张三")))
            self.assertTrue(ack._should_mark_read(_msg(user="张三", conv_type="2")))


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

    def test_group_non_at_marks_read_not_begin(self):
        # 群普通消息：不完整回执，但标记已读（本次需求）
        with patch.object(ack, "_O2O_ONLY", True), patch.object(ack, "_AT_MENTION", True), \
             patch.object(ack, "_MARK_READ", True), \
             patch.object(ack, "_begin") as beg, patch.object(ack, "_mark_read") as mr:
            self.assertFalse(ack.on_inbound(_msg(conv_type="2")))
            beg.assert_not_called()
            mr.assert_called_once()


class TestRaceUpgrade(unittest.TestCase):
    """群里被@时 group+at 双投同一 msgId、行序不定 → 恰好启动一次回执（#46）。
    群投递(非AT)先到会先标记已读，@投递到后升级为完整回执。"""
    def setUp(self):
        ack._seen.clear()

    def _grp(self, mid, at):
        return _msg(conv_type="2", conv_id="cidG==", msg_id=mid,
                    extra={"at_mention": True} if at else {})

    def test_untagged_then_tagged_begins_once(self):
        with patch.object(ack, "_O2O_ONLY", True), patch.object(ack, "_AT_MENTION", True), \
             patch.object(ack, "_MARK_READ", True), \
             patch.object(ack, "_begin") as beg, patch.object(ack, "_mark_read") as mr:
            ack.on_inbound(self._grp("m==", at=False))   # 群投递（未打标）先到 → 仅标已读
            ack.on_inbound(self._grp("m==", at=True))    # @我投递（打标）后到 → 升级启动
            self.assertEqual(beg.call_count, 1)
            self.assertEqual(mr.call_count, 1)           # 已读只标一次（begin 自带已读）

    def test_tagged_then_untagged_begins_once(self):
        with patch.object(ack, "_O2O_ONLY", True), patch.object(ack, "_AT_MENTION", True), \
             patch.object(ack, "_MARK_READ", True), \
             patch.object(ack, "_begin") as beg, patch.object(ack, "_mark_read") as mr:
            ack.on_inbound(self._grp("m==", at=True))    # 打标先到 → 启动
            ack.on_inbound(self._grp("m==", at=False))   # 未打标后到 → 不重复启动、不再标已读
            self.assertEqual(beg.call_count, 1)
            self.assertEqual(mr.call_count, 0)           # begin 内部标已读；这里不额外标

    def test_both_untagged_group_marks_read_once_no_begin(self):
        with patch.object(ack, "_O2O_ONLY", True), patch.object(ack, "_AT_MENTION", True), \
             patch.object(ack, "_MARK_READ", True), \
             patch.object(ack, "_begin") as beg, patch.object(ack, "_mark_read") as mr:
            ack.on_inbound(self._grp("m==", at=False))
            ack.on_inbound(self._grp("m==", at=False))
            beg.assert_not_called()
            self.assertEqual(mr.call_count, 1)           # 普通群消息：仅标已读一次


class TestMarkReadOnly(unittest.TestCase):
    """订阅群里的普通(非@)消息：只标记已读，不贴状态表情、不起 worker（本次需求）。"""
    def setUp(self):
        ack._seen.clear()

    def test_group_non_at_marks_read_no_emotion(self):
        with patch.object(ack, "_O2O_ONLY", True), patch.object(ack, "_AT_MENTION", True), \
             patch.object(ack, "_MARK_READ", True), \
             patch.object(ack, "_begin") as beg, patch.object(ack, "_mark_read") as mr:
            ack.on_inbound(_msg(conv_type="2", conv_id="cG==", msg_id="mg=="))
            beg.assert_not_called()
            mr.assert_called_once_with("cG==", "mg==")

    def test_mark_read_off_disables_group_read(self):
        with patch.object(ack, "_MARK_READ", False), \
             patch.object(ack, "_begin") as beg, patch.object(ack, "_mark_read") as mr:
            ack.on_inbound(_msg(conv_type="2", conv_id="cG==", msg_id="mg2=="))
            beg.assert_not_called()
            mr.assert_not_called()

    def test_should_mark_read_scope(self):
        with patch.object(ack, "_MARK_READ", True):
            self.assertTrue(ack._should_mark_read(_msg(conv_type="2")))   # 群
            self.assertTrue(ack._should_mark_read(_msg(conv_type="1")))   # 单聊
            self.assertFalse(ack._should_mark_read(_msg(conv_id="")))     # 缺 id
        with patch.object(ack, "_MARK_READ", False):
            self.assertFalse(ack._should_mark_read(_msg(conv_type="2")))



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

    def test_progress_texts_create_unique_ids(self):
        """#95 fix：每个不同的「处理中N分钟」创建独立 emotionId（不再归一缓存 key）。"""
        calls = []
        counter = [0]
        def fake_cli(args, timeout=15):
            calls.append(args)
            counter[0] += 1
            return 0, '{"result": {"emotionId": "%d", "backgroundId": "bg"}}' % counter[0]
        with patch.object(ack, "_run_cli", fake_cli), \
             patch.object(ack, "_PROGRESS_EMOJI", "咖啡"):
            eid1, _ = ack._emotion_id("咖啡", "处理中5分钟")
            eid2, _ = ack._emotion_id("咖啡", "处理中10分钟")
            eid3, _ = ack._emotion_id("咖啡", "处理中5分钟")   # 重复应复用
            self.assertEqual(eid1, "1")
            self.assertEqual(eid2, "2")
            self.assertEqual(eid3, "1")      # 缓存命中
            self.assertEqual(len(calls), 2)  # 只 create 两次（5min + 10min）


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
    def test_add_update_remove(self):
        """首次 add；升级走原地 update（不 remove+add）；清除走 remove（#85）。
        #95 fix：验证 update 使用实际挂载的 emotionId（rec.cur_eid），不同于新 eid。"""
        rec = ack._Pending("c==", "1", "m==")
        with patch.object(ack, "_emotion_id") as eid_fn, \
             patch.object(ack, "_add_text_emotion") as add, \
             patch.object(ack, "_run_cli") as cli:
            # 首次贴 → add（emotionId="10"）
            eid_fn.return_value = ("10", "bg1")
            ack._set_status(rec, ("稍等", "已收到"))
            add.assert_called_once_with("c==", "m==", "稍等", "已收到")
            self.assertEqual(rec.cur, ("稍等", "已收到"))
            self.assertEqual(rec.cur_eid, "10")
            self.assertEqual(rec.cur_bid, "bg1")

            # 升级 → update（old_eid="10", new_eid="20"，两者不同）
            add.reset_mock(); cli.reset_mock(); eid_fn.reset_mock()
            eid_fn.return_value = ("20", "bg2")
            cli.return_value = (0, "{}")
            ack._set_status(rec, ("咖啡", "还在处理"))
            # 验证 update-text-emotion 被调用，old-emotion-id="10" != emotion-id="20"
            cli.assert_called_once()
            args = cli.call_args[0][0]
            self.assertIn("update-text-emotion", args)
            self.assertIn("--old-emotion-id", args)
            self.assertIn("10", args)
            self.assertIn("--emotion-id", args)
            self.assertIn("20", args)
            self.assertEqual(rec.cur, ("咖啡", "还在处理"))
            self.assertEqual(rec.cur_eid, "20")

            # 清除 → remove（用挂载的 eid="20"）
            cli.reset_mock()
            cli.return_value = (0, "{}")
            ack._set_status(rec, None)
            cli.assert_called_once()
            args = cli.call_args[0][0]
            self.assertIn("remove-text-emotion", args)
            self.assertIn("--emotion-id", args)
            self.assertIn("20", args)
            self.assertIsNone(rec.cur)
            self.assertIsNone(rec.cur_eid)

    def test_upgrade_falls_back_to_remove_add(self):
        """update 失败时兜底回退 remove 旧 + add 新（#85）。
        #95 fix：兜底 remove 也用实际挂载的 emotionId。"""
        rec = ack._Pending("c==", "1", "m==")
        rec.cur = ("稍等", "已收到")
        rec.cur_eid = "10"
        rec.cur_bid = "bg1"
        with patch.object(ack, "_emotion_id", return_value=("20", "bg2")), \
             patch.object(ack, "_add_text_emotion") as add, \
             patch.object(ack, "_run_cli") as cli:
            # update 失败（rc=1）
            cli.return_value = (1, "update failed")
            ack._set_status(rec, ("咖啡", "还在处理"))
            # 第一次调用 update（失败），第二次调用 remove（兜底），然后 add
            self.assertEqual(cli.call_count, 2)
            # 验证 remove 用的是旧 eid="10"
            remove_args = cli.call_args_list[1][0][0]
            self.assertIn("remove-text-emotion", remove_args)
            self.assertIn("--emotion-id", remove_args)
            self.assertIn("10", remove_args)
            add.assert_called_once_with("c==", "m==", "咖啡", "还在处理")
            self.assertEqual(rec.cur, ("咖啡", "还在处理"))
            self.assertEqual(rec.cur_eid, "20")

    def test_same_status_noop(self):
        rec = ack._Pending("c==", "1", "m==")
        rec.cur = ("稍等", "处理中")
        with patch.object(ack, "_emotion_id") as eid_fn, \
             patch.object(ack, "_add_text_emotion") as add, \
             patch.object(ack, "_run_cli") as cli:
            ack._set_status(rec, ("稍等", "处理中"))
            eid_fn.assert_not_called()
            add.assert_not_called()
            cli.assert_not_called()


class TestUpdateTextEmotion(unittest.TestCase):
    def test_update_passes_explicit_ids(self):
        """#95 fix：_update_text_emotion 接收显式 emotionId 参数，不再从缓存反查。"""
        seen = {}
        def fake_cli(args, timeout=15):
            seen["args"] = args
            return 0, "{}"
        with patch.object(ack, "_run_cli", fake_cli):
            self.assertTrue(
                ack._update_text_emotion("c==", "m==", "10", "bg1", "咖啡", "更新", "42", "im_bg_3"))
        a = seen["args"]
        self.assertIn("update-text-emotion", a)
        for tok in ("--old-emotion-id", "10", "--emotion-id", "42",
                    "--emotion-name", "咖啡", "--text", "更新",
                    "--background-id", "im_bg_3", "--msg-id", "m=="):
            self.assertIn(tok, a)

    def test_update_skips_when_id_missing(self):
        with patch.object(ack, "_run_cli") as cli:
            self.assertFalse(
                ack._update_text_emotion("c==", "m==", None, "bg", "咖啡", "x", "42", "bg2"))
            self.assertFalse(
                ack._update_text_emotion("c==", "m==", "10", "bg", "咖啡", "x", None, "bg2"))
            cli.assert_not_called()

    def test_update_returns_false_on_cli_error(self):
        with patch.object(ack, "_run_cli", lambda a, timeout=15: (1, "boom")):
            self.assertFalse(
                ack._update_text_emotion("c==", "m==", "10", "bg", "咖啡", "x", "42", "bg2"))


class TestProcessingAndFinalize(unittest.TestCase):
    def test_do_processing_marks_read_and_first_status(self):
        rec = ack._Pending("c==", "1", "m==")
        with patch.object(ack, "_MARK_READ", True), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "已收到"), (5.0, "稍等", "处理中")]), \
             patch.object(ack, "_mark_read") as mr, \
             patch.object(ack, "_emotion_id", return_value=("10", "bg")), \
             patch.object(ack, "_add_text_emotion") as add:
            ack._do_processing(rec)
            mr.assert_called_once_with("c==", "m==")
            add.assert_called_once_with("c==", "m==", "收到", "已收到")
            self.assertEqual(rec.cur, ("收到", "已收到"))

    def test_do_processing_mark_read_off(self):
        rec = ack._Pending("c==", "1", "m==")
        with patch.object(ack, "_MARK_READ", False), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "已收到")]), \
             patch.object(ack, "_mark_read") as mr, \
             patch.object(ack, "_emotion_id", return_value=("10", "bg")), \
             patch.object(ack, "_add_text_emotion") as add:
            ack._do_processing(rec)
            mr.assert_not_called(); add.assert_called_once()

    def test_finalize_ok(self):
        rec = ack._Pending("c==", "1", "m==")
        rec.cur = ("稍等", "处理中")
        rec.cur_eid = "10"
        rec.cur_bid = "bg1"
        with patch.object(ack, "_DONE", ("OK", "完成")), \
             patch.object(ack, "_emotion_id", return_value=("20", "bg2")), \
             patch.object(ack, "_run_cli", return_value=(0, "{}")), \
             patch.object(ack, "_add_text_emotion") as add:
            ack._finalize(rec, True)
            # 应调用 update（old=10, new=20），不走 remove+add
            add.assert_not_called()

    def test_finalize_error(self):
        rec = ack._Pending("c==", "1", "m==")
        rec.cur = ("稍等", "处理中")
        rec.cur_eid = "10"
        rec.cur_bid = "bg1"
        with patch.object(ack, "_ERROR", ("疑问", "失败")), \
             patch.object(ack, "_emotion_id", return_value=("30", "bg3")), \
             patch.object(ack, "_run_cli", return_value=(0, "{}")) as cli:
            ack._finalize(rec, False)
            # 应调用 update-text-emotion
            cli.assert_called_once()
            args = cli.call_args[0][0]
            self.assertIn("update-text-emotion", args)

    def test_finalize_none_only_removes(self):
        rec = ack._Pending("c==", "1", "m==")
        rec.cur = ("咖啡", "还在处理")
        rec.cur_eid = "10"
        rec.cur_bid = "bg"
        with patch.object(ack, "_run_cli", return_value=(0, "{}")) as cli, \
             patch.object(ack, "_add_text_emotion") as add, \
             patch.object(ack, "_emotion_id") as eid_fn:
            ack._finalize(rec, None)
            # 超时收尾：只 remove，不 add 新的
            cli.assert_called_once()
            args = cli.call_args[0][0]
            self.assertIn("remove-text-emotion", args)
            self.assertIn("--emotion-id", args)
            self.assertIn("10", args)
            add.assert_not_called()
            eid_fn.assert_not_called()


class TestLifecycleWorker(unittest.TestCase):
    """驱动完整 worker 线程。CLI 全 mock，delay/timeout 设短。"""

    def setUp(self):
        with ack._pending_lock:
            ack._pending.clear()
        ack._seen.clear()

    def _record(self):
        calls = []
        eid_counter = [0]
        def make_eid(emoji, text):
            eid_counter[0] += 1
            return (str(eid_counter[0]), "bg")
        return calls, {
            "_mark_read": lambda *a: calls.append(("read",) + a),
            "_add_text_emotion": lambda *a: calls.append(("add",) + a),
            "_emotion_id": make_eid,
            # 原地更新：记录 ("upd", conv, msg, old_eid, old_bid, new_emoji, new_text, new_eid, new_bid)，返回 True
            "_update_text_emotion": lambda c, m, old_eid, old_bid, ne, nt, new_eid, new_bid: (
                calls.append(("upd", c, m, old_eid, old_bid, ne, nt, new_eid, new_bid)) or True),
            "_run_cli": lambda args, timeout=15: (calls.append(("cli", args)) or (0, "{}")),
        }

    @staticmethod
    def _shown(calls):
        """按时间顺序还原「消息上显示过的 (表情,文字)」：首次 add 的参数，或每次 upd 的新状态。"""
        out = []
        for c in calls:
            if c[0] == "add":
                out.append((c[3], c[4]))
            elif c[0] == "upd":
                # upd: (type, conv, msg, old_eid, old_bid, new_emoji, new_text, new_eid, new_bid)
                out.append((c[5], c[6]))
        return out

    def test_escalates_then_done(self):
        calls, m = self._record()
        with patch.object(ack, "_mark_read", m["_mark_read"]), \
             patch.object(ack, "_add_text_emotion", m["_add_text_emotion"]), \
             patch.object(ack, "_emotion_id", m["_emotion_id"]), \
             patch.object(ack, "_update_text_emotion", m["_update_text_emotion"]), \
             patch.object(ack, "_run_cli", m["_run_cli"]), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "t0"), (0.05, "稍等", "t1"), (0.1, "咖啡", "t2")]), \
             patch.object(ack, "_DONE_TIMEOUT", 5), patch.object(ack, "_DONE", ("OK", "done")):
            ack._begin(_msg(conv_id="cE==", msg_id="mE=="))
            time.sleep(0.25)
            ack.on_reply_sent("cE==", "1", True)
            self.assertTrue(_wait_gone("cE=="))
        shown = self._shown(calls)
        self.assertEqual(shown[0], ("收到", "t0"))       # 首贴 add
        self.assertIn(("稍等", "t1"), shown)              # 升级走 upd
        self.assertIn(("咖啡", "t2"), shown)
        self.assertEqual(shown[-1], ("OK", "done"))       # 收尾走 upd
        self.assertIn(("read", "cE==", "mE=="), calls)

    def test_reply_before_escalation_skips(self):
        calls, m = self._record()
        with patch.object(ack, "_mark_read", m["_mark_read"]), \
             patch.object(ack, "_add_text_emotion", m["_add_text_emotion"]), \
             patch.object(ack, "_emotion_id", m["_emotion_id"]), \
             patch.object(ack, "_update_text_emotion", m["_update_text_emotion"]), \
             patch.object(ack, "_run_cli", m["_run_cli"]), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "t0"), (5.0, "稍等", "t1")]), \
             patch.object(ack, "_DONE_TIMEOUT", 30), patch.object(ack, "_DONE", ("OK", "done")):
            ack._begin(_msg(conv_id="cF==", msg_id="mF=="))
            time.sleep(0.05)
            ack.on_reply_sent("cF==", "1", True)
            self.assertTrue(_wait_gone("cF=="))
        shown = self._shown(calls)
        self.assertNotIn(("稍等", "t1"), shown)
        self.assertEqual(shown[-1], ("OK", "done"))

    def test_timeout_only_removes(self):
        calls, m = self._record()
        with patch.object(ack, "_mark_read", m["_mark_read"]), \
             patch.object(ack, "_add_text_emotion", m["_add_text_emotion"]), \
             patch.object(ack, "_emotion_id", m["_emotion_id"]), \
             patch.object(ack, "_run_cli", m["_run_cli"]), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "t0")]), \
             patch.object(ack, "_DONE_TIMEOUT", 0.05):
            ack._begin(_msg(conv_id="cT==", msg_id="mT=="))
            self.assertTrue(_wait_gone("cT=="))
        shown = self._shown(calls)
        # 只贴过首个，无升级
        self.assertEqual(shown, [("收到", "t0")])
        # 超时收尾：应调用 remove-text-emotion（通过 _run_cli）
        cli_calls = [c for c in calls if c[0] == "cli"]
        remove_calls = [c for c in cli_calls if "remove-text-emotion" in c[1]]
        self.assertEqual(len(remove_calls), 1, "超时收尾应调用 remove")

    def test_reply_sent_none_closes_silently(self):
        """ok=None → 静默收尾：移除「处理中」但不贴完成/失败（#108 忽略场景）。

        贴「完成」= 骗提问者（他并没收到回复），贴「未完成」= 看着像故障。
        """
        calls, m = self._record()
        with patch.object(ack, "_mark_read", m["_mark_read"]), \
             patch.object(ack, "_add_text_emotion", m["_add_text_emotion"]), \
             patch.object(ack, "_emotion_id", m["_emotion_id"]), \
             patch.object(ack, "_update_text_emotion", m["_update_text_emotion"]), \
             patch.object(ack, "_run_cli", m["_run_cli"]), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "t0")]), \
             patch.object(ack, "_DONE_TIMEOUT", 30), \
             patch.object(ack, "_DONE", ("OK", "done")), \
             patch.object(ack, "_ERROR", ("疑问", "fail")):
            ack._begin(_msg(conv_id="cN==", msg_id="mN=="))
            time.sleep(0.05)
            ack.on_reply_sent("cN==", "1", None)
            self.assertTrue(_wait_gone("cN=="), "None 必须唤醒 worker，不能等到 DONE_TIMEOUT")
        shown = self._shown(calls)
        self.assertNotIn(("OK", "done"), shown)
        self.assertNotIn(("疑问", "fail"), shown)
        remove_calls = [c for c in calls if c[0] == "cli" and "remove-text-emotion" in c[1]]
        self.assertEqual(len(remove_calls), 1, "静默收尾应只移除进度表情")

    def test_best_effort_no_raise(self):
        def boom(*a, **k):
            raise RuntimeError("cli down")
        rec = ack._Pending("c==", "1", "m==")
        with patch.object(ack, "_add_text_emotion", boom), \
             patch.object(ack, "_mark_read", boom), \
             patch.object(ack, "_emotion_id", boom), \
             patch.object(ack, "_run_cli", boom), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "t0")]), \
             patch.object(ack, "_DONE_TIMEOUT", 0.01):
            ack._ack_worker(rec)  # 不应抛


class TestProgressHeartbeat(unittest.TestCase):
    """周期进度心跳（#75 长任务）：每 interval 更新表情 + 发独立进度消息，直到 reply-sent/超时。"""

    def setUp(self):
        with ack._pending_lock:
            ack._pending.clear()
        ack._seen.clear()

    def _record(self):
        calls = []
        eid_counter = [0]
        def make_eid(emoji, text):
            eid_counter[0] += 1
            return (str(eid_counter[0]), "bg")
        return calls, {
            "_mark_read": lambda *a: calls.append(("read",) + a),
            "_add_text_emotion": lambda *a: calls.append(("add",) + a),
            "_emotion_id": make_eid,
            "_update_text_emotion": lambda c, m, old_eid, old_bid, ne, nt, new_eid, new_bid: (
                calls.append(("upd", c, m, old_eid, old_bid, ne, nt, new_eid, new_bid)) or True),
            "_run_cli": lambda args, timeout=15: (calls.append(("cli", args)) or (0, "{}")),
        }

    @staticmethod
    def _shows_emoji(calls, emoji):
        """消息上是否显示过某表情：首贴 add(emoji=c[3]) 或原地 upd(new_emoji=c[5])。"""
        return any(
            (c[0] == "add" and c[3] == emoji) or (c[0] == "upd" and c[5] == emoji)
            for c in calls)

    def test_heartbeat_updates_emoji_and_sends_message(self):
        calls, m = self._record()
        notices = []
        with patch.object(ack, "_mark_read", m["_mark_read"]), \
             patch.object(ack, "_add_text_emotion", m["_add_text_emotion"]), \
             patch.object(ack, "_emotion_id", m["_emotion_id"]), \
             patch.object(ack, "_update_text_emotion", m["_update_text_emotion"]), \
             patch.object(ack, "_run_cli", m["_run_cli"]), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "t0")]), \
             patch.object(ack, "_PROGRESS_INTERVAL", 0.05), \
             patch.object(ack, "_PROGRESS_MESSAGE", True), \
             patch.object(ack, "_PROGRESS_EMOJI", "咖啡"), \
             patch.object(ack, "_PROGRESS_EMOJI_TEXT", "处理中{mins}分钟"), \
             patch.object(ack, "_PROGRESS_MSG", "还在跑 {mins}min"), \
             patch.object(ack, "_DONE_TIMEOUT", 5), \
             patch("custom.replier.send_notice", lambda cid, ct, text: notices.append((cid, ct, text))):
            ack._begin(_msg(conv_id="cH==", msg_id="mH=="))
            time.sleep(0.18)                       # 允许 ~3 次心跳
            ack.on_reply_sent("cH==", "1", True)
            self.assertTrue(_wait_gone("cH=="))
        # 至少贴过一次进度表情（咖啡）
        self.assertTrue(self._shows_emoji(calls, "咖啡"), "应更新进度表情")
        # 至少发过一条独立进度消息，且不影响收尾（最终仍切到完成态）
        self.assertGreaterEqual(len(notices), 1, "应发出进度消息")
        self.assertEqual(notices[0][0], "cH==")

    def test_progress_message_disabled_only_emoji(self):
        calls, m = self._record()
        notices = []
        with patch.object(ack, "_mark_read", m["_mark_read"]), \
             patch.object(ack, "_add_text_emotion", m["_add_text_emotion"]), \
             patch.object(ack, "_emotion_id", m["_emotion_id"]), \
             patch.object(ack, "_update_text_emotion", m["_update_text_emotion"]), \
             patch.object(ack, "_run_cli", m["_run_cli"]), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "t0")]), \
             patch.object(ack, "_PROGRESS_INTERVAL", 0.05), \
             patch.object(ack, "_PROGRESS_MESSAGE", False), \
             patch.object(ack, "_DONE_TIMEOUT", 5), \
             patch("custom.replier.send_notice", lambda *a, **k: notices.append(a)):
            ack._begin(_msg(conv_id="cN==", msg_id="mN=="))
            time.sleep(0.12)
            ack.on_reply_sent("cN==", "1", True)
            self.assertTrue(_wait_gone("cN=="))
        self.assertTrue(self._shows_emoji(calls, ack._PROGRESS_EMOJI))
        self.assertEqual(notices, [], "关闭进度消息时不应发送")

    def test_heartbeat_disabled_no_tick(self):
        calls, m = self._record()
        with patch.object(ack, "_mark_read", m["_mark_read"]), \
             patch.object(ack, "_add_text_emotion", m["_add_text_emotion"]), \
             patch.object(ack, "_emotion_id", m["_emotion_id"]), \
             patch.object(ack, "_run_cli", m["_run_cli"]), \
             patch.object(ack, "_STAGES", [(0.0, "收到", "t0")]), \
             patch.object(ack, "_PROGRESS_INTERVAL", 0), \
             patch.object(ack, "_DONE_TIMEOUT", 0.05):
            ack._begin(_msg(conv_id="cD==", msg_id="mD=="))
            self.assertTrue(_wait_gone("cD=="))
        added = [(c[3], c[4]) for c in calls if c[0] == "add"]
        self.assertEqual(added, [("收到", "t0")], "关闭心跳应回退旧行为，无进度表情")

    def test_progress_message_not_finalizing(self):
        """send_notice 不广播 reply-sent：进度消息不会误触发 ack 收尾。"""
        import core.capabilities as C
        from custom import replier
        fired = []
        C.clear()
        C.register(C.Capability(name="probe", on_reply_sent=lambda *a: fired.append(a)))
        with patch.object(replier, "_dingtalk_send", lambda *a, **k: True):
            self.assertTrue(replier.send_notice("cX==", "1", "进度"))
        self.assertEqual(fired, [], "send_notice 不应广播 reply-sent")


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
