#!/bin/bash
# test_brain_watchdog.sh — 大脑 serve 连接看门狗单元测试
#
# 纯 env 驱动：mock _bw_now / _bw_send_alert / _bw_restart_serve / _bw_gateway_ok，
# 不碰真实配置（BRAIN_WATCHDOG_SKIP_LOCAL=1）、不连网络、不重启真实 serve。
# 覆盖 _bw_check_once 全部分支 + _bw_scan 窗口过滤 + 防抖状态机。

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

export BRAIN_WATCHDOG_SKIP_LOCAL=1
export BRAIN_SERVE_CHECK_INTERVAL=300
export BRAIN_SERVE_PAIR_WINDOW=3600
export BRAIN_SERVE_FRESH_WINDOW=900
export BRAIN_SERVE_MIN_PAIRS=2
export BRAIN_SERVE_RETRY_INTERVAL=1800
export BRAIN_SERVE_MAX_RESTARTS=3
export BRAIN_GATEWAY_URL="http://gw.test/v1/models"
export DWS_EVENT_O2O_USERS="u-boss,u-other"
export DWS_PROFILE="corp:agent"

TMP="$(mktemp -d /tmp/test-brain-watchdog.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
export MONITOR_LOG="$TMP/monitor.log"
export BRAIN_SERVE_STATE="$TMP/.brain-serve-stall.state"

NOW=1700000000

# epoch → "YYYY-MM-DD HH:MM:SS"（macOS BSD date / Linux GNU date 双写法）
ts() {
    local s
    s="$(date -r "$1" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)" || true
    if [[ -z "$s" ]]; then
        s="$(date -d "@$1" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)" || true
    fi
    echo "$s"
}

# 一对差分证据（http err → CLI 回退成功）
pair_ok() {  # $1=epoch
    echo "[$(ts "$1")] [agent] brain opencode http err: session xx turn error: Cannot connect to API: Was there a typo in the url or port?"
    echo "[$(ts "$1")] [agent] brain(opencode): serve HTTP 不可用，回退 opencode run CLI"
    echo "[$(ts "$(( $1 + 1 ))")] [agent] brain(opencode): CLI 回退成功"
}
# 一对双路全挂（http err → CLI 回退失败）
pair_fail() {  # $1=epoch
    echo "[$(ts "$1")] [agent] brain opencode http err: session xx turn error: Cannot connect to API: Was there a typo in the url or port?"
    echo "[$(ts "$1")] [agent] brain(opencode): serve HTTP 不可用，回退 opencode run CLI"
    echo "[$(ts "$(( $1 + 1 ))")] [agent] brain(opencode): CLI 回退失败：opencode run rc=1"
}
# 干扰行（不应被计入任何标记）
noise() {
    echo "[$(ts "$1")] [agent] inbound: msgId=msgXX kind=text user=u1 conv=2:cidXX"
    echo "[$(ts "$1")] [agent] brain(opencode): 活动感知超时 abort，判失败不回退 CLI：xx"
}

# 状态文件读取：第 n 行
state_line() { sed -n "${1}p" "$BRAIN_SERVE_STATE" 2>/dev/null; }

# 直调 _bw_check_once 并捕获 stdout：不能写 $( _bw_check_once )——命令替换在
# 子 shell 里执行，mock 的计数变量（ALERT_COUNT/RESTART_MOCK_COUNT）传不回本 shell。
# 必须语句级调用 check（副作用留本 shell），再读 last_result。
CHECK_FILE="$TMP/check.out"
check() {
    _bw_check_once > "$CHECK_FILE" 2>/dev/null || true
}
last_result() { cat "$CHECK_FILE"; }

# ---- 被测代码（BASH_SOURCE != $0，不进主循环）----
# shellcheck disable=SC1091
source "$SCRIPT_DIR/bin/custom/brain_watchdog.sh"

# ---- mock 覆盖（必须在 source 之后）----
MOCK_NOW="$NOW"
GW_MOCK=2
ALERT_COUNT=0
RESTART_MOCK_COUNT=0
_bw_now() { echo "$MOCK_NOW"; }
_bw_send_alert() { ALERT_COUNT=$((ALERT_COUNT + 1)); return 0; }
_bw_restart_serve() { RESTART_MOCK_COUNT=$((RESTART_MOCK_COUNT + 1)); return 0; }
_bw_gateway_ok() { return "$GW_MOCK"; }

