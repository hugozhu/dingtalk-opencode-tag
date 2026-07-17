#!/bin/bash
# healthcheck.sh — N 项健康检查模板
#
# 提炼自: dingtalk-opencode-agent/healthcheck.sh (v4.1)
# 原作者: hugozhu
#
# 检查分级:
#   - 硬失败: 进程死了 / serve HTTP 无响应 → 不健康，触发全量重启
#   - 仅告警: 日志活跃度 / 非关键子组件 → 不健康，记日志但不触发重启
#
# 输出 JSON: {"healthy": 0/1, "message": "...", "checks": {...}}
# 退出码: 0 = 健康, 1 = 不健康

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$SCRIPT_DIR/bin/core/lib.sh"

COMPONENT_NAME="healthcheck"

# 加载组件配置
: "${CONNECT_PID_FILE:=$SCRIPT_DIR/.connect.pid}"
: "${SERVE_WATCHER_PID_FILE:=$SCRIPT_DIR/.serve-watcher.pid}"
: "${EVENT_WATCHER_PID_FILE:=$SCRIPT_DIR/.event-watcher.pid}"
: "${SERVE_PID_FILE:=$SCRIPT_DIR/.serve.pid}"
: "${SERVE_PORT_FILE:=$SCRIPT_DIR/.serve.port}"
: "${SERVE_PWD_FILE:=$SCRIPT_DIR/.serve.pwd}"
: "${LOG_FILE:=$SCRIPT_DIR/agent-connect.log}"
: "${LOG_INACTIVITY_THRESHOLD:=2100}"   # 日志活跃度阈值（秒，35 分钟）

# 检查1: connect 进程存活（硬失败）
check_connect() {
    if verify_pid "$CONNECT_PID_FILE" "dws-connect.sh"; then
        echo "OK"
    else
        echo "FAIL: connect 进程不存活"
    fi
}

# 检查2: 日志活跃度（仅告警，35 分钟内有活动）
check_log_activity() {
    if [[ ! -f "$LOG_FILE" ]]; then
        echo "WARN: 日志文件不存在"
        return
    fi
    local now mtime diff
    now=$(date +%s)
    # 跨平台取文件 mtime（epoch）：Linux GNU stat -c %Y / macOS BSD stat -f %m
    if [[ "$(harness_os)" == macos ]]; then
        mtime=$(stat -f %m "$LOG_FILE" 2>/dev/null || echo 0)
    else
        mtime=$(stat -c %Y "$LOG_FILE" 2>/dev/null || echo 0)
    fi
    diff=$((now - mtime))
    if [[ "$diff" -gt "$LOG_INACTIVITY_THRESHOLD" ]]; then
        echo "WARN: 日志 ${diff}s 无活动"
    else
        echo "OK: ${diff}s 前有活动"
    fi
}

# 检查3: 日志尾部是否有未恢复的致命错误（硬失败）
check_log_fatal() {
    if [[ ! -f "$LOG_FILE" ]]; then
        echo "SKIP: 日志文件不存在"
        return
    fi
    if tail -100 "$LOG_FILE" | grep -E "FATAL|panic:|fatal error" >/dev/null 2>&1; then
        echo "FAIL: 日志尾部有致命错误"
    else
        echo "OK"
    fi
}

# 检查4: event-watcher 进程活跃（仅告警）
check_event_watcher() {
    if verify_pid "$EVENT_WATCHER_PID_FILE" "event_watcher.py"; then
        echo "OK"
    else
        echo "WARN: event-watcher 不活跃"
    fi
}

# 检查5: serve 进程存活（硬失败）
check_serve() {
    if [[ -f "$SERVE_PID_FILE" ]] && kill -0 "$(cat "$SERVE_PID_FILE")" 2>/dev/null; then
        echo "OK"
    else
        echo "FAIL: serve 进程不存活"
    fi
}

# 检查6: serve HTTP /session 响应（硬失败，凭据自刷新）
check_serve_http() {
    local port pwd
    port=$(cat "$SERVE_PORT_FILE" 2>/dev/null || echo "")
    pwd=$(cat "$SERVE_PWD_FILE" 2>/dev/null || echo "")
    if [[ -z "$port" || -z "$pwd" ]]; then
        echo "FAIL: serve 凭据缺失"
        return
    fi
    local auth
    auth=$(echo -n "opencode:$pwd" | base64)
    if curl -s -o /dev/null -w "%{http_code}" \
            -H "Authorization: Basic $auth" \
            "http://127.0.0.1:$port/session" 2>/dev/null | grep -q "200"; then
        echo "HTTP_OK:$port"
    else
        # 凭据失效时尝试从进程表 + 日志刷新
        echo "HTTP_FAIL:$(cat "$SERVE_PORT_FILE" 2>/dev/null || echo '000')"
    fi
}

# 主流程
main() {
    local verbose=""
    local json=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --verbose) verbose="1" ;;
            --json)    json="1" ;;
        esac
        shift
    done

    # 跑所有检查
    declare -A results
    results[connect]=$(check_connect)
    results[log_activity]=$(check_log_activity)
    results[log_fatal]=$(check_log_fatal)
    results[event_watcher]=$(check_event_watcher)
    results[serve]=$(check_serve)
    results[serve_http]=$(check_serve_http)

    # 判定：硬失败 → 不健康
    local healthy=1
    local message=""
    for key in connect log_fatal serve serve_http; do
        if [[ "${results[$key]}" == FAIL* ]]; then
            healthy=0
            message="$message $key=${results[$key]}"
        fi
    done
    if [[ -z "$message" ]]; then
        message="健康"
    fi

    if [[ -n "$json" ]]; then
        cat <<EOF
{
  "healthy": $healthy,
  "message": "$message",
  "checks": {
    "connect": "${results[connect]}",
    "log_activity": "${results[log_activity]}",
    "log_fatal": "${results[log_fatal]}",
    "event_watcher": "${results[event_watcher]}",
    "serve": "${results[serve]}",
    "serve_http": "${results[serve_http]}"
  }
}
EOF
    else
        [[ -n "$verbose" ]] && for key in "${!results[@]}"; do
            echo "  $key: ${results[$key]}"
        done
        if [[ "$healthy" == "1" ]]; then
            echo "✅ 健康"
        else
            echo "❌ 不健康: $message"
        fi
    fi

    [[ "$healthy" == "1" ]]
}

main "$@"
