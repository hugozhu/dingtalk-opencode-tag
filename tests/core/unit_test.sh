#!/bin/bash
# unit_test.sh — shell 单元测试模板
#
# 提炼自: dingtalk-opencode-agent/tests/unit_test.sh (v4.1, 50 tests)
# 原作者: hugozhu
#
# 测试对象:
#   - lib.sh 的 verify_pid / acquire_lock / release_lock / log
#   - monitor.sh 的 is_running / cleanup_stale_state / cleanup 退出码
#   - reboot.sh 的常量 + 失败传播
#
# 不依赖网络/钉钉/agent serve，纯 shell 函数级断言

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

# 测试 lib.sh
echo "Testing lib.sh..."

# 加载被测代码
source "$SCRIPT_DIR/bin/core/lib.sh"

# verify_pid 文件不存在时返回非 0
assert_eq "verify_pid 文件不存在返回非0" "1" "$(verify_pid /tmp/nonexistent.pid 'some-pattern' >/dev/null 2>&1; echo $?)"

# acquire_lock 第一次成功
LOCK=/tmp/test_harness_lock_$$
rm -f "$LOCK"
assert_eq "acquire_lock 第一次成功" "0" "$(acquire_lock "$LOCK"; echo $?)"
rm -f "$LOCK"

# release_lock 后能再 acquire
acquire_lock "$LOCK"
release_lock "$LOCK"
assert_eq "release_lock 后能再 acquire" "0" "$(acquire_lock "$LOCK"; echo $?)"
rm -f "$LOCK"

# log 输出格式
LOG_OUT=$(COMPONENT_NAME=test log "hello" 2>&1)
# 含 [YYYY-MM-DD HH:MM:SS] [test] hello
if [[ "$LOG_OUT" =~ \[20[0-9-]+\ [0-9:]+\]\ \[test\]\ hello ]]; then
    assert_eq "log 含时间戳 + 组件名" "1" "1"
else
    assert_eq "log 含时间戳 + 组件名" "1" "0 (actual: $LOG_OUT)"
fi

# 测试 monitor.sh 的常量默认值
echo ""
echo "Testing monitor.sh constants..."

# 用 bash -n 语法检查（不需要执行）
assert_eq "monitor.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/monitor.sh" 2>&1; echo $?)"
assert_eq "healthcheck.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/healthcheck.sh" 2>&1; echo $?)"
assert_eq "reboot.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/reboot.sh" 2>&1; echo $?)"
assert_eq "start.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/start.sh" 2>&1; echo $?)"
assert_eq "stop.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/stop.sh" 2>&1; echo $?)"
assert_eq "lib.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/lib.sh" 2>&1; echo $?)"

