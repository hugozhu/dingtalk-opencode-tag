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


class TestIdentityAndEdges(_Base):
    """为知识图谱备料（#113）：稳定标识 + 现成的关系边。"""

    def _msg_with(self, **extra):
        m = _msg("mX")
        m.extra = extra
        return m

    def test_from_id_persisted(self):
        """`from` 是展示名，建不了实体；`from_id` 才是能跨会话合并同一人的东西。"""
        msgstore.record(self._msg_with(from_id="idKehan"), "in", path=self.root)
        rec = msgstore.find(CONV, "mX", path=self.root)
        self.assertEqual(rec["from_id"], "idKehan")
        self.assertEqual(rec["from"], "张三")        # 展示名仍然留着（人看日志要用）

    def test_reply_edge_persisted(self):
        """回复关系：bridge 早就把它送到了，以前用完就扔。"""
        msgstore.record(self._msg_with(from_id="idA", quoted_msg_id="mB",
                                       quoted_from_id="idB"), "in", path=self.root)
        rec = msgstore.find(CONV, "mX", path=self.root)
        self.assertEqual((rec["from_id"], rec["quoted"], rec["quoted_from_id"]),
                         ("idA", "mB", "idB"))

    def test_absent_fields_are_not_written(self):
        """缺就不写 —— 老记录没有这些键，读侧一律 .get()，别塞一堆空串进去。"""
        msgstore.record(_msg("mPlain"), "in", path=self.root)
        rec = msgstore.find(CONV, "mPlain", path=self.root)
        for k in ("from_id", "quoted", "quoted_from_id"):
            self.assertNotIn(k, rec)

    def test_old_records_without_ids_still_readable(self):
        """**向后兼容**：库里已有的记录没有这些字段，查询链路一条都不能炸。"""
        d = os.path.join(self.root, msgstore.conv_key(CONV))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{time.strftime('%Y-%m-%d')}.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write(json.dumps({"t": "msg", "dir": "in", "id": "mOld", "conv": CONV,
                                "from": "张三", "kind": "text", "text": "老记录",
                                "ts": int(time.time())}) + "\n")
        self.assertEqual(msgstore.find(CONV, "mOld", path=self.root)["text"], "老记录")
        rows = msgstore.transcript(CONV, path=self.root)
        self.assertEqual(rows[0]["msg"].get("from_id"), None)
        self.assertEqual(msgstore.message(CONV, "mOld", path=self.root)["msg"]["id"], "mOld")

    def test_at_mentions_are_not_stored(self):
        """**刻意不存**：事件 payload 里没有 atUsers（三种事件的 schema 都没有），
        唯一来源是正文里的 @展示名 —— 正文本来就存着，抽取时照样解析得到。
        存一份展示名进来只会多一处要维护的脏数据。"""
        m = self._msg_with(at_mention=True, from_id="idA")
        msgstore.record(m, "in", path=self.root)
        rec = msgstore.find(CONV, "mX", path=self.root)
        self.assertNotIn("at", rec)
        self.assertNotIn("at_mention", rec)


