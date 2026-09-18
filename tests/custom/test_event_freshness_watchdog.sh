#!/bin/bash
# test_event_freshness_watchdog.sh — 事件流新鲜度看门狗单元测试
#
# 纯 env 驱动：mock _wd_now / _wd_send_alert / _wd_spawn_reboot / _wd_dws_list，
# 不碰真实配置（EVENT_WATCHDOG_SKIP_LOCAL=1）、不连网络、不调 dws。
# 覆盖 _wd_check_once 全部分支 + 防抖状态机 + 交叉验证过滤逻辑。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PASS=0
FAIL=0
FAILED_TESTS=()

assert_eq() {
    local name="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        echo -e "  \033[32m✓\033[0m $name"
        PASS=$((PASS + 1))
    else
        echo -e "  \033[31m✗\033[0m $name"
        echo "    expected: $expected"
        echo "    actual:   $actual"
        FAIL=$((FAIL + 1))
        FAILED_TESTS+=("$name")
    fi
}

export EVENT_WATCHDOG_SKIP_LOCAL=1
export EVENT_STALL_THRESHOLD=2700
export EVENT_STALL_CHECK_INTERVAL=300
export EVENT_STALL_RETRY_INTERVAL=1800
export EVENT_STALL_MAX_REBOOTS=3
export DWS_EVENT_GROUP="cidTEST"
export DWS_EVENT_O2O_USERS="u-boss,u-other"
export DWS_PROFILE="corp:agent"
export AGENT_SELF_NAMES="opencode,数字员工"

TMP="$(mktemp -d /tmp/test-watchdog.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
export MONITOR_LOG="$TMP/monitor.log"
export EVENT_STALL_STATE="$TMP/.event-stall.state"

# mock 计数器
ALERT_COUNT=0
REBOOT_MOCK_COUNT=0

# 被测代码（不会进主循环：BASH_SOURCE != $0）
source "$SCRIPT_DIR/bin/custom/event_freshness_watchdog.sh"

# ---- mock 覆盖（必须在 source 之后）----
MOCK_NOW=0
MOCK_LIST_JSON=""
_wd_now() { echo "$MOCK_NOW"; }
_wd_send_alert() { ALERT_COUNT=$((ALERT_COUNT + 1)); return 0; }
_wd_spawn_reboot() { REBOOT_MOCK_COUNT=$((REBOOT_MOCK_COUNT + 1)); return 0; }
_wd_dws_list() {
    [[ -n "$MOCK_LIST_JSON" ]] || return 1
    printf '%s' "$MOCK_LIST_JSON"
}

# fixture：写 monitor.log 的 inbound 行（$1=时间戳，$2=msgId）
_mk_inbound() {
    echo "[$1] [agent] inbound: msgId=$2 kind=text user=PiBot conv=2:cidTEST" >> "$MONITOR_LOG"
}

# 案例间重置：防抖状态是脚本全局变量 + 状态文件，跨案例残留会互相污染
# （案例时间轴各不相同，LAST_ALERT/ LAST_ACTION 残留会让告警被错误冷却）
_reset_case() {
    rm -f "$EVENT_STALL_STATE"
    _WD_LAST_ACTION=0; _WD_LAST_ALERT=0; _WD_REBOOT_COUNT=0
    ALERT_COUNT=0; REBOOT_MOCK_COUNT=0
}

# 直调 _wd_check_once 并捕获 stdout：不能写 $( _wd_check_once )——命令替换在
# 子 shell 里执行，mock 的计数变量（ALERT_COUNT/REBOOT_MOCK_COUNT）传不回本 shell
CHECK_FILE="$TMP/check.out"
CHECK_OUT=""
_run_check() {
    _wd_check_once > "$CHECK_FILE" 2>/dev/null || true
    CHECK_OUT="$(cat "$CHECK_FILE")"
}

# 时间基准：2026-09-18 11:21:57（事故当天最后一条入站）
T0_TS="2026-09-18 11:21:57"
T0_EPOCH=$(_wd_ts_to_epoch "$T0_TS")

echo "Testing event_freshness_watchdog.sh..."

# ---- 基础函数 ----
assert_eq "_wd_ts_to_epoch 正常转换非零" "1" "$([[ ${T0_EPOCH:-0} -gt 0 ]] && echo 1 || echo 0)"

echo ""
echo "== 无入站记录 =="

: > "$MONITOR_LOG"
MOCK_NOW=$T0_EPOCH
MOCK_LIST_JSON=""
_reset_case
_run_check
assert_eq "无 inbound → unknown" "unknown" "$CHECK_OUT"
assert_eq "无 inbound 不告警" "0" "$ALERT_COUNT"
assert_eq "无 inbound 不 reboot" "0" "$REBOOT_MOCK_COUNT"

echo ""
echo "== 流活跃（age <= 阈值） =="

: > "$MONITOR_LOG"
_mk_inbound "$T0_TS" "msg-1"
MOCK_NOW=$((T0_EPOCH + 100))
MOCK_LIST_JSON=""
_reset_case
_run_check
assert_eq "age=100s → ok" "ok" "$CHECK_OUT"
assert_eq "流活跃不告警" "0" "$ALERT_COUNT"

# 预置停滞计数，流恢复后再次检查应清零
printf '9999999999\n9999999999\n2\n' > "$EVENT_STALL_STATE"
_wd_check_once >/dev/null
assert_eq "流活跃后停滞计数清零" "0
0
0" "$(cat "$EVENT_STALL_STATE")"

echo ""
echo "== 真静默（超时但服务端无新消息） =="

: > "$MONITOR_LOG"
_mk_inbound "$T0_TS" "msg-1"
MOCK_NOW=$((T0_EPOCH + 3000))
MOCK_LIST_JSON='{"messages":[]}'
_reset_case
_run_check
assert_eq "超时+DWS拉到0条 → quiet" "quiet" "$CHECK_OUT"
assert_eq "真静默不告警" "0" "$ALERT_COUNT"
assert_eq "真静默不 reboot" "0" "$REBOOT_MOCK_COUNT"

# 过滤：拉到的都是自己发的 / 已入站过的 → 等价 0 条 → quiet
MOCK_LIST_JSON='{"messages":[
  {"messageId":"msg-1","sender":"PiBot","content":"已收到的旧消息","createTime":"2026-09-18 12:11:57"},
  {"messageId":"msg-self-1","sender":"数字员工","content":"自己发的回复","createTime":"2026-09-18 12:12:57"}
]}'
_run_check
assert_eq "积压全是已入站/自己 → quiet" "quiet" "$CHECK_OUT"
assert_eq "过滤后仍不告警" "0" "$ALERT_COUNT"

