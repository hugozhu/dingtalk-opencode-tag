# 钉钉数字员工脚手架 · Agent Harness

**用免费模型、几分钟、零成本，在钉钉群里上线一个能对话、看图、读文件的数字员工。**

下载 opencode + 装 dws + 钉钉扫码授权 —— 三步就能让机器人上线。跑在 opencode 的**免费模型**上，**起步成本为 0**。

当前版本见 [VERSION](./VERSION)。

---

## 为什么用它

自己从零搭一个"群消息监听 → LLM 生成回复 → 发回群"的数字员工，你要处理进程守护、断线重连、图片/文件多模态、会话注入、测试隔离一堆脏活。这个 harness 把这些**生产环境打磨过的坑**全封装好了，你只填几个配置就能上线，想定制业务再写自己的能力插件。

- 🆓 **零成本起步**：默认跑 opencode 免费模型（文本 `deepseek-v4-flash-free`、看图 `mimo-v2.5-free`），不需要 API key、不烧钱。
- ⚡ **几分钟上线**：装两个工具 + 钉钉扫码，填一个群 ID 就能收发消息。
- 🧩 **开箱即用的 6 个能力**：文本对话、图片识别、文件解读、合并转发解析、Question 交互、群消息聚合 —— 都是可开关的插件。
- 🛡️ **生产级守护**：launchd 托管，崩溃自愈、健康检查、熔断、`/reboot` 远程重启。
- 🔧 **可定制、可 merge**：core/custom 分层，你只改 custom，upstream 的修复能干净合并回来。

---

## 项目理念

- 🤖 **对 Coding Agent 友好的 Harness 工程，功能开发 100% AI Coding**
  代码库刻意做成"给 AI 写代码"友好的形态：core/custom 物理分层、能力插件契约清晰、每个能力自带单测、边界写进 [AGENTS.md](./AGENTS.md)。**本项目的功能全部由 AI 编码完成** —— 人给方向和验收，AI 探查、实现、真实链路验证、提 PR。你要加能力，也可以直接把需求丢给 Coding Agent，它照着现有插件范式就能写。

- 🧬 **能力按需交付，背后是 opencode 的生态**
  数字员工的"推理 + 任务执行"这件重活，**交给更完备的 opencode**（它有模型生态、工具、会话、权限一整套）。本项目**只做"人机协同"那一层的最佳实践** —— 钉钉侧的收发、富媒体受控处理（图片识别 / 文件解读 / 合并转发）、Question 人在回路作答、群消息聚合、进程守护自愈。分工清晰：**opencode 负责"想和做"，本 harness 负责"人怎么跟它协同"**。能力可组装、可选配，按业务需要一个个交付。

---

## 开箱即用的能力

每个都是 `src/custom/capabilities/` 下的插件，用 `CAP_<NAME>_ENABLED` 开关，可组装、可选配：

| 能力 | 做什么 | 默认 |
|------|--------|:---:|
| **文本对话** | 群里发消息，数字员工用 LLM 回复 | 开 |
| **图片识别** | 发图片 → 免费多模态模型识别内容 → 基于内容回应 | 开 |
| **文件解读** | 发文档（txt/md/csv/json/代码…）→ 受控下载读正文 → 解读 | 开 |
| **合并转发** | 转发一段聊天记录 → 反查逐条解析（含图/文件）→ 总结 | 开 |
| **Question 交互** | agent 反问时，你在群里回复序号/选项作答 | 开 |
| **群消息聚合** | 短时多条消息合并成一次摘要回复，不逐条打扰 | 关 |

> 富媒体都是**受控处理**：harness 主动下载、识别、注入，不让 agent 自己乱下东西或执行 shell。

---

## 三步上线

### 前置：装两个工具

```bash
# 1. opencode（数字员工的"大脑"，自带免费模型）
curl -fsSL https://opencode.ai/install | bash      # 或见 https://opencode.ai

# 2. dws（钉钉工作台 CLI，负责收发消息）
#    安装见 https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli
#    装好后确认可用：
dws --version
```

### 第 1 步：钉钉授权 + 数字员工企业账号

数字员工本质是一个**企业里的钉钉账号**（用它的身份收发消息）。链路是"授权 → 组织 → 数字员工专属账号"：

**先授权本机：**

```bash
dws auth login          # 浏览器/扫码登录钉钉，把一个组织账号加成本机 profile
#   SSH / 容器 / 无头环境（本机没浏览器）用设备流：
dws auth login --device # 显示 user_code + 短链接，手机钉钉扫码授权

dws auth status         # 确认 authenticated: true
dws profile list        # 列出本机已登录的全部组织账号（corpId / userId / 组织名）
```

