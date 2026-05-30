#!/usr/bin/env python3
"""IRC Agent Daemon - Persistent IRC connection for Claude Code sessions.

Maintains a connection to an IRC server (SSL with plain fallback), watches an
outbox file for messages to send, and writes received messages to an inbox file.
Designed to run as a background process managed by cli.py.

Runtime data stored in: ~/.claude/irc-agent/sessions/<nick>/
"""

import json
import os
import signal
import socket
import ssl
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# IRC message line limit (RFC 2812: 512 bytes including CRLF)
IRC_MAX_LINE = 400  # Conservative limit for message body


class IRCDaemon:
    def __init__(
        self,
        nick,
        server="irc.meshrelay.xyz",
        port_ssl=6697,
        port_plain=6667,
        channel="#Agents",
        masters=None,
    ):
        self.nick = nick
        self.original_nick = nick
        self.server = server
        self.port_ssl = port_ssl
        self.port_plain = port_plain
        self.channel = channel
        self.masters = set(masters or [])
        self.running = False
        self.connected = False
        self.sock = None
        self.use_ssl = True

        # Session directory (global, shared across projects)
        self.base_dir = Path.home() / ".claude" / "irc-agent" / "sessions" / nick
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.inbox_path = self.base_dir / "inbox.jsonl"
        self.outbox_path = self.base_dir / "outbox.jsonl"
        self.pid_path = self.base_dir / "pid.txt"
        self.log_path = self.base_dir / "log.txt"
        self.status_path = self.base_dir / "status.txt"

        # Track outbox file position to only read new lines
        self.outbox_pos = 0
        self._lock = threading.Lock()

    def log(self, msg):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {msg}\n"
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def set_status(self, status):
        try:
            with open(self.status_path, "w", encoding="utf-8") as f:
                f.write(status)
        except Exception:
            pass

    def connect(self):
        """Connect to IRC server. Try SSL first, fallback to plain."""
        self.set_status("connecting")

        # Try SSL first
        try:
            self._connect_ssl()
        except (ssl.SSLError, ConnectionRefusedError, OSError, socket.timeout) as e:
            self.log(f"SSL connection failed: {e}")
            self.log(f"Falling back to plain on port {self.port_plain}...")
            try:
                self._connect_plain()
            except Exception as e2:
                self.log(f"Plain connection also failed: {e2}")
                self.set_status(f"error: connection failed")
                raise ConnectionError(
                    f"Both SSL ({self.port_ssl}) and plain ({self.port_plain}) failed"
                )

        # Register nick
        self._send_raw(f"NICK {self.nick}")
        self._send_raw(f"USER {self.nick} 0 * :Claude Code IRC Agent")

        # Wait for server welcome
        self._wait_for_registration()

        # Join channel
        self._send_raw(f"JOIN {self.channel}")
        proto = "SSL" if self.use_ssl else "plain"
        port = self.port_ssl if self.use_ssl else self.port_plain
        self.log(f"Joined {self.channel} as {self.nick} ({proto}:{port})")
        self.set_status("connected")
        self.connected = True

        # Announce presence
        self._send_raw(f"PRIVMSG {self.channel} :[connected] {self.nick} online")

    def _connect_ssl(self):
        """Establish SSL connection."""
        self.log(f"Trying SSL connection to {self.server}:{self.port_ssl}...")
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.settimeout(30)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw_sock, server_hostname=self.server)
        self.sock.connect((self.server, self.port_ssl))
        self.use_ssl = True
        self.log(f"SSL connection established")

    def _connect_plain(self):
        """Establish plain (non-SSL) connection."""
        self.log(f"Trying plain connection to {self.server}:{self.port_plain}...")
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.settimeout(30)
        self.sock = raw_sock
        self.sock.connect((self.server, self.port_plain))
        self.use_ssl = False
        self.log(f"Plain connection established")

    def _send_raw(self, msg):
        """Send raw IRC message."""
        with self._lock:
            try:
                self.sock.send(f"{msg}\r\n".encode("utf-8"))
                self.log(f">> {msg}")
            except Exception as e:
                self.log(f"Send error: {e}")
                self.connected = False

    def _wait_for_registration(self):
        """Wait for RPL_WELCOME (001) or handle nick collision."""
        buffer = ""
        attempts = 0
        while attempts < 60:
            try:
                data = self.sock.recv(4096).decode("utf-8", errors="replace")
                if not data:
                    raise ConnectionError("Server closed connection")
                buffer += data
                lines = buffer.split("\r\n")
                buffer = lines.pop()

                for line in lines:
                    self.log(f"<< {line}")
                    if line.startswith("PING"):
                        pong = line.replace("PING", "PONG", 1)
                        self._send_raw(pong)
                    elif " 001 " in line:
                        self.log("Registration successful")
                        return
                    elif " 433 " in line:  # Nick in use
                        self.nick = self.nick + "_"
                        self.log(f"Nick collision, trying: {self.nick}")
                        self._send_raw(f"NICK {self.nick}")
                    elif " 432 " in line:  # Erroneous nickname
                        self.nick = f"cc-{os.getpid()}"
                        self.log(f"Invalid nick, trying: {self.nick}")
                        self._send_raw(f"NICK {self.nick}")
            except socket.timeout:
                attempts += 1
                continue
        raise ConnectionError("Registration timeout")

    def _read_loop(self):
        """Thread: Read from IRC socket, dispatch messages to inbox."""
        buffer = ""
        while self.running:
            try:
                data = self.sock.recv(4096).decode("utf-8", errors="replace")
                if not data:
                    self.log("Connection closed by server")
                    self.connected = False
                    break

                buffer += data
                lines = buffer.split("\r\n")
                buffer = lines.pop()

                for line in lines:
                    self._handle_line(line)

            except socket.timeout:
                continue
            except OSError as e:
                if self.running:
                    self.log(f"Socket error: {e}")
                    self.connected = False
                break
            except Exception as e:
                self.log(f"Read error: {e}")
                self.connected = False
                break

    def _handle_line(self, line):
        """Parse an IRC protocol line and handle it."""
        if not line:
            return

        self.log(f"<< {line}")

        # PING/PONG keepalive
        if line.startswith("PING"):
            pong = line.replace("PING", "PONG", 1)
            self._send_raw(pong)
            return

        # Parse PRIVMSG: :nick!user@host PRIVMSG #channel :message
        if " PRIVMSG " in line:
            try:
                prefix = line[1 : line.index(" ")]
                sender = prefix.split("!")[0]

                # Skip our own messages
                if sender == self.nick:
                    return

                parts = line.split(" ", 3)
                if len(parts) < 4:
                    return
                target = parts[2]
                message = parts[3][1:] if parts[3].startswith(":") else parts[3]

                # Only log channel messages
                if target.lower() == self.channel.lower():
                    is_master = sender in self.masters
                    is_mention = self.nick.lower() in message.lower()

                    entry = {
                        "timestamp": datetime.now().isoformat(),
                        "sender": sender,
                        "message": message,
                        "channel": self.channel,
                    }
                    if is_master:
                        entry["master"] = True
                    if is_mention:
                        entry["mention"] = True

                    with open(self.inbox_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

            except Exception as e:
                self.log(f"PRIVMSG parse error: {e}")

        # Handle JOIN/PART/QUIT for awareness
        elif " JOIN " in line or " PART " in line or " QUIT " in line:
            try:
                prefix = line[1 : line.index(" ")]
                sender = prefix.split("!")[0]
                if sender == self.nick:
                    return

                if " JOIN " in line:
                    event = "joined"
                elif " PART " in line:
                    event = "left"
                else:
                    event = "quit"

                entry = {
                    "timestamp": datetime.now().isoformat(),
                    "sender": "***",
                    "message": f"{sender} {event} {self.channel}",
                    "channel": self.channel,
                    "event": event,
                }
                with open(self.inbox_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def _outbox_loop(self):
        """Thread: Watch outbox file for new messages, send to IRC."""
        # Start reading from end of existing outbox
        if self.outbox_path.exists():
            self.outbox_pos = self.outbox_path.stat().st_size

        while self.running:
            try:
                if not self.connected:
                    time.sleep(2)
                    continue

                if self.outbox_path.exists():
                    current_size = self.outbox_path.stat().st_size
                    if current_size > self.outbox_pos:
                        with open(self.outbox_path, "r", encoding="utf-8") as f:
                            f.seek(self.outbox_pos)
                            new_data = f.read()
                            self.outbox_pos = f.tell()

                        for raw_line in new_data.strip().split("\n"):
                            raw_line = raw_line.strip()
                            if not raw_line:
                                continue
                            try:
                                msg_data = json.loads(raw_line)
                                message = msg_data.get("message", "")
                            except json.JSONDecodeError:
                                message = raw_line

                            if message:
                                for chunk in self._split_message(message):
                                    self._send_raw(
                                        f"PRIVMSG {self.channel} :{chunk}"
                                    )
                                    time.sleep(0.5)  # Rate limit

                time.sleep(0.3)  # Poll interval

            except Exception as e:
                self.log(f"Outbox error: {e}")
                time.sleep(2)

    def _split_message(self, message):
        """Split a long message into IRC-safe chunks."""
        if len(message.encode("utf-8")) <= IRC_MAX_LINE:
            return [message]

        chunks = []
        current = ""
        for word in message.split(" "):
            test = f"{current} {word}".strip()
            if len(test.encode("utf-8")) > IRC_MAX_LINE:
                if current:
                    chunks.append(current)
                current = word
            else:
                current = test
        if current:
            chunks.append(current)
        return chunks

    def _reconnect_loop(self):
        """Thread: Monitor connection and reconnect if dropped."""
        while self.running:
            time.sleep(10)
            if self.running and not self.connected:
                self.log("Connection lost, attempting reconnect...")
                self.set_status("reconnecting")
                try:
                    if self.sock:
                        try:
                            self.sock.close()
                        except Exception:
                            pass
                    time.sleep(5)
                    self.connect()
                except Exception as e:
                    self.log(f"Reconnect failed: {e}")
                    self.set_status(f"reconnecting (failed: {e})")
                    time.sleep(30)

    def run(self):
        """Main entry point - start daemon."""
        # Write PID file
        with open(self.pid_path, "w") as f:
            f.write(str(os.getpid()))

        # Initialize files
        self.inbox_path.touch(exist_ok=True)
        self.outbox_path.touch(exist_ok=True)

        self.log(
            f"Daemon starting: nick={self.nick}, server={self.server}, "
            f"ssl={self.port_ssl}, plain={self.port_plain}, channel={self.channel}, "
            f"masters={list(self.masters)}"
        )

        try:
            self.connect()
            self.running = True

            # Start worker threads
            threads = [
                threading.Thread(
                    target=self._read_loop, daemon=True, name="irc-reader"
                ),
                threading.Thread(
                    target=self._outbox_loop, daemon=True, name="outbox-watcher"
                ),
                threading.Thread(
                    target=self._reconnect_loop, daemon=True, name="reconnector"
                ),
            ]
            for t in threads:
                t.start()

            self.log("All threads started, daemon running")

            # Main loop: check if we should stop
            while self.running:
                if not self.pid_path.exists():
                    self.log("PID file removed - shutting down")
                    break
                time.sleep(1)

        except KeyboardInterrupt:
            self.log("Keyboard interrupt received")
        except Exception as e:
            self.log(f"Fatal error: {e}")
            self.set_status(f"error: {e}")
        finally:
            self.shutdown()

    def shutdown(self):
        """Clean shutdown."""
        self.running = False
        self.connected = False
        self.set_status("disconnected")
        try:
            self._send_raw(
                f"PRIVMSG {self.channel} :[disconnected] {self.nick} offline"
            )
            time.sleep(0.5)
            self._send_raw("QUIT :Session ended")
            self.sock.close()
        except Exception:
            pass
        try:
            if self.pid_path.exists():
                self.pid_path.unlink()
        except Exception:
            pass
        self.log("Shutdown complete")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="IRC Agent Daemon")
    parser.add_argument("--nick", required=True, help="IRC nickname")
    parser.add_argument(
        "--server", default="irc.meshrelay.xyz", help="IRC server"
    )
    parser.add_argument(
        "--port-ssl", type=int, default=6697, help="SSL port (default: 6697)"
    )
    parser.add_argument(
        "--port-plain", type=int, default=6667, help="Plain port (default: 6667)"
    )
    parser.add_argument(
        "--channel", default="#Agents", help="IRC channel (default: #Agents)"
    )
    parser.add_argument(
        "--masters", default="", help="Comma-separated master nicks"
    )
    args = parser.parse_args()

    masters = [n.strip() for n in args.masters.split(",") if n.strip()]

    daemon = IRCDaemon(
        nick=args.nick,
        server=args.server,
        port_ssl=args.port_ssl,
        port_plain=args.port_plain,
        channel=args.channel,
        masters=masters,
    )

    def handle_signal(sig, frame):
        daemon.running = False

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    daemon.run()


if __name__ == "__main__":
    main()
