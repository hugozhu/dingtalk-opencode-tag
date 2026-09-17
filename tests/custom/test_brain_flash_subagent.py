#!/usr/bin/env python3
"""test_brain_flash_subagent.py — skill 委派给 flash-worker 子代理的用量统计（#123）

`/flash` 逐轮换模型（#117）要求用户在消息里带触发词；skill 里「要用 flash」发生在模型
选定之后，改不了本轮模型。#123 的解法是方向反过来：主模型不动（留在复用 session，cache
不受影响），把机械重活用 Task 工具委派给 opencode.json 定义的 flash-worker 子代理——
子代理跑在独立 child session，模型取 AGENT_OPENCODE_MODEL_FLASH。

本文件测 brain 侧的统计闭环（委派本身是 opencode serve 内部行为，不在这儿测）：
  1. 默认模型轮结束后，扫本轮新建 child sessions，flash 模型 assistant 消息的用量
     累进该 conv 的 flash_* 计数；主计数器（rounds/input_tokens）只含主轮自身。
  2. 一个 child = 一次委派 = flash_rounds +1；其内多条 assistant 消息 tokens 累加。
  3. 上一轮的旧 child（time.created ≤ since_ts）不重复计。
  4. 主模型跑的 child（继承主模型）不计——只认 flash modelID。
  5. FLASH 未配置 / 与默认同值 → 整体跳过。
  6. children / message 查询失败 → 静默跳过，主回复不受影响。

不依赖网络：全程 patch brain._serve_request。
"""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom import brain

brain._OPENCODE_LOG = os.path.join(tempfile.gettempdir(), "opencode_test.log")

_DEFAULT = "local/qwen3-8-max"
_FLASH = "local/qwen3-8-flash"
_FLASH_ID = "qwen3-8-flash"


def _child(cid, created_ts):
    """构造 GET /session/{sid}/children 里的一条 child session 记录。"""
    return {"id": cid, "parentID": "ses_main",
            "time": {"created": int(created_ts * 1000)}}


def _assistant(model_id, inp=100, out=50):
    """构造 GET /session/{cid}/message 里的一条 assistant 消息（info 带 tokens）。"""
    return {"info": {"role": "assistant", "modelID": model_id,
                     "tokens": {"input": inp, "output": out}},
            "parts": [{"type": "text", "text": "ok"}]}


class _FakeServe:
    """记录 POST body；按预置返回 children / child messages。

    children_by_sid: 主 sid -> [child session, ...]
    msgs_by_child:   child sid -> [message, ...]
    children_error:  非空时 GET children 抛错（测 best-effort 跳过）
    """

    def __init__(self, children_by_sid=None, msgs_by_child=None, children_error="",
                 reply="done"):
        self.children_by_sid = children_by_sid or {}
        self.msgs_by_child = msgs_by_child or {}
        self.children_error = children_error
        self.reply = reply
        self.children_queries = []   # 查过 children 的主 sid
        self.child_msg_queries = []  # 查过 message 的 child sid

    def __call__(self, method, port, pwd, path, body=None, timeout=8):
        if method == "POST" and path == "/session":
            return {"id": "ses_main"}
        if method == "DELETE":
            return True
        if method == "GET" and path.endswith("/children"):
            sid = path.split("/")[2]
            self.children_queries.append(sid)
            if self.children_error:
                raise RuntimeError(self.children_error)
            return self.children_by_sid.get(sid, [])
        if method == "GET" and path.endswith("/message"):
            sid = path.split("/")[2]
            if sid in self.msgs_by_child:
                self.child_msg_queries.append(sid)
                return self.msgs_by_child[sid]
            return []   # watchdog / 主 session 探测
        if method == "POST" and path.endswith("/message"):
            return {"info": {"tokens": {"input": 303, "output": 7}},
                    "parts": [{"type": "text", "text": self.reply}]}
        return None


def _ctx(conv_id="cidSub"):
    return {"conv_id": conv_id, "conv_type": "2", "msg_id": "m", "user": "u"}


def _cfg(**overrides):
    cfg = {"_BRAIN": "opencode", "_SESSION_REUSE": True,
           "_OPENCODE_ACTIVITY_POLL": 60, "_OPENCODE_IDLE_TIMEOUT": 300,
           "_OPENCODE_MAX_TIMEOUT": 0, "_OPENCODE_SOCK_TIMEOUT": None,
           "_OPENCODE_MODEL": _DEFAULT, "_OPENCODE_MODEL_FLASH": _FLASH,
           "_FLASH_KEYWORDS": ["use flash model", "用flash模型", "用flash", "/flash"]}
    cfg.update(overrides)
    return cfg


