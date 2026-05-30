#!/usr/bin/env python3
"""IRC Agent Configuration Generator.

Generates .claude/irc-config.json with IRC connection settings.
Called by Claude Code after gathering user preferences.

Usage:
    python configure.py --prefix claude --slug my-project --channel "#Agents" --masters "nick1,nick2" --output .claude/irc-config.json
"""

import argparse
import json
import re
import sys
from pathlib import Path


def auto_slug():
    """Auto-detect project slug from current directory name."""
    name = Path.cwd().name.lower()
    slug = re.sub(r"[^a-z0-9-]", "-", name)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:20]


def main():
    parser = argparse.ArgumentParser(description="Generate IRC agent config")
    parser.add_argument(
        "--prefix", default="claude", help="Nick prefix (default: claude)"
    )
    parser.add_argument(
        "--slug", default=None, help="Project slug (auto-detected if omitted)"
    )
    parser.add_argument(
        "--channel", default="#Agents", help="IRC channel (default: #Agents)"
    )
    parser.add_argument(
        "--masters", default="", help="Comma-separated master nicks"
    )
    parser.add_argument(
        "--language",
        default="auto",
        help="Response language: 'auto' (match incoming), 'es', 'en', etc. (default: auto)",
    )
    parser.add_argument(
        "--server", default="irc.meshrelay.xyz", help="IRC server"
    )
    parser.add_argument(
        "--output",
        default=".claude/irc-config.json",
        help="Output path (default: .claude/irc-config.json)",
    )
    args = parser.parse_args()

    slug = args.slug or auto_slug()
    masters = (
        [n.strip() for n in args.masters.split(",") if n.strip()]
        if args.masters
        else []
    )

    config = {
        "server": args.server,
        "port_ssl": 6697,
        "port_plain": 6667,
        "channel": args.channel,
        "nick_prefix": args.prefix,
        "project_slug": slug,
        "masters": masters,
        "language": args.language,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"[OK] Config written to {output_path}")
    print(f"  Nick pattern: {args.prefix}-{slug}-<hash>")
    print(f"  Channel:      {args.channel}")
    print(f"  Server:       {args.server}")
    print(f"  Masters:      {masters or '(none)'}")
    print(f"  Language:     {args.language}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
