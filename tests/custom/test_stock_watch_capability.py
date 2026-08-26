#!/usr/bin/env python3
"""test_stock_watch_capability.py — stock_watch 行情异动监控能力单测（custom）

覆盖：
- is_stock_snapshot：真实快照命中 / 普通文本不命中 / 缺指数标记不命中
- parse_snapshot：名称/现价/涨跌幅（千分位指数、截断名称、指数无盘后段）
- _key 归一化：'Apple Inc' 与 'Apple Inc.' 同 key
- on_inbound：
    * 首次推送只播种状态、不告警（无对比基准）
    * 常规波动 → 静默消费（不 send_reply，但返回 True）
    * 单次推送波动 ≥5% → 告警
    * 24h 累计 ≥10%（单次 <5%）→ 告警
    * 非快照文本 → 返回 False 不消费
- format_alert：统一告警格式可读性
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom.capabilities import stock_watch as sw
from core.inbound import InboundMessage, KIND_TEXT


# 真实链路抓取的行情快照样本（树莓派群推送格式）
SNAPSHOT = ("# Alibaba Group Holdin ## 130.53 +1.63(+1.26%) **130.27 -0.26(-0.2%)** **** "
            "# PDD Holdings Inc. ## 89.52 -0.68(-0.75%) **89.58 0.06(0.07%)** **** "
            "# Apple Inc. ## 311.30 -5.53(-1.75%) **312.1 0.8(0.26%)** **** "
            "# 道琼斯工业平均指数(.DJI) ## 52759.21 -703.84(-1.32%)  **** "
            "# 标普500指数(.INX) ## 7641.16 -66.82(-0.87%)  **** "
            "# 纳斯达克综合指数(.IXIC) ## 26067.17 -263.92(-1.00%)  ****")


def _msg(text, msg_id="msgA=="):
    return InboundMessage(user="树莓派", text=text, conv_type="2",
                          conv_id="cidGRP==", msg_id=msg_id, kind=KIND_TEXT)


def _snap(pairs):
    """构造快照文本：pairs 的标的 + 恒定三大指数（保证命中检测且指数不产生异动）。"""
    parts = [f"# {n} ## {p} +0(+0%)" for n, p in pairs]
    parts += ["# 道琼斯工业平均指数(.DJI) ## 50000 -1(-0.01%)",
              "# 标普500指数(.INX) ## 7000 -1(-0.01%)",
              "# 纳斯达克综合指数(.IXIC) ## 26000 -1(-0.01%)"]
    return " **** ".join(parts)


class TestParse(unittest.TestCase):
    def test_is_snapshot_true(self):
        self.assertTrue(sw.is_stock_snapshot(SNAPSHOT))

    def test_is_snapshot_false_plain_text(self):
        self.assertFalse(sw.is_stock_snapshot("今天天气不错，# 随便聊聊"))

    def test_is_snapshot_false_no_index_marker(self):
        txt = "# A ## 1 +1(+1%) # B ## 2 +1(+1%) # C ## 3 +1(+1%)"
        self.assertFalse(sw.is_stock_snapshot(txt))

    def test_parse_prices(self):
        entries = sw.parse_snapshot(SNAPSHOT)
        self.assertEqual(len(entries), 6)
        prices = {e[0]: e[1] for e in entries}
        self.assertAlmostEqual(prices["Alibaba Group Holdin"], 130.53)
        self.assertAlmostEqual(prices["道琼斯工业平均指数(.DJI)"], 52759.21)
        self.assertAlmostEqual(prices["Apple Inc."], 311.30)

    def test_parse_reported_pct(self):
        entries = {e[0]: e[2] for e in sw.parse_snapshot(SNAPSHOT)}
        self.assertAlmostEqual(entries["Alibaba Group Holdin"], 1.26)
        self.assertAlmostEqual(entries["PDD Holdings Inc."], -0.75)

    def test_key_normalize_apple(self):
        self.assertEqual(sw._key("Apple Inc."), sw._key("Apple Inc"))


class TestOnInbound(unittest.TestCase):
    def setUp(self):
        # 固定阈值，避免外部环境变量干扰
        sw._SINGLE_PCT = 5.0
        sw._DAY_PCT = 10.0
        sw._DAY_WINDOW = 24 * 3600
        sw._MIN_REF_AGE = 20 * 3600
        sw._RETENTION = 8 * 86400
        # 独立临时状态文件
        fd, self.state_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.state_path)  # 从不存在的状态开始
        self._sf = patch.object(sw, "_state_file", new=lambda: self.state_path)
        self._sf.start()
        self._send = patch.object(sw, "send_reply")
        self.mock_send = self._send.start()
        self._time = patch.object(sw.time, "time")
        self.mock_time = self._time.start()

    def tearDown(self):
        self._time.stop()
        self._send.stop()
        self._sf.stop()
        if os.path.exists(self.state_path):
            os.unlink(self.state_path)

    def test_first_push_seeds_no_alert(self):
        self.mock_time.side_effect = [1_000_000.0]
        r = sw.on_inbound(_msg(SNAPSHOT, "m1"))
        self.assertTrue(r)  # 消费
        self.mock_send.assert_not_called()

    def test_non_snapshot_not_consumed(self):
        r = sw.on_inbound(_msg("普通聊天消息", "m1"))
        self.assertFalse(r)
        self.mock_send.assert_not_called()

    def test_normal_push_silent_consumed(self):
        t0 = 1_000_000.0
        self.mock_time.side_effect = [t0, t0 + 3600]
        sw.on_inbound(_msg(_snap([("TestCo", 100)]), "m1"))
        sw.on_inbound(_msg(_snap([("TestCo", 101)]), "m2"))  # +1% 正常
        self.mock_send.assert_not_called()

    def test_single_move_alerts(self):
        t0 = 1_000_000.0
        self.mock_time.side_effect = [t0, t0 + 3600]
        sw.on_inbound(_msg(_snap([("TestCo", 100)]), "m1"))
        sw.on_inbound(_msg(_snap([("TestCo", 94)]), "m2"))   # -6% ≥5%
        self.assertTrue(self.mock_send.called)
        conv_id, conv_type, text = self.mock_send.call_args[0][:3]
        self.assertEqual(conv_id, "cidGRP==")
        self.assertIn("行情异动", text)
        self.assertIn("TestCo", text)
        self.assertIn("-6.00%", text)

    def test_day_cumulative_alerts_single_small(self):
        t0 = 1_000_000.0
        h = 3600
        self.mock_time.side_effect = [t0, t0 + 12 * h, t0 + 24 * h]
        sw.on_inbound(_msg(_snap([("TestCo", 100)]), "m1"))
        sw.on_inbound(_msg(_snap([("TestCo", 104.9)]), "m2"))  # 单次 +4.9% <5，无 24h 基准
        self.mock_send.assert_not_called()
        sw.on_inbound(_msg(_snap([("TestCo", 110)]), "m3"))    # 单次 +4.86%<5，24h 累计 +10% → 告警
        self.assertTrue(self.mock_send.called)
        text = self.mock_send.call_args[0][2]
        self.assertIn("24h 累计", text)

    def test_label_alias(self):
        t0 = 1_000_000.0
        self.mock_time.side_effect = [t0, t0 + 3600]
        sw.on_inbound(_msg(_snap([("Alibaba Group Holdin", 100)]), "m1"))
        sw.on_inbound(_msg(_snap([("Alibaba Group Holdin", 93)]), "m2"))  # -7%
        text = self.mock_send.call_args[0][2]
        self.assertIn("阿里 BABA", text)


class TestFormatAlert(unittest.TestCase):
    def test_format(self):
        txt = sw.format_alert([{
            "label": "TestCo", "price": 94.0, "single": -6.0,
            "prev_price": 100.0, "day": None, "reported": None,
        }])
        self.assertIn("行情异动", txt)
        self.assertIn("-6.00%", txt)
        self.assertIn("94.00", txt)


if __name__ == "__main__":
    unittest.main()
