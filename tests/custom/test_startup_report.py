#!/usr/bin/env python3
"""startup_report 主管解析单测：通讯录优先 → 配置兜底 → 都没有。

数字员工账号在钉钉通讯录里往往没挂汇报线（orgMasterUserId/orgMasterDisplayName 都是
null），旧逻辑直接判定"没有主管"，整份启动报告不发。这里锁住兜底行为。
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from custom.capabilities.startup_report import _resolve_supervisor


class TestResolveSupervisor(unittest.TestCase):
    def test_contact_wins(self):
        """通讯录有汇报线时以通讯录为准，配置兜底不生效。"""
        org = {"orgMasterUserId": "111", "orgMasterDisplayName": "张三"}
        with patch.dict(os.environ, {"AGENT_SUPERVISOR_USER_ID": "024083",
                                     "AGENT_SUPERVISOR_NAME": "hugozhu"}):
            self.assertEqual(_resolve_supervisor(org), ("111", "张三", "通讯录"))

    def test_falls_back_to_config_when_null(self):
        """通讯录两个字段都是 null（数字员工的实际情况）→ 用配置兜底。"""
        org = {"orgMasterUserId": None, "orgMasterDisplayName": None}
        with patch.dict(os.environ, {"AGENT_SUPERVISOR_USER_ID": "024083",
                                     "AGENT_SUPERVISOR_NAME": "hugozhu"}):
            self.assertEqual(_resolve_supervisor(org), ("024083", "hugozhu", "配置兜底"))

    def test_missing_keys_fall_back(self):
        """通讯录整个字段缺失（不是 null 而是没这个 key）也要兜底。"""
        with patch.dict(os.environ, {"AGENT_SUPERVISOR_USER_ID": "024083",
                                     "AGENT_SUPERVISOR_NAME": ""}):
            # 名字留空时用 userId 顶上，不能因此判成没主管
            self.assertEqual(_resolve_supervisor({}), ("024083", "024083", "配置兜底"))

    def test_no_supervisor_anywhere(self):
        """通讯录和配置都没有 → 保持原行为（不发报告）。"""
        org = {"orgMasterUserId": None, "orgMasterDisplayName": None}
        with patch.dict(os.environ, {"AGENT_SUPERVISOR_USER_ID": "",
                                     "AGENT_SUPERVISOR_NAME": ""}):
            self.assertEqual(_resolve_supervisor(org), (None, None, None))

    def test_contact_id_without_name(self):
        """通讯录有 id 没名字：名字用 id 顶上，仍算通讯录来源。"""
        org = {"orgMasterUserId": "111", "orgMasterDisplayName": None}
        with patch.dict(os.environ, {"AGENT_SUPERVISOR_USER_ID": "024083"}):
            self.assertEqual(_resolve_supervisor(org), ("111", "111", "通讯录"))


if __name__ == "__main__":
    unittest.main()
