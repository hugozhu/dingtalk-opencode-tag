#!/bin/bash
# constants.sh — 可配置常量模板
#
# 用户在这里覆盖所有可配置常量，被 bin/core/*.sh 引用
# 复制本文件为 constants.local.sh（被 .gitignore 忽略）填入真实值
#
# ⚠️ 两个部署必踩坑（后台/launchd/systemd 托管时；前台交互式 shell 不明显）：
#   1. PATH：托管进程的 PATH 极简，找不到 dws(~/.local/bin)、opencode(~/.opencode/bin)。
#      必须在 constants.local.sh 里把这两个目录加进 PATH（见文件末尾 PATH 行）。
#      症状：replier 报 "No such file or directory: 'dws'"。
#   2. AGENT_PROFILE：回复/CLI 路径读 agent_common.PROFILE（来自 AGENT_PROFILE），
#      **必须**与 DWS_PROFILE 填成同一个真实 profile。留占位 'your-profile' 会导致
#      dws 用不存在的 profile → "未登录，请先执行 dws auth login"。

# --- 时区 ---
# reboot.sh 用 `env -i` 起服务（#71，为了让改过的 config 真生效），它保留了 LANG 却
# **没保留 TZ** —— 守护进程于是跑在 UTC，而钉钉给的时间戳是本地时区。后果：
#   · monitor.log 的时间和钉钉会话里的时间差 8 小时，排查时对不上号；
#   · 任何"按时间窗口查消息"的代码锚点偏 8 小时（曾导致贴表情裁决静默失效）。
# 显式设死，别指望继承。非中国区改成自己的时区。
export TZ="${TZ:-Asia/Shanghai}"

# --- 数字员工身份 ---
export AGENT_ROBOT_CODE="${AGENT_ROBOT_CODE:-your-robot-code}"
export AGENT_USER_ID="${AGENT_USER_ID:-your-user-id}"
# ⚠️ AGENT_PROFILE 必须 = DWS_PROFILE（真实 profile），否则回复报未登录。见文件顶部坑#2。
export AGENT_PROFILE="${AGENT_PROFILE:-your-profile}"

# --- 路径 ---
export PROJECT_DIR="${PROJECT_DIR:-/path/to/your/project}"
export AGENT_BOT_DIR_SUBSTR="${AGENT_BOT_DIR_SUBSTR:-your-agent-workdir}"

# --- 守护进程参数 ---
export CHECK_INTERVAL="${CHECK_INTERVAL:-1800}"          # 健康检查间隔（秒）
export MAX_FAILURES="${MAX_FAILURES:-3}"                  # 连续失败熔断阈值
export WARMUP_TIMEOUT="${WARMUP_TIMEOUT:-60}"             # warmup 超时
# 健康检查自身的硬超时。serve 卡死（进程在但不应答）时，无超时的探测会让 monitor
# 的监督循环停摆——不重启也不告警。两个都是防御性上限，正常路径远远用不到。
export HEALTHCHECK_TIMEOUT="${HEALTHCHECK_TIMEOUT:-120}"  # monitor 包裹单次 healthcheck
export HEALTHCHECK_HTTP_TIMEOUT="${HEALTHCHECK_HTTP_TIMEOUT:-8}"  # check_serve_http 的 curl

# 大脑真实自检（检查7）。进程活着 + HTTP 200 **不等于**大脑活着——前两者都不碰模型。
# 触发式：每次健康检查统计 $AGENT_OPENCODE_LOG 里「距上次检查以来」新增的 ok=False 条数，
# ≥ 阈值才真发一次模型调用（"1+1"）。失败即硬失败 → monitor 重启 serve；
# 连续 MAX_FAILURES 次 → 熔断告警。零失败时不发任何请求 → 稳态零 token 成本。
# 注：一次失败的对话可能记两条（HTTP 一条 + CLI 回退一条），故阈值 3 约等于 2 轮失败。
export HEALTHCHECK_BRAIN_CHECK_ENABLED="${HEALTHCHECK_BRAIN_CHECK_ENABLED:-1}"
export HEALTHCHECK_BRAIN_FAIL_THRESHOLD="${HEALTHCHECK_BRAIN_FAIL_THRESHOLD:-3}"
export HEALTHCHECK_BRAIN_PROBE_TIMEOUT="${HEALTHCHECK_BRAIN_PROBE_TIMEOUT:-60}"
export KICKSTART_RETRY_INTERVAL="${KICKSTART_RETRY_INTERVAL:-10}"
export LAUNCHD_LABEL="${LAUNCHD_LABEL:-com.example.agent-connect}"
# /reboot 重启机制: auto(默认,自动判定) | launchd(launchctl kickstart) | nohup(直接重启
# monitor 进程)。auto=launchd agent 已加载则 launchd,否则 nohup。用 nohup 手动托管
# （非 launchd）的部署会被 auto 判为 nohup;想强制可显式设 REBOOT_RESTART_MODE=nohup。
export REBOOT_RESTART_MODE="${REBOOT_RESTART_MODE:-auto}"

