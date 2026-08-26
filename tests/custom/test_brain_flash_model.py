#!/usr/bin/env python3
"""test_brain_flash_model.py — 提示词触发的便宜模型逐轮切换（#117）

浏览器自动化那类任务多轮工具调用、token 量大但推理深度要求不高，跑贵模型性价比差。
消息里带「用flash模型」等触发词 → 本轮改用 AGENT_OPENCODE_MODEL_FLASH。

覆盖：
  1. 命中触发词 → POST body 的 model 是 flash，且触发词已从 prompt 摘掉。
  2. 未命中 → 默认模型，文本原样。
  3. 未配置 AGENT_OPENCODE_MODEL_FLASH（特性关闭）→ 默认模型，且**不摘**触发词。
  4. 大小写不敏感；「用flash」不会在「用flash模型」上留下孤零零的「模型」。
  5. 整句只有触发词（摘完为空）→ 不切模型，当普通消息处理。
  6. CLI 回退路径 --model 带的也是 flash（否则 serve 挂掉会静默切回贵模型）。
  7. 会话复用下命中触发词的那一轮**另起独立一次性 session**，不进主会话——provider 的
     prompt cache 按模型分桶，在长上下文的复用 session 里换模型反而更贵（见
     TestFlashRunsStandalone 的类注释）；主会话不被打断，之后照常复用。
  8. FLASH 误配成和默认模型同值 → 不分流（判据是「≠ 默认」而非「== FLASH」）。

不依赖网络：全程 patch brain._serve_request。
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

from custom import brain

brain._OPENCODE_LOG = os.path.join(tempfile.gettempdir(), "opencode_test.log")

_DEFAULT = "local/qwen3-8-max"
_FLASH = "local/deepseek-v4-flash"


class _FakeServe:
    """记录每次 POST /message 的 body，供断言取用的模型与 prompt。"""

    def __init__(self, reply="done"):
        self.reply = reply
        self.posts = []       # 每次 POST /message 的 body
        self.created = 0      # 建了几个 session
        self.titles = []      # 每次 POST /session 的 title
        self.post_sids = []   # 每次 POST message 落在哪个 sid
        self.deleted = []     # 被 DELETE 掉的 sid
        self._sid = 0

    def __call__(self, method, port, pwd, path, body=None, timeout=8):
        if method == "POST" and path == "/session":
            self._sid += 1
            self.created += 1
            self.titles.append((body or {}).get("title", ""))
            return {"id": f"ses_{self._sid}"}
        if method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[-1])
            return True
        if method == "GET" and path.endswith("/message"):
            return []
        if method == "POST" and path.endswith("/message"):
            self.posts.append(body)
            self.post_sids.append(path.split("/")[2])
            return {"info": {"tokens": {"input": 1, "output": 1}},
                    "parts": [{"type": "text", "text": self.reply}]}
        return None

    def model_of(self, i=0):
        m = self.posts[i]["model"]
        return f"{m['providerID']}/{m['modelID']}"

    def prompt_of(self, i=0):
        return self.posts[i]["parts"][0]["text"]

    def sid_of(self, i=0):
        return self.post_sids[i]


def _ctx(conv_id="cidT"):
    return {"conv_id": conv_id, "conv_type": "2", "msg_id": "m", "user": "u"}


def _cfg(**overrides):
    cfg = {"_BRAIN": "opencode", "_SESSION_REUSE": True,
           "_OPENCODE_ACTIVITY_POLL": 60, "_OPENCODE_IDLE_TIMEOUT": 300,
           "_OPENCODE_MAX_TIMEOUT": 0, "_OPENCODE_SOCK_TIMEOUT": None,
           "_OPENCODE_MODEL": _DEFAULT, "_OPENCODE_MODEL_FLASH": _FLASH,
           "_FLASH_KEYWORDS": ["use flash model", "用flash模型", "用flash", "/flash"]}
    cfg.update(overrides)
    return cfg


def _run(fake, text, cli=None, **overrides):
    cli = cli or MagicMock(return_value="CLI-SHOULD-NOT-RUN")
    with patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
         patch.object(brain, "_serve_request", side_effect=fake), \
         patch.object(brain, "_brain_opencode_cli", cli), \
         patch.multiple(brain, **_cfg(**overrides)):
        reply, status = brain.generate_reply_ex("u", text, ctx=_ctx())
    return reply, status, cli


class TestPickModel(unittest.TestCase):
    """_pick_model 纯函数语义。"""

    def _pick(self, text, **overrides):
        with patch.multiple(brain, **_cfg(**overrides)):
            return brain._pick_model(text)

    def test_hit_switches_and_strips(self):
        model, cleaned = self._pick("用flash模型 打开浏览器抓股价")
        self.assertEqual(model, _FLASH)
        self.assertEqual(cleaned, "打开浏览器抓股价")

    def test_miss_keeps_default(self):
        model, cleaned = self._pick("打开浏览器抓股价")
        self.assertEqual(model, _DEFAULT)
        self.assertEqual(cleaned, "打开浏览器抓股价")

    def test_disabled_keeps_text_intact(self):
        """未配置 flash 模型 = 特性关闭：不切模型，也不该偷偷改写用户原话。"""
        model, cleaned = self._pick("用flash模型 打开浏览器", _OPENCODE_MODEL_FLASH="")
        self.assertEqual(model, _DEFAULT)
        self.assertEqual(cleaned, "用flash模型 打开浏览器")

    def test_case_insensitive(self):
        model, cleaned = self._pick("用Flash模型 分析日志")
        self.assertEqual(model, _FLASH)
        self.assertEqual(cleaned, "分析日志")

    def test_longest_keyword_wins(self):
        """「用flash」是「用flash模型」的前缀；短的先匹配会留下孤零零的「模型」。"""
        model, cleaned = self._pick("用flash模型 抓数据")
        self.assertEqual(model, _FLASH)
        self.assertNotIn("模型", cleaned)
        self.assertEqual(cleaned, "抓数据")

    def test_keyword_only_not_switched(self):
        """整句只有触发词：摘完为空，发空 prompt 没意义 → 当普通消息。"""
        model, cleaned = self._pick("用flash模型")
        self.assertEqual(model, _DEFAULT)
        self.assertEqual(cleaned, "用flash模型")

    def test_mention_without_trigger_not_hijacked(self):
        """闲聊里提到 flash 模型不该被当成切换指令。"""
        model, cleaned = self._pick("帮我看下 flash模型 效果怎么样")
        self.assertEqual(model, _DEFAULT)
        self.assertEqual(cleaned, "帮我看下 flash模型 效果怎么样")

    def test_slash_command_trigger(self):
        model, cleaned = self._pick("/flash 打开浏览器抓股价")
        self.assertEqual(model, _FLASH)
        self.assertEqual(cleaned, "打开浏览器抓股价")

    def test_english_trigger(self):
        model, cleaned = self._pick("use flash model to scrape the page")
        self.assertEqual(model, _FLASH)
        self.assertEqual(cleaned, "to scrape the page")

    def test_english_trigger_case_insensitive(self):
        model, cleaned = self._pick("Use Flash Model 抓数据")
        self.assertEqual(model, _FLASH)
        self.assertEqual(cleaned, "抓数据")

    def test_word_boundary_blocks_longer_word(self):
        """/flashlight 不是 /flash——否则还会把 prompt 割成「light」。"""
        model, cleaned = self._pick("这个 /flashlight 工具怎么用")
        self.assertEqual(model, _DEFAULT)
        self.assertEqual(cleaned, "这个 /flashlight 工具怎么用")

    def test_word_boundary_blocks_glued_english(self):
        model, cleaned = self._pick("useflashmodels 是什么")
        self.assertEqual(model, _DEFAULT)
        self.assertEqual(cleaned, "useflashmodels 是什么")

    def test_skips_boundary_reject_finds_later_hit(self):
        """前面有 /flashlight 不该挡住后面真正的 /flash。"""
        model, cleaned = self._pick("先看下 /flashlight，再 /flash 抓数据")
        self.assertEqual(model, _FLASH)
        self.assertIn("/flashlight", cleaned)
        self.assertNotIn("/flash ", cleaned)

    def test_slash_command_only_not_switched(self):
        model, cleaned = self._pick("/flash")
        self.assertEqual(model, _DEFAULT)
        self.assertEqual(cleaned, "/flash")


class TestHttpPath(unittest.TestCase):
    """HTTP 主路径：POST body 里带的模型与 prompt。"""

    def setUp(self):
        brain._reset_sessions()

    def test_hit_posts_flash_model(self):
        fake = _FakeServe()
        reply, status, cli = _run(fake, "用flash模型 打开浏览器抓股价")
        self.assertEqual(reply, "done")
        self.assertEqual(fake.model_of(), _FLASH)
        self.assertNotIn("用flash模型", fake.prompt_of())
        self.assertIn("打开浏览器抓股价", fake.prompt_of())
        cli.assert_not_called()

    def test_miss_posts_default_model(self):
        fake = _FakeServe()
        _run(fake, "打开浏览器抓股价")
        self.assertEqual(fake.model_of(), _DEFAULT)

    def test_oneshot_mode_also_switches(self):
        """无状态模式（SESSION_REUSE=0）同样按提示词切模型，且仍是建→发→删。"""
        fake = _FakeServe()
        _run(fake, "用flash模型 抓数据", _SESSION_REUSE=False)
        self.assertEqual(fake.model_of(), _FLASH)
        self.assertEqual(fake.created, 1)
        self.assertEqual(fake.deleted, [fake.sid_of(0)], "一次性 session 用完必须删")


class TestFlashRunsStandalone(unittest.TestCase):
    """逐轮模型的那一轮走**独立一次性 session**，不进复用主会话（#117 跟进）。

    #117 原本假定「模型 per-message 传 → 换模型不必重建会话」。线上日志证否：
    provider 的 prompt cache 按模型分桶，在攒了长上下文的复用 session 里换模型 =
    整段历史对新模型全部 miss（实测一轮 input 从 303 涨到 57,557），比省下的还贵；
    而全新 session 只需重编 system+tools 前缀（provider 侧跨 session 命中）。
    另有正确性副作用：flash 轮继承主会话历史后会照抄上一轮答案。
    取舍：flash 轮看不到前文。
    """

    def setUp(self):
        brain._reset_sessions()

    def test_flash_turn_uses_its_own_session(self):
        fake = _FakeServe()
        _run(fake, "帮我记住暗号")            # 第一轮：默认模型 → 建主会话
        _run(fake, "用flash模型 抓股价")       # 第二轮：flash → 另起一次性 session
        self.assertEqual(fake.model_of(0), _DEFAULT)
        self.assertEqual(fake.model_of(1), _FLASH)
        self.assertEqual(fake.created, 2, "flash 轮必须另起 session")
        self.assertNotEqual(fake.sid_of(1), fake.sid_of(0))

    def test_flash_session_deleted_main_survives(self):
        fake = _FakeServe()
        _run(fake, "帮我记住暗号")
        _run(fake, "用flash模型 抓股价")
        main_sid, flash_sid = fake.sid_of(0), fake.sid_of(1)
        self.assertIn(flash_sid, fake.deleted, "一次性 session 用完要删")
        self.assertNotIn(main_sid, fake.deleted, "主会话不能被 flash 轮误删")
        self.assertEqual(brain._lookup_sid("cidT"), main_sid, "主会话记录应原封不动")

    def test_main_session_still_reused_after_flash(self):
        """flash 轮插在中间，之后的默认模型轮仍落回同一个主 session。"""
        fake = _FakeServe()
        _run(fake, "帮我记住暗号")
        _run(fake, "用flash模型 抓股价")
        _run(fake, "暗号是多少")
        self.assertEqual(fake.sid_of(2), fake.sid_of(0))
        self.assertEqual(fake.created, 2, "第三轮不该再建 session")

    def test_flash_equal_to_default_stays_in_session(self):
        """判据是「≠ 默认模型」而非「== FLASH」：配成同值时没换缓存桶，不该白丢上下文。"""
        fake = _FakeServe()
        _run(fake, "帮我记住暗号", _OPENCODE_MODEL_FLASH=_DEFAULT)
        _run(fake, "用flash模型 抓股价", _OPENCODE_MODEL_FLASH=_DEFAULT)
        self.assertEqual(fake.model_of(1), _DEFAULT)
        self.assertEqual(fake.created, 1, "同值不分流")
        self.assertEqual(fake.sid_of(1), fake.sid_of(0))

    def test_flash_session_title_marked(self):
        """独立 session 打 ⚡ 前缀，opencode 后台列表里能和主会话区分开。"""
        fake = _FakeServe()
        _run(fake, "用flash模型 抓股价")
        self.assertTrue(fake.titles[0].startswith("⚡ "), fake.titles)

    def test_normal_turn_title_unmarked(self):
        fake = _FakeServe()
        _run(fake, "帮我记住暗号")
        self.assertFalse(fake.titles[0].startswith("⚡"), fake.titles)

    def test_flash_tokens_counted_separately(self):
        """flash 轮的用量单列，不并进主计数器（否则窗口占用/缓存命中率会被算歪）。"""
        fake = _FakeServe()
        _run(fake, "帮我记住暗号")
        before = brain._get_session_stats("cidT")
        _run(fake, "用flash模型 抓股价")
        after = brain._get_session_stats("cidT")
        self.assertEqual(after["rounds"], before["rounds"], "主会话轮次不该被 flash 轮推进")
        self.assertEqual(after["input_tokens"], before["input_tokens"])
        self.assertEqual(after["flash_rounds"], 1)
        self.assertEqual(after["flash_input_tokens"], 1)
        self.assertEqual(after["flash_output_tokens"], 1)
        self.assertIn("独立模型轮", brain._format_session_summary(after))

    def test_summary_omits_flash_line_without_flash_rounds(self):
        fake = _FakeServe()
        _run(fake, "帮我记住暗号")
        self.assertNotIn("独立模型轮",
                         brain._format_session_summary(brain._get_session_stats("cidT")))


class TestCliFallback(unittest.TestCase):
    """serve 不可用时回退 CLI，--model 必须跟着切，否则静默变回贵模型。"""

    def setUp(self):
        brain._reset_sessions()

    def _run_cli(self, text):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(brain, "find_serve_credentials", return_value=(None, None, None)), \
             patch.object(brain.subprocess, "run", side_effect=fake_run), \
             patch.multiple(brain, **_cfg()):
            brain.generate_reply_ex("u", text, ctx=_ctx())
        return captured["cmd"]

    def test_cli_uses_flash_on_hit(self):
        cmd = self._run_cli("用flash模型 抓股价")
        self.assertEqual(cmd[cmd.index("--model") + 1], _FLASH)
        self.assertNotIn("用flash模型", cmd[2])

    def test_cli_uses_default_on_miss(self):
        cmd = self._run_cli("抓股价")
        self.assertEqual(cmd[cmd.index("--model") + 1], _DEFAULT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
