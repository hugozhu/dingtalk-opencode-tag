#!/usr/bin/env python3
"""msgstore — 消息落盘存储单测（#111）。

覆盖：会话名安全编码（可逆、不含路径分隔符）/ 写→按 msgId 查回 / 同 id 取最新 /
按天分片 / 裁决反馈 / 保留策略只删自己的分片 / 各种坏输入不抛 / 并发写不撕行 /
能力接线（priority 最小、loop_guard 必须关）。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from core.inbound import InboundMessage  # noqa: E402
from custom import msgstore  # noqa: E402

CONV = "cidQUwzlI5Y+edy9mlQuCbqf/PML5zzQGOkDHSQfIeaPP4g="   # 真实形状：含 + / =


def _msg(msg_id="m1", text="你好", user="张三", conv=CONV, conv_type="1", kind="text"):
    return InboundMessage(user=user, text=text, conv_type=conv_type,
                          conv_id=conv, msg_id=msg_id, kind=kind)


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="msgstore-test-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


class TestConvKey(unittest.TestCase):
    def test_encoding_is_reversible(self):
        k = msgstore.conv_key(CONV)
        self.assertEqual(msgstore.conv_id_of(k), CONV)

    def test_no_path_separators(self):
        """核心：conv_id 含 `/`，直接当目录名会把记录写到别的层级去。"""
        k = msgstore.conv_key(CONV)
        self.assertNotIn("/", k)
        self.assertNotIn("+", k)
        self.assertNotIn("=", k)

    def test_still_recognizable(self):
        """编码后仍看得出是哪个会话 —— 比 hash 强，排查时有用。"""
        self.assertTrue(msgstore.conv_key(CONV).startswith("cidQUwzlI5Y"))

    def test_empty_conv_does_not_escape_root(self):
        self.assertEqual(msgstore.conv_key(""), "_unknown")


class TestRecordAndFind(_Base):
    def test_write_then_find(self):
        msgstore.record(_msg("mA", "报销怎么走"), "in", path=self.root)
        got = msgstore.find(CONV, "mA", path=self.root)
        self.assertEqual(got["text"], "报销怎么走")
        self.assertEqual(got["dir"], "in")
        self.assertEqual(got["from"], "张三")

    def test_outbound_direction(self):
        """自己发的消息经订阅回显进来 —— 这是拿到出站 msgId 的唯一途径。"""
        msgstore.record(_msg("mB", "📋 待审 #7", user="一粟"), "out", path=self.root)
        self.assertEqual(msgstore.find(CONV, "mB", path=self.root)["dir"], "out")

    def test_same_id_takes_latest(self):
        """重投会写多条同 id 记录，查回要取最新那条。"""
        msgstore.record(_msg("mC", "旧"), "in", path=self.root)
        msgstore.record(_msg("mC", "新"), "in", path=self.root)
        self.assertEqual(msgstore.find(CONV, "mC", path=self.root)["text"], "新")

    def test_miss_returns_none(self):
        self.assertIsNone(msgstore.find(CONV, "不存在", path=self.root))
        self.assertIsNone(msgstore.find("别的会话", "mA", path=self.root))
        self.assertIsNone(msgstore.find(CONV, "", path=self.root))

    def test_message_without_id_not_stored(self):
        """没有 msgId 就无从查回，存了也没用。"""
        self.assertFalse(msgstore.record(_msg(msg_id=""), "in", path=self.root))

    def test_sharded_by_day(self):
        msgstore.record(_msg("mD"), "in", path=self.root)
        day = time.strftime("%Y-%m-%d")
        shard = os.path.join(self.root, msgstore.conv_key(CONV), f"{day}.jsonl")
        self.assertTrue(os.path.exists(shard))

    def test_searches_older_shards(self):
        """查回要能翻到更早的分片，不只看今天。"""
        d = os.path.join(self.root, msgstore.conv_key(CONV))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "2020-01-01.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"t": "msg", "id": "老消息", "text": "很久以前"}) + "\n")
        self.assertEqual(msgstore.find(CONV, "老消息", path=self.root)["text"], "很久以前")

    def test_long_text_truncated_and_flagged(self):
        with patch.object(msgstore, "_TEXT_MAX", 10):
            msgstore.record(_msg("mE", "x" * 50), "in", path=self.root)
        got = msgstore.find(CONV, "mE", path=self.root)
        self.assertEqual(len(got["text"]), 10)
        self.assertTrue(got["trunc"])


class TestFeedback(_Base):
    def test_feedback_attached_to_asker_message(self):
        """"我问的那句后来怎么样了" —— 反馈挂在提问者原始消息上。"""
        msgstore.record(_msg("mQ", "报销怎么走"), "in", path=self.root)
        msgstore.record_feedback(CONV, "mQ", seq=7, action="answered",
                                 answer="找财务小王", by="hugozhu", path=self.root)
        fb = msgstore.feedback_of(CONV, "mQ", path=self.root)
        self.assertEqual(fb["action"], "answered")
        self.assertEqual(fb["answer"], "找财务小王")
        self.assertEqual(fb["seq"], 7)

    def test_feedback_does_not_shadow_message(self):
        """同一 id 上既有消息又有反馈，find 只该返回消息记录。"""
        msgstore.record(_msg("mQ2", "原话"), "in", path=self.root)
        msgstore.record_feedback(CONV, "mQ2", action="ignored", path=self.root)
        self.assertEqual(msgstore.find(CONV, "mQ2", path=self.root)["text"], "原话")

    def test_no_feedback_returns_none(self):
        msgstore.record(_msg("mQ3"), "in", path=self.root)
        self.assertIsNone(msgstore.feedback_of(CONV, "mQ3", path=self.root))


class TestPrune(_Base):
    def _shard(self, day, name=None):
        d = os.path.join(self.root, msgstore.conv_key(CONV))
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name or f"{day}.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{}\n")
        return p

    def test_old_shards_removed_recent_kept(self):
        old = self._shard("2020-01-01")
        new = self._shard(time.strftime("%Y-%m-%d"))
        self.assertEqual(msgstore.prune(keep_days=30, path=self.root), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))

    def test_only_own_shards_removed(self):
        """目录里可能被人塞别的东西 —— 无脑按 mtime 删会误伤（同 file._cleanup_tmp 的防呆）。"""
        other = self._shard("2020-01-01", name="notes.txt")
        readme = self._shard("2020-01-01", name="README.md")
        msgstore.prune(keep_days=1, path=self.root)
        self.assertTrue(os.path.exists(other))
        self.assertTrue(os.path.exists(readme))

    def test_keep_zero_disables_prune(self):
        old = self._shard("2020-01-01")
        self.assertEqual(msgstore.prune(keep_days=0, path=self.root), 0)
        self.assertTrue(os.path.exists(old))

    def test_prune_on_missing_dir_is_noop(self):
        self.assertEqual(msgstore.prune(keep_days=1, path=os.path.join(self.root, "无")), 0)


class TestRobustness(_Base):
    def test_bad_lines_do_not_break_lookup(self):
        d = os.path.join(self.root, msgstore.conv_key(CONV))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{time.strftime('%Y-%m-%d')}.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write("这不是 JSON\n")
            f.write('{"t":"msg","id":"半行"\n')
            f.write("\n")
            f.write(json.dumps({"t": "msg", "id": "好的", "text": "找得到"}) + "\n")
        self.assertEqual(msgstore.find(CONV, "好的", path=self.root)["text"], "找得到")

    def test_unwritable_dir_does_not_raise(self):
        self.assertFalse(msgstore.record(_msg("mX"), "in", path="/proc/nonexistent"))

    def test_concurrent_writes_do_not_tear(self):
        """多线程并发追加不能把长记录撕开 —— reply/task 池和 log-tail 都会写。"""
        def w(i):
            for j in range(50):
                msgstore.record(_msg(f"m{i}-{j}", "长正文" * 200), "in", path=self.root)

        ts = [threading.Thread(target=w, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        p = os.path.join(self.root, msgstore.conv_key(CONV),
                         f"{time.strftime('%Y-%m-%d')}.jsonl")
        with open(p, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        self.assertEqual(len(lines), 200)
        for ln in lines:
            json.loads(ln)          # 撕行了这里就会抛


class TestCapabilityWiring(unittest.TestCase):
    def setUp(self):
        from custom.capabilities import msgstore_cap
        self.cap = msgstore_cap

    def test_runs_before_everything(self):
        """收到即存必须先于一切处理 —— 后面的能力可能消费掉消息或抛异常。"""
        from custom.capabilities import trace
        self.assertLess(self.cap.CAPABILITY.priority, trace.CAPABILITY.priority)

    def test_loop_guard_off(self):
        """**设计要害**：自己发的消息经回显进来，是拿到出站 msgId 的唯一途径。

        开着 loop_guard 就等于把出站消息全丢了，而主管引用的往往正是数字员工自己
        发的卡片/回执。
        """
        self.assertFalse(self.cap.CAPABILITY.loop_guard)

    def test_dedup_on(self):
        """群里被 @ 的消息会双流投递（msgId 相同），只存一份。"""
        self.assertTrue(self.cap.CAPABILITY.dedup)

    def test_handles_all_kinds(self):
        self.assertFalse(self.cap.CAPABILITY.handles_kinds)

    def test_never_consumes(self):
        with patch.object(msgstore, "record", return_value=True):
            self.assertFalse(self.cap.on_inbound(_msg()))

    def test_store_failure_does_not_break_dispatch(self):
        def boom(*a, **k):
            raise RuntimeError("disk on fire")
        with patch.object(msgstore, "record", boom):
            self.assertFalse(self.cap.on_inbound(_msg()))


if __name__ == "__main__":
    unittest.main()
