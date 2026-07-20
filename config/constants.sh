#!/bin/bash
# constants.sh — 可配置常量模板
#
# 用户在这里覆盖所有可配置常量，被 bin/core/*.sh 引用
# 复制本文件为 constants.local.sh（被 .gitignore 忽略）填入真实值
#
# ⚠️ 两个部署必踩坑（后台/launchd/systemd 托管时；前台交互式 shell 不明显）：
#   1. PATH：托管进程的 PATH 极简，找不到 dws(~/.local/bin)、opencode(~/.opencode/bin)。
#      必须在 constants.local.sh 里把这两个目录加进 PATH（见文件末尾 PATH 行）。
#      症状：replier 报 "No such file or directory: 'dws'"。
#   2. AGENT_PROFILE：回复/CLI 路径读 agent_common.PROFILE（来自 AGENT_PROFILE），
#      **必须**与 DWS_PROFILE 填成同一个真实 profile。留占位 'your-profile' 会导致
#      dws 用不存在的 profile → "未登录，请先执行 dws auth login"。

# --- 数字员工身份 ---
export AGENT_ROBOT_CODE="${AGENT_ROBOT_CODE:-your-robot-code}"
export AGENT_USER_ID="${AGENT_USER_ID:-your-user-id}"
# ⚠️ AGENT_PROFILE 必须 = DWS_PROFILE（真实 profile），否则回复报未登录。见文件顶部坑#2。
export AGENT_PROFILE="${AGENT_PROFILE:-your-profile}"

# --- 路径 ---
export PROJECT_DIR="${PROJECT_DIR:-/path/to/your/project}"
export AGENT_BOT_DIR_SUBSTR="${AGENT_BOT_DIR_SUBSTR:-your-agent-workdir}"

# --- 守护进程参数 ---
export CHECK_INTERVAL="${CHECK_INTERVAL:-1800}"          # 健康检查间隔（秒）
export MAX_FAILURES="${MAX_FAILURES:-3}"                  # 连续失败熔断阈值
export WARMUP_TIMEOUT="${WARMUP_TIMEOUT:-60}"             # warmup 超时
export KICKSTART_RETRY_INTERVAL="${KICKSTART_RETRY_INTERVAL:-10}"
export LAUNCHD_LABEL="${LAUNCHD_LABEL:-com.example.agent-connect}"
# /reboot 重启机制: auto(默认,自动判定) | launchd(launchctl kickstart) | nohup(直接重启
# monitor 进程)。auto=launchd agent 已加载则 launchd,否则 nohup。用 nohup 手动托管
# （非 launchd）的部署会被 auto 判为 nohup;想强制可显式设 REBOOT_RESTART_MODE=nohup。
export REBOOT_RESTART_MODE="${REBOOT_RESTART_MODE:-auto}"

# --- 健康检查 ---
export LOG_INACTIVITY_THRESHOLD="${LOG_INACTIVITY_THRESHOLD:-2100}"   # 35 分钟
# 进程 cmdline 匹配模式（healthcheck verify_pid 用，字面子串匹配）。默认对应 harness
# 自带实现（dws dev connect / event_watcher.py）。FDE 换了 connect 实现时必须覆盖，
# 否则 check_connect 恒硬失败 → monitor 全量重启循环。
# 例：本仓库 connect 用 dws-connect.sh → 在 constants.local.sh 设 CONNECT_CHECK_PATTERN=dws-connect.sh
export CONNECT_CHECK_PATTERN="${CONNECT_CHECK_PATTERN:-agent-connect.*--unified-app-id}"
export EVENT_WATCHER_CHECK_PATTERN="${EVENT_WATCHER_CHECK_PATTERN:-event_watcher.py}"

# --- 视觉/多模态 ---
export PROXY_URL="${PROXY_URL:-http://localhost:4000/v1}"
export PROXY_KEY="${PROXY_KEY:-sk-1234}"
export VISION_MODEL="${VISION_MODEL:-gemini-3.1-flash-image}"
# 经 opencode serve 自身识别图片的免费多模态模型（无需外部 proxy）。实测 opencode
# provider 已鉴权且能读图；opencode/mimo-v2.5-free 正确识别测试图（CODE/颜色/框），~7s。
# 图片能力后续可改走 serve 直传图片给此模型（provider/model 格式）。
export AGENT_VISION_MODEL="${AGENT_VISION_MODEL:-opencode/mimo-v2.5-free}"

# --- 业务特定（用户扩展）---
# 在这里加自己的业务常量

