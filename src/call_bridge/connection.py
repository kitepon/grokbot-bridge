"""Operator-only, read-only BellTeam connection export. See architecture/connection-handoff.md."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .auth import Authenticator, AuthError


class ExportError(Exception):
    pass


def export_connection(root: Path) -> dict[str, str]:
    try:
        config = json.loads((root / "connection-export.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ExportError("cannot read Bridge connection export configuration") from None
    if not isinstance(config, dict) or set(config) != {"url", "tokens_file", "token_command"}:
        raise ExportError("configuration requires url, tokens_file and token_command")
    url = config["url"]
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        valid_url = (parts is not None and parts.scheme in ("http", "https") and parts.hostname
                     and parts.port != 0 and parts.username is None and parts.password is None
                     and parts.path.endswith("/mcp") and "?" not in url and "#" not in url
                     and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url))
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ExportError("url must be an absolute HTTP(S) MCP endpoint without userinfo, query or fragment")
    registry = config["tokens_file"]
    command = config["token_command"]
    if not isinstance(registry, str) or not registry.strip() or "\0" in registry:
        raise ExportError("tokens_file must name Bridge's token registry")
    if (not isinstance(command, list) or not command
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command)):
        raise ExportError("token_command must be a nonempty argv array")
    try:
        auth = Authenticator(tokens_file=str(root / registry))
    except AuthError:
        raise ExportError("cannot load Bridge's token registry") from None
    try:
        result = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=45, check=False)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ExportError("credential provider failed or timed out") from None
    if result.returncode != 0:
        raise ExportError("credential provider failed")
    try:
        token = result.stdout.decode("utf-8").removesuffix("\n").removesuffix("\r")
    except UnicodeError:
        raise ExportError("credential provider returned an invalid token") from None
    if not token or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token):
        raise ExportError("credential provider must return one raw token without whitespace")
    headers = {"authorization": f"Bearer {token}"}
    try:
        principal = auth.authenticate(headers)
        delegated = auth.authenticate({**headers, "x-call-bridge-caller-id": "connection-export-check"})
    except AuthError:
        raise ExportError("token does not allow BellTeam caller-ID delegation") from None
    if (principal is None or principal.system != "bellteam" or principal.id is not None
            or principal.ops or principal.courier or delegated is None
            or delegated.id != "connection-export-check"):
        raise ExportError("token must be a registered BellTeam credential without fixed ID or elevated privileges")
    return {"schema": "call-bridge.connection.v1", "url": url, "token": token}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", required=True,
                        help="export connection JSON to a private pipe")
    parser.parse_args(argv)
    try:
        connection = export_connection(Path.cwd())
    except ExportError as error:
        print(f"call-bridge-connection: {error}", file=sys.stderr)
        return 1
    try:
        sys.stdout.write(json.dumps(connection, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        print("call-bridge-connection: cannot write connection output", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
