#!/bin/bash
# constants.sh — 可配置常量模板
#
# 用户在这里覆盖所有可配置常量，被 bin/core/*.sh 引用
# 复制本文件为 constants.local.sh（被 .gitignore 忽略）填入真实值

# --- 数字员工身份 ---
export AGENT_ROBOT_CODE="${AGENT_ROBOT_CODE:-your-robot-code}"
export AGENT_USER_ID="${AGENT_USER_ID:-your-user-id}"
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