# --- 健康检查 ---
export LOG_INACTIVITY_THRESHOLD="${LOG_INACTIVITY_THRESHOLD:-2100}"   # 35 分钟
# 进程 cmdline 匹配模式（healthcheck verify_pid 用，字面子串匹配）。默认对应 harness
# 自带实现（dws dev connect / event_watcher.py）。FDE 换了 connect 实现时必须覆盖，
# 否则 check_connect 恒硬失败 → monitor 全量重启循环。
# 例：本仓库 connect 用 dws-connect.sh → 在 constants.local.sh 设 CONNECT_CHECK_PATTERN=dws-connect.sh
export CONNECT_CHECK_PATTERN="${CONNECT_CHECK_PATTERN:-agent-connect.*--unified-app-id}"
export EVENT_WATCHER_CHECK_PATTERN="${EVENT_WATCHER_CHECK_PATTERN:-event_watcher.py}"

# --- 视觉/多模态 ---
export PROXY_URL="${PROXY_URL:-http://localhost:4000/v1}"
export PROXY_KEY="${PROXY_KEY:-sk-1234}"
export VISION_MODEL="${VISION_MODEL:-gemini-3.1-flash-image}"
# 经 opencode serve 自身识别图片的多模态模型（无需外部 proxy）。provider/model 格式。
# 可选免费模型：opencode/mimo-v2.5-free；或走外部 proxy 的 gemini（识别质量更好）。
# 留空则只用外部 proxy (PROXY_URL + VISION_MODEL)。
export AGENT_VISION_MODEL="${AGENT_VISION_MODEL:-}"

# --- 业务特定（用户扩展）---
# 在这里加自己的业务常量

# --- dws event connect（bin/custom/dws-connect.sh）---
# 敏感值：真实的群 conversationId / profile 填在 config/constants.local.sh（gitignored），
# 不要写进本模板文件。群订阅 + 单聊(o2o)订阅 + @我(at)订阅可任意组合，至少开一种。
export DWS_EVENT_KEY="${DWS_EVENT_KEY:-user_im_message_receive_group}"
export DWS_EVENT_GROUP="${DWS_EVENT_GROUP:-}"   # 群 openConversationId（订阅群消息必填，敏感）
export DWS_EVENT_GROUP_NAME="${DWS_EVENT_GROUP_NAME:-}"  # 群聊名称（可选，用于启动报告显示）
# 单聊(o2o)订阅：对端 userId 列表（逗号分隔）。钉钉 o2o 事件只能按”对端 userId”订阅，
# 每个对端起一个 consumer。留空=不订阅单聊。例：给数字员工发单聊的真人 userId。
export DWS_EVENT_O2O_USERS="${DWS_EVENT_O2O_USERS:-}"  # 敏感，勿提交
# @我订阅：数字员工账号在**任意群**被 @ 时收到消息（事件 user_im_message_receive_at，
# rule_type=at 个人级订阅，无需 group/user 参数）。1/true/yes/on=开，留空/0=不订阅。
# 适合“只在被 @ 时才响应、又不想逐个配置群 conversationId”的场景。与群/单聊订阅可并存 ——
# 但群里被 @ 的消息会被**投递两次**（群流一份、@ 流一份，且只有 @ 流那份带 atMention 标记），
# 由 capabilities/group_gate 合并成一份（core 的“按能力”dedup 挡不住，见该模块头注释）。
export DWS_EVENT_AT="${DWS_EVENT_AT:-}"
# 群消息闸门（capabilities/group_gate，默认开）：订阅整群时，群里**没 @ 数字员工**的消息
# 只记账/标已读，不进大脑、不回复（真人同事也不会插嘴每条群聊）；同时合并上面说的双流
# 重复投递。设 0 = 群里每条都回，且失去双流去重（订阅整群 + DWS_EVENT_AT 时会重复回复）。
export CAP_GROUP_GATE_ENABLED="${CAP_GROUP_GATE_ENABLED:-1}"
export DWS_PROFILE="${DWS_PROFILE:-}"           # 组织 profile（敏感，勿提交）

