#!/usr/bin/env python3
"""test_identity.py — 主管身份判定（custom/identity.py）单测

env 在调用时读取（不在 import 期定型），故用例直接改 os.environ。
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom import identity

_KEYS = ("AGENT_SUPERVISOR_NAME", "AGENT_SUPERVISOR_ALIASES", "AGENT_SUPERVISOR_USER_ID")


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig = {k: os.environ.get(k) for k in _KEYS}
        for k in _KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestSupervisorNames(_Base):
    def test_empty_when_unset(self):
        self.assertEqual(identity.supervisor_names(), set())

    def test_name_only(self):
        os.environ["AGENT_SUPERVISOR_NAME"] = "boss"
        self.assertEqual(identity.supervisor_names(), {"boss"})

    def test_name_plus_aliases_merged(self):
        os.environ["AGENT_SUPERVISOR_NAME"] = "hugozhu"
        os.environ["AGENT_SUPERVISOR_ALIASES"] = "朱鸿,老板"
        self.assertEqual(identity.supervisor_names(), {"hugozhu", "朱鸿", "老板"})

    def test_whitespace_and_empties_stripped(self):
        os.environ["AGENT_SUPERVISOR_ALIASES"] = " a , ,b ,"
        self.assertEqual(identity.supervisor_names(), {"a", "b"})


class TestSupervisorId(_Base):
    def test_empty_when_unset(self):
        self.assertEqual(identity.supervisor_id(), "")

    def test_stripped(self):
        os.environ["AGENT_SUPERVISOR_USER_ID"] = "  024083  "
        self.assertEqual(identity.supervisor_id(), "024083")


class TestHasSupervisor(_Base):
    def test_false_when_nothing_set(self):
        self.assertFalse(identity.has_supervisor())

    def test_true_with_name_only(self):
        os.environ["AGENT_SUPERVISOR_NAME"] = "boss"
        self.assertTrue(identity.has_supervisor())

    def test_true_with_id_only(self):
        """只配了 userId（能发卡片但认不出入站）也算配了主管。"""
        os.environ["AGENT_SUPERVISOR_USER_ID"] = "024083"
        self.assertTrue(identity.has_supervisor())

    def test_true_with_alias_only(self):
        os.environ["AGENT_SUPERVISOR_ALIASES"] = "老板"
        self.assertTrue(identity.has_supervisor())


class TestIsSupervisor(_Base):
    def test_matches_name(self):
        os.environ["AGENT_SUPERVISOR_NAME"] = "hugozhu"
        self.assertTrue(identity.is_supervisor("hugozhu"))

    def test_matches_alias(self):
        os.environ["AGENT_SUPERVISOR_NAME"] = "hugozhu"
        os.environ["AGENT_SUPERVISOR_ALIASES"] = "朱鸿"
        self.assertTrue(identity.is_supervisor("朱鸿"))

    def test_rejects_other_user(self):
        os.environ["AGENT_SUPERVISOR_NAME"] = "hugozhu"
        self.assertFalse(identity.is_supervisor("张三"))

    def test_false_when_unconfigured(self):
        """未配主管时对谁都是 False —— 调用方必须自己用 has_supervisor() 兜底。"""
        self.assertFalse(identity.is_supervisor("hugozhu"))

    def test_empty_user_is_false(self):
        os.environ["AGENT_SUPERVISOR_NAME"] = "hugozhu"
        self.assertFalse(identity.is_supervisor(""))
        self.assertFalse(identity.is_supervisor(None))

    def test_exact_match_only(self):
        """子串不算 —— 避免 "hugozhu2" 被误认成主管。"""
        os.environ["AGENT_SUPERVISOR_NAME"] = "hugozhu"
        self.assertFalse(identity.is_supervisor("hugozhu2"))
        self.assertFalse(identity.is_supervisor("hugo"))


if __name__ == "__main__":
    unittest.main()
