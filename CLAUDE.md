# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **production-grade DingTalk digital employee harness** that connects DingTalk messaging to OpenCode's AI brain. It's a template for building AI agents that can:
- Listen to group/private/@ messages in DingTalk
- Process text, images, and files with multimodal AI
- Reply intelligently using free OpenCode models
- Run 24/7 with self-healing daemon processes

The project follows a **core/custom separation pattern** where:
- `src/core/`, `bin/core/`, `tests/core/` contain the harness framework (don't modify)
- `src/custom/`, `bin/custom/`, `tests/custom/` contain DingTalk-specific implementations (modify here)
- `config/*.local.*` contain sensitive credentials (gitignored)

**IMPORTANT**: Read [AGENTS.md](./AGENTS.md) for comprehensive project instructions, layer boundaries, and detailed implementation guidance. AGENTS.md is the authoritative reference for AI agents working on this codebase.

## Core Architecture

Full architecture details in [ARCHITECTURE.md](./ARCHITECTURE.md) (整体架构图 + 13 个最佳实践).

### Three-Layer Boundary (详见 AGENTS.md)

- **@core** (`src/core/`, `bin/core/`, `tests/core/`) — Harness framework, DON'T modify (bug fixes → PR to upstream)
- **@custom** (`src/custom/`, `bin/custom/`, `tests/custom/`) — DingTalk-specific, modify freely here
- **@config** (`config/*.local.*`) — Sensitive credentials, gitignored

### Data Flow

See "数字员工架构图" in [README.md](./README.md) for complete flow diagram.

```
DingTalk → dws consume → event_watcher.py → Capability plugins → opencode serve → dws send → DingTalk
            (bridge)      (log-tail)         (registered)        (brain)          (replier)
```

## Common Commands

### Service Management

```bash
# Start all services (auto-detects launchd/nohup mode)
bash bin/core/start.sh

# Stop all services
bash bin/core/stop.sh

# Restart (also triggered by /reboot in chat)
bash bin/core/reboot.sh

# Health check (6 checks: connect/log/serve/http)
bash bin/core/healthcheck.sh
```

### Testing

```bash
# Shell unit tests (syntax + function-level assertions)
bash tests/core/unit_test.sh

# Python unit tests (all)
for t in tests/core/*.py tests/custom/*.py; do python3 "$t"; done

# Specific test
python3 tests/core/test_agent_common.py
python3 tests/custom/test_ack_capability.py

# E2E tests (require real DingTalk setup)
bash tests/custom/e2e_text_http_test.sh      # HTTP brain test (no DingTalk needed)
bash tests/custom/e2e_at_test.sh             # @ mention subscription test
```

### Configuration

```bash
# Copy templates and fill real values
cp config/constants.sh config/constants.local.sh
# Edit *.local.sh with:
#   DWS_EVENT_GROUP, DWS_PROFILE, AGENT_PROFILE (must match DWS_PROFILE)
#   AGENT_BRAIN=opencode, AGENT_OPENCODE_MODEL=opencode/deepseek-v4-flash-free
#   PATH must include ~/.local/bin (dws) and ~/.opencode/bin (opencode)

# Verify dws authentication
dws auth status
dws profile list
```

### Development

```bash
# Watch logs in real-time
tail -f monitor.log agent-connect.log opencode.log

# Check service status
pgrep -fl "monitor.sh|dws-connect|event_watcher.py|opencode serve"

# Manual component testing
source config/constants.local.sh
python3 src/core/event_watcher.py            # Run event watcher in foreground
bash bin/custom/dws-connect.sh               # Run DingTalk connector
```

## Key Design Patterns

### 1. Capability Plugin System

All features are capabilities registered via `Capability(...)`. Example:

```python
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT

def on_inbound(msg):
    return True  # True = consumed, False = pass to next

register(Capability(name="my_cap", on_inbound=on_inbound,
                    handles_kinds={KIND_TEXT}, priority=50,
                    dedup=True, loop_guard=True))
```

Registered in `src/custom/capabilities/__init__.py`, controlled by `CAP_<NAME>_ENABLED`.

### 2. Session Management

- **Session reuse** (`AGENT_SESSION_REUSE=1`): Multi-turn context per conversation
- **TTL expiry** (`AGENT_SESSION_TTL=1800`): Idle 30min → rebuild
- **LRU eviction** (`AGENT_SESSION_MAX=64`): Max concurrent sessions
- **Reset keywords** (`AGENT_SESSION_RESET_KEYWORDS="/new,新话题"`): User triggers context clear
- Credentials via `find_serve_credentials()` → caches to `.serve.{pid,port,pwd}`

### 3. Daemon + Self-Healing

See ARCHITECTURE.md "launchd 守护" for full details. Key points:

- `monitor.sh`: cleanup → start components → healthcheck loop → circuit breaker
- `healthcheck.sh`: 6 checks (4 hard failures + 2 warnings)
- After `MAX_FAILURES` consecutive failures → exit 0 (notify + wait for manual fix)

### 4. Testing Strategy

See AGENTS.md "测试约定" for complete testing patterns:
- Shell: `bash -n` + assertions, no dependencies
- Python: `unittest` + `patch.object(<module>, "<func>")`
- E2E: Real trigger + dual verification (log + `dws chat message list`)

When writing tests: mock I/O, patch polling constants to 0, use `new=` not `return_value=` for non-callables.

## Common Pitfalls

Complete list in AGENTS.md "常见坑". Critical ones:

1. **PATH in daemon mode**: Add `~/.local/bin` (dws) and `~/.opencode/bin` (opencode) to PATH in `config/constants.local.sh`. Symptom: "No such file or directory: 'dws'"

2. **AGENT_PROFILE must equal DWS_PROFILE**: Both must be same `corpId:userId`. Otherwise: "未登录"

3. **Session selection by time.updated, not id**: Use `time.updated` DESC, not session id sort

4. **Don't use pgrep -f**: Use `verify_pid` from `bin/core/lib.sh` (PID file + kill -0 + cmdline signature)

5. **Don't modify core for routing**: Put routing in `src/custom/routes.py` or capabilities, never in `src/core/event_watcher.py`

6. **asked_ts buffer 设 5s**: 依赖服务写日志时刻 vs serve POST 时刻有微小偏差

7. **轮询 do-while 风格**: 先调一次再判断，保证至少调一次

8. **patch.object 第三参数是 `new` 不是 `return_value`**: 指定 new 后不传 mock 给测试函数

9. **Kill `dws event consume` with its subtree** (#71): consume spawns a `dws event _bus` child holding the stream connection. Plain `kill <consumer>` orphans `_bus` → it keeps consuming, new/old `_bus` fight over delivery ("投递停滞"). Use subtree kill; `stop.sh`/`monitor.sh` call the custom `stop_extra_cleanup` hook to sweep residual `dws event` by `DWS_PROFILE`.

10. **/reboot uses a clean env** (#71): `reboot.sh` runs stop/start via `env -i` so edits to `config/constants.local.sh` take effect — inherited stale env would otherwise defeat `${VAR:-...}`-style assignments.

11. **Locked macOS keychain empties `dws profile list`** (#71): e2e sender autodetect then SKIPs. Fix: `security unlock-keychain ~/Library/Keychains/login.keychain-db` or set `E2E_SENDER_PROFILE`.

## Environment Variables

Key configuration (see `config/constants.sh` for full list):

```bash
# Identity
DWS_PROFILE="dinga...:userId"              # DingTalk org profile (must match AGENT_PROFILE)
AGENT_PROFILE="$DWS_PROFILE"               # Agent identity (MUST EQUAL DWS_PROFILE)
AGENT_SELF_NAMES="数字员工"                 # Agent's display name (for loop guard)

# Subscription (at least one required)
DWS_EVENT_GROUP="cid...=="                 # Group openConversationId
DWS_EVENT_O2O_USERS="userId1,userId2"      # Private chat user IDs (comma-separated)
DWS_EVENT_AT=1                             # @ mention subscription (any group)

# Brain
AGENT_BRAIN="opencode"                     # echo | opencode | proxy
AGENT_OPENCODE_MODEL="opencode/deepseek-v4-flash-free"  # Free text model
AGENT_VISION_MODEL="opencode/mimo-v2.5-free"            # Free vision model

# Session continuity (#56)
AGENT_SESSION_REUSE=1                      # Enable multi-turn memory
AGENT_SESSION_TTL=1800                     # Idle expiry (30min)
AGENT_SESSION_MAX=64                       # Max concurrent sessions (LRU)
AGENT_SESSION_RESET_KEYWORDS="/new,新话题"  # Context reset triggers

# Capabilities (CAP_<NAME>_ENABLED)
CAP_TEXT_REPLY_ENABLED=1                   # Basic text chat
CAP_QUESTION_ENABLED=1                     # Interactive question prompts
CAP_IMAGE_ENABLED=1                        # Image recognition
CAP_FILE_ENABLED=1                         # File reading
CAP_FORWARD_ENABLED=1                      # Merged forward messages
CAP_ACK_ENABLED=1                          # Read receipts + status reactions
CAP_AGGREGATION_ENABLED=0                  # Message batching (off by default)
```

## Adding a New Capability

See README.md "加一个自己的能力" for complete guide. Recommended: let a Coding Agent implement it.

Quick template:

```python
# src/custom/capabilities/my_cap.py
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT

def on_inbound(msg):
    if not should_handle(msg):
        return False
    # Process and reply
    return True

register(Capability(name="my_cap", on_inbound=on_inbound,
                    handles_kinds={KIND_TEXT}, priority=50,
                    dedup=True, loop_guard=True))
```

Then import in `src/custom/capabilities/__init__.py` and add unit test in `tests/custom/`.

## File Index

Complete index in AGENTS.md "关键文件 / 函数索引". Key files:

| File | Purpose |
|------|---------|
| `bin/core/monitor.sh` | Daemon supervisor (cleanup → start → healthcheck → circuit breaker) |
| `bin/core/healthcheck.sh` | 6 health checks |
| `bin/core/lib.sh` | Shared utilities (verify_pid, acquire_lock, stop_components) |
| `src/core/event_watcher.py` | Main event loop (SSE + log-tail + capability dispatch) |
| `src/core/capabilities.py` | Plugin registry |
| `src/core/agent_common.py` | Utilities (serve credentials, session ops, inject_and_forward) |
| `src/custom/capabilities/` | DingTalk capabilities (ack, forward, image, file, stats) |
| `src/custom/brain.py` | OpenCode serve implementation |
| `src/custom/replier.py` | DingTalk send via dws CLI |

## Additional Documentation

- `AGENTS.md` — Detailed agent-facing instructions (must-read for understanding boundaries)
- `ARCHITECTURE.md` — 13 best practices extracted from production
- `README.md` — User-facing setup guide
- `FORKING.md` — How to fork and sync with upstream
- `CONTRIBUTING.md` — How to contribute fixes back to upstream
- `SKILL.md` — Step-by-step operational skills (startup, health check)
