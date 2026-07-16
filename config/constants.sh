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

# --- 健康检查 ---
export LOG_INACTIVITY_THRESHOLD="${LOG_INACTIVITY_THRESHOLD:-2100}"   # 35 分钟

# --- 视觉/多模态 ---
export PROXY_URL="${PROXY_URL:-http://localhost:4000/v1}"
export PROXY_KEY="${PROXY_KEY:-sk-1234}"
export VISION_MODEL="${VISION_MODEL:-gemini-3.1-flash-image}"

# --- 业务特定（用户扩展）---
# 在这里加自己的业务常量

# --- dws event connect（bin/custom/dws-connect.sh）---
# 敏感值：真实的群 conversationId / profile 填在 config/constants.local.sh（gitignored），
# 不要写进本模板文件。
export DWS_EVENT_KEY="${DWS_EVENT_KEY:-user_im_message_receive_group}"
export DWS_EVENT_GROUP="${DWS_EVENT_GROUP:-}"   # 群 openConversationId（敏感，勿提交）
export DWS_PROFILE="${DWS_PROFILE:-}"           # 组织 profile（敏感，勿提交）

# --- 数字员工大脑 / 回复（src/custom/brain.py + replier.py）---
# 大脑后端: echo(默认,零依赖) | opencode(本机 opencode run) | proxy(LLM API)
export AGENT_BRAIN="${AGENT_BRAIN:-echo}"
export AGENT_OPENCODE_MODEL="${AGENT_OPENCODE_MODEL:-opencode/deepseek-v4-flash-free}"
# 回复模式: log(默认,只记日志不真发) | bot(机器人 send-by-bot) | user(当前用户 send)
export AGENT_REPLY_MODE="${AGENT_REPLY_MODE:-log}"
# 防回环：数字员工自己的发送名（逗号分隔），过滤掉避免自问自答。
# ⚠️ user 模式：回复以你本人身份发出，必须把你的真实显示名加进来（如 hugozhu）。
export AGENT_SELF_NAMES="${AGENT_SELF_NAMES:-数字员工,Claude Code}"

# --- PATH（部署坑#1）---
# 托管进程 PATH 极简，子进程调 dws/opencode 会找不到。取消注释并按实际路径填：
# export PATH="$PATH:$HOME/.local/bin:$HOME/.opencode/bin"
