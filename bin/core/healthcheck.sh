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

# 加载可配置常量（真实值在 config/constants.local.sh，被 .gitignore 忽略）——
# 与 monitor.sh 一致，保证单独手动运行时 CONNECT_CHECK_PATTERN 等覆盖也生效
if [[ -f "$SCRIPT_DIR/config/constants.local.sh" ]]; then
    source "$SCRIPT_DIR/config/constants.local.sh"
elif [[ -f "$SCRIPT_DIR/config/constants.sh" ]]; then
    source "$SCRIPT_DIR/config/constants.sh"
fi

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
# serve HTTP 探测的硬超时（秒）。**必须有**：serve 卡死（进程在、不再应答）时，
# 无超时的 curl 会一直阻塞 → healthcheck 永不返回 → monitor 的 run_forever 停摆，
# 失效模式变成「静默」而不是「重启」。
: "${HEALTHCHECK_HTTP_TIMEOUT:=8}"

# --- 检查7（大脑真实自检）相关 ---
# 触发式而非定时：只有当 opencode 失败计数超阈值时才真发一次模型调用，稳态零 token 成本。
# 数据源是 brain._oc_log 的 ok=False 行（失败恒记，不受 AGENT_DEBUG 开关影响）。
: "${AGENT_OPENCODE_LOG:=$SCRIPT_DIR/opencode.log}"
: "${BRAIN_FAIL_OFFSET_FILE:=$SCRIPT_DIR/.opencode-log.offset}"
: "${HEALTHCHECK_BRAIN_CHECK_ENABLED:=1}"
: "${HEALTHCHECK_BRAIN_FAIL_THRESHOLD:=3}"
: "${HEALTHCHECK_BRAIN_PROBE_TIMEOUT:=60}"

# 匹配 _oc_log 的失败行。**必须锚定行首**：AGENT_DEBUG=1 时同一文件里还混着
# `[ts] <<< RESP ... body={...}` 这类整段模型输出，不锚定的话 body 里出现 "ok=False"
# 就能伪造计数。限定 transport=http|cli 也顺带保证探针自身永远喂不回计数器。
_BRAIN_FAIL_PATTERN='^\[[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8}\] transport=(http|cli) .* ok=False'

# 加载 custom 钩子（brain_probe）。两层防护：
#   set +e  —— custom 代码绝不能有能力中止这个守着熔断的检查
#   重定向  —— start_funcs.sh 每次 source 会打一行日志，否则 monitor.log 每周期多一行
set +e
setup_components >/dev/null 2>&1
set -e

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
    # 文件 mtime：GNU %Y / BSD %m。可移植细节与踩坑说明见 lib.sh 的 stat_field。
    mtime=$(stat_field %Y %m "$LOG_FILE")
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
            --connect-timeout 3 --max-time "$HEALTHCHECK_HTTP_TIMEOUT" \
            -H "Authorization: Basic $auth" \
            "http://127.0.0.1:$port/session" 2>/dev/null | grep -q "200"; then
        echo "OK: HTTP $port"
    else
        # 注意：这里必须以 "FAIL" 开头 —— main() 的判定用的是 glob `FAIL*`。
        # 曾经这里返回 "HTTP_FAIL:$port"，匹配不上，导致 serve HTTP 异常
        # **永远无法触发熔断**（2026-08-08 大脑死了 16 分钟仍报「健康」）。
        echo "FAIL: serve HTTP 无响应 (port=$port)"
    fi
}

