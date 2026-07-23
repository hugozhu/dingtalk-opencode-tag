# SKILL.md — 项目技能手册

面向 agent 的可复用操作技能。每一项技能是一段"给定场景 → 怎么做 → 怎么验证"的可执行流程，
凭本文件即可复现，无需重新推导。约定层级见 [AGENTS.md](./AGENTS.md)。

---

## 技能 1：启动服务（kickstart）

**适用场景**：把数字员工从停止状态拉起，开始订阅群消息并自动回复。
适用于当前 **event-connect + `AGENT_BRAIN=opencode`** 配置（一次性 `opencode run` 生成回复，
**不跑 `opencode serve`**）。

### 前置条件

1. **真实敏感常量已就位**：`config/constants.local.sh`（被 `.gitignore` 忽略）。
   关键项：
   - `DWS_EVENT_KEY` / `DWS_EVENT_GROUP` / `DWS_PROFILE` — 订阅哪个群、用哪个组织 profile
   - `AGENT_PROFILE` — **必须与 `DWS_PROFILE` 一致**，否则回复报"未登录"（部署坑 #2）
   - `AGENT_BRAIN=opencode`、`AGENT_REPLY_MODE=user`、`AGENT_SELF_NAMES`（含本人显示名，防自问自答）
   - 末尾 `PATH` 追加 `~/.local/bin`（dws）与 `~/.opencode/bin`（opencode），否则子进程找不到二进制（部署坑 #1）
2. **dws 已登录且 token 有效**，且与 profile 一致：
   ```bash
   dws auth status
   # 期望 authenticated=true、token_valid=true，corp_id/user_id 与 AGENT_PROFILE 匹配
   ```
3. **二进制可达**：`command -v dws opencode python3` 三个都能找到。

### 启动步骤

在项目根目录执行。本配置只需拉起两个组件：**connect**（订阅群消息）和 **event_watcher**
（log-tail → brain → 回复）。**不要**用 `bin/core/monitor.sh` 全量托管——它把 `serve` 当硬性
必需组件（`healthcheck.sh` 对 serve 硬失败），本 serve-less 配置会触发熔断循环。

```bash
cd /path/to/dingtalk-opencode-tag
source config/constants.local.sh                 # 载入真实常量 + PATH
export CONNECT_LOG="$PWD/agent-connect.log"
export MONITOR_LOG="$PWD/monitor.log"

# 1) connect：dws event consume(群) | bridge → CONNECT_LOG
nohup bash bin/custom/dws-connect.sh >>"$CONNECT_LOG" 2>&1 &
echo $! > .connect.pid; disown $! 2>/dev/null || true

# 2) event_watcher：log-tail → route_reply → brain(opencode) → replier(dws send)
nohup python3 src/core/event_watcher.py >>"$MONITOR_LOG" 2>&1 &
echo $! > .event-watcher.pid; disown $! 2>/dev/null || true
```

### 验证

```bash
# 两个组件（及 dws consume 子进程）都在
pgrep -fl "dws-connect.sh|dws event consume|event_watcher.py"

# connect 已订阅、bridge 已就绪
cat agent-connect.log
#   [connect] dws-connect 启动: event=user_im_message_receive_group
#   [dws-bridge] bridge 启动，等待 dws event NDJSON …

# event_watcher 的 log-tail 在监听 connect 日志
tail -n 5 monitor.log
#   event-watcher 启动 - 无限重试 + 自动重连
#   log-tail 启动，监听 .../agent-connect.log
```

到群里发一条消息 → `agent-connect.log` 出现 `[connect] 收到 @<user>: ...` 行即链路通。

### 已知无害噪音

`monitor.log` 里循环出现的 `等待 serve 启动... (Ns)` **是预期的、可忽略的**：
event_watcher 的 SSE 线程一直尝试连 `opencode serve` 的 HTTP 端点，但本配置用 `opencode run`
一次性生成回复、没有 serve。回复走 log-tail 线程，**不依赖 SSE**，故不影响功能，仅每 30s 重试一次。

### 停止

```bash
kill "$(cat .connect.pid)" "$(cat .event-watcher.pid)" 2>/dev/null
pkill -f "dws event consume" 2>/dev/null      # 兜底清理 dws 子进程
rm -f .connect.pid .event-watcher.pid
```

### 排错

| 症状 | 原因 | 处理 |
|------|------|------|
| `agent-connect.log` 有 `ERROR: DWS_PROFILE 未设置` | 没 `source constants.local.sh` 或该文件缺字段 | 补齐 `config/constants.local.sh` 后重启 |
| 回复报 `未登录，请先执行 dws auth login` | `AGENT_PROFILE` ≠ `DWS_PROFILE`（坑 #2） | 两者填成同一真实 profile |
| 子进程 `No such file or directory: 'dws'` | 托管进程 PATH 极简（坑 #1） | 在 `constants.local.sh` 末尾把 `~/.local/bin`、`~/.opencode/bin` 加进 PATH |
| 自问自答刷屏 | `AGENT_REPLY_MODE=user` 但 `AGENT_SELF_NAMES` 没含本人显示名 | 把本人显示名加进 `AGENT_SELF_NAMES` |

