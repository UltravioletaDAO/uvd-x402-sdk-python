#!/usr/bin/env python3
"""IRC Agent CLI - Interface for Claude Code to manage IRC connections.

Reads config from .claude/irc-config.json and auto-generates unique nicks.
Active nick is stored in .claude/.irc-nick so subsequent commands don't need --nick.

Usage:
    python cli.py start                    # Start daemon (auto-nick from config)
    python cli.py stop                     # Stop daemon
    python cli.py send "message here"      # Send message
    python cli.py read [--new|--tail N|--watch]  # Read messages
    python cli.py status                   # Check connection
    python cli.py log [--tail N]           # View daemon log
    python cli.py clear                    # Clear inbox
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

SESSIONS_DIR = Path.home() / ".claude" / "irc-agent" / "sessions"
DAEMON_PATH = Path(__file__).parent / "daemon.py"


# --- Config helpers ---


def find_config():
    """Find .claude/irc-config.json starting from CWD."""
    cwd = Path.cwd()
    for d in [cwd] + list(cwd.parents):
        config_path = d / ".claude" / "irc-config.json"
        if config_path.exists():
            return config_path
    return None


def find_claude_dir():
    """Find the .claude directory (where config lives or CWD fallback)."""
    config_path = find_config()
    if config_path:
        return config_path.parent
    return Path.cwd() / ".claude"


def auto_slug():
    """Auto-detect project slug from current directory name."""
    name = Path.cwd().name.lower()
    slug = re.sub(r"[^a-z0-9-]", "-", name)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:20]


def load_config():
    """Load config from .claude/irc-config.json with defaults."""
    defaults = {
        "server": "irc.meshrelay.xyz",
        "port_ssl": 6697,
        "port_plain": 6667,
        "channel": "#Agents",
        "nick_prefix": "claude",
        "project_slug": auto_slug(),
        "masters": [],
    }
    config_path = find_config()
    if config_path:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                user_config = json.load(f)
            defaults.update(user_config)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] Failed to read config: {e}")
    else:
        print("[INFO] No .claude/irc-config.json found, using defaults")
    return defaults


def generate_nick(config):
    """Generate unique nick: {prefix}-{slug}-{hash5}."""
    hash_part = uuid.uuid4().hex[:5]
    prefix = config["nick_prefix"]
    slug = config["project_slug"]
    return f"{prefix}-{slug}-{hash_part}"


def save_active_nick(nick):
    """Save active nick to .claude/.irc-nick."""
    claude_dir = find_claude_dir()
    claude_dir.mkdir(parents=True, exist_ok=True)
    nick_file = claude_dir / ".irc-nick"
    nick_file.write_text(nick, encoding="utf-8")


def get_active_nick():
    """Read active nick from .claude/.irc-nick."""
    claude_dir = find_claude_dir()
    nick_file = claude_dir / ".irc-nick"
    if nick_file.exists():
        nick = nick_file.read_text(encoding="utf-8").strip()
        if nick:
            return nick
    return None


def clear_active_nick():
    """Remove active nick file."""
    claude_dir = find_claude_dir()
    nick_file = claude_dir / ".irc-nick"
    if nick_file.exists():
        nick_file.unlink()


def resolve_nick(args):
    """Resolve nick: explicit arg > active file > error."""
    if hasattr(args, "nick") and args.nick:
        return args.nick
    active = get_active_nick()
    if active:
        return active
    print("[FAIL] No active IRC session. Run 'start' first or pass --nick.")
    return None


def get_session_dir(nick):
    return SESSIONS_DIR / nick


def is_process_running(pid):
    """Check if a process with given PID is running."""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in result.stdout
        else:
            os.kill(pid, 0)
            return True
    except (OSError, subprocess.TimeoutExpired):
        return False


# --- Commands ---


def cmd_start(args):
    """Start the IRC daemon with auto-generated nick."""
    config = load_config()

    # Generate unique nick
    nick = generate_nick(config)

    session_dir = get_session_dir(nick)
    session_dir.mkdir(parents=True, exist_ok=True)

    # Build daemon command
    cmd = [
        sys.executable,
        str(DAEMON_PATH),
        "--nick", nick,
        "--server", config["server"],
        "--port-ssl", str(config["port_ssl"]),
        "--port-plain", str(config["port_plain"]),
        "--channel", config["channel"],
    ]
    if config["masters"]:
        cmd.extend(["--masters", ",".join(config["masters"])])

    # Start as detached background process
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        proc = subprocess.Popen(
            cmd,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    print(f"Starting daemon as {nick} (PID {proc.pid})...")

    # Save active nick for subsequent commands
    save_active_nick(nick)

    # Wait for connection (up to 60 seconds)
    status_path = session_dir / "status.txt"
    for i in range(60):
        time.sleep(1)
        if status_path.exists():
            status = status_path.read_text().strip()
            if status == "connected":
                print(f"[OK] Connected to {config['server']} as {nick} in {config['channel']}")
                return 0
            elif status.startswith("error"):
                print(f"[FAIL] {status}")
                clear_active_nick()
                return 1
        if i > 0 and i % 15 == 0:
            print(f"  Still connecting... ({i}s)")

    # Final check
    if status_path.exists() and status_path.read_text().strip() == "connected":
        print(f"[OK] Connected to {config['server']} as {nick} in {config['channel']}")
        return 0

    print("[WARN] Timeout waiting for connection - daemon may still be connecting")
    print(f"  Check: python cli.py status")
    return 1


def cmd_stop(args):
    """Stop the IRC daemon."""
    nick = resolve_nick(args)
    if not nick:
        return 1

    session_dir = get_session_dir(nick)
    pid_path = session_dir / "pid.txt"

    if not pid_path.exists():
        print(f"No daemon running for {nick}")
        clear_active_nick()
        return 0

    pid = int(pid_path.read_text().strip())

    # Remove PID file (daemon watches for this)
    pid_path.unlink()
    time.sleep(1)

    # Force kill if still running
    if is_process_running(pid):
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    timeout=5,
                )
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    clear_active_nick()
    print(f"[OK] Stopped daemon for {nick}")
    return 0


def cmd_send(args):
    """Send a message to IRC channel."""
    nick = resolve_nick(args)
    if not nick:
        return 1

    session_dir = get_session_dir(nick)
    outbox_path = session_dir / "outbox.jsonl"

    if not session_dir.exists():
        print(f"[FAIL] No session for {nick} - run 'start' first")
        return 1

    pid_path = session_dir / "pid.txt"
    if not pid_path.exists():
        print(f"[WARN] Daemon not running for {nick} - message queued")

    entry = {
        "timestamp": datetime.now().isoformat(),
        "message": args.message,
    }

    with open(outbox_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"[{nick}] >> {args.message}")
    return 0


def cmd_read(args):
    """Read messages from inbox."""
    nick = resolve_nick(args)
    if not nick:
        return 1

    # Watch mode: poll continuously
    if args.watch:
        timeout = args.timeout or 30
        interval = 5
        print(f"Watching for messages ({timeout}s timeout, {interval}s interval)...")
        start_time = time.time()
        try:
            while time.time() - start_time < timeout:
                messages = _get_new_messages(nick)
                for msg in messages:
                    _print_message(msg)
                if not messages:
                    remaining = int(timeout - (time.time() - start_time))
                    if remaining > 0:
                        sys.stdout.write(f"\r  Listening... ({remaining}s remaining)")
                        sys.stdout.flush()
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
        print("\n[OK] Watch ended")
        return 0

    session_dir = get_session_dir(nick)
    inbox_path = session_dir / "inbox.jsonl"

    if not inbox_path.exists() or inbox_path.stat().st_size == 0:
        print("No messages.")
        return 0

    messages = _load_all_messages(inbox_path)

    if not messages:
        print("No messages.")
        return 0

    # Filter based on mode
    if args.new:
        marker_path = session_dir / "read_marker.txt"
        last_read = 0
        if marker_path.exists():
            try:
                last_read = int(marker_path.read_text().strip())
            except ValueError:
                last_read = 0
        messages = messages[last_read:]
        # Update marker
        total = sum(1 for line in open(inbox_path) if line.strip())
        marker_path.write_text(str(total))
    elif args.tail:
        messages = messages[-args.tail :]

    if not messages:
        print("No new messages.")
        return 0

    for msg in messages:
        _print_message(msg)

    print(f"\n({len(messages)} messages)")
    return 0


def _load_all_messages(inbox_path):
    """Load all messages from inbox file."""
    messages = []
    with open(inbox_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return messages


def _get_new_messages(nick):
    """Get new unread messages for a nick."""
    session_dir = get_session_dir(nick)
    inbox_path = session_dir / "inbox.jsonl"
    marker_path = session_dir / "read_marker.txt"

    if not inbox_path.exists() or inbox_path.stat().st_size == 0:
        return []

    messages = _load_all_messages(inbox_path)
    last_read = 0
    if marker_path.exists():
        try:
            last_read = int(marker_path.read_text().strip())
        except ValueError:
            last_read = 0

    new_messages = messages[last_read:]

    # Update marker
    if new_messages:
        total = len(messages)
        marker_path.write_text(str(total))

    return new_messages


def _print_message(msg):
    """Format and print a single message."""
    ts = msg.get("timestamp", "?")
    sender = msg.get("sender", "?")
    text = msg.get("message", "")
    event = msg.get("event")
    is_master = msg.get("master", False)
    is_mention = msg.get("mention", False)

    try:
        dt = datetime.fromisoformat(ts)
        ts_short = dt.strftime("%H:%M:%S")
    except (ValueError, TypeError):
        ts_short = ts

    # Build prefix tags
    tags = ""
    if is_master:
        tags += "[MASTER] "
    if is_mention:
        tags += "[MENTION] "

    try:
        if event:
            print(f"[{ts_short}] *** {text}")
        else:
            print(f"[{ts_short}] {tags}<{sender}> {text}")
    except UnicodeEncodeError:
        safe_text = text.encode("ascii", errors="replace").decode("ascii")
        if event:
            print(f"[{ts_short}] *** {safe_text}")
        else:
            print(f"[{ts_short}] {tags}<{sender}> {safe_text}")


def cmd_status(args):
    """Show daemon and connection status."""
    nick = resolve_nick(args)
    if not nick:
        # Show info even without active nick
        config_path = find_config()
        print(f"Config:   {config_path or 'not found'}")
        print(f"Active:   no active session")
        return 1

    session_dir = get_session_dir(nick)

    if not session_dir.exists():
        print(f"No session for {nick}")
        return 1

    # Connection status
    status_path = session_dir / "status.txt"
    status = "unknown"
    if status_path.exists():
        status = status_path.read_text().strip()

    # Process status
    pid_path = session_dir / "pid.txt"
    pid = "none"
    running = False
    if pid_path.exists():
        pid = pid_path.read_text().strip()
        try:
            running = is_process_running(int(pid))
        except ValueError:
            pass

    # Inbox count
    inbox_path = session_dir / "inbox.jsonl"
    inbox_count = 0
    if inbox_path.exists():
        inbox_count = sum(1 for line in open(inbox_path) if line.strip())

    # Unread count
    marker_path = session_dir / "read_marker.txt"
    unread = inbox_count
    if marker_path.exists():
        try:
            last_read = int(marker_path.read_text().strip())
            unread = max(0, inbox_count - last_read)
        except ValueError:
            pass

    config_path = find_config()
    print(f"Nick:     {nick}")
    print(f"Status:   {status}")
    print(f"Process:  PID {pid} ({'running' if running else 'stopped'})")
    print(f"Inbox:    {inbox_count} total, {unread} unread")
    print(f"Config:   {config_path or 'not found'}")
    return 0


def cmd_log(args):
    """Show daemon log."""
    nick = resolve_nick(args)
    if not nick:
        return 1

    session_dir = get_session_dir(nick)
    log_path = session_dir / "log.txt"

    if not log_path.exists():
        print("No log file.")
        return 1

    lines = log_path.read_text(encoding="utf-8").strip().split("\n")
    tail = args.tail or 30
    for line in lines[-tail:]:
        print(line)
    return 0


def cmd_clear(args):
    """Clear inbox messages."""
    nick = resolve_nick(args)
    if not nick:
        # If no active nick, try to clear based on config
        print("[WARN] No active session to clear")
        return 1

    session_dir = get_session_dir(nick)
    inbox_path = session_dir / "inbox.jsonl"
    marker_path = session_dir / "read_marker.txt"

    if inbox_path.exists():
        count = sum(1 for line in open(inbox_path) if line.strip())
        inbox_path.write_text("")
        if marker_path.exists():
            marker_path.write_text("0")
        print(f"[OK] Cleared {count} messages from inbox")
    else:
        print("Inbox already empty.")
    return 0


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="IRC Agent CLI for Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python cli.py start                 # Auto-nick from config\n"
            "  python cli.py send \"Hello!\"          # Send message\n"
            "  python cli.py read --new             # Read unread messages\n"
            "  python cli.py read --watch            # Watch for 30 seconds\n"
            "  python cli.py status                 # Check connection\n"
            "  python cli.py stop                   # Disconnect\n"
        ),
    )
    parser.add_argument(
        "--nick",
        default=None,
        help="Override nick (default: auto from .claude/.irc-nick)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # start
    subparsers.add_parser("start", help="Start IRC daemon (auto-generates nick)")

    # stop
    subparsers.add_parser("stop", help="Stop IRC daemon")

    # send
    p_send = subparsers.add_parser("send", help="Send message to IRC channel")
    p_send.add_argument("message", help="Message text to send")

    # read
    p_read = subparsers.add_parser("read", help="Read messages from IRC")
    p_read.add_argument(
        "--new", action="store_true", help="Show only unread messages (default)"
    )
    p_read.add_argument("--tail", type=int, help="Show last N messages")
    p_read.add_argument("--all", action="store_true", help="Show all messages")
    p_read.add_argument(
        "--watch", action="store_true", help="Poll continuously for new messages"
    )
    p_read.add_argument(
        "--timeout", type=int, default=30, help="Watch timeout in seconds (default: 30)"
    )

    # status
    subparsers.add_parser("status", help="Check daemon and connection status")

    # log
    p_log = subparsers.add_parser("log", help="Show daemon log")
    p_log.add_argument(
        "--tail", type=int, default=30, help="Show last N lines (default: 30)"
    )

    # clear
    subparsers.add_parser("clear", help="Clear inbox messages")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    handlers = {
        "start": cmd_start,
        "stop": cmd_stop,
        "send": cmd_send,
        "read": cmd_read,
        "status": cmd_status,
        "log": cmd_log,
        "clear": cmd_clear,
    }

    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