def _run(fake, text="帮我排版", **overrides):
    with patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
         patch.object(brain, "_serve_request", side_effect=fake), \
         patch.object(brain, "_brain_opencode_cli", MagicMock(return_value="X")), \
         patch.multiple(brain, **_cfg(**overrides)):
        reply, status = brain.generate_reply_ex("u", text, ctx=_ctx())
    return reply, status


class TestSubagentFlashStats(unittest.TestCase):
    """reuse 主路径：委派出去的 flash 用量进 flash_*，主计数器不被污染。"""

    def setUp(self):
        brain._reset_sessions()

    def test_flash_child_counted_main_stats_intact(self):
        """本轮新建 child 里 flash 消息 → flash_* 记账；主轮 stats 只含主轮用量。"""
        fake = _FakeServe(
            children_by_sid={"ses_main": [_child("ch_1", time.time() + 60)]},
            msgs_by_child={"ch_1": [_assistant(_FLASH_ID, 2500, 800)]})
        _run(fake)
        stats = brain._get_session_stats("cidSub")
        self.assertEqual(stats["flash_rounds"], 1)
        self.assertEqual(stats["flash_input_tokens"], 2500)
        self.assertEqual(stats["flash_output_tokens"], 800)
        # 主计数器不受委派影响（验收点：cache_read / 主轮 token 不因委派劣化）
        self.assertEqual(stats["rounds"], 1)
        self.assertEqual(stats["input_tokens"], 303)
        self.assertEqual(stats["output_tokens"], 7)

    def test_multiple_messages_one_delegation(self):
        """一个 child 内多条 assistant 消息累加成 flash_rounds=1 的一轮。"""
        fake = _FakeServe(
            children_by_sid={"ses_main": [_child("ch_1", time.time() + 60)]},
            msgs_by_child={"ch_1": [_assistant(_FLASH_ID, 1000, 200),
                                    _assistant(_FLASH_ID, 500, 100)]})
        _run(fake)
        stats = brain._get_session_stats("cidSub")
        self.assertEqual(stats["flash_rounds"], 1)
        self.assertEqual(stats["flash_input_tokens"], 1500)
        self.assertEqual(stats["flash_output_tokens"], 300)

    def test_multiple_children_multiple_rounds(self):
        """委派两次（两个新 child）= flash_rounds=2。"""
        now = time.time() + 60
        fake = _FakeServe(
            children_by_sid={"ses_main": [_child("ch_1", now), _child("ch_2", now)]},
            msgs_by_child={"ch_1": [_assistant(_FLASH_ID, 10, 5)],
                           "ch_2": [_assistant(_FLASH_ID, 20, 6)]})
        _run(fake)
        stats = brain._get_session_stats("cidSub")
        self.assertEqual(stats["flash_rounds"], 2)
        self.assertEqual(stats["flash_input_tokens"], 30)
        self.assertEqual(stats["flash_output_tokens"], 11)

    def test_old_child_not_recounted(self):
        """复用 session 的 children 跨轮累积：上一轮的旧 child 不重复计。"""
        fake = _FakeServe(
            children_by_sid={"ses_main": [_child("ch_old", time.time() - 3600),
                                          _child("ch_new", time.time() + 60)]},
            msgs_by_child={"ch_old": [_assistant(_FLASH_ID, 999, 999)],
                           "ch_new": [_assistant(_FLASH_ID, 100, 40)]})
        _run(fake)
        stats = brain._get_session_stats("cidSub")
        self.assertEqual(stats["flash_rounds"], 1)
        self.assertEqual(stats["flash_input_tokens"], 100)
        self.assertNotIn("ch_old", fake.child_msg_queries)

    def test_main_model_child_ignored(self):
        """主模型自己跑的 child（@general 等，继承主模型）不进 flash 统计。"""
        fake = _FakeServe(
            children_by_sid={"ses_main": [_child("ch_1", time.time() + 60)]},
            msgs_by_child={"ch_1": [_assistant("qwen3-8-max", 500, 50)]})
        _run(fake)
        stats = brain._get_session_stats("cidSub")
        self.assertEqual(stats["flash_rounds"], 0)
        self.assertEqual(stats["flash_input_tokens"], 0)

    def test_no_children_no_stats(self):
        fake = _FakeServe()
        _run(fake)
        stats = brain._get_session_stats("cidSub")
        self.assertEqual(stats["flash_rounds"], 0)
        self.assertIn("ses_main", fake.children_queries)

    def test_flash_disabled_skips_query(self):
        """未配置 FLASH = 特性关闭：连 children 查询都不发。"""
        fake = _FakeServe(
            children_by_sid={"ses_main": [_child("ch_1", time.time() + 60)]},
            msgs_by_child={"ch_1": [_assistant(_FLASH_ID, 100, 40)]})
        _run(fake, _OPENCODE_MODEL_FLASH="")
        stats = brain._get_session_stats("cidSub")
        self.assertEqual(stats["flash_rounds"], 0)
        self.assertEqual(fake.children_queries, [])

    def test_flash_equals_default_skips(self):
        """FLASH 误配成和默认同值：没换缓存桶，委派不另记账（与 #117 判据一致）。"""
        fake = _FakeServe(
            children_by_sid={"ses_main": [_child("ch_1", time.time() + 60)]},
            msgs_by_child={"ch_1": [_assistant("qwen3-8-max", 100, 40)]})
        _run(fake, _OPENCODE_MODEL_FLASH=_DEFAULT)
        stats = brain._get_session_stats("cidSub")
        self.assertEqual(stats["flash_rounds"], 0)
        self.assertEqual(fake.children_queries, [])

    def test_children_endpoint_error_swallowed(self):
        """children 查询失败（serve 老版本无该端点 / 瞬断）→ 静默，主回复不受影响。"""
        fake = _FakeServe(children_error="404 no such route")
        reply, status = _run(fake)
        self.assertEqual(reply, "done")
        stats = brain._get_session_stats("cidSub")
        self.assertEqual(stats["flash_rounds"], 0)

    def test_summary_includes_subagent_rounds(self):
        """委派轮与逐轮换模型共用 flash_* 字段：统计摘要里同样能看到独立模型轮。"""
        fake = _FakeServe(
            children_by_sid={"ses_main": [_child("ch_1", time.time() + 60)]},
            msgs_by_child={"ch_1": [_assistant(_FLASH_ID, 2500, 800)]})
        _run(fake)
        summary = brain._format_session_summary(brain._get_session_stats("cidSub"))
        self.assertIn("独立模型轮", summary)