**创建组织（没有现成企业时，用 `dws contact org`）：**

```bash
dws contact org create --org-name "我的企业" --creator-username "你的名字"
# 建好后组织信息里会返回 corpId；已有企业就跳过这步
```

**入职数字员工专属账号（推荐，用 `dws contact account`）：**

给数字员工建一个**独立的企业登录账号**（和真人分开，身份清晰、可单独管权限）：

```bash
dws contact account create \
  --org-user-name "数字员工" \        # 它在企业里的显示名
  --login-id "opencode-bot-01" \      # 登录号（别含手机号，否则短信可能被拦）
  --dept-ids "1" \                    # 加入的部门（可选）
  --send-pwd-via-sms                  # 通过短信/邮件发登录邀请（可选）
# 需要在**已授权的企业**下执行；corpId 由系统按当前 profile 自动注入
```

然后把这个账号**拉进目标群**，并让它授权本机：

```bash
dws auth login          # 这次用「数字员工账号」扫码登录（不是你本人）
dws profile list        # 应能看到它：  <组织名> | <corpId> | 数字员工 | <userId>
```

- 一个组织可以有**多个账号**（真人 + 数字员工各一份 profile）。业务命令用 `--profile <corpId>:<userId>` 指定用谁的身份；本项目的 `DWS_PROFILE` / `AGENT_PROFILE` 填**数字员工账号**的 `corpId:userId`。

> 只是先跑通、还没建专属账号？用你本人账号也能上线（回复以你的身份发出），`AGENT_SELF_NAMES` 填你的显示名防自问自答即可，正式交付再换成专属账号。

### 第 2 步：填配置（一个群 + 你的身份）

```bash
cp config/constants.sh config/constants.local.sh   # *.local.* 被 gitignore
# 找到目标群的 openConversationId：
dws chat search --query "你的群名"
```

编辑 `config/constants.local.sh`，最少填这几个：

```bash
export DWS_EVENT_GROUP="cid...=="                          # 上面查到的群 ID
export DWS_PROFILE="dinga...:<userId>"                     # 数字员工账号的 corpId:userId（见 dws profile list）
export AGENT_PROFILE="$DWS_PROFILE"                        # 同上（数字员工以此身份回复）
export AGENT_BRAIN="opencode"                              # 用 opencode 大脑
export AGENT_OPENCODE_MODEL="opencode/deepseek-v4-flash-free"  # 免费文本模型
export AGENT_VISION_MODEL="opencode/mimo-v2.5-free"        # 免费看图模型
export AGENT_REPLY_MODE="user"                             # 以该账号身份回复到群
export AGENT_SELF_NAMES="数字员工的显示名"                  # 防自问自答，填数字员工自己的名字
```

### 第 3 步：上线

托管 `monitor.sh` 进程（开机自启 + 崩溃自愈），它会自动拉起 opencode serve + 群消息订阅 + 事件监听。按你的系统选一种：

**macOS（launchd）**

```bash
cp bin/custom/agent-template.plist ~/Library/LaunchAgents/com.<你的组织>.<你的agent>.plist
# 编辑 plist 的 Label / ProgramArguments 指向本目录的 bin/core/monitor.sh、PATH
launchctl load -w ~/Library/LaunchAgents/com.<你的组织>.<你的agent>.plist
```

**Linux（systemd --user，无需 root）**

```bash
mkdir -p ~/.config/systemd/user
cp bin/custom/agent-template.service ~/.config/systemd/user/dingtalk-agent.service
# 编辑 .service 里的 <PROJECT_DIR>（本目录绝对路径）和 <USER_LOCAL_BIN>（dws/opencode 所在目录）
systemctl --user daemon-reload
systemctl --user enable --now dingtalk-agent.service
loginctl enable-linger "$USER"   # 让服务在未登录时也开机自启

# 状态/日志/停止/重启：
systemctl --user status  dingtalk-agent.service
journalctl --user -u dingtalk-agent.service -f     # 或看 monitor.log
systemctl --user restart dingtalk-agent.service
```

> 不想装服务、先手动跑一下？`nohup bash bin/core/monitor.sh --foreground >> monitor.log 2>&1 &`（跨会话存活，但机器重启不自启）。

monitor 起来后，**去群里发条消息试试** —— 数字员工就回你了。