---

## 技能 2：服务状态巡检（health check）

**适用场景**：服务已经（或应该）在跑，需要快速判断"链路是否健康"——进程在不在、有没有在收消息、
回复路径通不通。用于定期巡检、排障第一步、或重启前后对比。

对本 serve-less 配置，**不要用 `bin/core/healthcheck.sh`** 判定整体健康：它含 `check_serve` /
`check_serve_http` 两项对 `opencode serve` 硬失败的检查，本配置没有 serve 会恒报"不健康"。
用下面这套只针对 connect + event_watcher + 回复路径的巡检。

### 一键巡检

在项目根目录执行：

```bash
cd /path/to/dingtalk-opencode-tag

echo "== 1. 进程存活 =="
for f in .connect.pid .event-watcher.pid; do
  if [[ -f $f ]] && kill -0 "$(cat "$f")" 2>/dev/null; then
    echo "  $f -> pid $(cat "$f") ALIVE"
  else
    echo "  $f -> DOWN"; fi
done
pgrep -fl "dws event consume" >/dev/null && echo "  dws consume 子进程 ALIVE" || echo "  dws consume 子进程 DOWN"

echo "== 2. connect 日志活跃度 =="
now=$(date +%s); mt=$(stat -f %m agent-connect.log 2>/dev/null || echo 0)
echo "  距上次写入 $((now-mt))s（越小越活跃；订阅空闲期也可能较大，非硬故障）"

echo "== 3. 收发链路痕迹（近 200 行）=="
tail -n 200 agent-connect.log 2>/dev/null | grep -cE "收到 @" | sed 's/^/  收到消息: /'
grep -cE "ERROR|未登录|No such file" agent-connect.log 2>/dev/null | sed 's/^/  connect 错误行累计: /'

echo "== 4. auth 有效 =="
dws auth status 2>/dev/null | grep -E '"authenticated"|"token_valid"|"user_id"' | sed 's/^/  /'
```

### 逐项判定

| 检查项 | 健康 | 不健康 → 处理 |
|--------|------|--------------|
| **进程存活** | 两个 pid ALIVE + `dws consume` 子进程在 | 任一 DOWN → 走[技能 1](#技能-1启动服务kickstart) 重新拉起（先清 pid 文件） |
| **connect 日志活跃度** | 有消息时秒数很小 | 秒数大**且群里确实刚发过消息** → connect 断连，重启 connect |
| **收发链路痕迹** | `收到 @` 计数随发消息增长 | 发了消息但计数不涨 → 检查 `DWS_EVENT_GROUP` 是否为目标群、profile 是否匹配 |
| **connect 错误行** | 0 | 出现 `未登录` / `No such file` → 对照技能 1 排错表（坑 #1/#2） |
| **auth** | `authenticated=true`、`token_valid=true`、`user_id` 与 `AGENT_PROFILE` 一致 | token 失效 → `dws auth login` 重新登录 |

### 端到端确认（可选）

最可靠的健康判定是真发一条消息看回复。**已脚本化，一键跑**（opencode / Claude Code / Codex 通用）：

```bash
# 以真人身份私聊发一条带唯一校验码的算式，V1-V4 双校验数字员工回复正确
bash tests/custom/e2e_text_reply_test.sh
# 期望结尾：✅ 文本回复真实链路端到端测试通过（V1-V4）
#   E2E-<ts>  "…37 加 5 等于多少？"  →  "42"
```

脚本自动：探测真人发送方（同 corp、非数字员工）→ 发消息 → 轮询 `agent-connect.log`/`monitor.log`
断言收发 → 从入站行取 convId、用 `dws chat message list --group` 独立拉回复断言答案。
无 dws / 未登录 / 服务未跑 → SKIP（不算失败）。若 V2 超时未见入站，多半是订阅投递停滞
（见 AGENTS.md 坑#3），先 `bash bin/core/reboot.sh` 再跑。

手动版（想自己盯日志时）：

```bash
# 实时跟随日志，然后到群里 @ 数字员工发一句话
tail -f agent-connect.log
# 期望依次出现：
#   [connect] 收到 @<你>: <消息>
#   （随后 event_watcher 触发 brain(opencode) 生成回复并经 dws send 发回群）
```

看到 `收到 @` 行即"收"通；群里出现机器人回复即"发"通、brain 正常。

### 忽略项

`monitor.log` 的 `等待 serve 启动...` 循环重试**不是**健康信号，巡检时忽略（原因见技能 1
"已知无害噪音"）。

