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
#    安装见 dws 官方说明，装好后确认可用：
dws --version
```

### 第 1 步：钉钉扫码授权

```bash
dws auth login          # 浏览器/扫码登录钉钉，拿到 profile
dws auth status         # 确认 authenticated: true，记下 corp_id / user_id
```

### 第 2 步：填配置（一个群 + 你的身份）

```bash
cp config/constants.sh config/constants.local.sh   # *.local.* 被 gitignore
# 找到目标群的 openConversationId：
dws chat search --query "你的群名"
```

编辑 `config/constants.local.sh`，最少填这几个：

```bash
export DWS_EVENT_GROUP="cid...=="                          # 上面查到的群 ID
export DWS_PROFILE="dinga...:<userId>"                     # dws auth status 里的 corpId:userId
export AGENT_PROFILE="$DWS_PROFILE"                        # 同上（回复用同一身份）
export AGENT_BRAIN="opencode"                              # 用 opencode 大脑
export AGENT_OPENCODE_MODEL="opencode/deepseek-v4-flash-free"  # 免费文本模型
export AGENT_VISION_MODEL="opencode/mimo-v2.5-free"        # 免费看图模型
export AGENT_REPLY_MODE="user"                             # 以当前登录身份回复到群
export AGENT_SELF_NAMES="你的机器人显示名"                  # 防自问自答，填数字员工自己的名字
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

写一个 `src/custom/capabilities/<name>.py`，声明一个 `Capability` 挂到入站/SSE 钩子上，注册即生效。core 只认注册表，加/删能力不碰 core，upstream 修复能干净 merge。三步范例见 [FORKING.md](./FORKING.md)。

```python
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT

def on_inbound(msg):          # msg: InboundMessage(user/text/conv_id/msg_id/kind…)
    ...                        # 处理并回复；return True=已消费
    return True

register(Capability(name="my_cap", on_inbound=on_inbound,
                    handles_kinds={KIND_TEXT}, priority=50, default_enabled=True))
```

---

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
- **依赖 dws CLI**：收发消息、下载媒体都用 dws。换平台需在 custom 层替换为对应 SDK。
- **语音消息**：opencode 免费模型不支持音频转写，需接外部 STT，见 [issue #42](https://github.com/hugozhu/dingtalk-opencode-tag/issues/42)。
- **serve 密码经 `ps` 可见**：`.serve.pwd` 为明文文件、密码在进程环境变量里，多用户主机上同机其他用户可见。详见 [FORKING.md](./FORKING.md) 安全说明。

## License

MIT