# --- 数字员工大脑 / 回复（src/custom/brain.py + replier.py）---
# 大脑后端: echo(默认,零依赖) | opencode(serve HTTP 优先, 失败回退 opencode run CLI) | proxy(LLM API)
export AGENT_BRAIN="${AGENT_BRAIN:-echo}"
export AGENT_OPENCODE_MODEL="${AGENT_OPENCODE_MODEL:-opencode/deepseek-v4-flash-free}"
# 工具授权审批（可选）：brain 临时 session 的 per-session 权限规则（serve v1 格式 JSON 数组）。
# 配了 ask 规则后命中的工具调用会挂起 → permission 能力发钉钉群审批（回「同意/总是/拒绝」，
# CAP_PERMISSION_TIMEOUT 默认 60s 未回自动拒绝）。空=不启用（serve 默认全放行）。示例：
#   export AGENT_OPENCODE_PERMISSION='[{"permission":"bash","pattern":"*","action":"ask"}]'
export AGENT_OPENCODE_PERMISSION="${AGENT_OPENCODE_PERMISSION:-}"
# 长程任务超时（#75）：活动感知，不再一刀切墙钟硬超时。只要 session 仍在产出就不主动超时，
# 直到完成 / 空闲卡死 / 超绝对上限 / 用户取消（发「取消/停止/stop//cancel」）。
export AGENT_OPENCODE_IDLE_TIMEOUT="${AGENT_OPENCODE_IDLE_TIMEOUT:-300}"    # 无活动多少秒判卡死并 abort
export AGENT_OPENCODE_MAX_TIMEOUT="${AGENT_OPENCODE_MAX_TIMEOUT:-3600}"     # 绝对上限硬超时兜底（0=不设上限）
export AGENT_OPENCODE_ACTIVITY_POLL="${AGENT_OPENCODE_ACTIVITY_POLL:-15}"   # 活动探测间隔秒
# 会话中毒自愈：后端 stream-error 会让复用 session 回空/终态坏回合，卡住后续所有轮。
export AGENT_OPENCODE_EMPTY_RETRY="${AGENT_OPENCODE_EMPTY_RETRY:-1}"   # 复用会话回空 → 丢弃并新建重试一次
export AGENT_OPENCODE_ERROR_ABORT="${AGENT_OPENCODE_ERROR_ABORT:-1}"   # 助手消息 completed-with-error/空 → 立即 abort，不等 idle 超时
# 兼容：旧 AGENT_OPENCODE_TIMEOUT 若显式设置，会作为 AGENT_OPENCODE_IDLE_TIMEOUT 的默认值。
# 用户取消关键词（整句匹配 → abort 当前会话正在跑的任务），逗号分隔：
export AGENT_CANCEL_KEYWORDS="${AGENT_CANCEL_KEYWORDS:-/cancel,取消,停止,stop}"
# 会话连续性（#56）：同一会话复用 serve session，带多轮上下文记忆。
# 开（默认）=同一 conv 复用 session：TTL 闲置过期 / LRU 逐出 / 用户发重置关键词断上下文。
# 设 AGENT_SESSION_REUSE=0（或空）回退旧的无状态语义（每条消息新建 session 即删，无记忆）。
export AGENT_SESSION_REUSE="${AGENT_SESSION_REUSE:-1}"
export AGENT_SESSION_TTL="${AGENT_SESSION_TTL:-1800}"          # 闲置多少秒后过期重建（默认 30min）
export AGENT_SESSION_MAX="${AGENT_SESSION_MAX:-64}"            # 最多保活多少个 conv 的 session（LRU）
# handler 派发线程池（#82）：拆成两条独立限流车道，避免长任务饿死轻交互。
# 某一池打满不会拖垮另一池（跨会话 head-of-line blocking 隔离）。
export AGENT_HANDLER_WORKERS="${AGENT_HANDLER_WORKERS:-8}"     # task 池：图片/文件/合并转发/聚合等较重处理
export AGENT_REPLY_WORKERS="${AGENT_REPLY_WORKERS:-4}"         # reply 池：交互式文本回复（快车道）
# 触发断上下文（删旧 session）的整句关键词，逗号分隔
export AGENT_SESSION_RESET_KEYWORDS="${AGENT_SESSION_RESET_KEYWORDS:-/new,新话题,重新开始,清空上下文}"

