---
name: e2e-smoke
description: 端到端冒烟——验证数字员工「收→处理→发」闭环。支持文本消息和文件消息（#68）两种测试，双校验（日志 + 钉钉实际会话）确认链路正常。
allowed-tools: Bash
---

# /e2e-smoke — 端到端冒烟测试

验证数字员工经完整链路（dws 订阅 → bridge → event_watcher → brain/file capability → replier）
收到并正确回复。支持**文本消息**和**文件消息**两种测试。

## 1. 文本消息测试

以真人身份私聊发一条带唯一校验码的算式，验证回复**正确答案**。

### 怎么做

直接跑封装好的脚本（身份自动探测、V1-V4 双校验、SKIP 友好）：

```bash
bash tests/custom/e2e_text_reply_test.sh
```

- 群聊链路：`E2E_TARGET=group bash tests/custom/e2e_text_reply_test.sh`
- 慢环境放宽超时：`E2E_WAIT=90 bash tests/custom/e2e_text_reply_test.sh`
- 指定发送方：`E2E_SENDER_PROFILE="<corpId>:<真人userId>" bash …`

### 结果判定

- `✅ 文本回复真实链路端到端测试通过（V1-V4）` → 链路正常，把 `校验码 → 回复` 一行回报用户。
- `⏭️ SKIP …` → 前置不满足（无 dws / 未登录 / 服务未跑）。按提示让用户先
  `bash bin/core/start.sh` 或登录，再重试。
- `❌ … 存在失败项` → 看是哪个 V 挂：
  - **V2 未见入站**：多半是订阅投递停滞（AGENTS.md 坑#3）。先
    `bash bin/core/reboot.sh` 重建订阅，等 ~20s warmup，再跑一次。
  - **V3/V4 挂而 V2 过**：brain / serve / replier 侧问题，
    `tail -n 40 monitor.log opencode.log` 定位。

## 2. 文件消息测试（#68）

验证文件能力的完整链路：创建 markdown 文件 → 发送 → 解析（type-based dispatch）→ 回复。

### 怎么做

```bash
# 创建测试 markdown 文件
cat > /tmp/e2e_file_test.md << 'EOF'
# E2E File Test
验证文件能力 (#68) 的端到端测试。
**Test Marker**: FILE-E2E-TEST
EOF

# 发送文件消息（使用 --msg-type file --file-path）
dws chat message send \
  --user 287179924 \
  --msg-type file \
  --file-path /tmp/e2e_file_test.md \
  --profile dinga626d60c1128d449:0420506555

# 等待 10 秒后检查日志
sleep 10

# 验证 V2: 文件消息入站
tail -5 agent-connect.log | grep "\[文件\].*fileId:"

# 验证 V3: 文件解析成功
tail -10 monitor.log | grep "file:.*解析成功.*type=text"

# 验证 V4: 回复已发送
tail -5 monitor.log | grep "reply user OK"
```

### 验收点

- ✓ V1: 文件消息发送成功（`"success": true`）
- ✓ V2: agent-connect.log 记录入站（`[文件] xxx fileId: xxx`）
- ✓ V3: monitor.log 记录解析（`file: 解析成功 type=text content_len=xxx`）
- ✓ V4: monitor.log 记录回复（`reply user OK`）

### 完整测试脚本

```bash
# 一键运行文件消息 e2e 测试
bash << 'SCRIPT'
set -euo pipefail

echo "=== 文件消息 E2E 测试 ==="

# 1. 创建测试文件
cat > /tmp/e2e_file_test.md << 'EOF'
# E2E File Test Document
验证文件消息处理能力 (#68)。
**Marker**: FILE-E2E-$(date +%s)
EOF
echo "✓ 测试文件已创建"

# 2. 发送文件消息
echo "📤 发送文件消息..."
result=$(dws chat message send \
  --user 287179924 \
  --msg-type file \
  --file-path /tmp/e2e_file_test.md \
  --profile dinga626d60c1128d449:0420506555 2>&1)

if echo "$result" | grep -q '"success": true'; then
  echo "✅ V1: 发送成功"
else
  echo "❌ V1: 发送失败"
  exit 1
fi

# 3. 等待处理
echo "⏳ 等待处理（60s）..."
for i in {1..60}; do
  sleep 1
  if tail -5 agent-connect.log 2>/dev/null | grep -q "\[文件\].*fileId:"; then
    echo "✅ V2: 入站已记录"
    break
  fi
done

for i in {1..10}; do
  sleep 1
  if tail -10 monitor.log 2>/dev/null | grep -q "file:.*解析成功.*type=text"; then
    echo "✅ V3: 解析成功"
    break
  fi
done

for i in {1..10}; do
  sleep 1
  if tail -5 monitor.log 2>/dev/null | grep -q "reply user OK"; then
    echo "✅ V4: 回复已发送"
    break
  fi
done

echo ""
echo "=== 验证完成 ==="
tail -5 agent-connect.log | grep "\[文件\]" || echo "未找到入站记录"
tail -10 monitor.log | grep "file:" | tail -2 || echo "未找到解析记录"
SCRIPT
```

## 注意

- 这些测试会**真实发消息**到钉钉，仅用于用户明确要做端到端确认时。
- 不改任何代码；纯读 + 发测试消息。
- 文本测试校验用 `dws chat message list`（o2o 私聊回复 list-by-sender 索引不到，见 AGENTS.md 坑#1）。
- 文件测试需要正确的发送方 profile（真人账号，非数字员工账号）。