reset_fixture() {
    rm -f "$MONITOR_LOG" "$BRAIN_SERVE_STATE"
    MOCK_NOW="$NOW"
    GW_MOCK=2
    ALERT_COUNT=0
    RESTART_MOCK_COUNT=0
}

# =====================================================================
echo "== _bw_scan 窗口过滤 =="
reset_fixture
{
    pair_ok "$((NOW - 4000))"        # 超窗口：不计数，但刷新 last_poison
    pair_ok "$((NOW - 300))"
    pair_ok "$((NOW - 60))"
    pair_fail "$((NOW - 30))"
    noise "$((NOW - 10))"
} > "$MONITOR_LOG"
res="$(_bw_scan "$NOW" 3600)"
assert_eq "scan: poisoned（窗口内 2 对）" "2	1	$((NOW - 30))	$((NOW - 59))" "$res"

# =====================================================================
echo "== unknown：无任何失败记录 =="
reset_fixture
: > "$MONITOR_LOG"
check
assert_eq "空日志 → unknown" "unknown" "$(last_result)"

# =====================================================================
echo "== ok：窗口外失败 + 状态清零 =="
reset_fixture
pair_fail "$((NOW - 7200))" > "$MONITOR_LOG"
printf '1000\n1000\n2\n' > "$BRAIN_SERVE_STATE"
check
assert_eq "陈旧失败 → ok" "ok" "$(last_result)"
assert_eq "restart_count 清零" "0" "$(state_line 3)"

# =====================================================================
echo "== suspect：单对证据，只告警不重启 =="
reset_fixture
pair_ok "$((NOW - 120))" > "$MONITOR_LOG"
check
assert_eq "1 对新鲜证据 → suspect" "suspect" "$(last_result)"
assert_eq "告警已发（1 次）" "1" "$ALERT_COUNT"
assert_eq "未重启" "0" "$RESTART_MOCK_COUNT"
assert_eq "suspect 也持久化告警时刻（供冷却）" "$NOW" "$(state_line 2)"

echo "-- 告警冷却期内复查 --"
MOCK_NOW=$((NOW + 300))
pair_ok "$((NOW - 120))" > "$MONITOR_LOG"   # 证据仍在窗口内且仍新鲜
check
assert_eq "冷却期内 → suspect_quiet" "suspect_quiet" "$(last_result)"
assert_eq "冷却期内不再发告警" "1" "$ALERT_COUNT"

# =====================================================================
echo "== poisoned_reboot：两对新鲜证据 → 重启 serve =="
reset_fixture
{
    pair_ok "$((NOW - 300))"
    pair_ok "$((NOW - 120))"
} > "$MONITOR_LOG"
check
assert_eq "2 对新鲜证据 → poisoned_reboot" "poisoned_reboot" "$(last_result)"
assert_eq "重启已执行（1 次）" "1" "$RESTART_MOCK_COUNT"
assert_eq "告警已发" "1" "$ALERT_COUNT"
assert_eq "state: last_action=now" "$NOW" "$(state_line 1)"
assert_eq "state: restart_count=1" "1" "$(state_line 3)"

echo "-- 自愈冷却期内持续异常 → poisoned_wait（不重复重启）--"
MOCK_NOW=$((NOW + 600))
{
    pair_ok "$((NOW - 300))"
    pair_ok "$((NOW - 120))"
    pair_ok "$((MOCK_NOW - 60))"
} > "$MONITOR_LOG"
check
assert_eq "冷却期内 → poisoned_wait" "poisoned_wait" "$(last_result)"
assert_eq "不重复重启" "1" "$RESTART_MOCK_COUNT"
assert_eq "冷却期内告警被抑制（上次告警 600s 前 < 1800s）" "1" "$ALERT_COUNT"