# 会话统计摘要（#63）：session 结束时自动发送统计信息。
# 是否启用统计摘要（默认开启）
export AGENT_SESSION_SUMMARY_ENABLED="${AGENT_SESSION_SUMMARY_ENABLED:-1}"
# 统计摘要的触发场景（逗号分隔）
# reset: 用户主动重置会话时
# ttl: TTL 过期时
# lru: LRU 逐出时（可能频繁，建议关闭）
# command: 用户发送 /stats 命令时
export AGENT_SESSION_SUMMARY_TRIGGERS="${AGENT_SESSION_SUMMARY_TRIGGERS:-reset,command}"
# 是否仅在单聊中发送（默认 1，群聊不发避免噪音）
export AGENT_SESSION_SUMMARY_O2O_ONLY="${AGENT_SESSION_SUMMARY_O2O_ONLY:-1}"

# 单次任务统计（#76）：每次任务产出回复后，单独发一条「本次任务统计」（耗时/工具调用/
# 输入输出 token/缓存命中率）。与上面 #63 会话摘要（累计、会话结束触发）互补——这里是
# 单次交互 delta、回复发出后即发。默认关（避免每条消息刷一条统计）。
export CAP_TASK_STATS_ENABLED="${CAP_TASK_STATS_ENABLED:-0}"          # 开启每次任务完成推送统计
export AGENT_TASK_STATS_O2O_ONLY="${AGENT_TASK_STATS_O2O_ONLY:-1}"   # 仅单聊发（群聊默认不发）

# LLM 不可用/超时/出错时给用户的兜底提示（#59）：避免消息被静默吞掉、ack 永远停在
# 「处理中」。设为空串关闭兜底（回退旧的静默行为）。模型正常但没话说（empty）仍静默。
export AGENT_FALLBACK_REPLY="${AGENT_FALLBACK_REPLY:-⚠️ 暂时无法处理你的消息，请稍后再试。}"
# 启动报告：服务启动后向订阅单聊用户的主管发送详细服务状态报告（默认开）。
# 报告包含数字员工身份、订阅配置、组件状态、健康检查、配置概要等。设 0 关闭。
export CAP_STARTUP_REPORT_ENABLED="${CAP_STARTUP_REPORT_ENABLED:-1}"
# 主管兜底：报告收件人优先取通讯录 orgMasterUserId/orgMasterDisplayName；数字员工账号
# 常常没挂组织汇报线（两个字段都是 null），此时报告会整份发不出去。配上这两项即可兜底，
# 通讯录查得到时以通讯录为准。留空 = 保持原行为（查不到就不发）。
export AGENT_SUPERVISOR_USER_ID="${AGENT_SUPERVISOR_USER_ID:-}"
export AGENT_SUPERVISOR_NAME="${AGENT_SUPERVISOR_NAME:-}"

# 主管审核回路（capabilities/supervisor_review）：数字员工不直接回复提问者，改为
# AI 先出草稿 → 转交主管审核 → 主管回「#N 同意」放行 / 「#N <答案>」改写 / 「#N 忽略」
# 不回。主管改写过的答案存入知识库并注入后续 system prompt（AI 下次能自己答对）。
# 单聊和群聊都审（群里是公开发言，更该先过一遍）；连主管自己在群里的提问也审 —— 闸门管的
# 是"数字员工说什么"而不是"谁在问"。**裁决只认主管单聊**，群里主管的话一律当新提问。
# **默认关**：它改变默认回复行为（提问者不再立即拿到答案），必须显式开。
# 依赖 AGENT_SUPERVISOR_USER_ID（发卡片）+ AGENT_SUPERVISOR_NAME（认主管入站消息）。
export CAP_SUPERVISOR_REVIEW_ENABLED="${CAP_SUPERVISOR_REVIEW_ENABLED:-0}"
# 超时未裁决(秒) → **按不回复处理**（不是放行草稿！没人管不等于默认同意，自动放行
# 等于把一条从没被人看过的草稿发出去，群里还是公开发言）。超时的待审会归档，主管
# 事后**引用那张卡片**回一句仍能补裁。0=永不超时（待审一直挂着，提问者也一直等）。
export SUPERVISOR_REVIEW_TIMEOUT="${SUPERVISOR_REVIEW_TIMEOUT:-600}"
# 1=只审单聊（群聊直接回，不经主管）。默认 0=群聊也审。群里被审的范围与 text_reply 一致：
# 数字员工本来会回的那些（整群订阅=每条；只订阅 DWS_EVENT_AT=只有被 @ 的）。
export SUPERVISOR_REVIEW_O2O_ONLY="${SUPERVISOR_REVIEW_O2O_ONLY:-0}"
# 主管显示名别名（逗号分隔）。bridge 只传显示名不传 userId，同一人可能显示为
# "hugozhu"/"朱鸿"，都列上避免认不出主管。
export AGENT_SUPERVISOR_ALIASES="${AGENT_SUPERVISOR_ALIASES:-}"

