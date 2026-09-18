#!/bin/bash
# event_freshness_watchdog.sh — 事件流新鲜度看门狗（投递停滞检测 + 自愈）
#
# 由 dws-connect.sh 拉起（生命周期与 connect 组件一致：connect 活着它就活着。
# 投递停滞时 connect 相关进程恰恰全都活着——这正是 healthcheck 的盲区）。
#
# 背景（2026-09-18 事故：11:22–13:37 投递停滞 2h15m，无告警无自愈）：
#   dws event consume 底层长连接静默失活时进程全活 → check_connect（只查进程存活）
#   恒 OK；check_log_activity 的 WARN 三重静默（不算硬失败 / 不进 monitor 日志 /
#   无通知通道）；check_brain 只探测「调了但失败」——没消息进来 = 没失败记录 = 恒 OK。
#   7 项健康检查没有任何一项探测「事件流新鲜度」，本看门狗补这个盲区。
#
# 判据（两级，零误报）：
#   1. monitor.log 最后一条 "[agent] inbound" 距今 > EVENT_STALL_THRESHOLD（默认
#      2700s=45min）→ 疑似停滞（仅本地视角；深夜真没人发消息也会触发，先不动）
#   2. DWS 独立拉取交叉验证（不依赖事件流本身，e2e 校验 B 同款思路）：
#      dws chat message list --group <订阅群> --time <最后入站时刻> --direction newer，
#      过滤掉「数字员工自己发的（AGENT_SELF_NAMES）」和「本地已入站过的（msgId 去重）」，
#      仍有剩余 = 服务端确有本地未收到的他人消息 → 确凿投递停滞
#      - 拉到 0 条 → 真静默（正常，无人发消息），清零停滞计数
#      - 命令失败 → 无法验证：仅告警，不 reboot（不基于误判重启）
#
# 动作（确凿停滞）：
#   - 告警发主管（DWS_EVENT_O2O_USERS 第一个，与 notify_alert_handler 同款收件人）
#   - 派生 bin/core/reboot.sh 自愈（start_new_session 脱离本进程树——reboot 的
#     stop 阶段 kill_tree connect，不脱离会连 reboot 自己一起杀）
#   - 防抖（状态文件 .event-stall.state；不在 clean_runtime_state 的清理表里，
#     reboot 后存活，防重启风暴）：
#       距上次自愈动作 < EVENT_STALL_RETRY_INTERVAL（默认 1800s）→ 只告警不重启
#       累计自愈 ≥ EVENT_STALL_MAX_REBOOTS（默认 3）仍停滞 → 只告警，等人工
#
# 配置（config/constants.local.sh 可覆盖，均有默认值）：
#   EVENT_STALL_WATCHDOG        开关（dws-connect.sh 判定，1/true/yes/on=开，默认开）
#   EVENT_STALL_THRESHOLD       疑似停滞阈值秒（默认 2700）
#   EVENT_STALL_CHECK_INTERVAL  轮询间隔秒（默认 300）
#   EVENT_STALL_RETRY_INTERVAL  自愈/告警动作最小间隔秒（默认 1800）
#   EVENT_STALL_MAX_REBOOTS     一次停滞期内自动 reboot 上限（默认 3）
#
# 单测：tests/custom/test_event_freshness_watchdog.sh（纯 env 驱动，mock
# _wd_now/_wd_send_alert/_wd_spawn_reboot/_wd_dws_list，不碰真实配置/网络）。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# 加载本地配置（与 dws-connect.sh 同款兜底；EVENT_WATCHDOG_SKIP_LOCAL=1 供单测跳过）
if [[ -z "${EVENT_WATCHDOG_SKIP_LOCAL:-}" && -f "$SCRIPT_DIR/config/constants.local.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config/constants.local.sh"
fi

: "${EVENT_STALL_THRESHOLD:=2700}"
: "${EVENT_STALL_CHECK_INTERVAL:=300}"
: "${EVENT_STALL_RETRY_INTERVAL:=1800}"
: "${EVENT_STALL_MAX_REBOOTS:=3}"
: "${MONITOR_LOG:=$SCRIPT_DIR/monitor.log}"
: "${EVENT_STALL_STATE:=$SCRIPT_DIR/.event-stall.state}"
: "${DWS_EVENT_GROUP:=}"
: "${DWS_EVENT_O2O_USERS:=}"
: "${DWS_PROFILE:=}"
: "${AGENT_SELF_NAMES:=}"

_wd_log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [watchdog] $*" >> "$MONITOR_LOG" 2>/dev/null || true
}

# 当前 epoch（单测覆盖此函数注入固定时间）
_wd_now() { date +%s; }

# "YYYY-MM-DD HH:MM:SS" → epoch。macOS BSD date / Linux GNU date 双写法
# （与 healthcheck.sh 的 stat 双写法同惯例）。
_wd_ts_to_epoch() {
    local ts="$1" e=""
    e="$(date -j -f '%Y-%m-%d %H:%M:%S' "$ts" +%s 2>/dev/null)" || true
    if [[ -z "$e" ]]; then
        e="$(date -d "$ts" +%s 2>/dev/null)" || true
    fi
    echo "${e:-0}"
}

# monitor.log 最后一条 [agent] inbound → 两行：epoch / "YYYY-MM-DD HH:MM:SS"。
# 无入站记录返回非 0（新部署/日志轮转后，无法判定，静默等待第一条消息）。
_wd_last_inbound() {
    local line ts
    line="$(grep '\[agent\] inbound' "$MONITOR_LOG" 2>/dev/null | tail -1)" || true
    [[ -z "$line" ]] && return 1
    ts="$(printf '%s' "$line" | sed -nE 's/^\[([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})\].*/\1/p')"
    [[ -z "$ts" ]] && return 1
    echo "$(_wd_ts_to_epoch "$ts")"
    echo "$ts"
}

# 最近 200 条入站消息的 msgId（交叉验证去重窗口：过滤时钟偏差下被 --time
# 边界带回的、本地其实已收到过的消息）
_wd_recent_msgids() {
    grep '\[agent\] inbound' "$MONITOR_LOG" 2>/dev/null | tail -200 \
        | sed -nE 's/.*msgId=([^ ]+).*/\1/p' || true
}

# DWS 拉取（单测覆盖此函数 mock 返回）。失败返回非 0。
_wd_dws_list() {
    local since="$1"
    [[ -n "$DWS_EVENT_GROUP" && -n "$DWS_PROFILE" ]] || return 1
    command -v dws >/dev/null 2>&1 || return 1
    dws chat message list --group "$DWS_EVENT_GROUP" \
        --time "$since" --direction newer --limit 50 \
        --profile "$DWS_PROFILE" -y 2>/dev/null || return 1
}

# 交叉验证：返回 "<count>\t<preview>"。
#   count=-1：拉取失败（无法验证）；count>=0：他人发的、本地未入站的、晚于
#   <since> 的消息条数（preview 为第一条内容预览）
_wd_count_newer() {
    local since="$1" json
    json="$(_wd_dws_list "$since")" || json=""
    if [[ -z "$json" ]]; then
        echo "-1	"
        return 0
    fi
    # 注意：env 前缀必须紧贴管道右侧的 python3（`VAR=x cmd | python3` 的 VAR 只进
    # cmd 的环境，python 读不到——实测踩坑：过滤变量全空导致去重/自过滤失效）
    printf '%s' "$json" | \
    SELF_NAMES="$AGENT_SELF_NAMES" \
    SEEN_MSGIDS="$(_wd_recent_msgids)" \
    SINCE="$since" \
    python3 -c '
import json, sys, os
try:
    d = json.load(sys.stdin)
except Exception:
    print("-1\t"); sys.exit(0)
msgs = d.get("messages", []) if isinstance(d, dict) else []
names = {n.strip() for n in os.environ.get("SELF_NAMES", "").split(",") if n.strip()}
seen = set(os.environ.get("SEEN_MSGIDS", "").split())
since = os.environ.get("SINCE", "")
fresh = []
for m in msgs:
    if str(m.get("sender") or "") in names:   # 数字员工自己发的（回复/定时任务）
        continue
    mid = str(m.get("messageId") or "")
    if mid and mid in seen:                    # 本地已入站过（时钟偏差防误判）
        continue
    ct = str(m.get("createTime") or "")
    if since and ct and ct <= since:           # 不晚于本地最后入站时刻
        continue
    fresh.append(m)
if fresh:
    preview = (fresh[0].get("content") or fresh[0].get("text") or "")
    preview = " ".join(preview.split())[:80]
    print("%d\t%s" % (len(fresh), preview))
else:
    print("0\t")
' 2>/dev/null || echo "-1	"
}

# 防抖状态（三行：last_action_epoch（自愈动作）/ last_alert_epoch（任意告警）/
# reboot_count（本次停滞期累计自愈次数））。文件不在 clean_runtime_state 清理表，
# reboot 后存活；事件流恢复活跃 / DWS 确认真静默时清零。
_WD_LAST_ACTION=0
_WD_LAST_ALERT=0
_WD_REBOOT_COUNT=0
_wd_state_load() {
    _WD_LAST_ACTION=0; _WD_LAST_ALERT=0; _WD_REBOOT_COUNT=0
    [[ -f "$EVENT_STALL_STATE" ]] || return 0
    local l1 l2 l3
    l1="$(sed -n '1p' "$EVENT_STALL_STATE" 2>/dev/null)" || true
    l2="$(sed -n '2p' "$EVENT_STALL_STATE" 2>/dev/null)" || true
    l3="$(sed -n '3p' "$EVENT_STALL_STATE" 2>/dev/null)" || true
    [[ "$l1" =~ ^[0-9]+$ ]] && _WD_LAST_ACTION="$l1"
    [[ "$l2" =~ ^[0-9]+$ ]] && _WD_LAST_ALERT="$l2"
    [[ "$l3" =~ ^[0-9]+$ ]] && _WD_REBOOT_COUNT="$l3"
}
_wd_state_save() {
    printf '%s\n%s\n%s\n' "$_WD_LAST_ACTION" "$_WD_LAST_ALERT" "$_WD_REBOOT_COUNT" \
        > "$EVENT_STALL_STATE" 2>/dev/null || true
}

# 告警（单测覆盖此函数 mock）。必须永不失败（看门狗不能被通知通道故障杀死）。
_wd_send_alert() {
    local text="$1"
    [[ -n "${DWS_PROFILE:-}" && -n "${DWS_EVENT_O2O_USERS:-}" ]] || return 0
    local to="${DWS_EVENT_O2O_USERS%%,*}"
    [[ -n "$to" ]] || return 0
    command -v dws >/dev/null 2>&1 || return 0
    dws chat message send --user "$to" --text "$text" \
        --profile "$DWS_PROFILE" -y >/dev/null 2>&1 || true
    return 0
}

# 派生 reboot（单测覆盖此函数 mock）。start_new_session 脱离本进程树：
# reboot 的 stop 阶段 kill_tree connect，不脱离会把 reboot 自身连带杀掉
# （与 event_watcher.py 派生 /reboot 同款做法）。
_wd_spawn_reboot() {
    python3 - "$SCRIPT_DIR" <<'PYEOF' >/dev/null 2>&1 || true
import subprocess, sys
root = sys.argv[1]
subprocess.Popen(
    ["bash", root + "/bin/core/reboot.sh"],
    stdin=subprocess.DEVNULL,
    stdout=open(root + "/monitor.log", "a"),
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
PYEOF
    return 0
}

# 发告警（带冷却：距上次告警 >= EVENT_STALL_RETRY_INTERVAL 才发，防止
# unverified 场景每 5 分钟刷屏）。返回 1 = 冷却期内跳过。
_wd_alert() {
    local text="$1" now="$2"
    if (( _WD_LAST_ALERT > 0 && (now - _WD_LAST_ALERT) < EVENT_STALL_RETRY_INTERVAL )); then
        return 1
    fi
    _WD_LAST_ALERT="$now"
    _wd_send_alert "$text"
    return 0
}

# 主判定（单测核心）。返回状态字符串（stdout）：
#   ok               流活跃（age <= 阈值）
#   unknown          无入站记录，无法判定（静默）
#   quiet            疑似超时但 DWS 确认服务端无新消息（真静默），计数清零
#   unverified       疑似超时且 DWS 验证失败，仅告警（带冷却）
#   unverified_quiet 同上但处于告警冷却期，跳过告警
#   stalled_reboot   确凿停滞：告警 + 派生 reboot 自愈
#   stalled_wait     确凿停滞但处于自愈冷却期：仅告警
#   stalled_giveup   确凿停滞且已达自愈上限：仅告警，等人工
_wd_check_once() {
    local now="$(_wd_now)" inbound last_epoch last_ts age
    inbound="$(_wd_last_inbound)" || inbound=""
    if [[ -z "$inbound" ]]; then
        echo "unknown"; return 0
    fi
    last_epoch="$(printf '%s\n' "$inbound" | sed -n '1p')"
    last_ts="$(printf '%s\n' "$inbound" | sed -n '2p')"
    age=$(( now - last_epoch ))

    if (( age <= EVENT_STALL_THRESHOLD )); then
        _wd_state_load
        if (( _WD_REBOOT_COUNT > 0 )); then
            _WD_REBOOT_COUNT=0; _WD_LAST_ACTION=0; _WD_LAST_ALERT=0
            _wd_state_save
            _wd_log "事件流恢复活跃（age=${age}s），停滞计数清零"
        fi
        echo "ok"; return 0
    fi

    # 疑似停滞 → DWS 独立拉取交叉验证
    local res count preview
    res="$(_wd_count_newer "$last_ts")"
    count="$(printf '%s' "$res" | cut -f1)"
    preview="$(printf '%s' "$res" | cut -f2-)"
    [[ "$count" =~ ^-?[0-9]+$ ]] || count=-1

    if (( count < 0 )); then
        _wd_state_load
        if _wd_alert "⚠️ 数字员工事件流疑似停滞 ${age}s（最后入站 ${last_ts}），但 DWS 交叉验证失败，暂不自动重启，请人工检查 agent-connect.log。" "$now"; then
            _wd_state_save
            _wd_log "疑似停滞 age=${age}s，DWS 交叉验证失败，已告警（不重启）"
            echo "unverified"; return 0
        fi
        _wd_log "疑似停滞 age=${age}s，DWS 交叉验证失败，告警冷却期内跳过"
        echo "unverified_quiet"; return 0
    fi

    if (( count == 0 )); then
        _wd_state_load
        if (( _WD_REBOOT_COUNT > 0 )); then
            _WD_REBOOT_COUNT=0; _WD_LAST_ACTION=0; _WD_LAST_ALERT=0
            _wd_state_save
            _wd_log "疑似停滞 age=${age}s 但 DWS 确认服务端无新消息（真静默），停滞计数清零"
        fi
        echo "quiet"; return 0
    fi

    # 确凿停滞（服务端有本地未收到的他人消息）
    _wd_state_load
    local age_min=$(( age / 60 ))
    if (( _WD_REBOOT_COUNT >= EVENT_STALL_MAX_REBOOTS )); then
        if _wd_alert "🚨 数字员工事件流投递停滞 ${age_min} 分钟（DWS 侧确认 ${count} 条消息未投递），已自动重启 ${_WD_REBOOT_COUNT} 次仍未恢复，暂停自愈等人工。积压示例：${preview}" "$now"; then
            _wd_state_save
        fi
        _wd_log "确凿投递停滞 age=${age}s 积压=${count}：已达自愈上限(${EVENT_STALL_MAX_REBOOTS})，仅告警"
        echo "stalled_giveup"; return 0
    fi
    if (( _WD_LAST_ACTION > 0 && (now - _WD_LAST_ACTION) < EVENT_STALL_RETRY_INTERVAL )); then
        if _wd_alert "🚨 数字员工事件流投递停滞 ${age_min} 分钟（DWS 侧确认 ${count} 条消息未投递），自动重启冷却中（${EVENT_STALL_RETRY_INTERVAL}s 内不重复重启）。积压示例：${preview}" "$now"; then
            _wd_state_save
        fi
        _wd_log "确凿投递停滞 age=${age}s 积压=${count}：自愈冷却期内，仅告警"
        echo "stalled_wait"; return 0
    fi
    _WD_REBOOT_COUNT=$(( _WD_REBOOT_COUNT + 1 ))
    _WD_LAST_ACTION="$now"
    _WD_LAST_ALERT="$now"
    _wd_state_save
    _wd_log "确凿投递停滞 age=${age}s 积压=${count}：告警 + 派生 reboot 自愈（第 ${_WD_REBOOT_COUNT}/${EVENT_STALL_MAX_REBOOTS} 次）"
    _wd_send_alert "🚨 数字员工事件流投递停滞 ${age_min} 分钟，DWS 侧确认 ${count} 条消息未投递，正在自动重启自愈（第 ${_WD_REBOOT_COUNT} 次，上限 ${EVENT_STALL_MAX_REBOOTS}）。积压示例：${preview}"
    _wd_spawn_reboot
    echo "stalled_reboot"; return 0
}

wd_main_loop() {
    _wd_log "看门狗启动: threshold=${EVENT_STALL_THRESHOLD}s interval=${EVENT_STALL_CHECK_INTERVAL}s retry=${EVENT_STALL_RETRY_INTERVAL}s max_reboots=${EVENT_STALL_MAX_REBOOTS}"
    while :; do
        sleep "$EVENT_STALL_CHECK_INTERVAL"
        _wd_check_once >/dev/null 2>&1 || true
    done
}

if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
    wd_main_loop
fi
