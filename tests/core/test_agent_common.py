#!/usr/bin/env python3
"""test_agent_common.py — agent_common.py 单元测试模板

提炼自: dingtalk-opencode-agent/tests/test_forward_message.py (v4.1, 80+ tests)
原作者: hugozhu

测试：
1. _clean_session_title（session title 归一：折行 + 限长）
2. 双池隔离限流（reply 池与 task 池互不阻塞，#82）

注：会话的建/删/复用不在 agent_common —— 那是 custom/brain.py 按 conv 维护的后端策略
（见 AGENT_SESSION_REUSE 与 tests/custom/test_digital_employee.py::TestSessionReuse）。

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


class TestCleanSessionTitle(unittest.TestCase):
    """Test _clean_session_title — session title 归一（单行 + 限长）。

    title 源自用户消息，会被渲染进 markdown 通知（/reboot 的在跑任务列表），
    所以换行和超长都得在这里收口。
    """

    def test_folds_newlines_and_collapses_whitespace(self):
        self.assertEqual(agent_common._clean_session_title("a\n\nb   c"), "a b c")

    def test_none_and_empty_yield_empty_string(self):
        self.assertEqual(agent_common._clean_session_title(None), "")
        self.assertEqual(agent_common._clean_session_title(""), "")

    def test_truncates_overlong_title(self):
        out = agent_common._clean_session_title("x" * 100)
        self.assertEqual(len(out), 61)          # 60 + 省略号
        self.assertTrue(out.endswith("…"))

    def test_short_title_unchanged(self):
        self.assertEqual(agent_common._clean_session_title("[群] 张三 · 看下报错"),
                         "[群] 张三 · 看下报错")


class TestHandlerPools(unittest.TestCase):
    """双池隔离限流（#82）：reply 池与 task 池互不阻塞。"""

    def test_reply_and_task_use_distinct_pools(self):
        self.assertIsNot(agent_common._reply_pool, agent_common._task_pool)

    def test_submit_handler_runs_on_task_pool(self):
        name = agent_common.submit_handler(
            lambda: __import__("threading").current_thread().name).result(timeout=5)
        self.assertTrue(name.startswith("task"), name)

    def test_submit_reply_runs_on_reply_pool(self):
        name = agent_common.submit_reply(
            lambda: __import__("threading").current_thread().name).result(timeout=5)
        self.assertTrue(name.startswith("reply"), name)

    def test_submit_passes_args_and_kwargs(self):
        f = agent_common.submit_handler(lambda a, b=0: a + b, 2, b=3)
        self.assertEqual(f.result(timeout=5), 5)

    def test_handler_exception_is_swallowed(self):
        # 内部异常被吞掉记日志，Future 正常完成（返回 None），不污染池
        def boom():
            raise ValueError("boom")
        f = agent_common.submit_handler(boom)
        self.assertIsNone(f.result(timeout=5))

    def test_task_pool_saturation_does_not_block_reply_pool(self):
        import threading
        release = threading.Event()
        started = []
        # 占满 task 池的所有 worker
        for _ in range(agent_common._TASK_MAX_WORKERS):
            agent_common.submit_handler(lambda: (started.append(1), release.wait(5)))
        # reply 池仍应能立即执行
        got = agent_common.submit_reply(lambda: "ok").result(timeout=5)
        self.assertEqual(got, "ok")
        release.set()


if __name__ == "__main__":
    unittest.main(verbosity=2)