# --- 裁决交互（#107）：让主管少打字 ---
# 贴表情裁决：主管直接给待审卡片贴个表情就完成裁决，最高频的「同意」降到一次点击。
# 实现是轮询卡片上的表情回应（list-emotion-replies），不是订阅 reaction 事件 —— 后者的
# msgId 是被贴表情那条原消息的 id，会和 ack/group_gate 的 msgId 表撞车。
# 秒=轮询间隔；0=关闭（退回纯文字裁决）。没有待审时不发任何请求。
export SUPERVISOR_REACTION_POLL="${SUPERVISOR_REACTION_POLL:-5}"
# 表情名 → 裁决。注意钉钉返回的是**表情名**而非 unicode 码点，各客户端叫法可能不同：
# 认不出的表情不会被当成裁决，数字员工会问一句并把真实名字记进 monitor.log，按需补进这里。
export SUPERVISOR_APPROVE_EMOJIS="${SUPERVISOR_APPROVE_EMOJIS:-赞,好的,OK,👍,✅}"
export SUPERVISOR_IGNORE_EMOJIS="${SUPERVISOR_IGNORE_EMOJIS:-不行,取消,❌,🚫}"
# 草稿超过多少行就在卡片里折叠（全文紧跟着单独发一条）。0=不折叠。
# 几十行的草稿会把「问题」和操作提示挤出一屏，手机上要滚半天才找得到怎么裁决。
export SUPERVISOR_CARD_DRAFT_MAX_LINES="${SUPERVISOR_CARD_DRAFT_MAX_LINES:-12}"
# 短于此长度、又不在裁决关键词里的回复 → **问一句而不是当答案发出去**。
# 老行为是「不在白名单=改写」，主管随口回「可以发」就会把这三个字发给提问者（群聊里
# 直接公开）。要写短答案就用无歧义前缀：「#N 改：<答案>」。
# 注意这个阈值只挡短句：长的随口评论（如「这个回答我觉得不太对，你再想想」）仍会被
# 当成答案发出去。**要彻底堵死就把它调得很大**（如 9999）—— 那等于「改写一律要带
# 改：/答： 前缀」，没有任何猜测；代价是每次改写多敲两个字。
export SUPERVISOR_UNCLEAR_MAX_LEN="${SUPERVISOR_UNCLEAR_MAX_LEN:-8}"
# 主管**引用**待审卡片回复时，把内容交大模型判"能不能原样发给提问者"：
#   能   → 直接发（不用敲「改：」前缀，主管想怎么写就怎么写）
#   不能 → 一个字都不发，回主管一句（比如「这个不太对，你再想想」是说给 AI 听的）
# 这是上面那个字数阈值的升级版——长度分不开「这个回答我觉得不太对，你再想想」（评语）
# 和「找财务小王签字」（答案）。判不了（模型不可用/答非所问）自动回落字数阈值。
# 分工：贴表情 = 采纳/忽略，引用回复 = 正式作答。0=关掉判断，退回纯字数启发式。
export SUPERVISOR_JUDGE_QUOTED="${SUPERVISOR_JUDGE_QUOTED:-1}"
# 审核流水（JSONL，相对 PROJECT_DIR）：短号 #N 靠它**跨重启唯一**。
# 以前 _seq_counter 是纯内存的，重启归零 → 主管会话里同时存在多张「待审 #3」，反查卡片
# 时会匹配到上一轮那张同号的，贴表情从此静默失效。放 knowledge/ 下而不是根目录点文件——
# 后者会被 stop/reboot 的 clean_runtime_state() 删掉。删了这个文件 = 短号重新从 1 开始。
export SUPERVISOR_REVIEW_JOURNAL="${SUPERVISOR_REVIEW_JOURNAL:-knowledge/supervisor_reviews.jsonl}"
# 启动时只回放流水末尾这么多字节（跑久了文件会很大，全量解析拖慢每次重启）
export SUPERVISOR_REVIEW_JOURNAL_TAIL="${SUPERVISOR_REVIEW_JOURNAL_TAIL:-262144}"
# 知识库（JSONL，相对 PROJECT_DIR）。AGENT_KNOWLEDGE_MAX=0 可关闭知识注入。
export AGENT_KNOWLEDGE_FILE="${AGENT_KNOWLEDGE_FILE:-knowledge/supervisor_qa.jsonl}"
export AGENT_KNOWLEDGE_MAX="${AGENT_KNOWLEDGE_MAX:-20}"
# opencode serve 端口（start_serve 用；密码自动生成写 .serve.pwd）。brain(opencode) 走
# 此 serve 的 HTTP 接口生成回复。
export OPENCODE_SERVE_PORT="${OPENCODE_SERVE_PORT:-4096}"
# 回复模式: log(默认,只记日志不真发) | bot(机器人 send-by-bot) | user(当前用户 send)
export AGENT_REPLY_MODE="${AGENT_REPLY_MODE:-log}"
# 回复总长度上限（字符，外层 backstop 防跑飞刷屏），默认 12000。超出才截断加「…（已截断）」。
export AGENT_MAX_REPLY_CHARS="${AGENT_MAX_REPLY_CHARS:-12000}"
# 单条钉钉消息分片上限（字符），默认 3500。长回复按段落/换行拆成多条「（i/n）」顺序发出，
# 不丢内容也不撑爆钉钉服务端单消息上限（~20000 字节）。设很大可实质关闭分片。
export AGENT_REPLY_CHUNK_CHARS="${AGENT_REPLY_CHUNK_CHARS:-3500}"
# 防回环：数字员工自己的发送名（逗号分隔），过滤掉避免自问自答。
# ⚠️ user 模式：回复以你本人身份发出，必须把你的真实显示名加进来（如 hugozhu）。
export AGENT_SELF_NAMES="${AGENT_SELF_NAMES:-数字员工,Claude Code}"

