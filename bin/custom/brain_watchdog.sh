#!/bin/bash
# brain_watchdog.sh — 大脑 serve 连接看门狗（零 token 差分检测 + 自愈）
#
# 注册为第 5 个组件（bin/custom/start_funcs.sh 末尾 COMP_NAMES+=），monitor 兜底拉起。
#
# 背景（2026-09-21 事故：08:01–10:11 大脑双路全挂 2h+）：
#   模型网关（内网 IP，走 VPN 路由）夜间闪断。恢复后 curl 直连 2.7ms 通、全新 CLI
#   进程 6s 出结果，但常驻 serve 仍持续 `Cannot connect to API`——闪断期间 serve
#   出站连接池被毒化（Bun 复用半开死连接，毫秒级快速失败），**网络恢复后不会自愈**。
#   期间每条消息「serve HTTP 失败 → CLI 回退」：网关断时 CLI 也挂 → 全发兜底提示；
#   check_brain 探针只告警不修复。重启 serve（reboot.sh）即恢复。
#
# 判据（差分，全部寄生在真实业务流量上，零 token 零额外模型请求）：
#   monitor.log 无条件记录的三个标记（brain.py 保证错误恒记）：
#     - `brain opencode http err:`        serve HTTP 路失败（每条消息先走这条路）
#     - `brain(opencode): CLI 回退失败：`  CLI 全新一次性子进程也失败
#     - `brain(opencode): CLI 回退成功`    CLI 成功（= 网关此刻对全新进程可达）
#   「http err + CLI 回退成功」在窗口内成对出现 ≥ N 次 = **serve 单侧故障实锤**
#   （CLI 是全新进程+全新连接，它能通就证明网关通，只有 serve 自己连不上）。
#   辅助差分（零 token）：curl 网关 baseURL/models，任何 HTTP 状态码（含 401）=
#   可达，000/超时 = 不可达。仅用于「双路全挂」时的告警分级，不作为重启判据。
#
# 动作：
#   - serve 单侧故障（≥ BRAIN_SERVE_MIN_PAIRS 对新鲜证据）：
#       告警主管 + **只重启 serve**（不动 connect/订阅，比整机 reboot 轻；复用
#       custom start_serve：.serve.pwd 保持稳定，in-flight 请求 401/拒连 → brain
#       自动走 CLI 兜底，用户仍能收到回复）
#   - 双路全挂：不重启（重启无效——CLI 全新进程也连不上说明是网关/网络层问题），
#       curl 差分分级告警：网关不可达（等恢复）/ opencode 外连异常（人工检查）。
#       网关恢复后的下一条消息自然产生「CLI 回退成功」→ 转入上面的自愈路径。
#
# 防抖（状态文件 .brain-serve-stall.state；不在 clean_runtime_state 清理表里，
# reboot 后存活，防重启风暴）：
#   距上次自愈动作 < BRAIN_SERVE_RETRY_INTERVAL → 只告警不重启
#   累计自愈 ≥ BRAIN_SERVE_MAX_RESTARTS 仍异常 → 只告警，等人工
#   窗口内无 http err（serve 健康/无流量）→ 计数清零
#
# 配置（config/constants.local.sh 可覆盖，均有默认值）：
#   BRAIN_SERVE_WATCHDOG        开关（start_funcs.sh 判定，默认开）
#   BRAIN_SERVE_CHECK_INTERVAL  轮询间隔秒（默认 300）
#   BRAIN_SERVE_PAIR_WINDOW     证据计数窗口秒（默认 3600）
#   BRAIN_SERVE_FRESH_WINDOW    触发重启要求最近一对证据的年龄上限秒（默认 900，
#                               防止拿陈旧证据重启一个其实已被修好的 serve）
#   BRAIN_SERVE_MIN_PAIRS       触发重启所需「CLI 回退成功」对数（默认 2，零误报）
#   BRAIN_SERVE_RETRY_INTERVAL  自愈/告警动作最小间隔秒（默认 1800）
#   BRAIN_SERVE_MAX_RESTARTS    一次异常期内自动重启 serve 上限（默认 3）
#   BRAIN_GATEWAY_URL           网关探测 URL（缺省自动发现 opencode 配置里第一个
#                               baseURL 并补 /models；显式配置优先生效）
#
# 单测：tests/custom/test_brain_watchdog.sh（纯 env 驱动，mock _bw_now /
# _bw_send_alert / _bw_restart_serve / _bw_gateway_ok，不碰真实配置/网络/serve）。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# 加载本地配置（与 event_freshness_watchdog.sh 同款兜底；BRAIN_WATCHDOG_SKIP_LOCAL=1
# 供单测跳过）
if [[ -z "${BRAIN_WATCHDOG_SKIP_LOCAL:-}" && -f "$SCRIPT_DIR/config/constants.local.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config/constants.local.sh"
fi

: "${BRAIN_SERVE_CHECK_INTERVAL:=300}"
: "${BRAIN_SERVE_PAIR_WINDOW:=3600}"
: "${BRAIN_SERVE_FRESH_WINDOW:=900}"
: "${BRAIN_SERVE_MIN_PAIRS:=2}"
: "${BRAIN_SERVE_RETRY_INTERVAL:=1800}"
: "${BRAIN_SERVE_MAX_RESTARTS:=3}"
: "${BRAIN_GATEWAY_URL:=}"
: "${BRAIN_GATEWAY_TIMEOUT:=4}"
: "${MONITOR_LOG:=$SCRIPT_DIR/monitor.log}"
: "${BRAIN_SERVE_STATE:=$SCRIPT_DIR/.brain-serve-stall.state}"
: "${DWS_EVENT_O2O_USERS:=}"
: "${DWS_PROFILE:=}"

_bw_log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [brain-wd] $*" >> "$MONITOR_LOG" 2>/dev/null || true
}

# 当前 epoch（单测覆盖此函数注入固定时间）
_bw_now() { date +%s; }

# 扫描 monitor.log 的差分标记。输出 "poisoned\tcli_fail\tlast_http\tlast_poison"：
#   poisoned  窗口内「CLI 回退成功」条数（= serve 失败且 CLI 通的配对数）
#   cli_fail  窗口内「CLI 回退失败」条数
#   last_http 最后一条 http err 的 epoch（0=无记录；不受窗口限制，供新鲜度判定）
#   last_poison 最后一条「CLI 回退成功」的 epoch（0=无）
# 日志缺失/解析失败由 _bw_check_once 的正则守卫兜成全 0（unknown）。
_bw_scan() {
    local now="$1" window="$2"
    # 注意：grep 无匹配退出码 1（pipefail 会传导），故不挂 || 兜底输出（会追加
    # 第二行污染 read）；python 对空输入天然输出全 0。
    grep -aE 'brain opencode http err|CLI 回退失败|CLI 回退成功' "$MONITOR_LOG" 2>/dev/null | tail -400 | \
    NOW="$now" WINDOW="$window" \
    python3 -c '
import sys, os, re, time
now = int(os.environ.get("NOW", "0"))
window = int(os.environ.get("WINDOW", "3600"))
ts_re = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
poisoned = cli_fail = last_http = last_poison = 0
for line in sys.stdin:
    m = ts_re.match(line)
    if not m:
        continue
    try:
        ts = int(time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")))
    except ValueError:
        continue
    if "brain opencode http err" in line:
        last_http = ts
    elif "CLI 回退成功" in line:
        # CLI 只在 serve 失败后才会被调用，成功即构成一对差分证据
        last_poison = ts
        if now - ts <= window:
            poisoned += 1
    elif "CLI 回退失败" in line:
        if now - ts <= window:
            cli_fail += 1
print("%d\t%d\t%d\t%d" % (poisoned, cli_fail, last_http, last_poison))
' 2>/dev/null
}

# 网关探测 URL（显式 BRAIN_GATEWAY_URL 优先；否则从 opencode 配置发现第一个
# baseURL，自动补 /models。结果按进程缓存）。空 = 未配置（探测返回 2 unknown）。
_BW_GW_URL=""
_bw_gateway_url() {
    if [[ -n "$BRAIN_GATEWAY_URL" ]]; then
        echo "$BRAIN_GATEWAY_URL"; return 0
    fi
    if [[ -n "$_BW_GW_URL" ]]; then
        echo "$_BW_GW_URL"; return 0
    fi
    local f url
    for f in "$HOME/.config/opencode/opencode.json" "$HOME/.config/opencode/opencode.jsonc" \
             "$SCRIPT_DIR/opencode.json" "$SCRIPT_DIR/opencode.jsonc"; do
        [[ -f "$f" ]] || continue
        url="$(grep -oE '"baseURL"[[:space:]]*:[[:space:]]*"[^"]+"' "$f" 2>/dev/null | head -1 \
               | sed -E 's/.*"baseURL"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')" || true
        [[ -n "$url" ]] && { _BW_GW_URL="${url%/}/models"; echo "$_BW_GW_URL"; return 0; }
    done
    return 0
}

# 网关直连探测（零 token：只到路由/鉴权层，不产生推理）。返回码：
#   0 = 可达（任何 HTTP 状态码，含 401/404）；1 = 不可达（000/超时）；2 = 未配置
_bw_gateway_ok() {
    local url status
    url="$(_bw_gateway_url)"
    [[ -z "$url" ]] && return 2
    status="$(curl -s -m "$BRAIN_GATEWAY_TIMEOUT" -o /dev/null -w '%{http_code}' \
              "$url" 2>/dev/null || echo 000)"
    [[ -n "$status" && "$status" != "000" ]] && return 0
    return 1
}

# 防抖状态（三行：last_action_epoch / last_alert_epoch / restart_count）
_BW_LAST_ACTION=0
_BW_LAST_ALERT=0
_BW_RESTART_COUNT=0
_bw_state_load() {
    _BW_LAST_ACTION=0; _BW_LAST_ALERT=0; _BW_RESTART_COUNT=0
    [[ -f "$BRAIN_SERVE_STATE" ]] || return 0
    local l1 l2 l3
    l1="$(sed -n '1p' "$BRAIN_SERVE_STATE" 2>/dev/null)" || true
    l2="$(sed -n '2p' "$BRAIN_SERVE_STATE" 2>/dev/null)" || true
    l3="$(sed -n '3p' "$BRAIN_SERVE_STATE" 2>/dev/null)" || true
    [[ "$l1" =~ ^[0-9]+$ ]] && _BW_LAST_ACTION="$l1"
    [[ "$l2" =~ ^[0-9]+$ ]] && _BW_LAST_ALERT="$l2"
    [[ "$l3" =~ ^[0-9]+$ ]] && _BW_RESTART_COUNT="$l3"
}
_bw_state_save() {
    printf '%s\n%s\n%s\n' "$_BW_LAST_ACTION" "$_BW_LAST_ALERT" "$_BW_RESTART_COUNT" \
        > "$BRAIN_SERVE_STATE" 2>/dev/null || true
}

# 告警（单测覆盖此函数 mock）。必须永不失败（看门狗不能被通知通道故障杀死）。
_bw_send_alert() {
    local text="$1"
    [[ -n "${DWS_PROFILE:-}" && -n "${DWS_EVENT_O2O_USERS:-}" ]] || return 0
    local to="${DWS_EVENT_O2O_USERS%%,*}"
    [[ -n "$to" ]] || return 0
    command -v dws >/dev/null 2>&1 || return 0
    dws chat message send --user "$to" --text "$text" \
        --profile "$DWS_PROFILE" -y >/dev/null 2>&1 || true
    return 0
}

# 发告警（带冷却：距上次告警 >= BRAIN_SERVE_RETRY_INTERVAL 才发）。返回 1 = 冷却期内跳过。
_bw_alert() {
    local text="$1" now="$2"
    if (( _BW_LAST_ALERT > 0 && (now - _BW_LAST_ALERT) < BRAIN_SERVE_RETRY_INTERVAL )); then
        return 1
    fi
    _BW_LAST_ALERT="$now"
    _bw_send_alert "$text"
    return 0
}

# 重启 serve（单测覆盖此函数 mock）。只动 serve，不动 connect/订阅。
# verify_pid 防 PID 复用误杀；复用 canonical 启动链（setup_components → custom
# start_serve）：.serve.pwd 保持稳定（in-flight 401 风险最小）、AGENT_DEBUG 日志参数一致。
_bw_restart_serve() {
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/bin/core/lib.sh"
    local pid
    if verify_pid "$SCRIPT_DIR/.serve.pid" "opencode serve"; then
        pid="$(cat "$SCRIPT_DIR/.serve.pid" 2>/dev/null || true)"
        kill_tree "$pid" TERM 2>/dev/null || true
        sleep 3
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill_tree "$pid" KILL 2>/dev/null || true
        fi
    fi
    rm -f "$SCRIPT_DIR/.serve.pid" 2>/dev/null || true
    # shellcheck disable=SC1091
    setup_components
    start_serve
    return 0
}

# 主判定（单测核心）。返回状态字符串（stdout）：
#   unknown          日志无 http 失败记录（新部署/日志轮转），静默等待
#   ok               窗口内无 http 失败（serve 健康/无流量），计数清零
#   suspect          有 1 对差分证据或证据陈旧：告警观察，不重启
#   poisoned_reboot  serve 单侧故障实锤：告警 + 重启 serve 自愈
#   poisoned_wait    实锤但处于自愈冷却期：仅告警
#   poisoned_giveup  实锤且已达自愈上限：仅告警，等人工
#   gateway_down     双路全挂 + curl 网关不可达：仅告警（重启无效，等网关恢复）
#   opencode_net     双路全挂 + curl 网关可达：仅告警（opencode 外连异常，人工检查）
#   both_fail        双路全挂 + 网关探测未配置：仅告警
#   *_quiet          同上但处于告警冷却期（跳过告警）
_bw_check_once() {
    local now="$(_bw_now)"
    local res poisoned cli_fail last_http last_poison
    res="$(_bw_scan "$now" "$BRAIN_SERVE_PAIR_WINDOW")"
    IFS=$'\t' read -r poisoned cli_fail last_http last_poison <<< "$res"
    [[ "$poisoned" =~ ^[0-9]+$ ]] || poisoned=0
    [[ "$cli_fail" =~ ^[0-9]+$ ]] || cli_fail=0
    [[ "$last_http" =~ ^[0-9]+$ ]] || last_http=0
    [[ "$last_poison" =~ ^[0-9]+$ ]] || last_poison=0

    if (( last_http == 0 )); then
        echo "unknown"; return 0
    fi

    local http_age=$(( now - last_http ))
    if (( http_age > BRAIN_SERVE_PAIR_WINDOW )); then
        _bw_state_load
        if (( _BW_RESTART_COUNT > 0 )); then
            _BW_RESTART_COUNT=0; _BW_LAST_ACTION=0; _BW_LAST_ALERT=0
            _bw_state_save
            _bw_log "大脑 serve 近 ${BRAIN_SERVE_PAIR_WINDOW}s 无失败记录，看门狗计数清零"
        fi
        echo "ok"; return 0
    fi

    # ---- serve 单侧故障（CLI 回退成功 = 网关对全新进程可达，只有 serve 连不上）----
    local poison_age=$(( last_poison > 0 ? now - last_poison : 999999999 ))
    if (( poisoned >= BRAIN_SERVE_MIN_PAIRS && poison_age <= BRAIN_SERVE_FRESH_WINDOW )); then
        _bw_state_load
        if (( _BW_RESTART_COUNT >= BRAIN_SERVE_MAX_RESTARTS )); then
            if _bw_alert "🚨 数字员工大脑 serve 连接异常持续（窗口内 ${poisoned} 次 CLI 回退成功佐证），已自动重启 ${_BW_RESTART_COUNT} 次仍未恢复，暂停自愈等人工。" "$now"; then
                _bw_state_save
            fi
            _bw_log "serve 单侧故障 evidence=${poisoned}：已达自愈上限(${BRAIN_SERVE_MAX_RESTARTS})，仅告警"
            echo "poisoned_giveup"; return 0
        fi
        if (( _BW_LAST_ACTION > 0 && (now - _BW_LAST_ACTION) < BRAIN_SERVE_RETRY_INTERVAL )); then
            if _bw_alert "🚨 数字员工大脑 serve 连接异常持续（${poisoned} 次 CLI 回退成功佐证），自动重启冷却中（${BRAIN_SERVE_RETRY_INTERVAL}s 内不重复重启）。期间消息由 CLI 兜底回复。" "$now"; then
                _bw_state_save
            fi
            _bw_log "serve 单侧故障 evidence=${poisoned}：自愈冷却期内，仅告警"
            echo "poisoned_wait"; return 0
        fi
        _BW_RESTART_COUNT=$(( _BW_RESTART_COUNT + 1 ))
        _BW_LAST_ACTION="$now"
        _BW_LAST_ALERT="$now"
        _bw_state_save
        _bw_log "serve 单侧故障 evidence=${poisoned}：告警 + 重启 serve（第 ${_BW_RESTART_COUNT}/${BRAIN_SERVE_MAX_RESTARTS} 次）"
        _bw_send_alert "🚨 数字员工大脑 serve 连接异常（${poisoned} 次 CLI 回退成功佐证网关可达，serve 出站连接池疑似毒化），正在自动重启 serve 自愈（第 ${_BW_RESTART_COUNT} 次，上限 ${BRAIN_SERVE_MAX_RESTARTS}）。期间消息由 CLI 兜底回复。"
        _bw_restart_serve
        echo "poisoned_reboot"; return 0
    fi

    # ---- 证据不足（仅 1 对）：告警观察，不重启 ----
    # 要求证据新鲜（最近毒化对或最近 http 失败任一在 FRESH_WINDOW 内）：全部陈旧时
    # 走下面的恢复判定，防止 reboot/看门狗重启后拿旧事故刷屏（2026-09-21 首启实测踩到）
    local http_fresh=$(( now - last_http <= BRAIN_SERVE_FRESH_WINDOW ? 1 : 0 ))
    if (( poisoned >= 1 && (poison_age <= BRAIN_SERVE_FRESH_WINDOW || http_fresh) )); then
        _bw_state_load
        if _bw_alert "⚠️ 数字员工大脑 serve 疑似连接异常（CLI 回退成功 ${poisoned} 次），继续观察中（新鲜证据达 ${BRAIN_SERVE_MIN_PAIRS} 次将自动重启 serve）。" "$now"; then
            _bw_state_save
            _bw_log "疑似 serve 单侧故障 evidence=${poisoned}：证据不足，告警观察"
            echo "suspect"; return 0
        fi
        _bw_log "疑似 serve 单侧故障 evidence=${poisoned}：证据不足，告警冷却期内跳过"
        echo "suspect_quiet"; return 0
    fi

    # ---- 证据陈旧（最近失败超出新鲜窗口）----
    # curl 独立确证网关状态（不依赖流量）：仍不可达 → 持续告警；可达 → 视为已恢复
    # （旧故障已过，若有新失败会刷新证据重新进上面的分支），静默。
    if (( ! http_fresh )); then
        local gw0=0
        _bw_gateway_ok; gw0=$?
        if (( gw0 == 1 )); then
            _bw_state_load
            if _bw_alert "⚠️ 模型网关仍不可达（curl 独立确证；最近失败证据 ${http_age}s 前），数字员工大脑降级中。" "$now"; then
                _bw_state_save
                _bw_log "失败证据陈旧(age=${http_age}s)且网关不可达：仍告警"
                echo "gateway_down_stale"; return 0
            fi
            _bw_log "失败证据陈旧(age=${http_age}s)且网关不可达：告警冷却期内跳过"
            echo "gateway_down_stale_quiet"; return 0
        fi
        _bw_log "失败证据陈旧(age=${http_age}s)且网关可达(gw_rc=${gw0})：视为已恢复，静默"
        echo "recovered"; return 0
    fi

    # ---- 双路全挂（新鲜证据）：重启无效，curl 差分分级告警 ----
    local gw=0 msg st
    _bw_gateway_ok; gw=$?
    case "$gw" in
        0)  msg="⚠️ 数字员工大脑 serve 与 CLI 双路全挂，但网关 curl 可达（opencode 外连异常，重启 serve 无效），请人工检查代理/DNS/防火墙。若网关刚恢复，下一条消息将自动触发 serve 自愈。"
            st="opencode_net" ;;
        1)  msg="⚠️ 模型网关不可达（curl 失败），数字员工大脑降级 CLI/兜底提示中，自动重启无效。网关恢复后下一条消息将自动触发 serve 自愈。"
            st="gateway_down" ;;
        *)  msg="⚠️ 数字员工大脑 serve 与 CLI 双路全挂（BRAIN_GATEWAY_URL 未配置，无法差分定位网关），请人工检查。"
            st="both_fail" ;;
    esac
    _bw_state_load
    if _bw_alert "$msg" "$now"; then
        _bw_state_save
        _bw_log "双路全挂 gw_rc=${gw}：${st}，仅告警（重启无效）"
        echo "$st"; return 0
    fi
    _bw_log "双路全挂 gw_rc=${gw}：${st}，告警冷却期内跳过"
    echo "${st}_quiet"; return 0
}

# 可被信号打断的 sleep（5s 小块，与 monitor.sh 同款：TERM 最多延迟 ~5s 生效）
_bw_interruptible_sleep() {
    local remaining="$1"
    local step=5
    while (( remaining > 0 )); do
        (( remaining < step )) && step="$remaining"
        sleep "$step"
        remaining=$(( remaining - step ))
    done
}

bw_main_loop() {
    _bw_log "大脑看门狗启动: interval=${BRAIN_SERVE_CHECK_INTERVAL}s window=${BRAIN_SERVE_PAIR_WINDOW}s fresh=${BRAIN_SERVE_FRESH_WINDOW}s min_pairs=${BRAIN_SERVE_MIN_PAIRS} retry=${BRAIN_SERVE_RETRY_INTERVAL}s max_restarts=${BRAIN_SERVE_MAX_RESTARTS}"
    trap 'exit 0' TERM INT
    while :; do
        _bw_check_once >/dev/null 2>&1 || true
        _bw_interruptible_sleep "$BRAIN_SERVE_CHECK_INTERVAL"
    done
}

if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
    bw_main_loop
fi