> 调试期想先不真发消息？把 `AGENT_REPLY_MODE=log`，回复只写日志不发群，验证链路无误再开真发。

---

## 验证在线

```bash
bash bin/core/healthcheck.sh                 # 6 项健康检查，应 ✅ 健康
# 群里发 "1+1" → 数字员工回 "2"
```

跑测试（不依赖网络/钉钉）：

```bash
bash tests/core/unit_test.sh                          # shell 单测
for t in tests/core/*.py tests/custom/*.py; do python3 "$t"; done   # Python 单测
```

---

## 加一个自己的能力

本项目**功能全部由 AI 编码完成**（见[项目理念](#项目理念)）。加能力的推荐姿势就是：**把需求用一句话丢给 Claude Code 或 opencode，让它照现有插件范式写。**

### 方式一（推荐）：让 Coding Agent 帮你写

在项目根目录起一个 Coding Agent（Claude Code / opencode 都行），给它这样的提示词：

```text
在 src/custom/capabilities/ 下新增一个能力：<描述你的能力，例如：
"收到含关键词 '排班' 的群消息时，查考勤 API 并回复本周排班表">。

要求：
- 参照现有能力的写法（如 src/custom/capabilities/text_reply.py / image.py），
  声明一个 Capability 并 register()，挂到合适的钩子（on_inbound / on_sse_event / on_cleanup）。
- 在 src/custom/capabilities/__init__.py 里 import 它。
- 加一个 CAP_<NAME>_ENABLED 开关（默认值自定），并在 config/constants.sh 文档化。
- 在 tests/custom/ 加对应单测（mock 掉网络/CLI，参照 test_image_capability.py）。
- 不要改 src/core/。遵守 AGENTS.md 里的边界。
- 跑一遍单测确认通过。
```

Agent 会照着现有 6 个能力（`text_reply` / `image` / `file` / `forward` / `question` / `aggregation`）的范式实现、写测试、验证。**AGENTS.md 里写好了边界（哪些能改、约定），Agent 读了就知道怎么改不越界。** 你只负责给方向和验收（最好去真实群里发条消息端到端验一下）。

> 想更省事：把上面这段连同"发一条 XX 消息测试一下效果"一起给 Agent，它能自己触发真实链路验证。

### 方式二：手写（了解插件契约）

一个能力就是一个 `Capability`，挂到入站/SSE 钩子上，注册即生效。core 只认注册表，加/删能力不碰 core，upstream 修复能干净 merge。三步范例见 [FORKING.md](./FORKING.md)。

```python
# src/custom/capabilities/my_cap.py
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT

def on_inbound(msg):          # msg: InboundMessage(user/text/conv_id/msg_id/kind…)
    ...                        # 处理并回复；return True=已消费，False=放行给下一个能力
    return True

register(Capability(name="my_cap", on_inbound=on_inbound,
                    handles_kinds={KIND_TEXT}, priority=50, default_enabled=True))
```

然后在 `src/custom/capabilities/__init__.py` 里 `import` 它即生效。

---

## 数字员工架构图

一条消息从钉钉群进来、经能力处理、再回到群里的完整数据流：

```
   钉钉群（数字员工账号在群里）
        │  ▲
   ①消息 │  │ ⑥回复
        ▼  │
┌───────────────────────────────────────────────────────────────────┐
│  dws CLI（钉钉工作台 CLI）                                             │
│    connect: dws event consume ──► dws_event_bridge.py               │
│             （订阅群消息，转成 connect-log 行）                         │
│    replier: dws chat message send ◄─ 把回复发回来源群                  │
└───────────────────────────────────────────────────────────────────┘
        │ ② connect-log                          ▲ ⑤ send_reply
        ▼  "[connect] 收到 @user: text …"         │
┌───────────────────────────────────────────────────────────────────┐
│  event_watcher（core，事件监听主进程）                                 │
│    log-tail ──► inbound.parse_line ──► InboundMessage(kind=…)        │
│                        │ ③ dispatch                                  │
│                        ▼                                            │
│    ┌─── 能力注册表（core.capabilities，按 kind + priority 分发）───┐   │
│    │  text_reply · image · file · forward · question · aggregation │  │
│    │  （custom 插件，各自 CAP_*_ENABLED 开关，可组装/可选配）        │   │
│    └───────────────────────────┬───────────────────────────────┘   │
│    SSE /event ◄── question 等能力挂 on_sse_event（人在回路作答）      │
└────────────────────────────────┼──────────────────────────────────┘
                                 │ ④ brain.generate_reply
                                 ▼
┌───────────────────────────────────────────────────────────────────┐
│  opencode serve（本机常驻，"想 + 做" 的大脑）                          │
│    POST /session/{id}/message  → 免费模型（deepseek / mimo 看图 …）    │
│    推理 · 工具调用 · 会话 · 权限 —— 由 opencode 生态负责                │
└───────────────────────────────────────────────────────────────────┘

╔═══════════════════════════════════════════════════════════════════╗
║  monitor.sh（launchd / systemd 托管）—— 全程守护                      ║
║   拉起 & 兜底：serve · connect · event_watcher                       ║
║   healthcheck（6 项，30 分钟自检）· 崩溃自愈 · 熔断 · /reboot 远程重启  ║
╚═══════════════════════════════════════════════════════════════════╝
```

**分工**：`dws` 管钉钉侧收发与富媒体下载；`event_watcher` + 能力插件做**人机协同层**（受控识别/解读、路由、作答、聚合）；`opencode serve` 做**推理与任务执行**；`monitor` 保证全程在线。

## 架构：core / custom 分层

FDE 交付时通过物理分层实现"改得动 + merge 得回"：

| 层 | 路径 | FDE 改？ | merge 回 upstream |
|----|------|:---:|:---:|
| **core** | `src/core/` `bin/core/` `tests/core/` | ❌ | ✅ bug fix 贡献回 |
| **custom** | `src/custom/` `bin/custom/` `tests/custom/` | ✅ 在这里改 | ❌ 业务特定 |
| **config** | `config/*.local.*` | ✅ 填真实值 | ❌ gitignored |

```
src/
├── core/                     ← harness 核心（不改）
│   ├── event_watcher.py      ← 事件监听主进程（SSE 重连 + log-tail + 能力分发）
│   ├── capabilities.py       ← 能力注册表（可组装/可选配的插件框架）
│   ├── inbound.py            ← 统一 InboundMessage（消息归一 + kind 分类）
│   └── agent_common.py       ← 共享工具（serve 访问 / 通知 / inject_and_forward）
├── custom/                   ← FDE 改这里
│   ├── capabilities/         ← 能力插件（text_reply / image / file / forward / question / aggregation）
│   ├── brain.py              ← "大脑"：调 opencode serve 生成回复（免费模型）
│   └── replier.py            ← 把回复发回钉钉
bin/
├── core/                     ← 守护/健康检查（不改）：monitor / healthcheck / reboot / lib
└── custom/                   ← start_funcs.sh（组件启动）/ dws-connect.sh（群订阅）/ plist + service（托管模板）
config/                       ← constants.sh（模板）+ constants.local.sh（真实值，gitignored）
```

- **运维手册**（启动/停止/状态查询）见 [SKILL.md](./SKILL.md)
- **派生指南**（哪些改/不改/同步 upstream）见 [FORKING.md](./FORKING.md)
- **架构 + 最佳实践**见 [ARCHITECTURE.md](./ARCHITECTURE.md)

---

## 免费模型说明（起步成本 = 0）

默认配置全用 opencode 内置免费模型，无需任何 API key：

| 用途 | 模型 | 实测 |
|------|------|------|
| 文本对话 | `opencode/deepseek-v4-flash-free` | ✅ |
| 图片识别 | `opencode/mimo-v2.5-free` | ✅ 能读图里的文字/内容 |
| 语音转写 | —— | ❌ 免费模型不支持，需外部 STT（见 issue #42） |

想换更强的模型（付费）？改 `AGENT_OPENCODE_MODEL` / `AGENT_VISION_MODEL` 即可，一行配置的事。

---

## 已知限制

- **平台**：支持 macOS（launchd）和 Linux（systemd）。Windows 需自行适配（用服务/任务计划器托管 `bin/core/monitor.sh`）。core 脚本已做 macOS/Linux 双兼容（stat/date/锁）+ bash 3.2 兼容。
- **依赖 dws CLI**：收发消息、下载媒体都用 [dws](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli)。换平台需在 custom 层替换为对应 SDK。
- **语音消息**：opencode 免费模型不支持音频转写，需接外部 STT，见 [issue #42](https://github.com/hugozhu/dingtalk-opencode-tag/issues/42)。
- **serve 密码经 `ps` 可见**：`.serve.pwd` 为明文文件、密码在进程环境变量里，多用户主机上同机其他用户可见。详见 [FORKING.md](./FORKING.md) 安全说明。

## License

MIT
