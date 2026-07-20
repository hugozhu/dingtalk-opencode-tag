#!/usr/bin/env python3
"""test_file_capability.py — 文档/文件消息处理能力单测（custom）

覆盖：fileId/文件名提取、文本类型判定、路由（防回环/去重）、下载→读取→注入→回复的
编排（受控下载到 tmpdir、用完删）、下载失败/读取失败/二进制文件的兜底、prompt 组装。
mock _run_cli + 文件系统。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom.capabilities import file as F
from core.inbound import InboundMessage, KIND_FILE, parse_line

_FILE_LINE_CONTENT = "[文件] testdoc.txt fileId: QOG9lyr 注意：如需下载使用dws drive download命令下载"


class TestDetection(unittest.TestCase):
    def test_classify_file(self):
        m = parse_line(f"[connect] 收到 @u: {_FILE_LINE_CONTENT} (convType=2 convId=c msgId=m)")
        self.assertEqual(m.kind, KIND_FILE)

    def test_extract_fileid_and_name(self):
        self.assertEqual(F._RE_FILE_ID.search(_FILE_LINE_CONTENT).group(1), "QOG9lyr")
        self.assertEqual(F._RE_FILE_NAME.search(_FILE_LINE_CONTENT).group(1), "testdoc.txt")

    def test_is_text_file(self):
        for good in ("a.txt", "b.md", "c.csv", "d.json", "e.log", "f.py", "g.yaml"):
            self.assertTrue(F._is_text_file(good), good)
        for bad in ("x.pdf", "y.docx", "z.png", "w.zip", "v.mp4"):
            self.assertFalse(F._is_text_file(bad), bad)


class TestRouting(unittest.TestCase):
    def _msg(self, user="hugozhu", mid="msgF==", text=_FILE_LINE_CONTENT):
        return InboundMessage(user=user, text=text, conv_type="2", conv_id="cid==",
                              msg_id=mid, kind=KIND_FILE)

    def test_dispatched(self):
        calls = []
        with patch.object(F, "submit_handler", side_effect=lambda fn, *a: calls.append(a)):
            self.assertTrue(F.on_inbound(self._msg()))
        self.assertEqual(len(calls), 1)

    def test_declares_dedup_and_loop_guard(self):
        # 防回环 + 去重由 core 依 Capability 声明处理（见 tests/core/test_capabilities）
        self.assertTrue(F.CAPABILITY.loop_guard)
        self.assertTrue(F.CAPABILITY.dedup)


class TestHandleFile(unittest.TestCase):
    def _fake_download(self, content):
        """返回一个 _download_image 替身：写内容到临时文件，返回其路径。"""
        d = tempfile.mkdtemp(prefix="agent_file_test_")
        p = os.path.join(d, "testdoc.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def test_full_pipeline_reads_content_and_replies(self):
        path = self._fake_download("密钥 SECRET-CODE: MANGO-8842\n磁盘 > 20%")
        tmp_dir = os.path.dirname(path)
        with patch.object(F, "_download_file", return_value=path), \
             patch.object(F, "generate_reply", return_value="文件是部署清单，含密钥。") as gen, \
             patch.object(F, "send_reply", return_value=True) as snd:
            F.handle_file("hugozhu", _FILE_LINE_CONTENT, "msgF==", "cid==", "2")
        gen.assert_called_once()
        prompt = gen.call_args[0][1]
        self.assertIn("MANGO-8842", prompt)          # 真实文件内容进 prompt
        self.assertIn("testdoc.txt", prompt)          # 文件名进 prompt
        self.assertTrue(gen.call_args.kwargs.get("raw"))
        snd.assert_called_once()
        self.assertEqual(snd.call_args[0][0], "cid==")  # 回来源群
        # 受控：用完删临时目录，不留文件
        self.assertFalse(os.path.exists(tmp_dir))

    def test_truncation_marked(self):
        big = "x" * (F._FILE_MAX_BYTES + 500)
        path = self._fake_download(big)
        with patch.object(F, "_download_file", return_value=path), \
             patch.object(F, "generate_reply", return_value="ok") as gen, \
             patch.object(F, "send_reply"):
            F.handle_file("u", _FILE_LINE_CONTENT, "m==", "c==", "2")
        self.assertIn("已截断", gen.call_args[0][1])

    def test_binary_file_notifies_not_read(self):
        content = "[文件] report.pdf fileId: XYZ 注意：如需下载"
        with patch.object(F, "_download_file") as dl, \
             patch.object(F, "generate_reply") as gen, \
             patch.object(F, "send_reply") as snd:
            F.handle_file("u", content, "m==", "c==", "2")
        dl.assert_not_called()      # 二进制不下载
        gen.assert_not_called()     # 不注入
        snd.assert_called_once()
        self.assertIn("不是文本", snd.call_args[0][2])

    def test_download_failure_notifies(self):
        with patch.object(F, "_download_file", return_value=None), \
             patch.object(F, "generate_reply") as gen, \
             patch.object(F, "send_reply") as snd:
            F.handle_file("u", _FILE_LINE_CONTENT, "m==", "c==", "2")
        gen.assert_not_called()
        snd.assert_called_once()
        self.assertIn("下载", snd.call_args[0][2])

    def test_no_fileid_noop(self):
        with patch.object(F, "_download_file") as dl, patch.object(F, "send_reply") as snd:
            F.handle_file("u", "[文件] noid.txt 注意：如需下载", "m==", "c==", "2")
        dl.assert_not_called()
        snd.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