# --- 能力开关（src/custom/capabilities/，可组装/可选配）---
# 每个能力一个 CAP_<NAME>_ENABLED 开关。1/true/yes/on=开，0/false/no/off=关；
# 不设则用能力自带默认。关掉的能力压根不注册、不参与分发。
export CAP_TEXT_REPLY_ENABLED="${CAP_TEXT_REPLY_ENABLED:-1}"   # 普通文本回复（brain→replier）
export CAP_CANCEL_ENABLED="${CAP_CANCEL_ENABLED:-1}"          # 用户主动取消长程任务（#75）
export CAP_FORWARD_ENABLED="${CAP_FORWARD_ENABLED:-1}"        # 合并转发（chatRecord 聊天记录）
# 合并转发检测正则（匹配 content 摘要特征）。DingTalk 合并转发 content 形如
# "群聊的聊天记录\n..."；默认匹配"聊天记录"。命中后 list-by-ids 反查 forwardMessages 二次确认。
# export CAP_FORWARD_SUMMARY_PATTERN="聊天记录"
# 合并转发注入 agent 的 prompt 末句指令（点明这是合并转发聊天记录 + 任务）。留空用内置默认。
# export CAP_FORWARD_PROMPT_FOOTER="以上是一段钉钉「合并转发」的聊天记录，…请理解语境后回应/总结。"
# 单条内层消息内容截断上限（防超长附件撑爆 prompt），默认 4000。
# export CAP_FORWARD_ENTRY_MAX="4000"
export CAP_IMAGE_ENABLED="${CAP_IMAGE_ENABLED:-1}"           # 图片识别（vision 兜底）
# 图片识别需要多模态 proxy 可达（见下方 PROXY_URL/VISION_MODEL）。注入 agent 的末句指令可覆盖：
# export CAP_IMAGE_PROMPT_FOOTER="以上「图片识别内容」由多模态模型提取…请结合说明回应。"
export CAP_AUDIO_ENABLED="${CAP_AUDIO_ENABLED:-1}"           # 语音消息识别（Whisper ASR）
# 语音识别使用本地 Whisper 模型（openai-whisper），将语音转录为文字后发送给大模型处理。
# export AGENT_AUDIO_WHISPER_MODEL="base"     # Whisper 模型：tiny/base/small/medium（默认 base）
# export AGENT_AUDIO_WHISPER_LANGUAGE="zh"    # 识别语言：zh/en/auto（默认 zh 中文）
# export CAP_AUDIO_WHISPER_TIMEOUT="120"      # 转录超时秒数（默认 120）
# export CAP_AUDIO_PROMPT_FOOTER="以上「语音转录内容」由语音识别模型转录…请根据内容回应。"
export CAP_FILE_ENABLED="${CAP_FILE_ENABLED:-1}"             # 文档/文件处理（受控下载+注入）
# 文件能力主动 drive download 到临时目录、读前 N 字节文本注入 agent（避免 agent 自主下载到
# 项目目录）。文本类文件（txt/md/csv/json/日志/代码等）读正文；二进制文件给说明不硬读。
# export CAP_FILE_MAX_BYTES="16384"     # 读取正文字节上限
# export CAP_FILE_PROMPT_FOOTER="以上是用户发送的文件内容…请结合说明回应。"
export CAP_QUESTION_ENABLED="${CAP_QUESTION_ENABLED:-1}"     # Question 交互（钉钉端答 agent 提问）
# Question 超时未答自动 reject 的秒数（serve 端 question 无 TTL，这是安全网防会话卡死），默认 60。
# export CAP_QUESTION_TIMEOUT="60"
export CAP_AGGREGATION_ENABLED="${CAP_AGGREGATION_ENABLED:-0}"  # 群消息聚合（默认关，与逐条回复互斥）
# 聚合开启后：群文本消息不逐条回复，缓冲到时间窗后合并成一次摘要回复。相关参数：
# export CAP_AGGREGATION_WINDOW="300"      # 时间窗秒数（缓冲第一条后多久 flush），默认 300
# export CAP_AGGREGATION_MAX_MSGS="50"     # 缓冲数量上限，达到即立即 flush
# export CAP_AGGREGATION_PROMPT_FOOTER="以上是群里最近多条消息，请做简洁总结/回应，不要逐条复述。"