# 测试 lib.sh 的服务控制常量默认值（v4.2 重构后从 reboot.sh 移至 lib.sh）
KICKSTART_LINE=$(grep 'KICKSTART_RETRY_INTERVAL' "$SCRIPT_DIR/bin/core/lib.sh" | grep '=' | head -1)
if [[ "$KICKSTART_LINE" =~ KICKSTART_RETRY_INTERVAL:=[[:space:]]*\"?([0-9]+) ]]; then
    KICKSTART_VAL="${BASH_REMATCH[1]}"
else
    KICKSTART_VAL=""
fi
assert_eq "lib.sh KICKSTART_RETRY_INTERVAL=10" "10" "$KICKSTART_VAL"

LAUNCHD_LINE=$(grep 'LAUNCHD_LABEL' "$SCRIPT_DIR/bin/core/lib.sh" | grep '=' | head -1)
if [[ "$LAUNCHD_LINE" =~ LAUNCHD_LABEL:=[[:space:]]*\"?([a-zA-Z.]+) ]]; then
    LAUNCHD_VAL="${BASH_REMATCH[1]}"
else
    LAUNCHD_VAL=""
fi
assert_eq "lib.sh LAUNCHD_LABEL 存在" "1" "$([ -n "$LAUNCHD_VAL" ] && echo 1 || echo 0)"

# 测试 reboot.sh 的委托契约（v4.2：reboot 应调用 stop.sh 和 start.sh）
if grep -q "bin/core/stop.sh" "$SCRIPT_DIR/bin/core/reboot.sh" && \
   grep -q "bin/core/start.sh" "$SCRIPT_DIR/bin/core/reboot.sh"; then
    assert_eq "reboot.sh 委托 stop.sh + start.sh" "1" "1"
else
    assert_eq "reboot.sh 委托 stop.sh + start.sh" "1" "0 (reboot.sh 未引用 stop/start)"
fi

# 测试 README 不硬编码版本号（应指向 VERSION，避免漂移）
echo ""
echo "Testing version consistency..."
# README 里不应出现形如 `1.2.3` 的裸版本号（VERSION 是唯一真相源）
if grep -Eq '版本[:：].*`[0-9]+\.[0-9]+\.[0-9]+`' "$SCRIPT_DIR/README.md"; then
    assert_eq "README 不硬编码版本号" "1" "0 (README 出现硬编码版本，应指向 VERSION)"
else
    assert_eq "README 不硬编码版本号" "1" "1"
fi

# 测试 dws-connect.sh 的订阅选择逻辑（含新增 @我(at) 订阅）
echo ""
echo "Testing dws-connect.sh subscription selection..."
DWS_CONNECT="$SCRIPT_DIR/bin/custom/dws-connect.sh"

assert_eq "dws-connect.sh 语法正确" "0" "$(bash -n "$DWS_CONNECT" 2>&1; echo $?)"

# dry-run 纯 env 驱动（跳过 constants.local.sh），只打印订阅计划
_dwsplan() {
    env DWS_CONNECT_SKIP_LOCAL=1 DWS_CONNECT_DRY_RUN=1 CONNECT_LOG=/dev/null \
        "$@" bash "$DWS_CONNECT" 2>/dev/null
}

# 只开 @我：group/o2o 关，at 开，且起了 at consumer
AT_ONLY="$(_dwsplan DWS_PROFILE=p DWS_EVENT_AT=1)"
assert_eq "仅 AT: plan at=1" "1" "$(echo "$AT_ONLY" | grep -c 'plan: group=0 o2o=0 at=1')"
assert_eq "仅 AT: 起 at consumer" "1" "$(echo "$AT_ONLY" | grep -c 'consumer: user_im_message_receive_at')"

# 三种同时开
ALL="$(_dwsplan DWS_PROFILE=p DWS_EVENT_GROUP=cidX== DWS_EVENT_O2O_USERS=u1 DWS_EVENT_AT=true)"
assert_eq "全开: plan" "1" "$(echo "$ALL" | grep -c 'plan: group=1 o2o=1 at=1')"
assert_eq "全开: 含 at consumer" "1" "$(echo "$ALL" | grep -c 'consumer: user_im_message_receive_at')"

# AT 关（值为 0）不起 at consumer
OFF="$(_dwsplan DWS_PROFILE=p DWS_EVENT_GROUP=cidY== DWS_EVENT_AT=0)"
assert_eq "AT=0 不起 at consumer" "0" "$(echo "$OFF" | grep -c 'consumer: user_im_message_receive_at')"

# O2O_ALL：订阅所有单聊，起 o2o_all consumer（rule_type=all，无 --user）
O2O_ALL="$(_dwsplan DWS_PROFILE=p DWS_EVENT_O2O_ALL=1)"
assert_eq "O2O_ALL: plan o2o=1 mode=all" "1" \
    "$(echo "$O2O_ALL" | grep -c 'plan: group=0 o2o=1 at=0 o2o_mode=all')"
assert_eq "O2O_ALL: 起 o2o_all consumer" "1" \
    "$(echo "$O2O_ALL" | grep -c 'consumer: user_im_message_receive_o2o_all')"

# O2O_ALL 优先于 USERS：开了 ALL 就不再按对端逐个订阅
O2O_BOTH="$(_dwsplan DWS_PROFILE=p DWS_EVENT_O2O_ALL=1 DWS_EVENT_O2O_USERS=u1,u2)"
assert_eq "O2O_ALL 优先: 起 o2o_all" "1" \
    "$(echo "$O2O_BOTH" | grep -c 'consumer: user_im_message_receive_o2o_all')"
assert_eq "O2O_ALL 优先: 不起 per-user consumer" "0" \
    "$(echo "$O2O_BOTH" | grep -c 'consumer: user_im_message_receive_o2o --user')"

# O2O_ALL 关（0）时回退 per-user 列表
O2O_FB="$(_dwsplan DWS_PROFILE=p DWS_EVENT_O2O_ALL=0 DWS_EVENT_O2O_USERS=u1,u2)"
assert_eq "O2O_ALL=0: 回退 per-user 两个 consumer" "2" \
    "$(echo "$O2O_FB" | grep -c 'consumer: user_im_message_receive_o2o --user')"
assert_eq "O2O_ALL=0: 不起 o2o_all" "0" \
    "$(echo "$O2O_FB" | grep -c 'consumer: user_im_message_receive_o2o_all')"

# 仅开 O2O_ALL 也算"配了订阅"，不该走无订阅报错分支
O2O_ONLY_RC="$(env DWS_CONNECT_SKIP_LOCAL=1 DWS_CONNECT_DRY_RUN=1 CONNECT_LOG=/dev/null \
    DWS_PROFILE=p DWS_EVENT_O2O_ALL=1 bash "$DWS_CONNECT" >/dev/null 2>&1; echo $?)"
assert_eq "仅 O2O_ALL → 退出 0" "0" "$O2O_ONLY_RC"

# 什么都不配 → 报错退出非 0（at 也没开）
NONE_RC="$(env DWS_CONNECT_SKIP_LOCAL=1 DWS_CONNECT_DRY_RUN=1 CONNECT_LOG=/dev/null \
    DWS_PROFILE=p bash "$DWS_CONNECT" >/dev/null 2>&1; echo $?)"
assert_eq "无任何订阅 → 退出非0" "1" "$NONE_RC"

# 测试 #71 进程生命周期修复（_bus 孤儿清扫 + reboot 干净环境）
echo ""
echo "Testing #71 process lifecycle fixes..."

# dws-connect.sh：consumer 收尾必须走子树清理（否则 dws event _bus 甩成孤儿）
assert_eq "dws-connect.sh 定义 _kill_subtree" "1" \
    "$(grep -c '^_kill_subtree()' "$SCRIPT_DIR/bin/custom/dws-connect.sh")"
assert_eq "dws-connect.sh 有 EXIT/TERM 收尾 trap" "1" \
    "$(grep -q "trap '_cleanup_consumers' EXIT" "$SCRIPT_DIR/bin/custom/dws-connect.sh" && echo 1 || echo 0)"

# stop.sh / monitor.sh：调用 custom 停机钩子 stop_extra_cleanup
assert_eq "stop.sh 调用 stop_extra_cleanup 钩子" "1" \
    "$(grep -q 'stop_extra_cleanup' "$SCRIPT_DIR/bin/core/stop.sh" && echo 1 || echo 0)"
assert_eq "monitor.sh stop_all 调用 stop_extra_cleanup 钩子" "1" \
    "$(grep -q 'stop_extra_cleanup' "$SCRIPT_DIR/bin/core/monitor.sh" && echo 1 || echo 0)"

# reboot.sh：用干净环境跑 stop/start（否则改 config 后 /reboot 不生效）
assert_eq "reboot.sh 用 env -i 干净环境重启" "1" \
    "$(grep -q 'env -i' "$SCRIPT_DIR/bin/core/reboot.sh" && echo 1 || echo 0)"

# custom start_funcs.sh 语法 + 钩子定义
assert_eq "custom start_funcs.sh 语法正确" "0" \
    "$(bash -n "$SCRIPT_DIR/bin/custom/start_funcs.sh" 2>&1; echo $?)"

# 功能测试：stop_extra_cleanup 按 profile 精确清扫假 dws event 进程树，
# 不误伤其他 profile 的进程
FAKE_DIR=$(mktemp -d)
cat > "$FAKE_DIR/dws" <<'EOF'
#!/bin/bash
sleep 300 &
sleep 300
EOF
chmod +x "$FAKE_DIR/dws"
"$FAKE_DIR/dws" event consume --profile "unittest:fakebot" >/dev/null 2>&1 &
FAKE_PID=$!
disown "$FAKE_PID" 2>/dev/null
"$FAKE_DIR/dws" event consume --profile "unittest:otherbot" >/dev/null 2>&1 &
OTHER_PID=$!
disown "$OTHER_PID" 2>/dev/null
sleep 1

# 载入钩子（COMP_NAMES 置空避免覆盖逻辑报错；log 输出屏蔽）
COMP_NAMES=()
source "$SCRIPT_DIR/bin/custom/start_funcs.sh" 2>/dev/null
DWS_PROFILE="unittest:fakebot" stop_extra_cleanup KILL 2>/dev/null
sleep 1

assert_eq "stop_extra_cleanup 清扫匹配 profile 的 dws event" "1" \
    "$(kill -0 "$FAKE_PID" 2>/dev/null && echo 0 || echo 1)"
assert_eq "stop_extra_cleanup 不误伤其他 profile" "1" \
    "$(kill -0 "$OTHER_PID" 2>/dev/null && echo 1 || echo 0)"

# teardown：清掉另一棵假进程树 + 临时目录
kill_tree "$OTHER_PID" KILL 2>/dev/null
rm -rf "$FAKE_DIR"

# ---------------------------------------------------------------------------
# 监督器阻塞性 bug 的回归钉子
#
# 两个 bug 都属于「失效模式是静默」那一类，靠人看日志发现不了，所以钉死在测试里：
#   1. check_serve_http 失败时曾返回 "HTTP_FAIL:$port"，而 main() 的判定是 glob `FAIL*`
#      —— 匹配不上，serve HTTP 异常从来无法触发熔断
#   2. 那个 curl 没有超时，serve 卡死时 healthcheck 永不返回，monitor 监督循环停摆
# ---------------------------------------------------------------------------
echo ""
echo "Testing 健康检查判定 + 超时（监督器阻塞性 bug 回归）..."

assert_eq "check_serve_http 失败返回值能匹配 FAIL* 判定" "0" \
    "$(grep -q 'echo "FAIL: serve HTTP' "$SCRIPT_DIR/bin/core/healthcheck.sh" && echo 0 || echo 1)"
assert_eq "healthcheck.sh 不再 echo 匹配不上判定的 HTTP_FAIL token" "0" \
    "$(grep -c 'echo "HTTP_FAIL' "$SCRIPT_DIR/bin/core/healthcheck.sh" || true)"
assert_eq "check_serve_http 的 curl 带硬超时" "0" \
    "$(grep -q -- '--max-time' "$SCRIPT_DIR/bin/core/healthcheck.sh" && echo 0 || echo 1)"
assert_eq "monitor 用 run_with_timeout 包裹 healthcheck" "0" \
    "$(grep -q 'run_with_timeout .*healthcheck.sh' "$SCRIPT_DIR/bin/core/monitor.sh" && echo 0 || echo 1)"

# run_with_timeout 的实际行为（不是 grep，是真跑）
assert_eq "run_with_timeout 超时返回 124" "124" \
    "$(run_with_timeout 2 sleep 10 >/dev/null 2>&1; echo $?)"
assert_eq "run_with_timeout 正常完成时透传退出码 0" "0" \
    "$(run_with_timeout 5 true >/dev/null 2>&1; echo $?)"
assert_eq "run_with_timeout 正常完成时透传非零退出码" "3" \
    "$(run_with_timeout 5 bash -c 'exit 3' >/dev/null 2>&1; echo $?)"
# 超时那次必须真的把进程杀掉，不能留孤儿继续跑
TMO_MARK="$(mktemp -t rwt_mark)"
rm -f "$TMO_MARK"
run_with_timeout 2 bash -c "sleep 6; echo leaked > '$TMO_MARK'" >/dev/null 2>&1 || true
sleep 6
assert_eq "run_with_timeout 超时后子进程被真正杀死（无孤儿）" "1" \
    "$([[ -f "$TMO_MARK" ]] && echo 0 || echo 1)"
rm -f "$TMO_MARK"

# ---------------------------------------------------------------------------
# 大脑真实自检：计数窗口语义 + 钩子存在性
#
# 窗口语义每一条都对应一种误报/漏报：倒算历史 → 一重启就误判；累计而非窗口 → 一旦
# 坏过一次就永远超阈值；body 行被计数 → 模型输出能伪造健康状态。
# ---------------------------------------------------------------------------
echo ""
echo "Testing 大脑自检计数窗口（count_new_matches）..."

_BP='^\[[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8}\] transport=(http|cli) .* ok=False'
CNT_LOG=$(mktemp); CNT_OFF="$CNT_LOG.off"; rm -f "$CNT_OFF"
_cnt_fail() { printf '[2026-08-08 09:18:35] transport=http model=m elapsed=1s prompt_len=1 reply_len=0 ok=False err=x\n' >> "$CNT_LOG"; }
_cnt() { count_new_matches "$CNT_LOG" "$CNT_OFF" "$_BP" "${1:-}" | awk '{print $3}'; }

_cnt_fail; _cnt_fail; _cnt_fail
assert_eq "首次运行不倒算历史失败" "0" "$(_cnt consume)"
_cnt_fail; _cnt_fail
assert_eq "统计新增 2 条" "2" "$(_cnt consume)"
assert_eq "窗口非累计（无新增归零）" "0" "$(_cnt consume)"
_cnt_fail
assert_eq "peek 不消费窗口（第一次）" "1" "$(_cnt)"
assert_eq "peek 不消费窗口（第二次仍可见）" "1" "$(_cnt)"
_cnt consume >/dev/null
printf '[2026-08-08 09:20:00] <<< RESP status=200 body={"t":"transport=http ok=False"}\n' >> "$CNT_LOG"
assert_eq "RESP body 里的 ok=False 不被计数（锚定行首）" "0" "$(_cnt consume)"
_cnt_fail; : > "$CNT_LOG"; _cnt_fail
assert_eq "日志被截断后从头计数" "1" "$(_cnt consume)"
CNT_LOG2=$(mktemp)
printf '[2026-08-08 09:18:35] transport=cli model=m elapsed=1s prompt_len=1 reply_len=0 ok=False\n' >> "$CNT_LOG2"
mv "$CNT_LOG2" "$CNT_LOG"
assert_eq "日志轮转（inode 变）后从头计数" "1" "$(_cnt consume)"
assert_eq "日志不存在时返回 0 且不报错" "0" \
    "$(count_new_matches /nonexistent/nope.log "$CNT_OFF" "$_BP" | awk '{print $3}')"
rm -f "$CNT_LOG" "$CNT_OFF"

assert_eq "brain 检查已接进硬失败判定列表" "0" \
    "$(grep -q '"brain|\$r_brain"' "$SCRIPT_DIR/bin/core/healthcheck.sh" && echo 0 || echo 1)"
assert_eq "healthcheck 支持 --consume" "0" \
    "$(grep -q -- '--consume) consume=' "$SCRIPT_DIR/bin/core/healthcheck.sh" && echo 0 || echo 1)"
assert_eq "monitor 守护循环传 --consume" "0" \
    "$(grep -q 'healthcheck.sh" --consume' "$SCRIPT_DIR/bin/core/monitor.sh" && echo 0 || echo 1)"
assert_eq "offset 文件登记进可清理状态表" "0" \
    "$(grep -q '.opencode-log.offset' "$SCRIPT_DIR/bin/core/lib.sh" && echo 0 || echo 1)"
assert_eq "brain_probe.py 语法正确" "0" \
    "$(python3 -m py_compile "$SCRIPT_DIR/bin/custom/brain_probe.py" 2>&1; echo $?)"

# custom 钩子存在性（COMP_NAMES=() 见上文：未绑定数组在 set -u + bash 3.2 下是致命的）
COMP_NAMES=()
source "$SCRIPT_DIR/bin/custom/start_funcs.sh" >/dev/null 2>&1
assert_eq "custom 定义 brain_probe 钩子" "0" \
    "$(declare -F brain_probe >/dev/null 2>&1 && echo 0 || echo 1)"
assert_eq "custom 定义 notify_alert_handler 钩子（熔断告警不再静默）" "0" \
    "$(declare -F notify_alert_handler >/dev/null 2>&1 && echo 0 || echo 1)"

# 主管审核回路：模板必须默认关（它改变默认回复行为——提问者不再立即拿到答案），
# 且开关/超时/知识库三项齐全，否则 fork 的人升级后行为会被动改变。
echo ""
echo "Testing supervisor_review config contract..."
CONST="$SCRIPT_DIR/config/constants.sh"
# 断言锚定 `^export VAR=` 而不是裸变量名：注释里提到同名变量（如 ACK_SUPERVISOR_ONLY
# 的说明引用了 CAP_SUPERVISOR_REVIEW_ENABLED）会让计数漂移，与定义本身无关。
assert_eq "constants.sh 定义 CAP_SUPERVISOR_REVIEW_ENABLED" "1" \
    "$(grep -c '^export CAP_SUPERVISOR_REVIEW_ENABLED=' "$CONST")"
assert_eq "模板默认关（:-0）" "1" \
    "$(grep -c '^export CAP_SUPERVISOR_REVIEW_ENABLED="${CAP_SUPERVISOR_REVIEW_ENABLED:-0}"' "$CONST")"
assert_eq "constants.sh 定义 SUPERVISOR_REVIEW_TIMEOUT" "1" \
    "$(grep -c '^export SUPERVISOR_REVIEW_TIMEOUT=' "$CONST")"
assert_eq "constants.sh 定义 AGENT_KNOWLEDGE_FILE" "1" \
    "$(grep -c '^export AGENT_KNOWLEDGE_FILE=' "$CONST")"
# #107：群聊也送审 —— 群里的回答是公开发言，比单聊更该先过主管。O2O_ONLY=1 是退回老行为的逃生门。
assert_eq "SUPERVISOR_REVIEW_O2O_ONLY 默认 0（群聊也审）" "1" \
    "$(grep -c '^export SUPERVISOR_REVIEW_O2O_ONLY="${SUPERVISOR_REVIEW_O2O_ONLY:-0}"' "$CONST")"
assert_eq "能力已在 capabilities/__init__ 注册" "1" \
    "$(grep -q 'from custom.capabilities import supervisor_review' "$SCRIPT_DIR/src/custom/capabilities/__init__.py" && echo 1 || echo 0)"
# 知识库含真实对话内容，绝不能提交
assert_eq "knowledge/ 已 gitignore" "1" \
    "$(grep -c '^knowledge/' "$SCRIPT_DIR/.gitignore")"

# ack 主管闸门（#106）：只给主管贴状态表情。默认开，但必须有 has_supervisor() 兜底——
# 否则未配主管的部署会一个表情都不贴（CAP_ACK_ENABLED 默认开，属无声行为回退）。
# 群消息闸门（group_gate）：订阅整群后，没 @ 我的群消息不进大脑；并合并"群流+@流"双投递。
assert_eq "constants.sh 定义 CAP_GROUP_GATE_ENABLED" "1" \
    "$(grep -c '^export CAP_GROUP_GATE_ENABLED=' "$CONST")"
assert_eq "group_gate 默认开（:-1）" "1" \
    "$(grep -c '^export CAP_GROUP_GATE_ENABLED="${CAP_GROUP_GATE_ENABLED:-1}"' "$CONST")"
assert_eq "group_gate 已在 capabilities/__init__ 注册" "1" \
    "$(grep -q 'from custom.capabilities import group_gate' "$SCRIPT_DIR/src/custom/capabilities/__init__.py" && echo 1 || echo 0)"
# 订阅相关的 export 必须用直引号：曾把 " 写成中文引号 ”，DWS_EVENT_GROUP 会得到字面值
# ”” —— 非空，于是 dws-connect 误判"要订阅群"并拿垃圾 conversationId 去 consume。
assert_eq "constants.sh 无中文引号赋值" "0" \
    "$(grep -c '^export [A-Z_]*=”' "$CONST")"

assert_eq "constants.sh 定义 ACK_SUPERVISOR_ONLY" "1" \
    "$(grep -c '^export ACK_SUPERVISOR_ONLY=' "$CONST")"
assert_eq "ACK_SUPERVISOR_ONLY 默认开（:-1）" "1" \
    "$(grep -c '^export ACK_SUPERVISOR_ONLY="${ACK_SUPERVISOR_ONLY:-1}"' "$CONST")"
assert_eq "ack 主管闸门带 has_supervisor 兜底" "1" \
    "$(grep -c 'has_supervisor() and not is_supervisor' "$SCRIPT_DIR/src/custom/capabilities/ack.py")"
# 身份判定收敛到 custom/identity.py，能力之间不互相 import（ack 常开 / supervisor_review 默认关）
assert_eq "identity 模块存在" "0" \
    "$([ -f "$SCRIPT_DIR/src/custom/identity.py" ] && echo 0 || echo 1)"
assert_eq "ack 不 import supervisor_review 能力" "0" \
    "$(grep -c 'import supervisor_review' "$SCRIPT_DIR/src/custom/capabilities/ack.py")"

# 「AI」角标：dws chat message send 的 --ai-tag 默认 true，不显式关掉消息右上角会常驻
# 一个 AI 标（拟人化的反面）。每个 send 调用点都必须带 --ai-tag=false。
# 注：send-by-bot 无此参数，机器人身份本就不伪装成真人，不在范围内。
echo ""
echo "Testing ai-tag suppression on dws send call sites..."
_ai_tag_missing=0
# 只数**真实调用/真实参数**，不数注释：
#   Python 调用 = 列表形式 `"message", "send"`（"send-by-bot" 不命中，模式含右引号）
#   Python 参数 = 带引号的字符串字面量 `"--ai-tag=false"`
# 两个坑都已用变异测试验证过：
#   1) 若数裸 `ai-tag=false`，注释里那句说明会把计数顶上去 → 真漏传也检不出（假阴性）
#   2) `grep -c` 零匹配时**自己就打印 0** 且退出码 1，再接 `|| echo 0` 会得到 "0\n0"
#      多行值，令 -gt 数值比较失效 → 恒通过。故用 `|| true` + 数字校验（同 lib.sh 写法）
_count() {
    local n
    n=$(grep -c "$1" "$2" 2>/dev/null || true)
    [[ "$n" =~ ^[0-9]+$ ]] || n=0
    printf '%s' "$n"
}
_count_re() {
    local n
    n=$(grep -cE "$1" "$2" 2>/dev/null || true)
    [[ "$n" =~ ^[0-9]+$ ]] || n=0
    printf '%s' "$n"
}
for f in "$SCRIPT_DIR/src/custom/replier.py" \
         "$SCRIPT_DIR/src/custom/capabilities/supervisor_review.py" \
         "$SCRIPT_DIR/src/custom/capabilities/startup_report.py"; do
    sends=$(_count '"message", "send"' "$f")
    tags=$(_count '"--ai-tag=false"' "$f")
    if [[ "$sends" -gt "$tags" ]]; then
        echo "    ${f##*/}: send=$sends ai-tag=$tags"
        _ai_tag_missing=$((_ai_tag_missing + 1))
    fi
done
for f in "$SCRIPT_DIR/bin/custom/start_funcs.sh"; do
    sends=$(_count_re '^[[:space:]]*dws chat message send' "$f")
    tags=$(_count_re '^[[:space:]]*--ai-tag=false' "$f")
    if [[ "$sends" -gt "$tags" ]]; then
        echo "    ${f##*/}: send=$sends ai-tag=$tags"
        _ai_tag_missing=$((_ai_tag_missing + 1))
    fi
done
assert_eq "所有 dws send 调用点都带 --ai-tag=false" "0" "$_ai_tag_missing"

# 报告
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Results: $PASS passed, $FAIL failed, 0 skipped"
if [[ $FAIL -gt 0 ]]; then
    echo "Failed tests:"
    for t in "${FAILED_TESTS[@]}"; do
        echo "  - $t"
    done
    exit 1
fi
