#!/usr/bin/env python3
"""test_image_capability.py — 图片识别能力单测（custom）

覆盖：mediaId/caption 提取、路由（防回环/去重）、下载→识别→注入→回复的编排、
下载失败/识别失败的兜底、prompt 组装（raw + 无前缀 + 末句）。mock CLI + vision。
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom.capabilities import image
from core.inbound import InboundMessage, KIND_IMAGE, parse_line


class TestImageDetection(unittest.TestCase):
    def test_classify_image_marker(self):
        m = parse_line("[connect] 收到 @u: [图片消息](mediaId=$abc) (convType=2 convId=c msgId=m)")
        self.assertEqual(m.kind, KIND_IMAGE)

    def test_media_id_and_caption(self):
        m = image._RE_MEDIA_ID.search("[图片消息](mediaId=@lQ123)看标红处")
        self.assertEqual(m.group(1), "@lQ123")
        cap = image._RE_IMAGE_TAG.sub("", "[图片消息](mediaId=@lQ123)看标红处").strip()
        self.assertEqual(cap, "看标红处")


class TestImageRouting(unittest.TestCase):
    def _msg(self, user="hugozhu", mid="msgIMG==", text="[图片消息](mediaId=$x)"):
        return InboundMessage(user=user, text=text, conv_type="2",
                              conv_id="cid==", msg_id=mid, kind=KIND_IMAGE)

    def test_dispatched(self):
        calls = []
        with patch.object(image, "submit_handler", side_effect=lambda fn, *a: calls.append(a)):
            self.assertTrue(image.on_inbound(self._msg()))
        self.assertEqual(len(calls), 1)

    def test_declares_dedup_and_loop_guard(self):
        # 防回环 + 去重由 core 依 Capability 声明处理（见 tests/core/test_capabilities）
        self.assertTrue(image.CAPABILITY.loop_guard)
        self.assertTrue(image.CAPABILITY.dedup)


class TestHandleImage(unittest.TestCase):
    def test_full_pipeline_replies_to_group(self):
        with patch.object(image, "_download_image", return_value="/tmp/fake.png"), \
             patch.object(image, "_recognize", return_value="图中是等式 1 + 1 = 2"), \
             patch.object(image, "generate_reply", return_value="这张图是 1+1=2，正确。") as gen, \
             patch.object(image, "send_reply", return_value=True) as snd:
            image.handle_image("hugozhu", "[图片消息](mediaId=$x)这题对吗",
                               "msgIMG==", "cid==", "2")
        gen.assert_called_once()
        prompt = gen.call_args[0][1]
        self.assertIn("图中是等式 1 + 1 = 2", prompt)          # 识别内容进 prompt
        self.assertIn("这题对吗", prompt)                       # 用户 caption 进 prompt
        self.assertIn("图片识别内容", prompt)
        self.assertTrue(gen.call_args.kwargs.get("raw"))       # raw=True，无前缀
        self.assertFalse(prompt.startswith("hugozhu："))
        snd.assert_called_once()
        self.assertEqual(snd.call_args[0][0], "cid==")         # 回来源群
        self.assertEqual(snd.call_args[0][2], "这张图是 1+1=2，正确。")

    def test_download_failure_notifies(self):
        with patch.object(image, "_download_image", return_value=None), \
             patch.object(image, "generate_reply") as gen, \
             patch.object(image, "send_reply") as snd:
            image.handle_image("u", "[图片消息](mediaId=$x)", "m==", "c==", "2")
        gen.assert_not_called()               # 没下到图，不调大脑
        snd.assert_called_once()              # 但明确告知用户
        self.assertIn("下载", snd.call_args[0][2])

    def test_recognize_failure_notifies(self):
        with patch.object(image, "_download_image", return_value="/tmp/f.png"), \
             patch.object(image, "_recognize", return_value=""), \
             patch.object(image, "generate_reply") as gen, \
             patch.object(image, "send_reply") as snd:
            image.handle_image("u", "[图片消息](mediaId=$x)", "m==", "c==", "2")
        gen.assert_not_called()
        snd.assert_called_once()
        self.assertIn("识别失败", snd.call_args[0][2])

    def test_no_media_id_noop(self):
        with patch.object(image, "send_reply") as snd, \
             patch.object(image, "_download_image") as dl:
            image.handle_image("u", "[图片消息](没有mediaId)", "m==", "c==", "2")
        dl.assert_not_called()
        snd.assert_not_called()


class TestRecognize(unittest.TestCase):
    """_recognize 优先经 serve 识别，空则回退 _proxy_vision。"""

    def _tmp_png(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".png")
        os.write(fd, b"\x89PNG\r\n\x1a\nfakebytes")
        os.close(fd)
        return path

    def test_prefers_serve_over_proxy(self):
        path = self._tmp_png()
        with patch.object(image, "_recognize_via_serve", return_value="serve识别结果") as srv, \
             patch.object(image, "_proxy_vision") as proxy:
            desc = image._recognize(path)
        self.assertEqual(desc, "serve识别结果")
        srv.assert_called_once()
        proxy.assert_not_called()            # serve 成功 → 不回退
        self.assertFalse(os.path.exists(path))  # 用完删文件

    def test_falls_back_to_proxy_when_serve_empty(self):
        path = self._tmp_png()
        with patch.object(image, "_recognize_via_serve", return_value=""), \
             patch.object(image, "_proxy_vision", return_value="proxy识别结果") as proxy:
            desc = image._recognize(path)
        self.assertEqual(desc, "proxy识别结果")
        proxy.assert_called_once()

    def test_serve_disabled_when_no_model(self):
        with patch.object(image, "_VISION_MODEL", ""):
            self.assertEqual(image._recognize_via_serve(b"x"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