class TestQueryJoins(_Base):
    """查询时把 desc/fb join 到消息上 —— 存储保持追加日志，AI Ready 体现在查询侧。"""

    def _write(self, day, recs):
        d = os.path.join(self.root, msgstore.conv_key(CONV))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{day}.jsonl"), "a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _today(self):
        return time.strftime("%Y-%m-%d")

    def _yesterday(self):
        return time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))

    def test_transcript_joins_desc_and_fb(self):
        msgstore.record(_msg("mIMG", "[图片消息](mediaId=$x)", kind="image"), "in",
                        path=self.root)
        msgstore.record(_msg("mASK", "这个图你统计下"), "in", path=self.root)
        msgstore.record_description(CONV, "mIMG", "8月考勤表", by="premedia",
                                    path=self.root)
        msgstore.record_feedback(CONV, "mASK", seq=7, action="answered",
                                 answer="迟到 2 次", by="朱鸿", path=self.root)
        rows = msgstore.transcript(CONV, path=self.root)
        by_id = {r["msg"]["id"]: r for r in rows}
        self.assertEqual(by_id["mIMG"]["desc"]["text"], "8月考勤表")
        self.assertIsNone(by_id["mIMG"]["fb"])
        self.assertEqual(by_id["mASK"]["fb"]["answer"], "迟到 2 次")
        self.assertIsNone(by_id["mASK"]["desc"])

    def test_transcript_is_oldest_first(self):
        """正序：倒着讲的对话读起来是反的。"""
        for i in range(3):
            msgstore.record(_msg(f"m{i}", f"第{i}句"), "in", path=self.root)
        rows = msgstore.transcript(CONV, path=self.root)
        self.assertEqual([r["msg"]["id"] for r in rows], ["m0", "m1", "m2"])

    def test_transcript_limit_takes_newest(self):
        for i in range(5):
            msgstore.record(_msg(f"m{i}"), "in", path=self.root)
        rows = msgstore.transcript(CONV, limit=2, path=self.root)
        self.assertEqual([r["msg"]["id"] for r in rows], ["m3", "m4"])

    def test_desc_written_after_midnight_still_joins(self):
        """**跨午夜**：图在昨天发、识别在今天才落盘，只扫同样天数会漏（531d039 那类边界）。"""
        self._write(self._yesterday(), [
            {"t": "msg", "dir": "in", "id": "mIMG", "conv": CONV, "from": "张三",
             "kind": "image", "text": "[图片消息](mediaId=$x)",
             "ts": int(time.time()) - 86400}])
        self._write(self._today(), [
            {"t": "desc", "id": "mIMG", "conv": CONV, "text": "考勤表",
             "by": "ondemand", "ok": True, "ts": int(time.time())}])
        rows = msgstore.transcript(CONV, days=1, path=self.root)
        self.assertEqual(len(rows), 0, "days=1 时昨天的消息本就不该出现")
        rows = msgstore.transcript(CONV, days=2, path=self.root)
        self.assertEqual(rows[0]["desc"]["text"], "考勤表")

    def test_search_hits_ocr_text(self):
        """「上次那张写着预算的截图」—— 只有搜 OCR 才找得到，这就是描述落盘的意义。"""
        msgstore.record(_msg("mIMG", "[图片消息](mediaId=$x)", kind="image"), "in",
                        path=self.root)
        msgstore.record_description(CONV, "mIMG", "Q3 预算表：市场 120 万", by="image",
                                    path=self.root)
        hits = msgstore.search(CONV, "预算", path=self.root)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["msg"]["id"], "mIMG")      # 返回的是所属消息
        self.assertIn("预算", hits[0]["desc"]["text"])

    def test_search_deduplicates_double_hits(self):
        """正文和 OCR 都命中时只算一条，否则 limit 会被同一条消息吃掉。"""
        msgstore.record(_msg("mIMG", "预算的图", kind="image"), "in", path=self.root)
        msgstore.record_description(CONV, "mIMG", "预算表", by="image", path=self.root)
        self.assertEqual(len(msgstore.search(CONV, "预算", path=self.root)), 1)

    def test_search_is_case_insensitive_and_bounded(self):
        for i in range(30):
            msgstore.record(_msg(f"m{i}", "Deploy 上线"), "in", path=self.root)
        self.assertEqual(len(msgstore.search(CONV, "deploy", limit=5, path=self.root)), 5)

    def test_search_empty_keyword_returns_nothing(self):
        msgstore.record(_msg("m1", "随便"), "in", path=self.root)
        self.assertEqual(msgstore.search(CONV, "   ", path=self.root), [])

    def test_message_returns_full_view(self):
        msgstore.record(_msg("m1", "报销怎么走"), "in", path=self.root)
        msgstore.record_feedback(CONV, "m1", action="answered", answer="找小王",
                                 path=self.root)
        v = msgstore.message(CONV, "m1", path=self.root)
        self.assertEqual(v["msg"]["text"], "报销怎么走")
        self.assertEqual(v["fb"]["answer"], "找小王")
        self.assertIsNone(msgstore.message(CONV, "不存在", path=self.root))

    def test_empty_conversation_returns_empty(self):
        self.assertEqual(msgstore.transcript("cid空==", path=self.root), [])
        self.assertEqual(msgstore.search("cid空==", "x", path=self.root), [])


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

    def test_enriches_extra_from_raw_line(self):
        """行尾的 id 字段在这里补进 extra —— 本能力 priority=-10 最先跑，
        所以**后面所有能力**都能看到，不用各自去解析 raw_line。"""
        from core.inbound import parse_line
        m = parse_line("[connect] 收到 @可菡: hi (convType=1 convId=cidK msgId=msgA "
                       "senderId=idKehan quotedMsgId=msgB quotedSenderId=idHugo)")
        with patch.object(msgstore, "record", return_value=True):
            self.cap.on_inbound(m)
        self.assertEqual(m.extra["from_id"], "idKehan")
        self.assertEqual(m.extra["quoted_msg_id"], "msgB")
        self.assertEqual(m.extra["quoted_from_id"], "idHugo")

    def test_enrich_does_not_overwrite_existing(self):
        """supervisor_review.classify_line 可能已经解析过 quoted_msg_id，别覆盖它。"""
        from core.inbound import parse_line
        m = parse_line("[connect] 收到 @a: hi (convType=1 convId=c msgId=m "
                       "quotedMsgId=msgFromLine)")
        m.extra["quoted_msg_id"] = "msgAlreadyParsed"
        with patch.object(msgstore, "record", return_value=True):
            self.cap.on_inbound(m)
        self.assertEqual(m.extra["quoted_msg_id"], "msgAlreadyParsed")

    def test_enrich_failure_does_not_block_record(self):
        """富化炸了也必须照常落盘 —— 落盘是本能力的本职。"""
        from custom.capabilities import msgstore_cap as mc
        with patch.object(mc.connline, "field", side_effect=RuntimeError("boom")), \
             patch.object(msgstore, "record", return_value=True) as rec:
            self.assertFalse(self.cap.on_inbound(_msg()))
        rec.assert_called_once()

    def test_store_failure_does_not_break_dispatch(self):
        def boom(*a, **k):
            raise RuntimeError("disk on fire")
        with patch.object(msgstore, "record", boom):
            self.assertFalse(self.cap.on_inbound(_msg()))


if __name__ == "__main__":
    unittest.main()