class TestCollectDirect(unittest.TestCase):
    """边界：畸形 child 数据 / 单 child 查询失败 / 无状态模式无 conv 记录。"""

    def setUp(self):
        brain._reset_sessions()

    def test_malformed_children_ignored(self):
        """children 里混入非 dict / 缺 id / 缺 time 的条目：跳过不崩，好的照记。"""
        fake = _FakeServe(
            children_by_sid={"ses_main": ["not-a-dict", {"id": ""},
                                          {"id": "ch_notime"},
                                          _child("ch_ok", time.time() + 60)]},
            msgs_by_child={"ch_ok": [_assistant(_FLASH_ID, 5, 2)]})
        _run(fake)
        stats = brain._get_session_stats("cidSub")
        self.assertEqual(stats["flash_rounds"], 1)
        self.assertEqual(stats["flash_input_tokens"], 5)

    def test_child_message_query_error_skipped(self):
        """某个 child 的 message 查询失败只跳过该 child，不影响其他 child 记账。"""
        now = time.time() + 60

        class _Partial(_FakeServe):
            def __call__(self, method, port, pwd, path, body=None, timeout=8):
                if method == "GET" and path == "/session/ch_bad/message":
                    raise RuntimeError("boom")
                return super().__call__(method, port, pwd, path, body, timeout)

        _run(_Partial(children_by_sid={"ses_main": [_child("ch_bad", now),
                                                    _child("ch_ok", now)]},
                      msgs_by_child={"ch_ok": [_assistant(_FLASH_ID, 70, 30)]}))
        stats = brain._get_session_stats("cidSub")
        self.assertEqual(stats["flash_rounds"], 1)
        self.assertEqual(stats["flash_input_tokens"], 70)

    def test_stateless_mode_noop(self):
        """无状态模式：无 conv 记录，查询照跑但记账 no-op（_update_flash_stats 语义）。"""
        fake = _FakeServe(
            children_by_sid={"ses_main": [_child("ch_1", time.time() + 60)]},
            msgs_by_child={"ch_1": [_assistant(_FLASH_ID, 100, 40)]})
        reply, status = _run(fake, _SESSION_REUSE=False)
        self.assertEqual(reply, "done")
        self.assertIsNone(brain._get_session_stats("cidSub"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
