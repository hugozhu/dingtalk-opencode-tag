#!/usr/bin/env python3
"""mediadesc — 图片描述的单飞识别 + 落盘缓存单测。

核心命题：**同一张图只识别一次**。at_mention 是每份投递的属性不是消息的属性，
群流那份没有标记，靠它分流必然导致一条被 @ 的图被识别两次（两次下载 + 两次视觉调用）。
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from custom import mediadesc, msgstore  # noqa: E402

CONV, MSG = "cid群==", "msgIMG=="
TEXT = "[图片消息](mediaId=$abc123)"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mediadesc-test-")
        os.environ["AGENT_MSGSTORE_DIR"] = self.tmp
        mediadesc._reset()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("AGENT_MSGSTORE_DIR", None)
        mediadesc._reset()


class TestSingleFlight(_Base):
    def test_concurrent_describe_downloads_once(self):
        """**本模块存在的理由**：两条路同时要同一张图的描述，只能真跑一次。"""
        calls = []
        gate = threading.Event()

        def slow_download(media_id, msg_id, conv_id):
            calls.append(msg_id)
            gate.wait(2)                      # 卡住，制造"两个请求撞在一起"
            return "/tmp/fake.png", "/tmp/d"

        with patch("custom.capabilities.image._download_image", slow_download), \
             patch("custom.capabilities.image._recognize", return_value="一张表格"):
            outs = []
            ts = [threading.Thread(target=lambda: outs.append(
                mediadesc.describe(CONV, MSG, TEXT, wait=5))) for _ in range(2)]
            [t.start() for t in ts]
            time.sleep(0.3)
            gate.set()
            [t.join() for t in ts]

        self.assertEqual(len(calls), 1, "同一张图被下载了多次")
        self.assertEqual([o[0] for o in outs], ["一张表格", "一张表格"])

    def test_second_call_uses_cache(self):
        """第二次直接读落盘的描述，完全不再下载。"""
        with patch("custom.capabilities.image._download_image",
                   return_value=("/tmp/f.png", "/tmp/d")), \
             patch("custom.capabilities.image._recognize", return_value="一张表格"):
            mediadesc.describe(CONV, MSG, TEXT, wait=5)
        with patch("custom.capabilities.image._download_image") as dl:
            desc, st = mediadesc.describe(CONV, MSG, TEXT, wait=5)
        dl.assert_not_called()
        self.assertEqual((desc, st), ("一张表格", "ok"))

    def test_cache_survives_process_restart(self):
        """描述落在 msgstore 里，重启（=清空在途表）后仍复用。"""
        with patch("custom.capabilities.image._download_image",
                   return_value=("/tmp/f.png", "/tmp/d")), \
             patch("custom.capabilities.image._recognize", return_value="一张表格"):
            mediadesc.describe(CONV, MSG, TEXT, wait=5)
        mediadesc._reset()                    # 模拟重启：内存全丢
        with patch("custom.capabilities.image._download_image") as dl:
            self.assertEqual(mediadesc.describe(CONV, MSG, TEXT, wait=5)[0], "一张表格")
        dl.assert_not_called()


class TestFailures(_Base):
    def test_download_failure_is_distinguished(self):
        """下载失败和识别失败要分得开 —— 给用户的建议不一样。"""
        with patch("custom.capabilities.image._download_image",
                   return_value=(None, None)):
            self.assertEqual(mediadesc.describe(CONV, MSG, TEXT, wait=5),
                             ("", "download"))

    def test_recognize_failure_is_distinguished(self):
        with patch("custom.capabilities.image._download_image",
                   return_value=("/tmp/f.png", "/tmp/d")), \
             patch("custom.capabilities.image._recognize", return_value=""):
            self.assertEqual(mediadesc.describe(CONV, MSG, TEXT, wait=5),
                             ("", "recognize"))

    def test_failure_not_retried_within_ttl(self):
        """坏 mediaId 不该被时间窗内每条追问反复重试（每次 30s 下载 + 视觉超时）。"""
        with patch("custom.capabilities.image._download_image",
                   return_value=(None, None)) as dl:
            mediadesc.describe(CONV, MSG, TEXT, wait=5)
            mediadesc._reset()
            mediadesc.describe(CONV, MSG, TEXT, wait=5)
        self.assertEqual(dl.call_count, 1, "失败后 TTL 内又重试了")

    def test_failure_retried_after_ttl(self):
        with patch("custom.capabilities.image._download_image",
                   return_value=(None, None)) as dl:
            mediadesc.describe(CONV, MSG, TEXT, wait=5)
            mediadesc._reset()
            with patch.object(mediadesc, "_FAIL_TTL", 0):
                mediadesc.describe(CONV, MSG, TEXT, wait=5)
        self.assertEqual(dl.call_count, 2)

    def test_recognize_exception_does_not_escape(self):
        def boom(*a, **k):
            raise RuntimeError("vision down")
        with patch("custom.capabilities.image._download_image",
                   return_value=("/tmp/f.png", "/tmp/d")), \
             patch("custom.capabilities.image._recognize", boom):
            self.assertEqual(mediadesc.describe(CONV, MSG, TEXT, wait=5)[1], "recognize")


class TestBasics(_Base):
    def test_non_media_is_skipped(self):
        self.assertEqual(mediadesc.describe(CONV, MSG, "普通文本", wait=1), ("", "skip"))
        self.assertEqual(mediadesc.describe(CONV, "", TEXT, wait=1), ("", "skip"))

    def test_media_id_extraction(self):
        self.assertEqual(mediadesc.media_id_of(TEXT), "$abc123")
        self.assertEqual(mediadesc.media_id_of("没有"), "")

    def test_no_wait_returns_pending(self):
        """预识别只发起、不等 —— 调用方不该被视觉调用堵住。"""
        with patch("custom.capabilities.image._download_image",
                   return_value=("/tmp/f.png", "/tmp/d")), \
             patch("custom.capabilities.image._recognize", return_value="x"):
            self.assertEqual(mediadesc.describe(CONV, MSG, TEXT, wait=None)[1], "pending")

    def test_wait_timeout_degrades_not_blocks(self):
        """等不到就降级返回 pending，**绝不无限等**（调用方在只有 4 个 worker 的 reply 池里）。"""
        def slow(*a, **k):
            time.sleep(3)
            return "/tmp/f.png", "/tmp/d"
        with patch("custom.capabilities.image._download_image", slow), \
             patch("custom.capabilities.image._recognize", return_value="x"):
            t0 = time.monotonic()
            desc, st = mediadesc.describe(CONV, MSG, TEXT, wait=0.3)
        self.assertEqual(st, "pending")
        self.assertLess(time.monotonic() - t0, 2)

    def test_description_recorded_with_metadata(self):
        with patch("custom.capabilities.image._download_image",
                   return_value=("/tmp/f.png", "/tmp/d")), \
             patch("custom.capabilities.image._recognize", return_value="一张表格"):
            mediadesc.describe(CONV, MSG, TEXT, wait=5, by="premedia")
        rec = msgstore.description_of(CONV, MSG)
        self.assertEqual(rec["by"], "premedia")     # 谁识别的（A 靠它跳过已回复过的图）
        self.assertTrue(rec["ok"])
        self.assertTrue(rec["ts"])                  # 有时间戳才能做 TTL 和排查
        self.assertEqual(rec["conv"], CONV)


if __name__ == "__main__":
    unittest.main()
