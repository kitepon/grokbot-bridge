#!/usr/bin/env python3
"""Issue a bearer token bound to a system and record its hash.

Run on the bridge host from the repository (it checks the result with
``src/call_bridge/auth.py``). The token is generated here, its SHA-256 goes
into ``CALL_BRIDGE_TOKENS_FILE`` and the token itself is written only to
``--out`` (mode 600) or, with ``--out -``, to stdout for a pipe such as
``ssh main-server ... --out - > file-on-the-box``. It is never printed otherwise.

    python3 scripts/issue_token.py --tokens-file tokens.json \\
        --name bellteam --system bellteam --caller-id-header --out /path/token

An existing name or ``--out`` file stops the run unless ``--replace`` is given.
A failed run leaves the previous token working: the tokens file is only
replaced once the new token is safely out, and put back if placing ``--out``
fails. The bridge reads the file at startup, so restart it after issuing.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from call_bridge.auth import SYSTEMS, AuthError, _load_entries  # noqa: E402


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


def _read_tokens(tokens_file: Path) -> tuple[dict[str, Any], str | None]:
    """The parsed tokens file and its original text (None when missing)."""
    try:
        text = tokens_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"tokens": []}, None
    try:
        data = json.loads(text)
    except ValueError as e:
        raise IssueError(f"{tokens_file} is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("tokens", []), list):
        raise IssueError(f"{tokens_file} must be an object with a tokens list")
    data.setdefault("tokens", [])
    return data, text


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _stage(path: Path, text: str, mode: int) -> Path:
    """Write ``text`` to a mode-``mode`` temporary file next to ``path``."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        _discard(Path(tmp))
        raise
    return Path(tmp)


def _place(staged: Path, path: Path) -> None:
    """Rename ``staged`` over ``path``. Once renamed, only warn if the
    directory fsync fails: the new file is in place and must not be undone."""
    os.replace(staged, path)
    try:
        _fsync_dir(path.parent)
    except OSError as e:
        print(f"issue_token: warning: {path.parent} was not synced to disk: {e}", file=sys.stderr)


def _discard(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _clean(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise IssueError(f"{label} must not be empty")
    return value


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


def _write_stdout(text: str) -> None:
    if sys.stdout is None:
        raise IssueError("stdout is closed; nothing was registered")
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except (OSError, ValueError) as e:
        raise IssueError(f"could not write the token to stdout; nothing was registered: {e}") from e


def issue(args: argparse.Namespace) -> None:
    args.name = _clean(args.name, "--name")
    args.id = _clean(args.id, "--id")
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
    token_text = _token_text(token, args.format)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _locked(tokens_file):
        data, original = _read_tokens(tokens_file)
        names = [t.get("name") for t in data["tokens"] if isinstance(t, dict)]
        if args.name in names and not args.replace:
            raise IssueError(f"token name {args.name} exists; use --replace to rotate it")
        mode = tokens_file.stat().st_mode & 0o777 if original is not None else 0o600
        data["tokens"] = [t for t in data["tokens"]
                          if not (isinstance(t, dict) and t.get("name") == args.name)]
        data["tokens"].append(_entry(args, digest))

        staged_tokens = staged_out = None
        try:
            staged_tokens = _stage(tokens_file, json.dumps(data, ensure_ascii=False, indent=2) + "\n", mode)
            # The bridge refuses to start on a bad file; check the whole
            # result with its own loader before anything is replaced.
            try:
                _load_entries(str(staged_tokens))
            except AuthError as e:
                raise IssueError(f"the resulting tokens file would not load: {e}") from e
            if out is not None:
                staged_out = _stage(out, token_text, 0o600)
            else:
                # Hand the token over before registering it: a token that
                # never got registered just does not work.
                _write_stdout(token_text)
            _place(staged_tokens, tokens_file)
            staged_tokens = None
            if staged_out is not None and out is not None:
                try:
                    _place(staged_out, out)
                    staged_out = None
                except OSError as e:
                    # Put the previous registration back so the old token
                    # (still in --out) keeps working.
                    restore = None
                    try:
                        if original is None:
                            _discard(tokens_file)
                        else:
                            restore = _stage(tokens_file, original, mode)
                            _place(restore, tokens_file)
                            restore = None
                    except OSError as restore_error:
                        _discard(restore)
                        kept, staged_out = staged_out, None
                        raise IssueError(
                            f"could not place {out} ({e}) nor restore {tokens_file} ({restore_error}). "
                            f"{tokens_file} now registers the new token for {args.name}, which is kept in {kept}; "
                            f"{out} still holds the old token, which no longer works. "
                            f"Move {kept} to {out}, or rerun with --replace."
                        ) from e
                    raise
        finally:
            _discard(staged_tokens)
            _discard(staged_out)
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
