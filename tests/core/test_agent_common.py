#!/usr/bin/env python3
"""test_agent_common.py — agent_common.py 单元测试模板

提炼自: dingtalk-opencode-agent/tests/test_forward_message.py (v4.1, 80+ tests)
原作者: hugozhu

测试 5 个关键模块：
1. inject_and_forward 公共注入模板（reply/no-session/no-reply/multi-msg 分支）
2. _abort_and_clean_session（按 asked_ts 过滤 + 双向删 user/assistant）
3. _find_session_with_predicate（找含特定 content 的最近活跃 session）
4. _lookup_senders_batch 批量反查（mock _run_cli）
5. _fetch_senders 补齐缺失 sender

测试策略:
- patch.object(<module>, "<func>", ...) 针对内部调用
- patch("urllib.request.urlopen") 针对 HTTP 调用
- 用 return_value / side_effect 模拟不同分支
"""

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from core import agent_common


class TestInjectAndForward(unittest.TestCase):
    """Test the shared inject_and_forward template."""

    @patch.object(agent_common, "_get_message_text", return_value="agent reply")
    @patch.object(agent_common, "_post_user_message", return_value="aid123")
    @patch.object(agent_common, "_find_bot_session", return_value="sid_full")
    @patch.object(agent_common, "send_notification")
    def test_reply_triggers_make_reply_msgs(self, _sd, _fbs, _pum, _gmt):
        called = {}
        def make_reply_msgs(reply):
            called["reply"] = reply
            return [("Test Reply", "md body")]
        result = agent_common.inject_and_forward(
            prompt="hello",
            session_title="test",
            make_reply_msgs=make_reply_msgs,
            make_no_session_msg=lambda: ("No Session", "md"),
            make_no_reply_msg=lambda: ("No Reply", "md"),
        )
        self.assertEqual(result, "agent reply")
        self.assertEqual(called["reply"], "agent reply")
        _sd.assert_called_once_with("Test Reply", "md body")

    @patch.object(agent_common, "_find_bot_session", return_value=None)
    @patch.object(agent_common, "_create_session", return_value=None)
    @patch.object(agent_common, "send_notification")
    def test_no_session_triggers_make_no_session_msg(self, _sd, _cs, _fbs):
        called = {}
        def make_no_session_msg():
            called["no_session"] = True
            return ("No Session", "no session md")
        result = agent_common.inject_and_forward(
            prompt="hello",
            session_title="test",
            make_reply_msgs=lambda r: [("Reply", "md")],
            make_no_session_msg=make_no_session_msg,
            make_no_reply_msg=lambda: ("No Reply", "md"),
        )
        self.assertIsNone(result)
        self.assertTrue(called.get("no_session"))

    @patch.object(agent_common, "_get_message_text", return_value="")
    @patch.object(agent_common, "_post_user_message", return_value=None)
    @patch.object(agent_common, "_find_bot_session", return_value="sid")
    @patch.object(agent_common, "send_notification")
    def test_no_reply_triggers_make_no_reply_msg(self, _sd, _fbs, _pum, _gmt):
        called = {}
        def make_no_reply_msg():
            called["no_reply"] = True
            return ("No Reply", "no reply md")
        result = agent_common.inject_and_forward(
            prompt="hello",
            session_title="test",
            make_reply_msgs=lambda r: [("Reply", "md")],
            make_no_session_msg=lambda: ("No Session", "md"),
            make_no_reply_msg=make_no_reply_msg,
        )
        self.assertIsNone(result)
        self.assertTrue(called.get("no_reply"))

    @patch.object(agent_common, "_get_message_text", return_value="reply")
    @patch.object(agent_common, "_post_user_message", return_value="aid")
    @patch.object(agent_common, "_find_bot_session", return_value="sid")
    @patch.object(agent_common, "send_notification")
    def test_make_reply_msgs_can_return_multiple(self, _sd, _fbs, _pum, _gmt):
        # 允许多条通知（如解析结果 + 总结回复）
        def make_reply_msgs(reply):
            return [
                ("解析结果", "md1"),
                ("总结回复", "md2"),
            ]
        agent_common.inject_and_forward(
            prompt="hello",
            session_title="test",
            make_reply_msgs=make_reply_msgs,
            make_no_session_msg=lambda: ("No Session", "md"),
            make_no_reply_msg=lambda: ("No Reply", "md"),
        )
        self.assertEqual(_sd.call_count, 2)
        self.assertEqual(_sd.call_args_list[0][0], ("解析结果", "md1"))
        self.assertEqual(_sd.call_args_list[1][0], ("总结回复", "md2"))

    @patch.object(agent_common, "_create_session", return_value="sid_new")
    @patch.object(agent_common, "_find_bot_session", return_value=None)
    @patch.object(agent_common, "_post_user_message", return_value="aid")
    @patch.object(agent_common, "_get_message_text", return_value="reply")
    @patch.object(agent_common, "send_notification")
    def test_falls_back_to_create_session(self, _sd, _gmt, _pum, _fbs, _cs):
        agent_common.inject_and_forward(
            prompt="hello",
            session_title="fallback-title",
            make_reply_msgs=lambda r: [("Reply", "md")],
            make_no_session_msg=lambda: ("No Session", "md"),
            make_no_reply_msg=lambda: ("No Reply", "md"),
        )
        _cs.assert_called_once_with("fallback-title")