# 真静默也应清零此前停滞计数
printf '9999999999\n9999999999\n2\n' > "$EVENT_STALL_STATE"
_wd_check_once >/dev/null
assert_eq "真静默后停滞计数清零" "0
0
0" "$(cat "$EVENT_STALL_STATE")"

echo ""
echo "== 确凿停滞 → 告警 + 自愈 =="

: > "$MONITOR_LOG"
_mk_inbound "$T0_TS" "msg-1"
MOCK_NOW=$((T0_EPOCH + 3000))
MOCK_LIST_JSON='{"messages":[
  {"messageId":"msg-new-1","sender":"PiBot","content":"DOWN Google","createTime":"2026-09-18 12:11:57"}
]}'
_reset_case
_run_check
assert_eq "超时+积压1条他人消息 → stalled_reboot" "stalled_reboot" "$CHECK_OUT"
assert_eq "确凿停滞发告警" "1" "$ALERT_COUNT"
assert_eq "确凿停滞派生 reboot" "1" "$REBOOT_MOCK_COUNT"
assert_eq "状态文件记 reboot_count=1" "1" "$(sed -n '3p' "$EVENT_STALL_STATE")"

echo ""
echo "== 自愈冷却期 → 仅告警不重启（告警统一冷却，不重复发） =="