# =====================================================================
echo "== poisoned_giveup：达到上限只告警 =="
reset_fixture
printf "$((NOW - 7200))\n$((NOW - 7200))\n3\n" > "$BRAIN_SERVE_STATE"
{
    pair_ok "$((NOW - 300))"
    pair_ok "$((NOW - 120))"
} > "$MONITOR_LOG"
check
assert_eq "restart_count=3 → poisoned_giveup" "poisoned_giveup" "$(last_result)"
assert_eq "不重启" "0" "$RESTART_MOCK_COUNT"

# =====================================================================
echo "== 陈旧证据：配对超出新鲜窗口 → 不重启 =="
reset_fixture
{
    pair_ok "$((NOW - 2000))"
    pair_ok "$((NOW - 1500))"
    pair_ok "$((NOW - 60))"
} > "$MONITOR_LOG"
# poisoned=3 ≥ 2，但最近一对年龄 2000+... 实际 last_poison=NOW-59（新鲜）
# → 仍应 reboot；改成最后一对也陈旧：
reset_fixture
{
    pair_ok "$((NOW - 2000))"
    pair_ok "$((NOW - 1500))"
    echo "[$(ts "$((NOW - 30))")] [agent] brain opencode http err: session xx turn error: xx"
} > "$MONITOR_LOG"
check
assert_eq "证据陈旧（>fresh window）→ suspect" "suspect" "$(last_result)"
assert_eq "陈旧证据不重启" "0" "$RESTART_MOCK_COUNT"

# =====================================================================
echo "== 双路全挂：curl 差分分级 =="
reset_fixture
pair_fail "$((NOW - 120))" > "$MONITOR_LOG"
GW_MOCK=1
check
assert_eq "curl 不可达 → gateway_down" "gateway_down" "$(last_result)"
assert_eq "gateway_down 不重启" "0" "$RESTART_MOCK_COUNT"
assert_eq "gateway_down 告警已发" "1" "$ALERT_COUNT"

reset_fixture
pair_fail "$((NOW - 120))" > "$MONITOR_LOG"
GW_MOCK=0
check
assert_eq "curl 可达 → opencode_net" "opencode_net" "$(last_result)"
assert_eq "opencode_net 不重启" "0" "$RESTART_MOCK_COUNT"

reset_fixture
pair_fail "$((NOW - 120))" > "$MONITOR_LOG"
GW_MOCK=2
check
assert_eq "探测未配置 → both_fail" "both_fail" "$(last_result)"
assert_eq "both_fail 不重启" "0" "$RESTART_MOCK_COUNT"

# =====================================================================
echo "== 陈旧证据 + curl 差分（防 reboot 后拿旧事故刷屏）=="
reset_fixture
pair_fail "$((NOW - 2000))" > "$MONITOR_LOG"
GW_MOCK=0
check
assert_eq "陈旧失败 + 网关可达 → recovered（静默）" "recovered" "$(last_result)"
assert_eq "recovered 不告警" "0" "$ALERT_COUNT"
assert_eq "recovered 不重启" "0" "$RESTART_MOCK_COUNT"

reset_fixture
pair_fail "$((NOW - 2000))" > "$MONITOR_LOG"
GW_MOCK=1
check
assert_eq "陈旧失败 + 网关仍不可达 → gateway_down_stale（持续告警）" "gateway_down_stale" "$(last_result)"
assert_eq "gateway_down_stale 告警已发" "1" "$ALERT_COUNT"
assert_eq "gateway_down_stale 不重启" "0" "$RESTART_MOCK_COUNT"

# =====================================================================
echo "== 混合证据：poisoned 主导（最新状态=网关已恢复）=="
reset_fixture
{
    pair_fail "$((NOW - 1500))"   # 网关断期间的失败
    pair_ok "$((NOW - 300))"      # 恢复后 serve 仍毒化、CLI 通
    pair_ok "$((NOW - 120))"
} > "$MONITOR_LOG"
GW_MOCK=1
check
assert_eq "poisoned(2) + cli_fail(1) → poisoned_reboot" "poisoned_reboot" "$(last_result)"

# =====================================================================
echo
echo "========================================"
echo "PASS: $PASS  FAIL: $FAIL"
if (( FAIL > 0 )); then
    printf '失败用例: %s\n' "${FAILED_TESTS[*]}"
    exit 1
fi
echo "全部通过"
