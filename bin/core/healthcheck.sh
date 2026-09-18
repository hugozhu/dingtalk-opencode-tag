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
: "${BRAIN_PENDING_FILE:=$SCRIPT_DIR/.brain-fail.pending}"
: "${HEALTHCHECK_BRAIN_CHECK_ENABLED:=1}"
: "${HEALTHCHECK_BRAIN_FAIL_THRESHOLD:=2}"
: "${HEALTHCHECK_BRAIN_PROBE_TIMEOUT:=60}"

# --- 检查8（事件流新鲜度）相关 ---
# 数据源是 monitor.log 的 [agent] inbound 行（event_watcher 每收一条消息必记，
# 跨部署统一，不受 connect 日志无关写入的 mtime 污染）。
: "${MONITOR_LOG:=$SCRIPT_DIR/monitor.log}"
: "${EVENT_FRESHNESS_THRESHOLD:=7200}"   # 秒；0 = 禁用本检查

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
    # 文件 mtime：macOS 用 `stat -f %m`，Linux 用 `stat -c %Y`——两个都试，取到为准
    mtime=$(stat -f %m "$LOG_FILE" 2>/dev/null || stat -c %Y "$LOG_FILE" 2>/dev/null || echo 0)
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
# 触发式设计：只有当「未消失败」（本次窗口新增 + 上次探针通过后攒下的，存
# BRAIN_PENDING_FILE）≥ 阈值时，才真发一次模型调用，健康时一次请求都不发（零 token）。
#
# 为什么必须**跨窗口累计**而不是单窗口计数：低频部署（单聊几小时一条消息）一条消息
# 彻底失败只记 2 条 ok=False（http + cli 回退各一），旧「单窗口条数 ≥ 3」语义在每个
# 检查窗口里最多看到 2 条、阈值永远凑不满——2026-09-17 网关间歇不可达 7 小时，9 条
# 消息全失败（18 行），探针一次没触发，healthcheck 全程每 5 分钟报「健康」。
# 失败证据只被「探针通过」消费（清零），不被时间流逝消费；探针 FAIL/WARN 时证据
# 保留，下个周期 total 仍 ≥ 阈值 → 继续探。默认阈值 2 = 一条消息彻底失败（两行）
# 立刻探针；被 CLI 回退救回的瞬时抖动只记 1 行，攒到第二次才探，不误报。
check_brain() {
    case "$HEALTHCHECK_BRAIN_CHECK_ENABLED" in
        1|true|yes|on) ;;
        *) echo "SKIP: 未启用"; return ;;
    esac

    local out n pending total
    out=$(count_new_matches "$AGENT_OPENCODE_LOG" "$BRAIN_FAIL_OFFSET_FILE" \
                            "$_BRAIN_FAIL_PATTERN" "$consume")
    n=$(echo "$out" | awk '{print $3}')
    [[ "$n" =~ ^[0-9]+$ ]] || n=0
    pending=$(cat "$BRAIN_PENDING_FILE" 2>/dev/null || echo 0)
    [[ "$pending" =~ ^[0-9]+$ ]] || pending=0
    total=$((pending + n))

    if [[ "$total" -lt "$HEALTHCHECK_BRAIN_FAIL_THRESHOLD" ]]; then
        echo "OK: 未消失败 ${total}(<${HEALTHCHECK_BRAIN_FAIL_THRESHOLD})"
        if [[ -n "$consume" ]]; then echo "$total" > "$BRAIN_PENDING_FILE"; fi
        return
    fi

    if ! declare -F brain_probe >/dev/null 2>&1; then
        echo "WARN: 未消失败 ${total} 但未实现 brain_probe 探针"
        if [[ -n "$consume" ]]; then echo "$total" > "$BRAIN_PENDING_FILE"; fi
        return
    fi

    # 三层超时的最外层：探针自身也有 signal.alarm 和 HTTP timeout。
    # 「探针挂了」和「大脑挂了」不能混为一谈，所以宁可多包一层。
    # 把本脚本已解析出的 port/pwd 传下去：否则探针会自己再发现一遍凭据，两边可能指向
    # **不同的 serve 实例**，出现「serve_http 说不通、探针说通」这种自相矛盾的裁决。
    # `|| rc=$?` 防 set -e：探针非零退出时裸的 var=$(cmd) 会当场杀掉整个脚本，
    # FAIL 文案和下面的累计状态持久化全被跳过，只剩一个裸退出码。
    local rc=0 probe_out=""
    probe_out=$(AGENT_PROBE_PORT="$(cat "$SERVE_PORT_FILE" 2>/dev/null || echo "")" \
                AGENT_PROBE_PWD="$(cat "$SERVE_PWD_FILE" 2>/dev/null || echo "")" \
                run_with_timeout "$((HEALTHCHECK_BRAIN_PROBE_TIMEOUT + 15))" brain_probe 2>&1) || rc=$?
    case "$rc" in
        0) echo "OK: 探针通过 (未消失败 ${total})" ;;
        # 无凭据不硬失败：那是 check_serve_http 的地盘，同一个根因报两次只会让消息更难读。
        # 失败证据保留，凭据恢复后自动补探。
        2) echo "WARN: 探针无法运行（serve 凭据缺失，见 serve_http）" ;;
        124) echo "FAIL: 大脑自检超时 (未消失败 ${total})" ;;
        *) echo "FAIL: 大脑自检失败 (未消失败 ${total}, $(printf '%s' "$probe_out" | tail -1 | cut -c1-80))" ;;
    esac
    # 失败证据只有探针通过才清零（上面 rc=0 分支之外都原样保留）。**FAIL 也必须持久化**：
    # 本窗口的 offset 已被 count_new_matches 消费，不写回的话下个周期 total 回落 < 阈值，
    # 大脑还挂着 healthcheck 却报 OK，熔断的连续失败计数会被搅成 OK/FAIL 交替。
    if [[ -n "$consume" ]]; then
        if [[ "$rc" == "0" ]]; then echo 0 > "$BRAIN_PENDING_FILE"
        else echo "$total" > "$BRAIN_PENDING_FILE"; fi
    fi
}