# --- dws event connect（bin/custom/dws-connect.sh）---
# 敏感值：真实的群 conversationId / profile 填在 config/constants.local.sh（gitignored），
# 不要写进本模板文件。群订阅 + 单聊(o2o)订阅 + @我(at)订阅可任意组合，至少开一种。
export DWS_EVENT_KEY="${DWS_EVENT_KEY:-user_im_message_receive_group}"
export DWS_EVENT_GROUP="${DWS_EVENT_GROUP:-}"   # 群 openConversationId（订阅群消息必填，敏感）
# 单聊(o2o)订阅：对端 userId 列表（逗号分隔）。钉钉 o2o 事件只能按“对端 userId”订阅，
# 每个对端起一个 consumer。留空=不订阅单聊。例：给数字员工发单聊的真人 userId。
export DWS_EVENT_O2O_USERS="${DWS_EVENT_O2O_USERS:-}"  # 敏感，勿提交
# @我订阅：数字员工账号在**任意群**被 @ 时收到消息（事件 user_im_message_receive_at，
# rule_type=at 个人级订阅，无需 group/user 参数）。1/true/yes/on=开，留空/0=不订阅。
# 适合“只在被 @ 时才响应、又不想逐个配置群 conversationId”的场景。与群/单聊订阅可并存，
# 同一条消息即便被多个订阅命中，能力层按 msgId 去重不会重复处理。
export DWS_EVENT_AT="${DWS_EVENT_AT:-}"
export DWS_PROFILE="${DWS_PROFILE:-}"           # 组织 profile（敏感，勿提交）

# --- 数字员工大脑 / 回复（src/custom/brain.py + replier.py）---
# 大脑后端: echo(默认,零依赖) | opencode(serve HTTP 优先, 失败回退 opencode run CLI) | proxy(LLM API)
export AGENT_BRAIN="${AGENT_BRAIN:-echo}"
export AGENT_OPENCODE_MODEL="${AGENT_OPENCODE_MODEL:-opencode/deepseek-v4-flash-free}"
# opencode serve 端口（start_serve 用；密码自动生成写 .serve.pwd）。brain(opencode) 走
# 此 serve 的 HTTP 接口生成回复。
export OPENCODE_SERVE_PORT="${OPENCODE_SERVE_PORT:-4096}"
# 回复模式: log(默认,只记日志不真发) | bot(机器人 send-by-bot) | user(当前用户 send)
export AGENT_REPLY_MODE="${AGENT_REPLY_MODE:-log}"
# 防回环：数字员工自己的发送名（逗号分隔），过滤掉避免自问自答。
# ⚠️ user 模式：回复以你本人身份发出，必须把你的真实显示名加进来（如 hugozhu）。
export AGENT_SELF_NAMES="${AGENT_SELF_NAMES:-数字员工,Claude Code}"

# --- 能力开关（src/custom/capabilities/，可组装/可选配）---
# 每个能力一个 CAP_<NAME>_ENABLED 开关。1/true/yes/on=开，0/false/no/off=关；
# 不设则用能力自带默认。关掉的能力压根不注册、不参与分发。
export CAP_TEXT_REPLY_ENABLED="${CAP_TEXT_REPLY_ENABLED:-1}"   # 普通文本回复（brain→replier）
export CAP_FORWARD_ENABLED="${CAP_FORWARD_ENABLED:-1}"        # 合并转发（chatRecord 聊天记录）
# 合并转发检测正则（匹配 content 摘要特征）。DingTalk 合并转发 content 形如
# "群聊的聊天记录\n..."；默认匹配"聊天记录"。命中后 list-by-ids 反查 forwardMessages 二次确认。
# export CAP_FORWARD_SUMMARY_PATTERN="聊天记录"
# 合并转发注入 agent 的 prompt 末句指令（点明这是合并转发聊天记录 + 任务）。留空用内置默认。
# export CAP_FORWARD_PROMPT_FOOTER="以上是一段钉钉「合并转发」的聊天记录，…请理解语境后回应/总结。"
# 单条内层消息内容截断上限（防超长附件撑爆 prompt），默认 4000。
# export CAP_FORWARD_ENTRY_MAX="4000"
export CAP_IMAGE_ENABLED="${CAP_IMAGE_ENABLED:-1}"           # 图片识别（vision 兜底）
# 图片识别需要多模态 proxy 可达（见下方 PROXY_URL/VISION_MODEL）。注入 agent 的末句指令可覆盖：
# export CAP_IMAGE_PROMPT_FOOTER="以上「图片识别内容」由多模态模型提取…请结合说明回应。"
export CAP_FILE_ENABLED="${CAP_FILE_ENABLED:-1}"             # 文档/文件处理（受控下载+注入）
# 文件能力主动 drive download 到临时目录、读前 N 字节文本注入 agent（避免 agent 自主下载到
# 项目目录）。文本类文件（txt/md/csv/json/日志/代码等）读正文；二进制文件给说明不硬读。
# export CAP_FILE_MAX_BYTES="16384"     # 读取正文字节上限
# export CAP_FILE_PROMPT_FOOTER="以上是用户发送的文件内容…请结合说明回应。"
export CAP_QUESTION_ENABLED="${CAP_QUESTION_ENABLED:-1}"     # Question 交互（钉钉端答 agent 提问）
# Question 超时未答自动 reject 的秒数（serve 端 question 无 TTL，这是安全网防会话卡死），默认 60。
# export CAP_QUESTION_TIMEOUT="60"
export CAP_AGGREGATION_ENABLED="${CAP_AGGREGATION_ENABLED:-0}"  # 群消息聚合（默认关，与逐条回复互斥）
# 聚合开启后：群文本消息不逐条回复，缓冲到时间窗后合并成一次摘要回复。相关参数：
# export CAP_AGGREGATION_WINDOW="300"      # 时间窗秒数（缓冲第一条后多久 flush），默认 300
# export CAP_AGGREGATION_MAX_MSGS="50"     # 缓冲数量上限，达到即立即 flush
# export CAP_AGGREGATION_PROMPT_FOOTER="以上是群里最近多条消息，请做简洁总结/回应，不要逐条复述。"