# 检查7: 大脑真实可用性（硬失败）
#
# 为什么需要它：检查 5/6 只能证明「serve 进程在」「HTTP 监听器会应答」——都不碰模型。
# 2026-08-08 大脑与模型网关失联 16 分钟，这两项全程 OK，任何请求都答不出来。
#
# 触发式设计：只有当「距上次检查以来」新增的失败条数 ≥ 阈值时，才真发一次模型调用。
# 健康时一次请求都不发（零 token），坏了则在一个检查周期内就能被抓到。
check_brain() {
    case "$HEALTHCHECK_BRAIN_CHECK_ENABLED" in
        1|true|yes|on) ;;
        *) echo "SKIP: 未启用"; return ;;
    esac

    local out n
    out=$(count_new_matches "$AGENT_OPENCODE_LOG" "$BRAIN_FAIL_OFFSET_FILE" \
                            "$_BRAIN_FAIL_PATTERN" "$consume")
    n=$(echo "$out" | awk '{print $3}')
    [[ "$n" =~ ^[0-9]+$ ]] || n=0

    if [[ "$n" -lt "$HEALTHCHECK_BRAIN_FAIL_THRESHOLD" ]]; then
        echo "OK: 新增失败 ${n}(<${HEALTHCHECK_BRAIN_FAIL_THRESHOLD})"
        return
    fi

    if ! declare -F brain_probe >/dev/null 2>&1; then
        echo "WARN: 新增失败 ${n} 但未实现 brain_probe 探针"
        return
    fi

    # 三层超时的最外层：探针自身也有 signal.alarm 和 HTTP timeout。
    # 「探针挂了」和「大脑挂了」不能混为一谈，所以宁可多包一层。
    # 把本脚本已解析出的 port/pwd 传下去：否则探针会自己再发现一遍凭据，两边可能指向
    # **不同的 serve 实例**，出现「serve_http 说不通、探针说通」这种自相矛盾的裁决。
    local rc probe_out
    probe_out=$(AGENT_PROBE_PORT="$(cat "$SERVE_PORT_FILE" 2>/dev/null || echo "")" \
                AGENT_PROBE_PWD="$(cat "$SERVE_PWD_FILE" 2>/dev/null || echo "")" \
                run_with_timeout "$((HEALTHCHECK_BRAIN_PROBE_TIMEOUT + 15))" brain_probe 2>&1)
    rc=$?
    case "$rc" in
        0) echo "OK: 探针通过 (新增失败 ${n})" ;;
        # 无凭据不硬失败：那是 check_serve_http 的地盘，同一个根因报两次只会让消息更难读
        2) echo "WARN: 探针无法运行（serve 凭据缺失，见 serve_http）" ;;
        124) echo "FAIL: 大脑自检超时 (新增失败 ${n})" ;;
        *) echo "FAIL: 大脑自检失败 (新增失败 ${n}, $(printf '%s' "$probe_out" | tail -1 | cut -c1-80))" ;;
    esac
}

# 是否消费「距上次检查以来」的计数窗口（--consume）。**默认 peek 不写状态**：
# 本脚本还被 startup_report 和多个 e2e 当门禁调用，若它们也消费窗口，会把真实失败
# 对下一次 monitor 检查静默掩盖。只有 monitor 的守护循环传 --consume。
consume=""

# 主流程
main() {
    local verbose=""
    local json=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --verbose) verbose="1" ;;
            --json)    json="1" ;;
            --consume) consume="1" ;;
        esac
        shift
    done

    # 跑所有检查
    # 注意：用普通变量而非关联数组（declare -A）——macOS 自带 /bin/bash 是 3.2，
    # 不支持关联数组，monitor.sh 经 /bin/bash 调本脚本会 declare 报错、set -e 退出，
    # 导致 monitor 误判"不健康"进入全量重启/熔断循环。保持 bash 3.2 兼容。
    local r_connect r_log_activity r_log_fatal r_event_watcher r_serve r_serve_http r_brain
    r_connect=$(check_connect)
    r_log_activity=$(check_log_activity)
    r_log_fatal=$(check_log_fatal)
    r_event_watcher=$(check_event_watcher)
    r_serve=$(check_serve)
    r_serve_http=$(check_serve_http)
    # 只有这一项的消息里可能嵌入模型返回的自由文本 → 去掉引号和换行，避免撑坏 JSON 输出
    r_brain=$(check_brain | tr -d '"' | tr '\n' ' ')

    # 判定：硬失败 → 不健康（connect / log_fatal / serve / serve_http / brain）
    local healthy=1
    local message=""
    local pair key val
    for pair in "connect|$r_connect" "log_fatal|$r_log_fatal" "serve|$r_serve" "serve_http|$r_serve_http" "brain|$r_brain"; do
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
    "serve_http": "$r_serve_http",
    "brain": "$r_brain"
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
            echo "  brain: $r_brain"
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
