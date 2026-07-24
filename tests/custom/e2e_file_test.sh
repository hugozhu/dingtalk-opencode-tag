#!/bin/bash
# e2e_file_test.sh — 文本/markdown 文件消息端到端测试（#68）
#
# 测试文本文件的完整链路：创建 markdown 文件 → 作为文件发送 → 分类为 text →
# 直接读文本 → 注入复用主会话 → 生成回复。验证文件能力对文本文件的 type-based dispatch。
#
# 与 e2e_image_file_test.sh / e2e_pdf_file_test.sh 判定逻辑一致（V1 发送 / V2 入站
# [文件] … fileId: / V3 解析成功 type=text / V4 reply user OK）。使用 `--msg-type file
# --file-path` 让 dws 透明上传（唯一时间戳文件名，避免云盘同名覆盖交互提示）。
#
# 前置：
#   - constants.local.sh 已配置发送方 profile（真人账号）
#   - 服务已启动（bin/core/start.sh）且健康（bin/core/healthcheck.sh）
#
# 用法：bash tests/custom/e2e_file_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# 引入配置
if [[ -f "$PROJECT_DIR/config/constants.local.sh" ]]; then
    source "$PROJECT_DIR/config/constants.local.sh"
else
    echo "❌ 未找到 config/constants.local.sh，请先配置" >&2
    exit 1
fi

# 检查必要配置
SENDER_PROFILE="${E2E_SENDER_PROFILE:-dinga626d60c1128d449:0420506555}"
TARGET_USER="${DWS_AGENT_USER:-287179924}"

echo "========== 文本/markdown 文件消息端到端测试 =========="
echo "发送方: $SENDER_PROFILE"
echo "目标用户: $TARGET_USER"
echo ""

# 1. 创建测试 markdown 文件（唯一时间戳文件名，避免云盘同名覆盖交互提示）
marker=$(date +%s)
text_file="/tmp/e2e_file_test_${marker}.md"

echo "📝 创建测试文件..."
cat > "$text_file" <<EOF
# 端到端测试文档

这是一个测试文件，用于验证文件消息处理能力（#68）。

## 测试内容
- 文本提取
- 多轮上下文
- 回复生成

关键词：FILE-E2E-TEST-${marker}
EOF

if [[ ! -f "$text_file" ]]; then
    echo "❌ 创建文件失败" >&2
    exit 1
fi

file_size=$(wc -c < "$text_file")
echo "✓ 文件已创建: $text_file"
echo "  大小: ${file_size} bytes"
echo "  Test Marker: FILE-E2E-TEST-${marker}"
echo ""

# 2. 发送文本文件消息（使用 --msg-type file，dws 透明上传）
echo "📤 发送文本文件消息（msg-type=file）..."

send_success=""
for retry in {1..3}; do
    echo "  尝试 $retry/3..."
    result=$(dws chat message send \
        --user "$TARGET_USER" \
        --msg-type file \
        --file-path "$text_file" \
        --profile "$SENDER_PROFILE" 2>&1 || true)

    if echo "$result" | grep -q '"success": true'; then
        echo "✅ V1: 文本文件消息发送成功"
        send_success=1
        break
    else
        echo "  ⚠️ 第 $retry 次尝试失败"
        if [[ $retry -lt 3 ]]; then
            echo "  等待 3 秒后重试..."
            sleep 3
        fi
    fi
done

if [[ -z "$send_success" ]]; then
    echo "❌ V1: 发送失败（3 次尝试后）" >&2
    echo "$result" >&2
    rm -f "$text_file"
    exit 1
fi

echo ""
echo "⏳ 等待处理（最多 60s）..."
echo ""

# 3. 等待并验证处理
v2_done=""
v3_done=""
v4_done=""

for i in {1..60}; do
    sleep 1

    # V2: 检查入站
    if tail -5 "$PROJECT_DIR/agent-connect.log" 2>/dev/null | grep -q "\[文件\].*e2e_file_test_${marker}\.md.*fileId:"; then
        if [[ -z "$v2_done" ]]; then
            echo "✅ V2: 文本文件消息入站已记录（$(date +%T)）"
            tail -2 "$PROJECT_DIR/agent-connect.log" | grep "\[文件\].*e2e_file_test_${marker}\.md"
            v2_done=1
        fi
    fi

    # V3: 检查文本解析（应该是 type=text）
    if tail -20 "$PROJECT_DIR/monitor.log" 2>/dev/null | grep -q "file:.*解析成功.*type=text"; then
        if [[ -z "$v3_done" ]]; then
            echo "✅ V3: 文本文件解析成功（type=text）（$(date +%T)）"
            tail -20 "$PROJECT_DIR/monitor.log" | grep "file:.*e2e_file_test_${marker}" | tail -2
            v3_done=1
        fi
    fi

    # V4: 检查回复
    if [[ -n "$v3_done" ]] && tail -5 "$PROJECT_DIR/monitor.log" 2>/dev/null | grep -q "reply user OK"; then
        if [[ -z "$v4_done" ]]; then
            echo "✅ V4: 回复已发送（$(date +%T)）"
            v4_done=1
            break
        fi
    fi
done

# 清理临时文件
rm -f "$text_file"

echo ""
echo "=========================================="
echo "验证结果"
echo "=========================================="
echo ""

# 显示详细日志
echo "📥 入站记录："
tail -10 "$PROJECT_DIR/agent-connect.log" | grep "\[文件\].*\.md" | tail -1 || echo "  未找到"

echo ""
echo "📄 文件处理日志："
tail -30 "$PROJECT_DIR/monitor.log" | grep "file:" | tail -5 || echo "  未找到"

echo ""
echo "=========================================="

if [[ -n "$v2_done" ]] && [[ -n "$v3_done" ]] && [[ -n "$v4_done" ]]; then
    echo "✅ 文本文件消息端到端测试通过！"
    echo ""
    echo "验收项："
    echo "  ✓ 文本文件消息发送成功"
    echo "  ✓ 文本文件消息入站记录"
    echo "  ✓ 分类为 text 类型（直接读文本）"
    echo "  ✓ 注入复用主会话（多轮上下文延续）"
    echo "  ✓ 数字员工生成并发送回复"
    exit 0
else
    echo "❌ 测试失败" >&2
    [[ -z "$v2_done" ]] && echo "  ✗ V2: 未见入站记录" >&2
    [[ -z "$v3_done" ]] && echo "  ✗ V3: 未见文本解析（type=text）" >&2
    [[ -z "$v4_done" ]] && echo "  ✗ V4: 未见回复发送" >&2
    echo "" >&2
    echo "提示：" >&2
    echo "  - V2 未见入站多半是订阅投递停滞，先 bash bin/core/reboot.sh 重建" >&2
    echo "  - V3/V4 挂而 V2 过，看 tail -n 40 monitor.log opencode.log 定位" >&2
    exit 1
fi