# 回执能力（已读 + 状态「文字表情」时间线）。收到消息即 mark-read + 在**用户消息上**贴一条
# 「文字表情」回应（DingTalk text-emotion：表情 + 文字同时呈现），随处理进度**原地更新**
# （收到→处理中→处理久了…→完成/失败），不发独立消息、不刷屏、无卡片"生成中"加载态。
# 非消费型：不影响正常回复链路（best-effort：任一步失败只记日志不阻断回复）。
# **默认开**：默认文案已实测可被 create-text-emotion 保存。改文案后建议先手测能否保存
# （`dws chat message create-text-emotion --emotion-name <名> --text <文>`；部分含特殊
# emoji/标点的文案会报"暂不支持保存该文字表情"）。需数字员工 profile 有回执权限。停用设 0。
export CAP_ACK_ENABLED="${CAP_ACK_ENABLED:-1}"
# 只对单聊(conv_type=1)回执（群里逐条贴噪音大）。设 0 则群消息也回执（配合 #46 的
# 「群里@我」场景更佳）。默认 1。
export ACK_O2O_ONLY="${ACK_O2O_ONLY:-1}"
export ACK_MARK_READ="${ACK_MARK_READ:-1}"                 # 是否同时标记已读（1=开）
# 文字表情时间线：`delay秒:表情名:文字`，多阶段用 `|` 分隔，按 delay 升序。delay=消息到达后
# 多少秒切到该状态（首个应为 0=收到即贴；文字里可含 : 和 ,，只按前两个 : 切分）。任一时刻
# 只显示一个文字表情（升级=移除旧+贴新）。表情名为 DingTalk **具名表情**（实测 收到/稍等/
# 咖啡/OK/疑问 等；文字表情会先 create-text-emotion 拿 emotionId 再 add，模块内按(名,文)缓存）。
# 默认文案用纯文字：实测含 emoji/特殊标点的文案（🈺、（约 5 分钟）…）会被 create-text-emotion
# 拒（"暂不支持保存该文字表情"），纯文字稳定可存。改文案后建议先手测 create-text-emotion 能存。
export ACK_STAGES="${ACK_STAGES:-0:稍等:已收到，正在处理|5:稍等:正在处理中|300:咖啡:仍在处理，请稍候}"
export ACK_DONE="${ACK_DONE:-OK:✅ 已处理完成}"          # 完成（表情名:文字，✅ 实测可存）
export ACK_ERROR="${ACK_ERROR:-疑问:⚠️ 处理未完成}"      # 失败（表情名:文字，⚠️ 实测可存）
# 等"回复已发出"信号的上限秒数（brain 慢 / 空回复不发送时兜底收尾）。留空=自动取
# max(180, 最后阶段delay + 300)，保证时间线走完后仍留足冗余。
# export ACK_DONE_TIMEOUT="600"


# --- 调试 ---
# AGENT_DEBUG=1 时：
#   - agent_common 每次 serve 请求成功也打 [serve] 访问日志到 monitor.log；
#   - brain 每次 opencode 调用单独记一条到 opencode.log（transport=http|cli / model / 耗时 /
#     prompt+reply 长度 / reply 预览 / 成败）。opencode 调用出错恒记，不受此开关影响；
#   - start_serve 给 opencode serve 加 --print-logs --log-level，serve 自身日志打到 serve.log。
export AGENT_DEBUG="${AGENT_DEBUG:-0}"
# opencode 调用日志路径（默认 <PROJECT_DIR>/opencode.log）
# export AGENT_OPENCODE_LOG="$PROJECT_DIR/opencode.log"
# AGENT_DEBUG 时 opencode serve 自身日志：级别（DEBUG|INFO|WARN|ERROR）+ 路径
# export AGENT_SERVE_LOG_LEVEL="DEBUG"
# export AGENT_SERVE_LOG="$PROJECT_DIR/serve.log"

# --- PATH（部署坑#1）---
# 托管进程 PATH 极简，子进程调 dws/opencode 会找不到。取消注释并按实际路径填：
# export PATH="$PATH:$HOME/.local/bin:$HOME/.opencode/bin"
