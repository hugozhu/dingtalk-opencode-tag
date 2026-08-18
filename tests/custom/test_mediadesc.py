#!/usr/bin/env python3
"""mediadesc — 图片描述的单飞识别 + 落盘缓存单测。

核心命题：**同一张图只识别一次**。at_mention 是每份投递的属性不是消息的属性，
群流那份没有标记，靠它分流必然导致一条被 @ 的图被识别两次（两次下载 + 两次视觉调用）。
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


class TestCrossProcessLock(_Base):
    """跨进程单飞：在途表是进程内的，而 convq CLI 是独立进程。"""

    def _hold(self, pid, ts=None):
        """伪造一把别的进程持有的锁。"""
        p = mediadesc._lock_path(CONV, MSG, self.tmp)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "ts": ts or int(time.time())}, f)
        if ts:
            os.utime(p, (ts, ts))
        return p

    def test_live_peer_is_not_disturbed(self):
        """对方（活着的进程）在识别 → 我一次都不该下载，等它落盘就行。"""
        self._hold(os.getpid())          # 当前进程一定活着
        def landed():
            time.sleep(0.3)
            msgstore.record_description(CONV, MSG, "对方识别的结果", by="image",
                                        ok=True, path=self.tmp)
        t = threading.Thread(target=landed); t.start()
        with patch("custom.capabilities.image._download_image") as dl, \
             patch.object(mediadesc, "_PEER_WAIT", 5):
            desc, st = mediadesc.describe_sync(CONV, MSG, TEXT, path=self.tmp)
        t.join()
        dl.assert_not_called()
        self.assertEqual((desc, st), ("对方识别的结果", "ok"))

    def test_dead_peer_lock_is_stolen(self):
        """重启后锁的持有者早没了 —— 干等 3 分钟 TTL 没有意义，看 pid 活没活。"""
        self._hold(pid=999999)           # 几乎不可能存在的 pid
        with patch("custom.capabilities.image._download_image",
                   return_value=("/tmp/f.png", "/tmp/d")) as dl, \
             patch("custom.capabilities.image._recognize", return_value="我自己识别的"):
            desc, st = mediadesc.describe_sync(CONV, MSG, TEXT, path=self.tmp)
        self.assertEqual(dl.call_count, 1)
        self.assertEqual((desc, st), ("我自己识别的", "ok"))

    def test_expired_lock_is_stolen(self):
        """持有者还活着但锁太老（进程卡死/被 SIGSTOP）→ TTL 到了就抢。"""
        self._hold(os.getpid(), ts=int(time.time()) - 10_000)
        with patch.object(mediadesc, "_LOCK_TTL", 60), \
             patch("custom.capabilities.image._download_image",
                   return_value=("/tmp/f.png", "/tmp/d")), \
             patch("custom.capabilities.image._recognize", return_value="抢过来了"):
            self.assertEqual(mediadesc.describe_sync(CONV, MSG, TEXT,
                                                     path=self.tmp)[0], "抢过来了")

    def test_record_lands_before_unlock(self):
        """**顺序即正确性**：先落盘后解锁。反过来对方会在空窗期看到"没锁也没结果"又跑一遍。"""
        seen = {}
        real_release = mediadesc._release

        def spy(conv, mid, store):
            seen["desc_at_unlock"] = msgstore.description_of(conv, mid, path=store)
            return real_release(conv, mid, store)

        with patch.object(mediadesc, "_release", spy), \
             patch("custom.capabilities.image._download_image",
                   return_value=("/tmp/f.png", "/tmp/d")), \
             patch("custom.capabilities.image._recognize", return_value="结果"):
            mediadesc.describe_sync(CONV, MSG, TEXT, path=self.tmp)
        self.assertIsNotNone(seen["desc_at_unlock"], "解锁时描述还没落盘")

    def test_lock_released_on_exception(self):
        """识别炸了也必须解锁，否则这张图 3 分钟内没人能再试。"""
        with patch("custom.capabilities.image._download_image",
                   side_effect=RuntimeError("boom")):
            mediadesc.describe_sync(CONV, MSG, TEXT, path=self.tmp)
        self.assertFalse(os.path.exists(mediadesc._lock_path(CONV, MSG, self.tmp)))

    def test_peer_failure_is_not_retried(self):
        """对方失败并落了 ok:false → 我在 _FAIL_TTL 内直接接受，不重试。

        这就是"每进程一次"升级成"全局一次"的地方。
        """
        msgstore.record_description(CONV, MSG, "", by="image", ok=False,
                                    err="download", path=self.tmp)
        with patch("custom.capabilities.image._download_image") as dl:
            desc, st = mediadesc.describe_sync(CONV, MSG, TEXT, path=self.tmp)
        dl.assert_not_called()
        self.assertEqual((desc, st), ("", "download"))

    def test_await_peer_times_out_to_pending(self):
        """对方一直不落盘 → 降级 pending，**绝不无限等**。"""
        self._hold(os.getpid())
        with patch.object(mediadesc, "_PEER_WAIT", 0.3), \
             patch("custom.capabilities.image._download_image") as dl:
            t0 = time.monotonic()
            desc, st = mediadesc.describe_sync(CONV, MSG, TEXT, path=self.tmp)
        dl.assert_not_called()
        self.assertEqual(st, "pending")
        self.assertLess(time.monotonic() - t0, 3)

    def test_locks_live_under_store_not_repo_root(self):
        """锁必须挂在 msgstore 根下：测试注入 path 时锁跟着走，不污染生产 knowledge/。"""
        p = mediadesc._lock_path(CONV, MSG, self.tmp)
        self.assertTrue(p.startswith(os.path.join(self.tmp, ".locks")))
        self.assertTrue(p.endswith(".lock"))

    def test_lock_dir_survives_prune(self):
        """prune 只删 YYYY-MM-DD.jsonl —— 别把锁目录当过期分片删了。"""
        self._hold(os.getpid())
        msgstore.prune(keep_days=1, path=self.tmp)
        self.assertTrue(os.path.exists(mediadesc._lock_path(CONV, MSG, self.tmp)))


class TestDescribeSync(_Base):
    def test_does_not_use_thread_pool(self):
        """一次性 CLI 进程用它 —— 走线程池会让进程退出时被 atexit join 卡住。"""
        with patch.object(mediadesc, "submit_handler",
                          side_effect=AssertionError("describe_sync 不该碰线程池")), \
             patch("custom.capabilities.image._download_image",
                   return_value=("/tmp/f.png", "/tmp/d")), \
             patch("custom.capabilities.image._recognize", return_value="同步跑的"):
            self.assertEqual(mediadesc.describe_sync(CONV, MSG, TEXT,
                                                     path=self.tmp)[0], "同步跑的")

    def test_cache_hit_costs_nothing(self):
        msgstore.record_description(CONV, MSG, "早就识别过了", by="premedia", ok=True,
                                    path=self.tmp)
        with patch("custom.capabilities.image._download_image") as dl:
            self.assertEqual(mediadesc.describe_sync(CONV, MSG, TEXT, path=self.tmp),
                             ("早就识别过了", "ok"))
        dl.assert_not_called()

    def test_non_media_is_skipped(self):
        self.assertEqual(mediadesc.describe_sync(CONV, MSG, "普通文本", path=self.tmp),
                         ("", "skip"))

    def test_default_by_is_agent(self):
        with patch("custom.capabilities.image._download_image",
                   return_value=("/tmp/f.png", "/tmp/d")), \
             patch("custom.capabilities.image._recognize", return_value="x"):
            mediadesc.describe_sync(CONV, MSG, TEXT, path=self.tmp)
        self.assertEqual(msgstore.description_of(CONV, MSG, path=self.tmp)["by"],
                         "agent")


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
