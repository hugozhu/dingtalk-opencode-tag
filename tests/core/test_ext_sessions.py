#!/usr/bin/env python3
"""test_ext_sessions.py — 跨进程 session 抑制登记（core/brain.py）单测

healthcheck 的大脑探针是**独立进程**，它建的 session 不在 event_watcher 的进程内
登记表里。不抑制的话，每次探针都会让 SSE 事件落到默认转发逻辑上，往钉钉推
「📥 收到新请求」+「✅ 会话完成」——探针本该是静默的运维动作。

用文件旁路解决；这里覆盖登记/查询/摘除/缓存失效/降级。
"""

import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from core import brain as core_brain


class TestExtSessions(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, ".ext-sessions")
        self._saved = core_brain._EXT_SESSION_FILE
        core_brain._EXT_SESSION_FILE = self.path
        # 缓存按 mtime 判断，测试间必须清掉，否则串味
        core_brain._ext_cache["mtime"] = -1.0
        core_brain._ext_cache["sids"] = frozenset()

    def tearDown(self):
        core_brain._EXT_SESSION_FILE = self._saved
        core_brain._ext_cache["mtime"] = -1.0
        core_brain._ext_cache["sids"] = frozenset()

    def test_unregistered_not_suppressed(self):
        self.assertFalse(core_brain.is_textreply_session("ses_probe"))

    def test_registered_is_suppressed(self):
        core_brain.register_ext_session("ses_probe", self.path)
        self.assertTrue(core_brain.is_textreply_session("ses_probe"))

    def test_other_sid_unaffected(self):
        core_brain.register_ext_session("ses_probe", self.path)
        self.assertFalse(core_brain.is_textreply_session("ses_other"))

    def test_unregister_stops_suppression_and_removes_file(self):
        core_brain.register_ext_session("ses_probe", self.path)
        core_brain.unregister_ext_session("ses_probe", self.path)
        self.assertFalse(core_brain.is_textreply_session("ses_probe"))
        # 最后一条摘掉后文件也删掉，不留残留
        self.assertFalse(os.path.exists(self.path))

    def test_unregister_one_keeps_others(self):
        core_brain.register_ext_session("a", self.path)
        core_brain.register_ext_session("b", self.path)
        core_brain.unregister_ext_session("a", self.path)
        self.assertTrue(core_brain.is_textreply_session("b"))
        self.assertFalse(core_brain.is_textreply_session("a"))

    def test_empty_sid_is_noop(self):
        core_brain.register_ext_session("", self.path)
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(core_brain.is_textreply_session(""))

    def test_missing_file_degrades_to_empty(self):
        core_brain._EXT_SESSION_FILE = os.path.join(self.dir, "does-not-exist")
        self.assertFalse(core_brain.is_textreply_session("ses_x"))

    def test_in_process_registry_still_works(self):
        # 文件旁路不能把原来的进程内抑制搞坏
        core_brain.register_session("ses_inproc", {"conv_id": "c1"})
        self.assertTrue(core_brain.is_textreply_session("ses_inproc"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
