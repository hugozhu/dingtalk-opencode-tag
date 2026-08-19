#!/usr/bin/env python3
"""convq — 会话查询 CLI 单测。

这个工具的输出是**喂给大脑**的，所以测的重点不是"能跑"，而是渲染出来的东西会不会让
模型读错：自己发的话有没有标成「我」、截断有没有留下可见出口、mediaId 有没有污染正文、
会不会把别的会话捞进来、上限拦不拦得住"给我全部"。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from core.inbound import InboundMessage  # noqa: E402
from custom import convq, mediadesc, msgstore  # noqa: E402

CONV = "cidQUwzlI5Y+edy9mlQuCbqf/PML5zzQGOkDHSQfIeaPP4g="   # 真实形状：含 + / =
OTHER = "cid别的群=="


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="convq-test-")
        os.environ["AGENT_MSGSTORE_DIR"] = self.tmp
        mediadesc._reset()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("AGENT_MSGSTORE_DIR", None)
        mediadesc._reset()

    def _in(self, msg_id, text, user="张三", conv=CONV, kind="text", direction="in"):
        msgstore.record(InboundMessage(user=user, text=text, conv_type="2",
                                       conv_id=conv, msg_id=msg_id, kind=kind),
                        direction)

    def _run(self, *argv):
        """跑一次 CLI → (退出码, stdout, stderr)。"""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = convq.main(list(argv))
        return rc, out.getvalue(), err.getvalue()


class TestArgs(_Base):
    def test_conv_is_required(self):
        """**没有"全部会话"模式** —— 枚举别的群正是不想暴露的面。"""
        with self.assertRaises(SystemExit) as cm:
            self._run("recent")
        self.assertEqual(cm.exception.code, 2)

    def test_no_subcommand_prints_help(self):
        rc, _, err = self._run()
        self.assertEqual(rc, convq.EXIT_ARGS)
        self.assertIn("convq", err)

    def test_there_is_no_list_conversations_command(self):
        for bad in ("convs", "list", "conversations"):
            with self.assertRaises(SystemExit):
                self._run(bad)


class TestRecentRendering(_Base):
    def test_own_messages_render_as_me(self):
        """`dir=out` 必须是「我」，否则模型把自己过去的回复当第三方断言反复推翻。"""
        self._in("m1", "报销怎么走", user="张三")
        self._in("m2", "找财务小王", user="一粟", direction="out")
        rc, out, _ = self._run("recent", "--conv", CONV)
        self.assertEqual(rc, convq.EXIT_OK)
        self.assertIn("张三: 报销怎么走", out)
        self.assertIn("我: 找财务小王", out)

    def test_media_id_stripped_but_msg_id_kept(self):
        """mediaId 对文本模型是纯 token 浪费；msg_id 才是 convq image 的把手。"""
        self._in("mIMG", "[图片消息](mediaId=$iwEcAqNqcGcDAQ)", kind="image")
        _, out, _ = self._run("recent", "--conv", CONV)
        self.assertNotIn("mediaId", out)
        self.assertNotIn("iwEcAqNqcGc", out)
        self.assertIn("msg=mIMG", out)

    def test_desc_and_fb_joined_under_message(self):
        self._in("mIMG", "[图片消息](mediaId=$x)", kind="image")
        self._in("mASK", "这个图你统计下")
        msgstore.record_description(CONV, "mIMG", "8月考勤表", by="premedia", ok=True)
        msgstore.record_feedback(CONV, "mASK", action="answered", answer="迟到2次",
                                 by="朱鸿")
        _, out, _ = self._run("recent", "--conv", CONV)
        self.assertIn("图片内容(OCR): 8月考勤表", out)
        self.assertIn("主管裁决: answered by 朱鸿 → 「迟到2次」", out)

    def test_failed_recognition_is_labelled(self):
        self._in("mIMG", "[图片消息](mediaId=$x)", kind="image")
        msgstore.record_description(CONV, "mIMG", "", by="image", ok=False,
                                    err="download")
        _, out, _ = self._run("recent", "--conv", CONV)
        self.assertIn("图片识别失败(download)", out)

    def test_header_states_timezone_and_who_is_me(self):
        """表头是给模型的读法说明书 —— 没有它，「我」和显示名都会被读歪。"""
        self._in("m1", "hi")
        _, out, _ = self._run("recent", "--conv", CONV)
        self.assertIn("「我」= 数字员工自己发的", out)
        self.assertIn("显示名不是 userId", out)

    def test_both_absolute_and_relative_time(self):
        """大脑没有可靠的"现在"，相对时间才解得开「刚才」；绝对时间才能和日志对上号。"""
        self._in("m1", "hi")
        _, out, _ = self._run("recent", "--conv", CONV)
        self.assertIn(time.strftime("%m-%d"), out)
        self.assertRegex(out, r"·\s*\d+(秒|分钟|小时|天)前")

    def test_empty_conversation_says_so(self):
        rc, out, _ = self._run("recent", "--conv", "cid空==")
        self.assertEqual(rc, convq.EXIT_OK)
        self.assertIn("没有消息记录", out)


class TestCaps(_Base):
    def test_limit_is_clamped(self):
        """「给我全部」不该把大脑的上下文撑爆。"""
        for i in range(30):
            self._in(f"m{i}", f"第{i}条")
        _, out, _ = self._run("recent", "--conv", CONV, "--limit", "99999")
        self.assertLessEqual(out.count("msg=m"), convq._LIMIT_MAX)

    def test_truncation_leaves_a_visible_exit(self):
        """**静默截断会让模型以为"没有更多了"**，所以尾注必须带定点取全文的命令。"""
        self._in("m1", "很长的正文" * 200)
        _, out, _ = self._run("recent", "--conv", CONV, "--text-max", "50")
        self.assertIn("正文已截断", out)
        self.assertIn("convq.py msg 'm1'", out)

    def test_max_bytes_truncates_with_trailer(self):
        for i in range(50):
            self._in(f"m{i}", "内容" * 50)
        _, out, _ = self._run("recent", "--conv", CONV, "--limit", "50",
                              "--max-bytes", "2000")
        self.assertIn("输出已截到", out)
        self.assertIn("没显示", out)
        self.assertLess(len(out.encode()), 4000)

    def test_msg_subcommand_gives_untruncated_text(self):
        """它正是 recent 截断后的出口 —— 这里再截就成了死胡同。"""
        long = "细节" * 500
        self._in("m1", long)
        _, out, _ = self._run("msg", "m1", "--conv", CONV, "--max-bytes", "64000")
        self.assertIn("细节" * 400, out)


class TestScoping(_Base):
    def test_other_conversation_is_not_reachable(self):
        self._in("mMine", "本会话的秘密")
        self._in("mTheirs", "别的群的秘密", conv=OTHER)
        _, out, _ = self._run("recent", "--conv", CONV)
        self.assertIn("本会话的秘密", out)
        self.assertNotIn("别的群的秘密", out)

    def test_search_stays_in_conversation(self):
        self._in("mMine", "预算表在这")
        self._in("mTheirs", "预算表在那", conv=OTHER)
        _, out, _ = self._run("search", "预算", "--conv", CONV)
        self.assertIn("预算表在这", out)
        self.assertNotIn("预算表在那", out)


class TestSearch(_Base):
    def test_finds_keyword_only_present_in_ocr(self):
        """「上次那张写着预算的截图」—— 只有搜 OCR 才找得到。"""
        self._in("mIMG", "[图片消息](mediaId=$x)", kind="image")
        msgstore.record_description(CONV, "mIMG", "Q3 预算表：市场 120 万", by="image",
                                    ok=True)
        _, out, _ = self._run("search", "预算", "--conv", CONV)
        self.assertIn("msg=mIMG", out)

    def test_no_hits_explains_what_was_searched(self):
        self._in("m1", "hi")
        _, out, _ = self._run("search", "找不到的词", "--conv", CONV)
        self.assertIn("图片识别文本都搜过了", out)


class TestJson(_Base):
    def test_json_is_structured_and_parsable(self):
        """算数题给结构化数据比给散文强 —— 触发本次改造的正是「这个图你统计下」。"""
        self._in("mIMG", "[图片消息](mediaId=$x)", kind="image")
        msgstore.record_description(CONV, "mIMG", "考勤表", by="image", ok=True)
        _, out, _ = self._run("recent", "--conv", CONV, "--json")
        data = json.loads(out)
        self.assertEqual(data[0]["msg_id"], "mIMG")
        self.assertEqual(data[0]["image_desc"]["text"], "考勤表")
        self.assertIn("time", data[0])          # 可读时间，不是裸 epoch


class TestImage(_Base):
    def test_cached_description_costs_nothing(self):
        self._in("mIMG", "[图片消息](mediaId=$x)", kind="image")
        msgstore.record_description(CONV, "mIMG", "考勤表全文", by="premedia", ok=True)
        with patch("custom.capabilities.image._download_image") as dl:
            rc, out, _ = self._run("image", "mIMG", "--conv", CONV)
        dl.assert_not_called()
        self.assertEqual(rc, convq.EXIT_OK)
        self.assertEqual(out.strip(), "考勤表全文")     # stdout 只有内容，可管道

    def test_recognizes_on_demand(self):
        """没识别过就现在识别 —— 这是"回看等不到"的正解。"""
        self._in("mIMG", "[图片消息](mediaId=$x)", kind="image")
        with patch("custom.capabilities.image._download_image",
                   return_value=("/tmp/f.png", "/tmp/d")), \
             patch("custom.capabilities.image._recognize", return_value="现场识别的"):
            rc, out, _ = self._run("image", "mIMG", "--conv", CONV, "--wait", "5")
        self.assertEqual((rc, out.strip()), (convq.EXIT_OK, "现场识别的"))

    def test_download_failure_gives_actionable_advice(self):
        """下载失败和识别失败对用户是不同的建议，合并成一条会让人无从下手。"""
        self._in("mIMG", "[图片消息](mediaId=$x)", kind="image")
        with patch("custom.capabilities.image._download_image",
                   return_value=(None, None)):
            rc, out, err = self._run("image", "mIMG", "--conv", CONV, "--wait", "5")
        self.assertEqual(rc, convq.EXIT_FAILED)
        self.assertEqual(out, "")               # 失败时 stdout 必须干净
        self.assertIn("重发", err)

    def test_non_image_message_is_rejected(self):
        self._in("m1", "就是一句话")
        rc, _, err = self._run("image", "m1", "--conv", CONV)
        self.assertEqual(rc, convq.EXIT_NOT_FOUND)
        self.assertIn("不是图片消息", err)

    def test_missing_message_is_not_found(self):
        rc, _, err = self._run("image", "m不存在", "--conv", CONV)
        self.assertEqual(rc, convq.EXIT_NOT_FOUND)
        self.assertIn("找不到", err)

    def test_wait_is_clamped_below_watchdog(self):
        """brain 的 idle watchdog 是 300s —— 单次 bash 调用绝不能逼近它。"""
        self._in("mIMG", "[图片消息](mediaId=$x)", kind="image")
        seen = {}

        def slow(*a, **k):
            seen["peer_wait"] = mediadesc._PEER_WAIT
            return None, None

        with patch("custom.capabilities.image._download_image", slow):
            self._run("image", "mIMG", "--conv", CONV, "--wait", "99999")
        self.assertLessEqual(seen["peer_wait"], convq._IMAGE_WAIT_MAX)

    def test_peer_wait_is_restored(self):
        """临时压低等锁时间不能泄漏到进程后续行为里。"""
        before = mediadesc._PEER_WAIT
        self._in("mIMG", "[图片消息](mediaId=$x)", kind="image")
        with patch("custom.capabilities.image._download_image",
                   return_value=(None, None)):
            self._run("image", "mIMG", "--conv", CONV, "--wait", "5")
        self.assertEqual(mediadesc._PEER_WAIT, before)


class TestCmdHint(_Base):
    def test_ids_are_single_quoted(self):
        """conv_id/msg_id 含 `+ / =`，不加引号大脑复制过去就是一条坏命令。"""
        h = convq.cmd_hint(CONV, "image", "msg+A/B==")
        self.assertIn(f"--conv '{CONV}'", h)
        self.assertIn("'msg+A/B=='", h)

    def test_path_is_absolute(self):
        """serve 的 cwd 不保证是仓库根，相对路径迟早失灵。"""
        self.assertTrue(os.path.isabs(convq.CLI_PATH))
        self.assertTrue(convq.CLI_PATH.endswith("bin/custom/convq.py"))


class TestRobustness(_Base):
    def test_store_explosion_is_reported_not_raised(self):
        """CLI 不该把 Python 栈吐给大脑 —— 它会把栈当成会话内容读。"""
        with patch.object(msgstore, "transcript", side_effect=RuntimeError("disk on fire")):
            rc, out, err = self._run("recent", "--conv", CONV)
        self.assertEqual(rc, convq.EXIT_FAILED)
        self.assertIn("convq 出错", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