# 检查8: 事件流新鲜度（最后一条入站消息距今多久，硬失败）
#
# 背景（2026-09-18 事故）：dws event 长连接静默失活 2h15m，进程全活、
# check_connect 恒 OK、check_brain 因「没消息进来 = 没失败记录」恒 OK——
# 此前的 7 项检查没有任何一项探测「流是否还在投递」。本检查读 monitor.log
# 最后一条 [agent] inbound 的时间戳补该盲区。
#
# 判 FAIL（→ monitor 全量重启自愈，重建事件流连接）注意：纯本地视角无法
# 区分「投递停滞」与「真静默」（深夜无人发消息）。默认 7200s 取两者分界：
# 有心跳/定时消息的数字员工部署日常静默远小于 2h；真静默误判的代价是一次
# 无害重启（连续 3 次熔断才停服）。低流量部署请调大 EVENT_FRESHNESS_THRESHOLD
# 或置 0 禁用；有 DWS 侧交叉验证条件的部署可用 custom 层零误报完整版
# （DWS 独立拉取确认「服务端有新消息而本地没收到」才动作）。
check_event_freshness() {
    if [[ "${EVENT_FRESHNESS_THRESHOLD:-0}" =~ ^[0-9]+$ ]] \
       && [[ "${EVENT_FRESHNESS_THRESHOLD}" -le 0 ]]; then
        echo "SKIP: 未启用"
        return
    fi
    if [[ ! -f "$MONITOR_LOG" ]]; then
        echo "SKIP: monitor 日志不存在"
        return
    fi
    local line ts epoch age now
    line="$(grep '\[agent\] inbound' "$MONITOR_LOG" 2>/dev/null | tail -1)" || true
    if [[ -z "$line" ]]; then
        echo "SKIP: 尚无入站消息记录"
        return
    fi
    ts="$(printf '%s' "$line" | sed -nE 's/^\[([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})\].*/\1/p')"
    if [[ -z "$ts" ]]; then
        echo "SKIP: 入站行无时间戳"
        return
    fi
    # macOS BSD date / Linux GNU date 双写法（同本文件 stat 的双写法惯例）
    epoch="$(date -j -f '%Y-%m-%d %H:%M:%S' "$ts" +%s 2>/dev/null)" || epoch=""
    if [[ -z "$epoch" ]]; then
        epoch="$(date -d "$ts" +%s 2>/dev/null)" || epoch=""
    fi
    if ! [[ "$epoch" =~ ^[0-9]+$ ]]; then
        echo "SKIP: 时间戳解析失败"
        return
    fi
    now="$(date +%s)"
    age=$(( now - epoch ))
    if (( age > EVENT_FRESHNESS_THRESHOLD )); then
        echo "FAIL: 事件流 ${age}s 无入站消息（阈值 ${EVENT_FRESHNESS_THRESHOLD}s）"
    else
        echo "OK: ${age}s 前有入站"
    fi
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
    local r_connect r_log_activity r_log_fatal r_event_watcher r_serve r_serve_http r_brain r_freshness
    r_connect=$(check_connect)
    r_log_activity=$(check_log_activity)
    r_log_fatal=$(check_log_fatal)
    r_event_watcher=$(check_event_watcher)
    r_serve=$(check_serve)
    r_serve_http=$(check_serve_http)
    # 只有这一项的消息里可能嵌入模型返回的自由文本 → 去掉引号和换行，避免撑坏 JSON 输出
    r_brain=$(check_brain | tr -d '"' | tr '\n' ' ')
    r_freshness=$(check_event_freshness)

    # 判定：硬失败 → 不健康（connect / log_fatal / serve / serve_http / brain / freshness）
    local healthy=1
    local message=""
    local pair key val
    for pair in "connect|$r_connect" "log_fatal|$r_log_fatal" "serve|$r_serve" "serve_http|$r_serve_http" "brain|$r_brain" "freshness|$r_freshness"; do
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
    "brain": "$r_brain",
    "event_freshness": "$r_freshness"
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
            echo "  event_freshness: $r_freshness"
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
