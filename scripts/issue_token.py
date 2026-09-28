#!/usr/bin/env python3
"""Issue a bearer token bound to a system and record its hash.

Run on the bridge host. The token is generated here, its SHA-256 goes into
``CALL_BRIDGE_TOKENS_FILE`` and the token itself is written only to ``--out``
(created with mode 600) or, with ``--out -``, to stdout for a pipe such as
``ssh main-server ... --out - > file-on-the-box``. It is never printed otherwise.

    python3 scripts/issue_token.py --tokens-file tokens.json \\
        --name bellteam --system bellteam --caller-id-header --out /path/token

An existing name stops the run unless ``--replace`` is given. The tokens file
is rewritten through a temporary file and a lock. The bridge reads the file at
startup, so restart it after issuing.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SYSTEMS = ("local", "grokbot", "bellteam")


class IssueError(Exception):
    pass


@contextmanager
def _locked(tokens_file: Path) -> Iterator[None]:
    lock_path = tokens_file.with_name(tokens_file.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_tokens(tokens_file: Path) -> dict[str, Any]:
    try:
        data = json.loads(tokens_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"tokens": []}
    except ValueError as e:
        raise IssueError(f"{tokens_file} is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("tokens", []), list):
        raise IssueError(f"{tokens_file} must be an object with a tokens list")
    data.setdefault("tokens", [])
    return data


def _write_atomic(path: Path, text: str, mode: int) -> None:
    """Write ``text`` to a mode-``mode`` temporary file, then rename it over ``path``."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _entry(args: argparse.Namespace, digest: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": args.name, "sha256": digest}
    if args.system:
        entry["system"] = args.system
    if args.id:
        entry["id"] = args.id
    if args.caller_id_header:
        entry["caller_id_header"] = True
    if args.ops:
        entry["ops"] = True
    return entry


def _token_text(token: str, fmt: str) -> str:
    return f"Authorization: Bearer {token}\n" if fmt == "header" else f"{token}\n"


def issue(args: argparse.Namespace) -> None:
    if not args.system and not args.ops:
        raise IssueError("give --system, or --ops for an unrestricted entry")
    if args.caller_id_header and not args.system:
        raise IssueError("--caller-id-header needs --system")
    tokens_file = Path(args.tokens_file)
    if not tokens_file.parent.is_dir():
        raise IssueError(f"directory does not exist: {tokens_file.parent}")
    out = None if args.out == "-" else Path(args.out)
    if out is not None:
        if not out.parent.is_dir():
            raise IssueError(f"directory does not exist: {out.parent}")
        if out.exists() and not args.replace:
            raise IssueError(f"{out} exists; use --replace to issue a new token over it")

    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _locked(tokens_file):
        data = _read_tokens(tokens_file)
        names = [t.get("name") for t in data["tokens"] if isinstance(t, dict)]
        if args.name in names and not args.replace:
            raise IssueError(f"token name {args.name} exists; use --replace to rotate it")
        mode = tokens_file.stat().st_mode & 0o777 if tokens_file.exists() else 0o600
        tokens = [t for t in data["tokens"] if not (isinstance(t, dict) and t.get("name") == args.name)]
        tokens.append(_entry(args, digest))
        data["tokens"] = tokens
        # Token first: if recording its hash then fails, the token just does
        # not authenticate. Nothing half-written is ever accepted.
        if out is not None:
            _write_atomic(out, _token_text(token, args.format), 0o600)
        _write_atomic(tokens_file, json.dumps(data, ensure_ascii=False, indent=2) + "\n", mode)
    if out is None:
        sys.stdout.write(_token_text(token, args.format))
        sys.stdout.flush()
    print(
        f"issued {args.name} ({args.system or 'ops'}) into {tokens_file}; restart call-bridge to load it",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tokens-file", required=True, help="CALL_BRIDGE_TOKENS_FILE on the host")
    parser.add_argument("--name", required=True)
    parser.add_argument("--system", choices=SYSTEMS)
    parser.add_argument("--id", help="fix the caller id for this token")
    parser.add_argument("--caller-id-header", action="store_true",
                        help="let this system name the caller in X-Call-Bridge-Caller-Id")
    parser.add_argument("--ops", action="store_true", help="allow ops hangup; alone, unrestricted")
    parser.add_argument("--out", required=True, help="file for the token (mode 600), or - for stdout")
    parser.add_argument("--format", choices=("token", "header"), default="token",
                        help="token: the token line; header: an Authorization header line")
    parser.add_argument("--replace", action="store_true", help="rotate an existing name or --out file")
    args = parser.parse_args(argv)
    try:
        issue(args)
    except (IssueError, OSError) as e:
        print(f"issue_token: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
