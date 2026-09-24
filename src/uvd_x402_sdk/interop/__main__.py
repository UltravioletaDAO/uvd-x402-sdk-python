"""``uvd-interop check`` / ``python -m uvd_x402_sdk.interop check``: the
interop conformance suite, offline.

Exit status: 0 green, 1 red (a fixture, a vector or a manifest does not
conform), 2 could not run (no ``interop/`` directory, a manifest file that is
not there, ``jsonschema`` missing, or the runner itself failed).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from uvd_x402_sdk.interop.conformance import (
    MissingDependencyError,
    Report,
    default_interop_dir,
    run,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uvd-interop",
        description="Offline conformance runner of the stack's interop specification.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "check",
        help="run the fixtures and the vectors of interop/, and check manifest files",
        description=(
            "Runs the schema fixtures (fixtures/cases.json) and the vectors "
            "(vectors/index.json) of an interop/ directory, then checks each MANIFEST "
            "file (an app's /.well-known/uvd-stack.json saved to disk) against the "
            "manifest schema and R1.10, R3.3 and R5.5. Nothing goes over the network."
        ),
    )
    check.add_argument("manifests", nargs="*", type=Path, metavar="MANIFEST")
    check.add_argument(
        "--interop",
        type=Path,
        metavar="DIR",
        help=(
            "the interop/ directory to run (default: the one of the source checkout this "
            "SDK runs from; an installed wheel does not ship it)"
        ),
    )
    return parser


def _summary(report: Report) -> list[str]:
    lines = [
        f"interop: {report.interop_dir}",
        f"fixtures: {report.fixtures} cases",
        "vectors: "
        + (", ".join(f"{kind} {count}" for kind, count in report.vectors.items()) or "none"),
    ]
    for path, problems in report.manifests.items():
        lines.append(f"manifest {path}: " + ("ok" if not problems else f"{problems} problems"))
    lines.extend(f"FAIL {failure}" for failure in report.failures)
    lines.append("GREEN" if report.green else f"RED: {len(report.failures)} failures")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # A path or a detail with a character the console cannot encode must not
    # turn a verdict into a traceback.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
    interop_dir = args.interop if args.interop is not None else default_interop_dir()
    if interop_dir is None or not (interop_dir / "fixtures" / "cases.json").is_file():
        where = f"at {interop_dir}" if interop_dir is not None else "next to this installation"
        print(
            f"uvd-interop: no interop/ directory {where} (it holds fixtures/cases.json). "
            "Pass --interop DIR: a copy of interop/ taken from a commit of main.",
            file=sys.stderr,
        )
        return 2
    missing = [str(path) for path in args.manifests if not path.is_file()]
    if missing:
        print(f"uvd-interop: no such manifest file: {', '.join(missing)}", file=sys.stderr)
        return 2
    try:
        report = run(interop_dir, args.manifests)
    except MissingDependencyError as exc:
        print(f"uvd-interop: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # A bug of the runner is not a verdict: exit 1 would read as red.
        print(f"uvd-interop: the runner failed: {exc!r}", file=sys.stderr)
        return 2
    print("\n".join(_summary(report)))
    return 0 if report.green else 1


if __name__ == "__main__":
    sys.exit(main())