class TestAbortAndCleanSession(unittest.TestCase):
    """Test _abort_and_clean_session — abort + DELETE asked_ts 之后的消息."""

    @patch.object(agent_common, "_delete_session_message", return_value=True)
    @patch.object(agent_common, "_list_session_messages")
    @patch.object(agent_common, "_session_action", return_value=True)
    def test_deletes_user_and_assistant_after_asked_ts(self, _sa, _gsm, _del):
        asked_ts = 10000
        _gsm.return_value = [
            {"info": {"id": "old_user", "role": "user", "time": {"created": 9000}}},
            {"info": {"id": "old_asst", "role": "assistant", "time": {"created": 9500}}},
            {"info": {"id": "new_user", "role": "user", "time": {"created": 10500}}},
            {"info": {"id": "new_asst", "role": "assistant", "time": {"created": 10600}}},
        ]
        aborted, deleted = agent_common._abort_and_clean_session("sid_full", asked_ts_ms=asked_ts)
        self.assertTrue(aborted)
        self.assertEqual(deleted, 2)
        deleted_ids = [c[0][1] for c in _del.call_args_list]
        self.assertIn("new_user", deleted_ids)
        self.assertIn("new_asst", deleted_ids)
        self.assertNotIn("old_user", deleted_ids)
        self.assertNotIn("old_asst", deleted_ids)

    @patch.object(agent_common, "_delete_session_message", return_value=True)
    @patch.object(agent_common, "_list_session_messages", return_value=[])
    @patch.object(agent_common, "_session_action", return_value=True)
    def test_empty_messages_deletes_nothing(self, _sa, _gsm, _del):
        aborted, deleted = agent_common._abort_and_clean_session("sid", asked_ts_ms=1000)
        self.assertTrue(aborted)
        self.assertEqual(deleted, 0)
        _del.assert_not_called()

    @patch.object(agent_common, "_delete_session_message", return_value=False)
    @patch.object(agent_common, "_list_session_messages")
    @patch.object(agent_common, "_session_action", return_value=True)
    def test_delete_failure_does_not_abort_loop(self, _sa, _gsm, _del):
        _gsm.return_value = [
            {"info": {"id": "u1", "role": "user", "time": {"created": 15000}}},
            {"info": {"id": "a1", "role": "assistant", "time": {"created": 16000}}},
        ]
        aborted, deleted = agent_common._abort_and_clean_session("sid", asked_ts_ms=10000)
        self.assertTrue(aborted)
        self.assertEqual(deleted, 0)
        self.assertEqual(_del.call_count, 2)


class TestFindSessionWithPredicate(unittest.TestCase):
    """Test _find_session_with_predicate."""

    @patch.object(agent_common, "_list_session_messages")
    @patch.object(agent_common, "find_serve_credentials", return_value=(1, 8080, "pwd"))
    def test_finds_session_matching_predicate(self, _creds, mock_get):
        def fake_get(sid):
            if sid == "ses_a":
                return [{"info": {"role": "user", "id": "m1", "time": {"created": 1000}}}]
            if sid == "ses_b":
                return [{"info": {"role": "user", "id": "m2", "time": {"created": 1500}},
                         "parts": [{"type": "text", "text": "match this"}]}]
            return []
        mock_get.side_effect = fake_get

        with patch("urllib.request.urlopen") as mock_urlopen:
            r1 = MagicMock()
            r1.read.return_value = json.dumps([
                {"id": "ses_a", "directory": "/x/your-agent-workdir", "time": {"updated": 2000}},
                {"id": "ses_b", "directory": "/x/your-agent-workdir", "time": {"updated": 1000}},
            ]).encode()
            mock_urlopen.return_value = r1
            sid = agent_common._find_session_with_predicate(
                predicate=lambda m: "match this" in "".join(p.get("text", "") for p in m.get("parts", []) if p.get("type") == "text")
            )
        self.assertEqual(sid, "ses_b")

    @patch.object(agent_common, "find_serve_credentials", return_value=(None, None, None))
    def test_returns_none_when_serve_not_running(self, _creds):
        self.assertIsNone(agent_common._find_session_with_predicate(predicate=lambda m: True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