# 回执能力（已读 + 状态「文字表情」时间线）。收到消息即 mark-read + 在**用户消息上**贴一条
# 「文字表情」回应（DingTalk text-emotion：表情 + 文字同时呈现），随处理进度**原地更新**
# （收到→处理中→处理久了…→完成/失败），不发独立消息、不刷屏、无卡片"生成中"加载态。
# 非消费型：不影响正常回复链路（best-effort：任一步失败只记日志不阻断回复）。
# **默认开**：默认文案已实测可被 create-text-emotion 保存。改文案后建议先手测能否保存
# （`dws chat message create-text-emotion --emotion-name <名> --text <文>`；部分含特殊
# emoji/标点的文案会报"暂不支持保存该文字表情"）。需数字员工 profile 有回执权限。停用设 0。
export CAP_ACK_ENABLED="${CAP_ACK_ENABLED:-1}"
# 只对单聊(conv_type=1)回执（群里逐条贴噪音大）。设 0 则**所有**群消息也回执（噪音大，默认关）。
# 注：群里“被 @ 数字员工”的消息由 ACK_AT_MENTION 单独控制，不受本项影响。默认 1。
export ACK_O2O_ONLY="${ACK_O2O_ONLY:-1}"
# 群里“被 @ 数字员工”的消息也回执（#46）。默认 1。与 ACK_O2O_ONLY 独立：即便只单聊
# （ACK_O2O_ONLY=1），被 @ 的群消息仍回执；未被 @ 的普通群消息不回执。设 0 关闭“被@”这一路。
export ACK_AT_MENTION="${ACK_AT_MENTION:-1}"
# 标记已读总开关（1=开）。已读的范围比“状态表情回执”更宽：**订阅到的所有消息**（单聊 /
# 群被@ / 群普通消息）都会 mark-read；只有单聊和群被@ 才额外贴状态表情（群普通消息只已读、
# 不贴表情，避免逐条噪音）。设 0 则完全不标已读。
export ACK_MARK_READ="${ACK_MARK_READ:-1}"
# 只对**主管**的消息贴状态表情（#106，拟人化）。真人同事不会在每条收到的消息上贴一个
# 状态标签；且主管审核回路（CAP_SUPERVISOR_REVIEW_ENABLED）开启后，非主管的消息实际是
# 转交主管处理的，给提问者贴「正在处理中」与事实不符。
# 主管身份按显示名判定（AGENT_SUPERVISOR_NAME + AGENT_SUPERVISOR_ALIASES）。
# **已读不受本项影响**：所有人的消息照常 mark-read（真人也会已读）。
# **未配主管时自动退化为原行为（都贴）**，故默认 1 对未配主管的部署零影响。
# 设 0 = 回到「所有人都贴」。
export ACK_SUPERVISOR_ONLY="${ACK_SUPERVISOR_ONLY:-1}"
# 文字表情时间线：`delay秒:表情名:文字`，多阶段用 `|` 分隔，按 delay 升序。delay=消息到达后
# 多少秒切到该状态（首个应为 0=收到即贴；文字里可含 : 和 ,，只按前两个 : 切分）。任一时刻
# 只显示一个文字表情（升级=移除旧+贴新）。表情名为 DingTalk **具名表情**（实测 收到/稍等/
# 咖啡/OK/疑问 等；文字表情会先 create-text-emotion 拿 emotionId 再 add，模块内按(名,文)缓存）。
# 默认文案用纯文字：实测含 emoji/特殊标点的文案（🈺、（约 5 分钟）…）会被 create-text-emotion
# 拒（"暂不支持保存该文字表情"），纯文字稳定可存。改文案后建议先手测 create-text-emotion 能存。
# 注：从 5s 改为 1s，避免快速响应时状态滞后于实际回复（#62）
export ACK_STAGES="${ACK_STAGES:-0:稍等:收到|1:稍等:处理中}"
export ACK_DONE="${ACK_DONE:-OK:完成}"          # 完成（表情名:文字）
export ACK_ERROR="${ACK_ERROR:-疑问:未完成}"      # 失败（表情名:文字）
# 周期进度心跳（长任务，#75）：每隔 N 秒若仍在处理 → 原地更新消息上的文字表情（带已耗时分钟）
# + 另发一条独立进度消息到来源会话。配合活动感知超时，长任务用户能周期看到"还在干活"。
export ACK_PROGRESS_INTERVAL="${ACK_PROGRESS_INTERVAL:-300}"   # 心跳间隔秒（默认 5min；0=关闭）
export ACK_PROGRESS_MESSAGE="${ACK_PROGRESS_MESSAGE:-1}"       # 是否额外发独立进度消息（0=只更新表情）
export ACK_PROGRESS_EMOJI="${ACK_PROGRESS_EMOJI:-咖啡}"        # 心跳阶段的表情名
export ACK_PROGRESS_EMOJI_TEXT="${ACK_PROGRESS_EMOJI_TEXT:-处理中{mins}分钟}"   # 表情文字（{mins}=已耗时分钟）
export ACK_PROGRESS_MSG="${ACK_PROGRESS_MSG:-⏳ 仍在处理中，已耗时约 {mins} 分钟，请稍候…}"  # 进度消息模板
# 等"回复已发出"信号的上限秒数（brain 慢 / 空回复不发送时兜底收尾）。留空=自动取
# max(180, 最后阶段delay + 300, AGENT_OPENCODE_MAX_TIMEOUT + 300)，覆盖长任务全程再留冗余。
# export ACK_DONE_TIMEOUT="4200"


