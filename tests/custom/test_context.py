#!/usr/bin/env python3
"""context — 上下文组装单测（#112 步骤 2）。

正例只占一半。真正值钱的是**负例**：窗口外的图不该挂、已经单独回复过的图不该重挂、
任何异常都不该冒出去（冒出去就是 #109 那套「卡片不发 + ack 停在处理中」的症状）。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from core.inbound import InboundMessage  # noqa: E402
from custom import context, convq, mediadesc, msgstore, quoted  # noqa: E402

CONV = "cid群=="


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="context-test-")
        os.environ["AGENT_MSGSTORE_DIR"] = self.tmp
        mediadesc._reset()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("AGENT_MSGSTORE_DIR", None)
        mediadesc._reset()

    def _img(self, msg_id, user="张三", ago=5):
        """往库里塞一条"刚发的图"，ago 秒之前。"""
        msgstore.record(InboundMessage(user=user, text=f"[图片消息](mediaId=${msg_id})",
                                       conv_type="2", conv_id=CONV, msg_id=msg_id,
                                       kind="image"), "in")
        # record 写的是 now，直接改 ts 比 mock time 干净
        self._age(msg_id, ago)

    def _age(self, msg_id, ago):
        import json
        p = msgstore._shard(CONV, day=time.strftime("%Y-%m-%d"))
        with open(p, encoding="utf-8") as f:
            recs = [json.loads(ln) for ln in f if ln.strip()]
        for r in recs:
            if r.get("id") == msg_id and r.get("t") == "msg":
                r["ts"] = int(time.time()) - ago
        with open(p, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)


class TestLookback(_Base):
    def test_recent_image_is_attached(self):
        """本次要修的正主：群里刚发过图（未 @），追问时要看得到它。"""
        self._img("mIMG")
        with patch.object(mediadesc, "describe", return_value=("图中是一张考勤表", "ok")):
            prompt, raw = context.build("张三", "这个怎么理解", CONV,
                                        exclude_msg_id="mASK")
        self.assertTrue(raw)
        self.assertIn("考勤表", prompt)
        self.assertIn("这个怎么理解", prompt)

    def test_wording_is_speculative(self):
        """时间邻近只是**猜测** —— 措辞说死会让模型给无关的图强行编联系。"""
        self._img("mIMG")
        with patch.object(mediadesc, "describe", return_value=("一张表格", "ok")):
            prompt, _ = context.build("张三", "今天天气怎么样", CONV)
        self.assertIn("可能", prompt)
        self.assertIn("忽略", prompt)
        self.assertNotIn("用户是**针对上面那条被引用的消息**说这句话的", prompt)

    def test_out_of_window_image_not_attached(self):
        """**负例**：10 分钟前的图 + 一句新问题，不该挂上去。"""
        self._img("mOLD", ago=600)
        with patch.object(mediadesc, "describe") as d:
            prompt, raw = context.build("张三", "报销怎么弄", CONV)
        d.assert_not_called()
        self.assertEqual((prompt, raw), ("报销怎么弄", False))

    def test_already_answered_image_not_reattached(self):
        """**负例**：image 能力已经就这张图单独回复过，描述在会话历史里了，别再塞。"""
        self._img("mIMG")
        msgstore.record_description(CONV, "mIMG", "一张考勤表", by="image", ok=True)
        with patch.object(mediadesc, "describe") as d:
            prompt, raw = context.build("张三", "这个怎么理解", CONV)
        d.assert_not_called()
        self.assertEqual(raw, False)

    def test_ondemand_description_is_reused(self):
        """反过来：回看自己识别出来的（by=ondemand）要能复用，不是每次重识别。"""
        self._img("mIMG")
        msgstore.record_description(CONV, "mIMG", "一张考勤表", by="ondemand", ok=True)
        with patch("custom.capabilities.image._download_image") as dl:
            prompt, raw = context.build("张三", "这个怎么理解", CONV)
        dl.assert_not_called()
        self.assertIn("考勤表", prompt)

    def test_self_message_excluded(self):
        """当前这条消息自己不能被当成"刚才发的图"。"""
        self._img("mSELF")
        with patch.object(mediadesc, "describe") as d:
            _, raw = context.build("张三", "看图", CONV, exclude_msg_id="mSELF")
        d.assert_not_called()
        self.assertFalse(raw)

    def test_multiple_images_in_time_order(self):
        """连发几张再问"这几张什么意思" —— 按**发送顺序**编号，不是倒序。"""
        self._img("mA", ago=30)
        self._img("mB", ago=20)
        descs = {"mA": "第一张：进度表", "mB": "第二张：预算表"}
        with patch.object(mediadesc, "describe",
                          side_effect=lambda c, m, t, **k: (descs[m], "ok")):
            prompt, _ = context.build("张三", "这几张什么意思", CONV)
        self.assertLess(prompt.index("进度表"), prompt.index("预算表"))
        self.assertIn("图片 1/2", prompt)

    def test_limit_caps_images(self):
        for i in range(6):
            self._img(f"m{i}", ago=10)
        with patch.object(mediadesc, "describe", return_value=("图", "ok")) as d:
            context.build("张三", "看看", CONV)
        self.assertLessEqual(d.call_count, context._MAX_IMAGES)

    def test_pending_degrades_not_blocks(self):
        """识别没赶上 → 说明"还在识别中"，而不是干等或假装有内容。"""
        self._img("mIMG")
        with patch.object(mediadesc, "describe", return_value=("", "pending")):
            prompt, raw = context.build("张三", "这个怎么理解", CONV)
        self.assertTrue(raw)
        self.assertIn("识别中", prompt)

    def test_failed_recognition_is_dropped(self):
        """下载/识别失败就当没这张图 —— 告诉模型"有张图但读不出来"只会让它绕着编。"""
        self._img("mIMG")
        with patch.object(mediadesc, "describe", return_value=("", "download")):
            prompt, raw = context.build("张三", "这个怎么理解", CONV)
        self.assertEqual((prompt, raw), ("这个怎么理解", False))

    def test_wait_budget_shared_across_images(self):
        """预算是所有图共享的：3 张图不能等 3×20s（reply 池只有 4 个 worker）。"""
        self._img("mA", ago=30)
        self._img("mB", ago=20)
        waits = []

        def slow(conv, mid, text, wait=None, by=""):
            waits.append(wait)
            time.sleep(0.4)
            return "图", "ok"

        with patch.object(context, "_WAIT", 0.5), \
             patch.object(mediadesc, "describe", side_effect=slow):
            context.build("张三", "看看", CONV)
        self.assertEqual(len(waits), 2)
        self.assertLess(waits[1], waits[0], "第二张的等待预算没被第一张扣减")


class TestNoLongerRacesTheVisionModel(_Base):
    """本次改造的正主：不再拿 reply 池（4 个 worker）去跟视觉模型赛跑。"""

    def test_does_not_block_on_recognition(self):
        """**2026-08-18 14:28 的事故**：等了 20s 还是没赶上，白堵一个 worker。"""
        self._img("mIMG")

        def slow(conv, mid, text, wait=None, by=""):
            # 忠实于 describe 的契约：wait 有值才阻塞，wait=None 立刻返回 pending。
            # 不这么写的话，stub 自己在睡觉，测的就不是"我们等没等"了。
            if wait:
                time.sleep(3)
                return "考勤表", "ok"
            return "", "pending"

        with patch.object(mediadesc, "describe", side_effect=slow):
            t0 = time.monotonic()
            prompt, raw = context.build("张三", "这个图你统计下", CONV)
        self.assertLess(time.monotonic() - t0, 1.0, "还在等识别")
        self.assertTrue(raw)

    def test_still_initiates_recognition(self):
        """不等 ≠ 不发起：daemon 先跑起来，大脑稍后 convq 时多半直接命中缓存。"""
        self._img("mIMG")
        with patch.object(mediadesc, "describe", return_value=("", "pending")) as d:
            context.build("张三", "这个图你统计下", CONV)
        d.assert_called_once()
        self.assertIsNone(d.call_args.kwargs["wait"])

    def test_pending_block_gives_a_runnable_command(self):
        """死胡同 + **一扇标好的门**。只说"还在识别中"是在邀请模型编内容。"""
        self._img("mIMG")
        with patch.object(mediadesc, "describe", return_value=("", "pending")):
            prompt, _ = context.build("张三", "这个图你统计下", CONV)
        self.assertIn(convq.CLI_PATH, prompt)
        self.assertIn("image 'mIMG'", prompt)
        self.assertIn(f"--conv '{CONV}'", prompt)
        self.assertIn("不要猜测", prompt)

    def test_ready_description_is_still_inlined(self):
        """已经识别好的仍然直接内联 —— 能省一次工具往返就省。"""
        self._img("mIMG")
        with patch.object(mediadesc, "describe", return_value=("考勤表", "ok")):
            prompt, _ = context.build("张三", "这个怎么理解", CONV)
        self.assertIn("考勤表", prompt)
        self.assertNotIn("convq.py image", prompt)

    def test_msg_id_rendered_even_when_ready(self):
        """描述被截断时，msg_id 是取全文的唯一把手。"""
        self._img("mIMG")
        with patch.object(mediadesc, "describe", return_value=("考勤表", "ok")):
            prompt, _ = context.build("张三", "这个怎么理解", CONV)
        self.assertIn("msg=mIMG", prompt)

    def test_rollback_lever_restores_old_behavior(self):
        """设回 AGENT_CONTEXT_WAIT_SEC=20 就是改造前的行为 —— 线上出问题的退路。"""
        self._img("mIMG")
        with patch.object(context, "_WAIT", 20), \
             patch.object(mediadesc, "describe", return_value=("考勤表", "ok")) as d:
            context.build("张三", "这个怎么理解", CONV)
        self.assertGreater(d.call_args.kwargs["wait"], 0)


class TestQuotedWins(_Base):
    def test_quoted_takes_precedence(self):
        """引用是**显式证据**，比时间邻近强 —— 有引用就不看时间窗。"""
        self._img("mIMG")
        msgstore.record(InboundMessage(user="李四", text="季度目标定了吗？", conv_type="2",
                                       conv_id=CONV, msg_id="mQ", kind="text"), "in")
        with patch.object(mediadesc, "describe") as d:
            prompt, raw = context.build("张三", "你怎么看", CONV, quoted_msg_id="mQ")
        self.assertIn("季度目标定了吗？", prompt)
        self.assertIn("被引用的消息", prompt)
        d.assert_not_called()           # 没去碰时间窗里那张图

    def test_falls_back_to_lookback_when_quote_unresolvable(self):
        """引用取不到（老消息 + CLI 挂了）时仍走时间窗 —— 被引用的多半就在窗口里。"""
        self._img("mIMG")
        with patch.object(quoted, "_run_cli", lambda a, timeout=60: (1, "boom")), \
             patch.object(mediadesc, "describe", return_value=("一张考勤表", "ok")):
            prompt, raw = context.build("张三", "看一下", CONV, quoted_msg_id="mGONE")
        self.assertTrue(raw)
        self.assertIn("考勤表", prompt)


class TestNeverRaises(_Base):
    def test_store_explosion_falls_through(self):
        """**兜底**：这里抛出去 = 卡片不发 + 提问者收不到 + ack 停在「处理中」（#109）。"""
        with patch.object(msgstore, "recent_media", side_effect=RuntimeError("disk on fire")):
            self.assertEqual(context.build("张三", "在吗", CONV), ("在吗", False))

    def test_describe_explosion_falls_through(self):
        self._img("mIMG")
        with patch.object(mediadesc, "describe", side_effect=RuntimeError("vision down")):
            self.assertEqual(context.build("张三", "在吗", CONV), ("在吗", False))

    def test_empty_store_is_cheap_and_quiet(self):
        self.assertEqual(context.build("张三", "在吗", CONV), ("在吗", False))


if __name__ == "__main__":
    unittest.main()
