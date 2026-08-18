#!/usr/bin/env python3
"""quoted — 被引用消息进入上下文单测（#112）。

覆盖：优先查本地存储（零网络往返）/ 查不到回落 CLI / 取不到不抛 /
媒体消息不把 mediaId 当正文 / prompt 分块（下游能分清哪句是用户说的）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from core.inbound import InboundMessage  # noqa: E402
from custom import msgstore, quoted  # noqa: E402

CONV = "cid群=="


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quoted-test-")
        os.environ["AGENT_MSGSTORE_DIR"] = self.tmp
        # resolve() 现在**恒发起**识别（wait 只决定等不等），没这层默认桩，任何存了
        # 媒体消息的用例都会真的 shell 出去调 dws 下载。关心识别的用例自己再覆盖。
        from custom import mediadesc
        p = patch.object(mediadesc, "describe", return_value=("", "pending"))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("AGENT_MSGSTORE_DIR", None)

    def _store(self, msg_id, text, user="hugozhu"):
        msgstore.record(InboundMessage(user=user, text=text, conv_type="2",
                                       conv_id=CONV, msg_id=msg_id, kind="text"), "in")


class TestResolve(_Base):
    def test_prefers_local_store(self):
        """存储里有就别打 CLI —— 群消息本来就入库（#111）。"""
        self._store("mQ", "季度目标定了吗？")
        with patch.object(quoted, "_run_cli") as cli:
            q = quoted.resolve(CONV, "mQ")
        cli.assert_not_called()
        self.assertEqual(q["text"], "季度目标定了吗？")
        self.assertEqual(q["sender"], "hugozhu")
        self.assertFalse(q["media"])

    def test_falls_back_to_cli(self):
        """老消息不在库里 → 回落 list-by-ids。"""
        payload = json.dumps({"result": {"messages": [
            {"openMessageId": "mOld", "content": "很久以前说的话", "sender": "可菡"}]}})
        with patch.object(quoted, "_run_cli", lambda a, timeout=60: (0, payload)):
            q = quoted.resolve(CONV, "mOld")
        self.assertEqual(q["text"], "很久以前说的话")
        self.assertEqual(q["sender"], "可菡")

    def test_cli_failure_returns_none(self):
        """取不到就当没有引用，照常处理 —— 不该让整条消息处理不了。"""
        with patch.object(quoted, "_run_cli", lambda a, timeout=60: (1, "boom")):
            self.assertIsNone(quoted.resolve(CONV, "mX"))

    def test_bad_payload_returns_none(self):
        with patch.object(quoted, "_run_cli", lambda a, timeout=60: (0, "不是 JSON")):
            self.assertIsNone(quoted.resolve(CONV, "mX"))

    def test_no_msg_id(self):
        self.assertIsNone(quoted.resolve(CONV, ""))

    def test_media_message_flagged(self):
        """引用的是图片时不能把 mediaId 当正文喂给大脑。"""
        self._store("mImg", "[图片消息](mediaId=$iwEcAqNqcGcDAQTRAk4F0QUABrA4)")
        self.assertTrue(quoted.resolve(CONV, "mImg")["media"])
        for t in ("[文件](x)", "[语音消息]", "[视频](y)"):
            self._store("m" + t, t)
            self.assertTrue(quoted.resolve(CONV, "m" + t)["media"], t)


class TestQuotedMedia(_Base):
    """引用一张图 = **显式证据**，用户明确指了它 —— 这条路最该带上识别结果。"""

    def test_media_wait_triggers_recognition(self):
        from custom import mediadesc
        self._store("mImg", "[图片消息](mediaId=$abc)")
        with patch.object(mediadesc, "describe", return_value=("图中是一张考勤表", "ok")):
            q = quoted.resolve(CONV, "mImg", media_wait=5)
        self.assertEqual(q["desc"], "图中是一张考勤表")
        self.assertIn("考勤表", quoted.build_prompt("张三", "看一下", q))

    def test_without_wait_still_initiates_recognition(self):
        """**wait 只决定等不等，不决定发不发起。**

        写成 `if media and media_wait:` 会让 wait=0 的调用方（改造后的 context.py 就是）
        连识别都不触发 —— 描述永远不会出现，convq 那条路也只能从冷启动干起。
        """
        from custom import mediadesc
        self._store("mImg2", "[图片消息](mediaId=$abc)")
        with patch.object(mediadesc, "describe", return_value=("", "pending")) as d:
            q = quoted.resolve(CONV, "mImg2")
        d.assert_called_once()
        self.assertIsNone(d.call_args.kwargs["wait"])       # 只发起、不等
        self.assertEqual(q["desc"], "")

    def test_recognition_pending_points_at_convq(self):
        """识别没赶上 → 给取内容的命令，并禁止在拿到之前瞎猜。

        以前这里写的是「请说明你需要对方直接把它发给你」—— 有了 convq 之后那是**错误
        建议**：内容拿得到，只是还没识别完。
        """
        from custom import convq, mediadesc
        self._store("mImg3", "[图片消息](mediaId=$abc)")
        with patch.object(mediadesc, "describe", return_value=("", "pending")):
            q = quoted.resolve(CONV, "mImg3", media_wait=1)
        p = quoted.build_prompt("张三", "看一下", q)
        self.assertIn(convq.CLI_PATH, p)
        self.assertIn("'mImg3'", p)
        self.assertIn(f"--conv '{CONV}'", p)
        self.assertIn("不要猜测", p)
        self.assertNotIn("直接把它发给你", p)      # 旧的投降话术不该回来


class TestBuildPrompt(unittest.TestCase):
    def test_blocks_are_separated(self):
        """分块写 —— 混成一句话下游会把引用内容当成用户的诉求。"""
        p = quoted.build_prompt("张三", "看一下",
                                {"text": "季度目标定了吗？", "sender": "hugozhu",
                                 "media": False})
        self.assertIn("【被引用的消息】（来自 hugozhu）", p)
        self.assertIn("季度目标定了吗？", p)
        self.assertIn("【张三 说】", p)
        self.assertIn("看一下", p)
        self.assertLess(p.index("季度目标定了吗？"), p.index("看一下"))

    def test_media_prompt_gives_a_way_to_get_content(self):
        """没有描述时给的是**一扇门**，不是死胡同 —— 只说"看不到"是在邀请模型编内容。"""
        p = quoted.build_prompt("张三", "看一下",
                                {"text": "[图片消息](mediaId=x)", "sender": "hugozhu",
                                 "media": True, "msg_id": "mQ", "conv_id": "cid群=="})
        self.assertIn("convq.py image 'mQ'", p)
        self.assertNotIn("mediaId", p)      # mediaId 不该出现在 prompt 里
        self.assertIn("不要猜测", p)

    def test_empty_quoted_text(self):
        p = quoted.build_prompt("张三", "看一下",
                                {"text": "", "sender": "", "media": False})
        self.assertIn("（空消息）", p)
        self.assertIn("某人", p)


if __name__ == "__main__":
    unittest.main()