# --- 调试 ---
# AGENT_DEBUG=1 时（统一调试总开关）：
#   - agent_common 把每个 serve 请求/响应的完整 body（含发给模型的 prompt / 模型返回）
#     写到 opencode.log，长 body（图片 data_url 等）自动截断头尾；
#   - brain 每次 opencode 调用单独记一条到 opencode.log（transport=http|cli / model / 耗时 /
#     prompt+reply 长度 / reply 预览 / 成败）。opencode 调用出错恒记，不受此开关影响；
#   - ack 能力把动作轨迹（收到/升级/收尾/仅已读）记到 monitor.log；
#   - start_serve 给 opencode serve 加 --print-logs --log-level，serve 自身日志打到 serve.log。
export AGENT_DEBUG="${AGENT_DEBUG:-0}"
# opencode 调用日志路径（默认 <PROJECT_DIR>/opencode.log）
# export AGENT_OPENCODE_LOG="$PROJECT_DIR/opencode.log"
# AGENT_DEBUG 时 opencode serve 自身日志：级别（DEBUG|INFO|WARN|ERROR）+ 路径
# export AGENT_SERVE_LOG_LEVEL="DEBUG"
# export AGENT_SERVE_LOG="$PROJECT_DIR/serve.log"

# --- PATH（部署坑#1）---
# 托管进程 PATH 极简，子进程调 dws/opencode 会找不到。取消注释并按实际路径填：
# export PATH="$PATH:$HOME/.local/bin:$HOME/.opencode/bin"
