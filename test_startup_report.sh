#!/bin/bash
# test_startup_report.sh - 测试启动报告功能

set -e

# 加载配置
source config/constants.local.sh

echo "=========================================="
echo "测试启动报告功能"
echo "=========================================="
echo ""

# 执行启动报告
python3 -c "
import sys
sys.path.insert(0, 'src')
from custom.capabilities.startup_report import send_startup_report
send_startup_report()
"

echo ""
echo "=========================================="
echo "测试完成"
echo "=========================================="