# 推进 100s（仍在 1800s 自愈冷却内），仍停滞
MOCK_NOW=$((T0_EPOCH + 3100))
MOCK_LIST_JSON='{"messages":[
  {"messageId":"msg-new-2","sender":"PiBot","content":"UP Google","createTime":"2026-09-18 12:16:57"}
]}'
ALERT_COUNT=0; REBOOT_MOCK_COUNT=0
_run_check
assert_eq "冷却期内 → stalled_wait" "stalled_wait" "$CHECK_OUT"
assert_eq "冷却期内不重复告警（reboot 时已发过）" "0" "$ALERT_COUNT"
assert_eq "冷却期内不重复 reboot" "0" "$REBOOT_MOCK_COUNT"

echo ""
echo "== 冷却过后仍停滞 → 再次自愈（计数递增） =="

MOCK_NOW=$((T0_EPOCH + 3000 + 1800 + 10))
ALERT_COUNT=0; REBOOT_MOCK_COUNT=0
_run_check
assert_eq "冷却过后 → 再次 stalled_reboot" "stalled_reboot" "$CHECK_OUT"
assert_eq "再次自愈计数=2" "2" "$(sed -n '3p' "$EVENT_STALL_STATE")"

echo ""
echo "== 达自愈上限 → 等人工 =="

: > "$MONITOR_LOG"
_mk_inbound "$T0_TS" "msg-1"
MOCK_NOW=$((T0_EPOCH + 3000 + 3 * 1800))
MOCK_LIST_JSON='{"messages":[
  {"messageId":"msg-new-9","sender":"PiBot","content":"仍停滞","createTime":"2026-09-18 14:00:00"}
]}'
# 预置：已自愈 3 次（达到上限），上次动作时间在冷却之外
printf '%s\n%s\n3\n' "$((T0_EPOCH + 3000))" "0" "3" > "$EVENT_STALL_STATE"
ALERT_COUNT=0; REBOOT_MOCK_COUNT=0
_run_check
assert_eq "count=3 → stalled_giveup" "stalled_giveup" "$CHECK_OUT"
assert_eq "giveup 发告警" "1" "$ALERT_COUNT"
assert_eq "giveup 不再 reboot" "0" "$REBOOT_MOCK_COUNT"

echo ""
echo "== DWS 验证失败 → 仅告警不重启 =="

: > "$MONITOR_LOG"
_mk_inbound "$T0_TS" "msg-1"
MOCK_NOW=$((T0_EPOCH + 3000))
MOCK_LIST_JSON=""   # mock 拉取失败
_reset_case
_run_check
assert_eq "拉取失败 → unverified" "unverified" "$CHECK_OUT"
assert_eq "unverified 发告警" "1" "$ALERT_COUNT"
assert_eq "unverified 不 reboot" "0" "$REBOOT_MOCK_COUNT"

# 5 分钟后再验仍失败 → 告警冷却期内不重复发
MOCK_NOW=$((T0_EPOCH + 3300))
ALERT_COUNT=0
_run_check
assert_eq "再次失败 → unverified_quiet" "unverified_quiet" "$CHECK_OUT"
assert_eq "告警冷却期内不重复告警" "0" "$ALERT_COUNT"

echo ""
echo "== msgId 去重窗口（_wd_recent_msgids） =="

: > "$MONITOR_LOG"
_mk_inbound "2026-09-18 10:00:00" "msg-a"
_mk_inbound "2026-09-18 11:21:57" "msg-b"
assert_eq "recent_msgids 返回全部入站 msgId" "msg-a
msg-b" "$(_wd_recent_msgids)"

echo ""
# ---- 汇总 ----
echo ""
if [[ "$FAIL" -eq 0 ]]; then
    echo -e "\033[32m全部通过: $PASS 项\033[0m"
else
    echo -e "\033[31m失败 $FAIL / $((PASS + FAIL)) 项\033[0m"
    printf '%s\n' "${FAILED_TESTS[@]}"
    exit 1
fi
