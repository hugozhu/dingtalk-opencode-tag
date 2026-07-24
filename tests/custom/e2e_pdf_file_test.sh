#!/bin/bash
# e2e_pdf_file_test.sh — PDF 文件消息端到端测试（#68）
#
# 测试 PDF 文件的完整链路：创建 PDF → 作为文件发送 → 分类为 pdf → pdfplumber 提取 →
# 注入复用主会话 → 生成回复。验证文件能力对 PDF 文件的 type-based dispatch。
#
# 前置：
#   - constants.local.sh 已配置发送方 profile（真人账号）
#   - 服务已启动（bin/core/start.sh）且健康（bin/core/healthcheck.sh）
#   - pdfplumber 已安装（pip install pdfplumber）
#
# 用法：bash tests/custom/e2e_pdf_file_test.sh

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

echo "========== PDF 文件消息端到端测试 =========="
echo "发送方: $SENDER_PROFILE"
echo "目标用户: $TARGET_USER"
echo ""

# 1. 创建测试 PDF（带可提取文本）
marker=$(date +%s)
pdf_file="/tmp/e2e_pdf_test_${marker}.pdf"

echo "📄 创建测试 PDF..."

# 使用 Python + reportlab 或备用方法
python3 << EOF 2>/dev/null || true
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter

    c = canvas.Canvas("$pdf_file", pagesize=letter)
    c.setFont("Helvetica", 14)
    c.drawString(100, 750, "E2E PDF Test Document")
    c.setFont("Helvetica", 12)
    c.drawString(100, 720, "Test Marker: PDF-FILE-E2E-${marker}")
    c.drawString(100, 690, "")
    c.drawString(100, 660, "This is a test PDF for Issue #68 file capability.")
    c.drawString(100, 640, "Expected: PDF text extraction with pdfplumber.")
    c.drawString(100, 620, "")
    c.drawString(100, 600, "Validation Keywords:")
    c.drawString(100, 580, "  - type=pdf classification")
    c.drawString(100, 560, "  - pdfplumber text extraction")
    c.drawString(100, 540, "  - context injection to main session")
    c.save()
    print("✓ PDF created with reportlab")
except:
    pass
EOF

# 备用：创建简单的 PDF（无 reportlab 时）
if [[ ! -f "$pdf_file" ]]; then
    cat > "$pdf_file" << 'PDFEOF'
%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /Resources 4 0 R /MediaBox [0 0 612 792] /Contents 5 0 R >>
endobj
4 0 obj
<< /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>
endobj
5 0 obj
<< /Length 150 >>
stream
BT
/F1 14 Tf
100 750 Td
(E2E PDF Test Document) Tj
0 -30 Td
/F1 12 Tf
(Test Marker: PDF-FILE-E2E-MARKER) Tj
0 -20 Td
(This PDF tests pdfplumber extraction.) Tj
0 -20 Td
(Issue #68 file capability validation.) Tj
ET
endstream
endobj
xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000214 00000 n
0000000304 00000 n
trailer
<< /Size 6 /Root 1 0 R >>
startxref
504
%%EOF
PDFEOF
    echo "✓ PDF 已创建（备用方法）"
fi

if [[ ! -f "$pdf_file" ]]; then
    echo "❌ 创建 PDF 失败" >&2
    exit 1
fi

file_size=$(wc -c < "$pdf_file")
echo "✓ PDF 文件已创建: $pdf_file"
echo "  大小: ${file_size} bytes"
echo "  Test Marker: PDF-FILE-E2E-${marker}"
echo ""

# 2. 发送 PDF 文件消息（使用 --msg-type file）
echo "📤 发送 PDF 文件消息（msg-type=file）..."

send_success=""
for retry in {1..3}; do
    echo "  尝试 $retry/3..."
    result=$(dws chat message send \
        --user "$TARGET_USER" \
        --msg-type file \
        --file-path "$pdf_file" \
        --profile "$SENDER_PROFILE" 2>&1 || true)

    if echo "$result" | grep -q '"success": true'; then
        echo "✅ V1: PDF 文件消息发送成功"
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
    rm -f "$pdf_file"
    exit 1
fi

echo ""
echo "⏳ 等待处理（最多 60s，PDF 文本提取需要时间）..."
echo ""

# 3. 等待并验证处理
v2_done=""
v3_done=""
v4_done=""

for i in {1..60}; do
    sleep 1

    # V2: 检查入站
    if tail -5 "$PROJECT_DIR/agent-connect.log" 2>/dev/null | grep -q "\[文件\].*e2e_pdf_test_${marker}\.pdf.*fileId:"; then
        if [[ -z "$v2_done" ]]; then
            echo "✅ V2: PDF 文件消息入站已记录（$(date +%T)）"
            tail -2 "$PROJECT_DIR/agent-connect.log" | grep "\[文件\].*e2e_pdf_test_${marker}\.pdf"
            v2_done=1
        fi
    fi

    # V3: 检查 PDF 解析（应该是 type=pdf）
    if tail -20 "$PROJECT_DIR/monitor.log" 2>/dev/null | grep -q "file:.*解析成功.*type=pdf"; then
        if [[ -z "$v3_done" ]]; then
            echo "✅ V3: PDF 文件解析成功（type=pdf）（$(date +%T)）"
            tail -20 "$PROJECT_DIR/monitor.log" | grep "file:.*e2e_pdf_test_${marker}" | tail -2
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
rm -f "$pdf_file"

echo ""
echo "=========================================="
echo "验证结果"
echo "=========================================="
echo ""

# 显示详细日志
echo "📥 入站记录："
tail -10 "$PROJECT_DIR/agent-connect.log" | grep "\[文件\].*\.pdf" | tail -1 || echo "  未找到"

echo ""
echo "📄 文件处理日志："
tail -30 "$PROJECT_DIR/monitor.log" | grep "file:" | tail -5 || echo "  未找到"

echo ""
echo "=========================================="

if [[ -n "$v2_done" ]] && [[ -n "$v3_done" ]] && [[ -n "$v4_done" ]]; then
    echo "✅ PDF 文件消息端到端测试通过！"
    echo ""
    echo "验收项："
    echo "  ✓ PDF 文件消息发送成功"
    echo "  ✓ PDF 文件消息入站记录"
    echo "  ✓ 分类为 pdf 类型"
    echo "  ✓ 文本提取成功（pdfplumber）"
    echo "  ✓ 数字员工生成并发送回复"
    echo ""
    echo "注：PDF 文本提取依赖 pdfplumber 库（pip install pdfplumber）"
    exit 0
else
    echo "❌ 测试失败" >&2
    [[ -z "$v2_done" ]] && echo "  ✗ V2: 未见入站记录" >&2
    [[ -z "$v3_done" ]] && echo "  ✗ V3: 未见 PDF 解析（type=pdf）" >&2
    [[ -z "$v4_done" ]] && echo "  ✗ V4: 未见回复发送" >&2
    echo "" >&2
    echo "提示：" >&2
    echo "  - PDF 解析需要 pdfplumber 库：pip install pdfplumber" >&2
    echo "  - 可选 OCR 回退需要：pip install pdf2image（需 poppler 工具）" >&2
    echo "  - 查看 monitor.log 和 opencode.log 详细日志" >&2
    exit 1
fi
