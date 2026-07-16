#!/bin/bash
# e2e_test.sh — 端到端测试模板
#
# 提炼自: dingtalk-opencode-agent/e2e_test.sh + 转发消息 e2e 测试实践
# 原作者: hugozhu
#
# 用真实链路验证：
#   1. 触发业务消息（用 dws chat message forward / send 等）
#   2. 实时盯日志关键事件（按顺序）
#   3. 用 dws list 验证通知渠道消息流
#   4. 用 opencode serve HTTP API 验证 session history 干净
#
# 5 个核心验证点（用户按业务调整）:
#   V1. event-watcher 检测到事件（monitor.log 有 "forward: 反查消息" 之类）
#   V2. cleanup 命中正确 session（aborted=True, deleted>=1）
#   V3. 无依赖服务的实质误导回复（"我解析了这条..." 之类）
#   V4. 通知渠道消息流正确（应有 N 条，不应有空回复 / 误导回复）
#   V5. opencode session history 干净（多余轮次被 DELETE）
#   V6. 通知渠道无 "本地 agent 无文本输出" 类空回复

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BIN_DIR="$SCRIPT_DIR/bin/core"

# 用户配置
BOT_CID="${BOT_CID:-cid+your-bot-conversation-id}"
PROFILE="${AGENT_PROFILE:-your-profile}"
SOURCE_MSG_ID="${SOURCE_MSG_ID:-msg-your-source-message-id}"
SOURCE_CONV_ID="${SOURCE_CONV_ID:-$BOT_CID}"

echo "=== 阶段 1: 准备 ==="
START_TIME=$(date '+%Y-%m-%d %H:%M:%S')
echo "测试开始时间: $START_TIME"

echo ""
echo "=== 阶段 2: 触发 ==="
echo "转发消息 $SOURCE_MSG_ID 从 $SOURCE_CONV_ID 到 $BOT_CID"
TRIGGER_RESULT=$(dws chat message forward \
    --src-conversation-id "$SOURCE_CONV_ID" \
    --msg-id "$SOURCE_MSG_ID" \
    --dest-conversation-id "$BOT_CID" \
    --profile "$PROFILE" -y 2>&1)
echo "$TRIGGER_RESULT"

echo ""
echo "等 90 秒看完整流程..."
sleep 90

echo ""
echo "=== 阶段 3: 验证 ==="

# V1. event-watcher 检测
echo "V1: event-watcher 检测"
if grep -E "forward: 反查消息|handle: msgId" "$SCRIPT_DIR/monitor.log" 2>/dev/null | tail -3; then
    echo "  ✅ 检测到"
else
    echo "  ❌ 未检测到"
fi

# V2. cleanup 命中
echo ""
echo "V2: cleanup 命中"
if grep -E "cleanup.*aborted=True deleted=[1-9]" "$SCRIPT_DIR/monitor.log" 2>/dev/null | tail -1; then
    echo "  ✅ cleanup 命中"
else
    echo "  ❌ cleanup 未命中或 deleted=0"
fi

# V3. 无实质误导回复
echo ""
echo "V3: 无实质误导回复"
if grep -E "我解析了这条|基于.*JSON.*回复" "$SCRIPT_DIR/agent-connect.log" 2>/dev/null | tail -1; then
    echo "  ❌ 仍有实质误导回复"
else
    echo "  ✅ 无实质误导回复"
fi

# V4 + V6. 通知渠道消息流
echo ""
echo "V4+V6: 通知渠道消息流"
dws chat message list \
    --group "$BOT_CID" \
    --time "$START_TIME" \
    --direction newer \
    --limit 15 \
    --profile "$PROFILE" -y 2>&1 | python3 -c "
import json, sys
d = json.load(sys.stdin)
msgs = d.get('result', {}).get('messages', [])
print(f'  total messages: {len(msgs)}')
empty_count = 0
for i, m in enumerate(msgs):
    s = m.get('sender', '?')
    c = m.get('content', '')[:80]
    is_empty = '本地 agent 无文本' in m.get('content', '')
    if is_empty:
        empty_count += 1
        print(f'  [{i}] ❌ 空回复: sender={s!r} content={c!r}')
    else:
        print(f'  [{i}] sender={s!r} content={c!r}')
print()
if empty_count > 0:
    print(f'  ❌ V6 失败：{empty_count} 条空回复')
else:
    print(f'  ✅ V6 通过：无空回复')
"

echo ""
echo "=== 阶段 4: 报告 ==="
echo "详细日志见 $SCRIPT_DIR/monitor.log + $SCRIPT_DIR/agent-connect.log"
echo "用户应人工核对 6 个验证点（V1-V6）"
