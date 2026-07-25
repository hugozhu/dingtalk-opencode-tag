#!/usr/bin/env python3
"""test_handler.py — custom handler.py 纯逻辑单测

覆盖两块此前无测试、最易随外部日志/消息格式变化而崩的逻辑：
  1. render_prompt —— 零 I/O 纯函数（含不改调用方 senders 的纯函数性质）
  2. match_business_line —— 跨行状态机 + 线程安全去重

FDE 改 handler.py 后，这些测试是回归基线。不依赖网络。
"""

import os
import sys
import threading
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom import handler


class TestRenderPrompt(unittest.TestCase):
    def _body(self, n):
        return {"messages": [{"createTime": f"t{i}", "content": f"c{i}"} for i in range(n)]}

    def test_returns_none_on_empty(self):
        self.assertIsNone(handler.render_prompt({"messages": []}, [], [], "bob"))

    def test_pads_missing_senders(self):
        body = self._body(2)
        atts = [{"time": "t0", "text": "c0"}, {"time": "t1", "text": "c1"}]
        out = handler.render_prompt(body, [], atts, "bob")
        self.assertIn("未知发送人", out)
        self.assertIn("共 2 条", out)

    def test_does_not_mutate_caller_senders(self):
        body = self._body(3)
        atts = [{"time": f"t{i}", "text": f"c{i}"} for i in range(3)]
        senders = ["alice"]
        handler.render_prompt(body, senders, atts, "bob")
        self.assertEqual(senders, ["alice"])  # 纯函数：不改入参

    def test_uses_attachment_text_over_raw(self):
        body = self._body(1)
        atts = [{"time": "t0", "text": "[图片，识别内容]"}]
        out = handler.render_prompt(body, ["alice"], atts, "bob")
        self.assertIn("[图片，识别内容]", out)
        self.assertIn("alice", out)


class TestMatchBusinessLine(unittest.TestCase):
    def setUp(self):
        handler.reset_dedup_state()

    def test_single_line_match(self):
        line = 'stuff msgtype="business-special" more msgId=msgABC end'
        self.assertEqual(handler.match_business_line(line), ("msgABC", []))

    def test_dedup_same_msgid(self):
        line = 'msgtype="business-special" msgId=msgABC'
        self.assertIsNotNone(handler.match_business_line(line))
        self.assertIsNone(handler.match_business_line(line))  # 第二次去重

    def test_non_business_line_ignored(self):
        self.assertIsNone(handler.match_business_line("just a normal log line"))

    def test_cross_line_match(self):
        # 行1 有 msgtype 无 msgId → 暂存；行2 有 msgId → 命中
        self.assertIsNone(handler.match_business_line('msgtype="business-special" no id here'))
        self.assertEqual(handler.match_business_line("next line msgId=msgXYZ"), ("msgXYZ", []))

    def test_thread_safe_dedup(self):
        # 并发喂同一 msgId，只应命中一次
        line = 'msgtype="business-special" msgId=msgRACE'
        hits = []
        lock = threading.Lock()

        def worker():
            r = handler.match_business_line(line)
            if r:
                with lock:
                    hits.append(r)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(hits), 1)


class TestBoundedSeen(unittest.TestCase):
    def test_fifo_eviction(self):
        s = handler._BoundedSeen(3)
        for k in ["a", "b", "c", "d"]:
            s.add(k)
        self.assertNotIn("a", s)  # 最旧被淘汰
        self.assertIn("d", s)


class TestRecoverFileIds(unittest.TestCase):
    """合并转发剥离 fileId 的回源恢复（#图片不能下载 bug）。"""

    def _fm(self, mid, content, conv="cidSRC==", ct="2026-07-25 14:57:28"):
        return {"openMessageId": mid, "content": content,
                "openConversationId": conv, "createTime": ct}

    def test_time_minus_60s(self):
        self.assertEqual(handler._time_minus_60s("2026-07-25 14:57:28"),
                         "2026-07-25 14:56:28")
        self.assertEqual(handler._time_minus_60s("bad"), "bad")  # 解析失败原样返回

    def test_recover_strips_recovered_content(self):
        # 内层文件消息 content 被剥离 fileId，回源 list --group 拿到带 fileId 的原文
        fms = [self._fm("msgA==", "[文件] math_1plus1.png")]
        list_resp = (0, '{"result": {"messages": [{"openMessageId": "msgA==", '
                      '"content": "[文件] math_1plus1.png fileId: REAL_A 注意：如需下载使用dws drive download命令下载"}], '
                      '"hasMore": false}}')
        with patch.object(handler, "_run_cli", return_value=list_resp) as cli:
            result = handler._recover_file_ids(fms)
        self.assertEqual(result.get("msgA=="),
                         "[文件] math_1plus1.png fileId: REAL_A 注意：如需下载使用dws drive download命令下载")
        args = cli.call_args[0][0]
        self.assertIn("--group", args)
        self.assertIn("cidSRC==", args)
        self.assertIn("2026-07-25 14:56:28", args)  # createTime 往前 buffer 60s

    def test_fetch_attachments_uses_recovered_fileid(self):
        # 端到端：fetch_attachments 对缺 fileId 的文本文件消息回源恢复后下载正文
        msgs = [self._fm("msgA==", "[文件] notes.txt")]
        list_resp = (0, '{"result": {"messages": [{"openMessageId": "msgA==", '
                      '"content": "[文件] notes.txt fileId: REAL_A"}], "hasMore": false}}')
        with patch.object(handler, "_run_cli", return_value=list_resp), \
             patch.object(handler, "_download_file_text", return_value="hello") as dl:
            out = handler.fetch_attachments(msgs)
        self.assertEqual(out[0]["type"], "file")
        self.assertIn("hello", out[0]["text"])
        self.assertNotIn("未获取到 fileId", out[0]["text"])
        dl.assert_called_once_with("REAL_A")  # 用恢复出的 fileId 下载

    def test_image_file_routes_to_vision(self):
        # 图片类文件（png）走 vision 识别，而非当文本读成乱码
        content = "[文件] math_1plus1.png fileId: REAL_IMG"
        with patch.object(handler, "_download_file_to_path", return_value="/tmp/x.png") as dl, \
             patch("custom.capabilities.image._recognize", return_value="1+1=2") as rec:
            entry = handler._fetch_file_entry(content)
        dl.assert_called_once_with("REAL_IMG")
        self.assertIn("1+1=2", entry)
        self.assertIn("[图片识别内容]", entry)
        self.assertTrue(rec.called)

    def test_no_recovery_when_fileid_present(self):
        # content 已带 fileId → 不触发回源反查
        msgs = [self._fm("msgB==", "[文件] a.txt fileId: EXISTING")]
        with patch.object(handler, "_run_cli") as cli, \
             patch.object(handler, "_download_file_text", return_value="hi"):
            handler.fetch_attachments(msgs)
        cli.assert_not_called()

    def test_recovery_failure_falls_back(self):
        # 回源失败 → 仍走原 content（报"未获取到 fileId"），不崩
        msgs = [self._fm("msgC==", "[文件] c.png")]
        with patch.object(handler, "_run_cli", return_value=(1, "")), \
             patch.object(handler, "_download_file_text") as dl:
            out = handler.fetch_attachments(msgs)
        self.assertIn("未获取到 fileId", out[0]["text"])
        dl.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
