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
: "${WATCHER_PID_FILE:=$SCRIPT_DIR/.watcher.pid}"
: "${EVENT_WATCHER_PID_FILE:=$SCRIPT_DIR/.event-watcher.pid}"
: "${SERVE_PID_FILE:=$SCRIPT_DIR/.serve.pid}"
: "${SERVE_PORT_FILE:=$SCRIPT_DIR/.serve.port}"
: "${SERVE_PWD_FILE:=$SCRIPT_DIR/.serve.pwd}"
: "${LOG_FILE:=$SCRIPT_DIR/agent-connect.log}"
: "${LOG_INACTIVITY_THRESHOLD:=2100}"   # 日志活跃度阈值（秒，35 分钟）
# 进程 cmdline 匹配模式（verify_pid 用，字面子串匹配）。FDE 换了 connect/event_watcher
# 的实现时，在 config/constants.local.sh 覆盖这两个，否则默认模式匹配不到自定义进程、
# 健康检查恒失败。默认值对应 harness 自带实现（dws dev connect / event_watcher.py）。
: "${CONNECT_CHECK_PATTERN:=agent-connect.*--unified-app-id}"
: "${EVENT_WATCHER_CHECK_PATTERN:=event_watcher.py}"

# 检查1: connect 进程存活（硬失败）
check_connect() {
    if verify_pid "$CONNECT_PID_FILE" "$CONNECT_CHECK_PATTERN"; then
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
    mtime=$(stat -f %m "$LOG_FILE" 2>/dev/null || echo 0)
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
    if verify_pid "$EVENT_WATCHER_PID_FILE" "$EVENT_WATCHER_CHECK_PATTERN"; then
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
    # 注意：用普通变量而非关联数组（declare -A）——macOS 自带 /bin/bash 是 3.2，
    # 不支持关联数组，monitor.sh 经 /bin/bash 调本脚本会 declare 报错、set -e 退出，
    # 导致 monitor 误判"不健康"进入全量重启/熔断循环。保持 bash 3.2 兼容。
    local r_connect r_log_activity r_log_fatal r_event_watcher r_serve r_serve_http
    r_connect=$(check_connect)
    r_log_activity=$(check_log_activity)
    r_log_fatal=$(check_log_fatal)
    r_event_watcher=$(check_event_watcher)
    r_serve=$(check_serve)
    r_serve_http=$(check_serve_http)

    # 判定：硬失败 → 不健康（connect / log_fatal / serve / serve_http）
    local healthy=1
    local message=""
    local pair key val
    for pair in "connect|$r_connect" "log_fatal|$r_log_fatal" "serve|$r_serve" "serve_http|$r_serve_http"; do
        key="${pair%%|*}"
        val="${pair#*|}"
        if [[ "$val" == FAIL* ]]; then
            healthy=0
            message="$message $key=$val"
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
    "connect": "$r_connect",
    "log_activity": "$r_log_activity",
    "log_fatal": "$r_log_fatal",
    "event_watcher": "$r_event_watcher",
    "serve": "$r_serve",
    "serve_http": "$r_serve_http"
  }
}
EOF
    else
        if [[ -n "$verbose" ]]; then
            echo "  connect: $r_connect"
            echo "  log_activity: $r_log_activity"
            echo "  log_fatal: $r_log_fatal"
            echo "  event_watcher: $r_event_watcher"
            echo "  serve: $r_serve"
            echo "  serve_http: $r_serve_http"
        fi
        if [[ "$healthy" == "1" ]]; then
            echo "✅ 健康"
        else
            echo "❌ 不健康: $message"
        fi
    fi

    [[ "$healthy" == "1" ]]
}

main "$@"
