#!/usr/bin/env python3
"""Fail if a secret or a private host detail would be committed.

Run before every push, and in CI:

    python3 scripts/check_secrets.py          # tracked files (or the tree)
    python3 scripts/check_secrets.py --all    # also untracked, except ignored

The check is deliberately blunt. It knows the shapes this project actually
uses — a Telegram bot token, a raw secp256k1 key, a cached bearer token, a
literal IPv4 address, a home directory — and treats a hit as fatal. Add an
explicit allowlist entry only for a value that is genuinely public.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (name, regex). Keep these as strings that cannot match themselves.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{33,}\b")),
    ("raw 32-byte key", re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")),
    ("0x-prefixed key", re.compile(r"0x[0-9a-fA-F]{64}")),
    ("pem private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer token", re.compile(r"[Bb]earer\s+[A-Za-z0-9._~+/-]{24,}=*")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}")),
    (
        "assigned secret",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*"
            r"[\"']?[A-Za-z0-9/+_-]{24,}[\"']?"
        ),
    ),
    ("home directory", re.compile(r"/home/[A-Za-z0-9._-]+/")),
]

# Lines allowed to look like secrets. Keep this list short and boring.
ALLOW = (
    "replace-with-the-botfather-token",
    "123456789:replace",
    "spectator.key",
)

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "data", "node_modules", ".mypy_cache"}


def _git(*args: str) -> list[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def candidates(include_untracked: bool) -> list[Path]:
    """Exactly the files that could end up in a commit.

    `git ls-files` is the truth when there is a repository: tracked files
    normally, plus untracked-but-not-ignored with `--all`. Falling back to a
    walk only happens outside git, and skips the same local artifacts by hand.
    """
    names = _git("ls-files") if not include_untracked else []
    if not names:
        names = _git("ls-files", "-co", "--exclude-standard")
    if names:
        return [ROOT / name for name in names if (ROOT / name).is_file()]

    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            continue
        files.append(path)
    return files


def looks_private(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    )


IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def scan(path: Path) -> list[str]:
    findings: list[str] = []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return findings
    for number, line in enumerate(text.splitlines(), 1):
        if any(marker in line for marker in ALLOW):
            continue
        for name, pattern in PATTERNS:
            if pattern.search(line):
                findings.append(f"{path.relative_to(ROOT)}:{number}: {name}")
        for ip in IPV4.findall(line):
            if not looks_private(ip):
                findings.append(f"{path.relative_to(ROOT)}:{number}: public IP {ip}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="scan untracked files too")
    parser.add_argument("--quiet", action="store_true", help="only print on failure")
    args = parser.parse_args()

    files = candidates(args.all)
    findings: list[str] = []
    for path in files:
        findings += scan(path)

    if findings:
        print("secret / host detail found — do not commit:\n", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"ok — {len(files)} file(s) scanned, no secrets or host details")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
