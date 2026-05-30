---
name: irc-agent
description: IRC communication for Claude Code sessions. Connects to IRC servers for real-time inter-agent and human-agent collaboration. Use when the user says "connect to IRC", "configure IRC", "chat on IRC", "join IRC", "start IRC", "conéctate al IRC", "configura IRC", "charla en IRC", "charla con el equipo", or wants Claude Code sessions to communicate with each other or with humans via IRC channels. Handles "/configure-irc" and "/connect-irc" triggers.
---

# IRC Agent

Real-time IRC communication for Claude Code sessions. Enables multi-session collaboration between Claude Code instances and humans.

All script paths below are relative to this skill's directory. If installed at `.claude/skills/irc-agent/`, resolve as `.claude/skills/irc-agent/scripts/cli.py`, etc.

## Configuration

Before connecting, configure the IRC agent. If `.claude/irc-config.json` doesn't exist when the user wants to connect, run the configure flow first.

### Configure Flow

Ask the user these questions (use AskUserQuestion if available, otherwise ask in text):

1. **Nick prefix** — Prefix for IRC nickname (default: `claude`)
2. **Channel** — IRC channel to join (default: `#Agents`)
3. **Masters** — Comma-separated IRC nicks whose messages are directives (default: none)
4. **Language** — Response language: `auto` (match incoming message language), `es`, `en`, etc. (default: `auto`)
5. **Project slug** — Short identifier, auto-detected from directory name (offer override)

Then generate config:

```bash
python scripts/configure.py --prefix "<prefix>" --slug "<slug>" --channel "<channel>" --masters "<nick1>,<nick2>" --language "<language>" --output .claude/irc-config.json
```

## Connecting

```bash
# Stop any existing daemon
python scripts/cli.py stop 2>nul || true

# Clear inbox for fresh session
python scripts/cli.py clear

# Start daemon (reads .claude/irc-config.json, auto-generates unique nick)
python scripts/cli.py start
```

The daemon generates a unique nick: `{prefix}-{slug}-{hash5}` (e.g., `claude-exec-market-a3f2d`). Tries SSL port 6697 first, falls back to plain 6667.

## Language Rules

Read `language` from `.claude/irc-config.json` before composing any message.

- **`"auto"`** (default): Detect the language of the last incoming message and reply in that same language. If someone writes in Spanish, respond in Spanish. If they write in English, respond in English.
- **`"es"`, `"en"`, etc.**: Always respond in that language regardless of what others use.

This applies to ALL messages you send — greetings, answers, proposals, acknowledgments, everything. Consistency matters for readability in multi-agent channels.

## Idle Monitoring

**CRITICAL**: After connecting, do NOT just sit idle. The daemon receives messages in the background, but YOU must poll for them.

### When actively chatting
Use the normal chat loop (send → sleep → read).

### When idle but connected
If you are connected and not in an active conversation, you MUST periodically check for new messages. Run this between other tasks:

```bash
python scripts/cli.py read --new
```

Check at least every **60 seconds** while connected. If there are messages directed at you (mentions, master directives, or questions from others), respond promptly.

### Watch mode for passive listening

```bash
python scripts/cli.py read --watch --timeout 120
```

Use this when waiting for someone to join or respond. It polls every 5 seconds and shows messages as they arrive.

### When to read proactively

- After finishing any task while IRC is connected
- Before starting a new task (check if someone pinged you)
- When the user hasn't given you anything to do
- After any `sleep` or wait period
- When you see `[MASTER]` or `[MENTION]` in output — respond immediately

## Chat Loop

```bash
# Send a message
python scripts/cli.py send "[HELLO] Online. Topic: {topic}"

# Wait 15-30 seconds for responses
sleep 20

# Read new messages
python scripts/cli.py read --new
```

Repeat send/wait/read until discussion is complete.

### Handling Master Messages

Messages from nicks in the `masters` config list are **directives**. They appear with a `[MASTER]` tag in read output. When you see one:

- Absorb the information immediately
- Adjust behavior accordingly
- Acknowledge receipt in the channel
- Relay to other participants if relevant

### Watching for Responses

For extended listening (waits up to N seconds, prints messages as they arrive):

```bash
python scripts/cli.py read --watch --timeout 60
```

## Disconnecting

```bash
python scripts/cli.py send "[DONE] Session ending."
python scripts/cli.py stop
```

## Commands Reference

| Command | Description |
|---------|-------------|
| `cli.py start` | Start daemon, auto-generate nick from config |
| `cli.py stop` | Graceful shutdown |
| `cli.py send "msg"` | Send message to channel |
| `cli.py read --new` | Show unread messages |
| `cli.py read --tail N` | Show last N messages |
| `cli.py read --watch` | Poll continuously (default 30s timeout) |
| `cli.py status` | Connection status and unread count |
| `cli.py log` | View daemon log |
| `cli.py clear` | Clear inbox |
| `configure.py` | Generate config file |

## Message Protocol

See `references/protocol.md` for structured message prefixes and conventions.

## Architecture

- **Config**: `.claude/irc-config.json` (per-project, committed to repo)
- **Runtime**: `~/.claude/irc-agent/sessions/{nick}/` (global, not committed)
- **Active nick**: `.claude/.irc-nick` (written by `start`, read by other commands)
- **Scripts**: Bundled in skill's `scripts/` directory
