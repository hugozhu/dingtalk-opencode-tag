#!/usr/bin/env python3
"""test_file_capability.py — 文档/文件消息处理能力单测（custom）(#68)

覆盖：fileId/文件名提取、类型分类、路由（防回环/去重）、下载→按类型解析→注入→回复的
编排（受控下载到 tmpdir、用完删）、各类型解析器（text/image/pdf/office/video）、
下载失败/解析失败/不支持类型的兜底、prompt 组装、独立 session 解析模式。
mock _run_cli + 文件系统 + vision 识别。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

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

    def test_classify_file_types(self):
        # 文本
        for fname in ("a.txt", "b.md", "c.csv", "d.json", "e.log", "f.py", "g.yaml"):
            self.assertEqual(F._classify_file(fname), "text", fname)
        # 图片
        for fname in ("x.png", "y.jpg", "z.jpeg", "w.gif"):
            self.assertEqual(F._classify_file(fname), "image", fname)
        # PDF
        self.assertEqual(F._classify_file("doc.pdf"), "pdf")
        # Office
        for fname in ("a.docx", "b.xlsx", "c.pptx"):
            self.assertEqual(F._classify_file(fname), "office", fname)
        # 视频
        for fname in ("v.mp4", "w.avi", "x.mov"):
            self.assertEqual(F._classify_file(fname), "video", fname)
        # 未知
        for fname in ("z.zip", "w.exe", "unknown"):
            self.assertEqual(F._classify_file(fname), "unknown", fname)


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
    def _fake_download(self, content, filename="testdoc.txt"):
        """返回一个 _download_file 替身：写内容到临时文件，返回其路径。"""
        d = tempfile.mkdtemp(prefix="agent_file_test_")
        p = os.path.join(d, filename)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def test_text_file_full_pipeline(self):
        """文本文件：下载 → 读取 → 注入复用主会话 → 回复。"""
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
        # 验证 ctx 传递（复用主会话）
        ctx = gen.call_args.kwargs.get("ctx", {})
        self.assertEqual(ctx.get("conv_id"), "cid==")
        self.assertEqual(ctx.get("msg_id"), "msgF==")
        snd.assert_called_once()
        self.assertEqual(snd.call_args[0][0], "cid==")  # 回来源群
        # 受控：用完删临时目录，不留文件
        self.assertFalse(os.path.exists(tmp_dir))

    def test_text_truncation_marked(self):
        big = "x" * (F._FILE_MAX_BYTES + 500)
        path = self._fake_download(big)
        with patch.object(F, "_download_file", return_value=path), \
             patch.object(F, "generate_reply", return_value="ok") as gen, \
             patch.object(F, "send_reply"):
            F.handle_file("u", _FILE_LINE_CONTENT, "m==", "c==", "2")
        self.assertIn("仅读取前", gen.call_args[0][1])

    def test_image_file_dispatch(self):
        """图片文件：下载 → vision 识别（独立 session）→ 注入复用主会话 → 回复。"""
        content = "[文件] photo.png fileId: IMG123 注意：如需下载"
        path = self._fake_download("fake image bytes", "photo.png")
        with patch.object(F, "_download_file", return_value=path), \
             patch.object(F, "_parse_image", return_value=("图中显示一只猫", True)) as parse, \
             patch.object(F, "generate_reply", return_value="可爱的猫咪！") as gen, \
             patch.object(F, "send_reply") as snd:
            F.handle_file("u", content, "m==", "c==", "2")
        parse.assert_called_once()
        gen.assert_called_once()
        prompt = gen.call_args[0][1]
        self.assertIn("图中显示一只猫", prompt)
        self.assertIn("photo.png", prompt)
        self.assertIn("图片识别内容", prompt)
        snd.assert_called_once()

    def test_pdf_file_dispatch(self):
        """PDF 文件：下载 → PDF 解析 → 注入复用主会话 → 回复。"""
        content = "[文件] report.pdf fileId: PDF456 注意：如需下载"
        path = self._fake_download("fake pdf", "report.pdf")
        with patch.object(F, "_download_file", return_value=path), \
             patch.object(F, "_parse_pdf", return_value=("第一章 引言\n本文档...", True)) as parse, \
             patch.object(F, "generate_reply", return_value="报告总结如下...") as gen, \
             patch.object(F, "send_reply") as snd:
            F.handle_file("u", content, "m==", "c==", "2")
        parse.assert_called_once()
        gen.assert_called_once()
        prompt = gen.call_args[0][1]
        self.assertIn("第一章 引言", prompt)
        self.assertIn("report.pdf", prompt)
        snd.assert_called_once()

    def test_office_file_dispatch(self):
        """Office 文件：下载 → Office 解析 → 注入复用主会话 → 回复。"""
        content = "[文件] slides.pptx fileId: PPTX789 注意：如需下载"
        path = self._fake_download("fake pptx", "slides.pptx")
        with patch.object(F, "_download_file", return_value=path), \
             patch.object(F, "_parse_office", return_value=("标题：项目进展\n内容...", True)) as parse, \
             patch.object(F, "generate_reply", return_value="PPT 内容已了解") as gen, \
             patch.object(F, "send_reply") as snd:
            F.handle_file("u", content, "m==", "c==", "2")
        parse.assert_called_once()
        gen.assert_called_once()
        prompt = gen.call_args[0][1]
        self.assertIn("项目进展", prompt)
        self.assertIn("slides.pptx", prompt)
        snd.assert_called_once()

    def test_video_file_dispatch(self):
        """视频文件：下载 → 抽帧识别 → 注入复用主会话 → 回复。"""
        content = "[文件] demo.mp4 fileId: VID999 注意：如需下载"
        path = self._fake_download("fake video", "demo.mp4")
        with patch.object(F, "_download_file", return_value=path), \
             patch.object(F, "_parse_video", return_value=("帧1：界面截图\n帧2：点击按钮", True)) as parse, \
             patch.object(F, "generate_reply", return_value="视频展示了操作流程") as gen, \
             patch.object(F, "send_reply") as snd:
            F.handle_file("u", content, "m==", "c==", "2")
        parse.assert_called_once()
        gen.assert_called_once()
        prompt = gen.call_args[0][1]
        self.assertIn("界面截图", prompt)
        self.assertIn("demo.mp4", prompt)
        snd.assert_called_once()

    def test_unknown_type_notifies(self):
        """未知类型：不下载，明确告知用户。"""
        content = "[文件] archive.zip fileId: ZIP111 注意：如需下载"
        with patch.object(F, "_download_file", return_value="/tmp/archive.zip"), \
             patch.object(F, "generate_reply") as gen, \
             patch.object(F, "send_reply") as snd:
            F.handle_file("u", content, "m==", "c==", "2")
        gen.assert_not_called()     # 不注入
        snd.assert_called_once()
        msg = snd.call_args[0][2]
        self.assertIn("不是我能处理的文件类型", msg)
        self.assertIn("archive.zip", msg)

    def test_parse_failure_notifies(self):
        """解析失败：明确告知用户。"""
        content = "[文件] broken.pdf fileId: PDF000 注意：如需下载"
        path = self._fake_download("fake", "broken.pdf")
        with patch.object(F, "_download_file", return_value=path), \
             patch.object(F, "_parse_pdf", return_value=("", False)), \
             patch.object(F, "generate_reply") as gen, \
             patch.object(F, "send_reply") as snd:
            F.handle_file("u", content, "m==", "c==", "2")
        gen.assert_not_called()
        snd.assert_called_once()
        self.assertIn("解析失败", snd.call_args[0][2])

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


class TestParsers(unittest.TestCase):
    """测试各类型解析器（mock 外部依赖）。"""

    def test_parse_text(self):
        d = tempfile.mkdtemp(prefix="test_")
        p = os.path.join(d, "test.txt")
        try:
            with open(p, "w") as f:
                f.write("Hello World")
            text, ok = F._parse_text(p)
            self.assertTrue(ok)
            self.assertIn("Hello World", text)
        finally:
            os.remove(p)
            os.rmdir(d)

    def test_parse_image_vision(self):
        """图片解析：mock vision 识别。"""
        d = tempfile.mkdtemp(prefix="test_")
        p = os.path.join(d, "test.png")
        try:
            with open(p, "wb") as f:
                f.write(b"fake image")
            with patch.object(F, "_recognize_via_serve", return_value="识别文字：测试"):
                text, ok = F._parse_image(p)
            self.assertTrue(ok)
            self.assertEqual(text, "识别文字：测试")
        finally:
            os.remove(p)
            os.rmdir(d)

    def test_split_model(self):
        """测试 provider/model 切分。"""
        self.assertEqual(F._split_model("opencode/mimo-v2.5-free"), ("opencode", "mimo-v2.5-free"))
        self.assertEqual(F._split_model("gemini-flash"), ("", "gemini-flash"))
        self.assertEqual(F._split_model(""), ("", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
