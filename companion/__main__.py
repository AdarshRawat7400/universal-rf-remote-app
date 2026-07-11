"""Command-line entry point for ``python -m companion``."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

from .database import CompanionDatabase, DatabaseError
from .http_api import (
    DEFAULT_HOST,
    DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_PORT,
    create_server,
)
from .profile import MAX_PROFILE_BYTES, ProfileValidationError


DEFAULT_DATABASE_PATH = "badge-ir.sqlite3"
MAX_IMPORT_FILE_BYTES = max(MAX_PROFILE_BYTES * 2, 256 * 1024)


def is_loopback_host(host):
    """Return true only when every resolved bind address is loopback."""

    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    if not addresses:
        return False
    try:
        return all(ipaddress.ip_address(item[4][0]).is_loopback for item in addresses)
    except ValueError:
        return False


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m companion",
        description="SQLite companion for the GitHub Universe badge IR remote",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DATABASE_PATH,
        help="SQLite file (default: %(default)s)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the localhost JSON API")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument(
        "--max-request-bytes", type=int, default=DEFAULT_MAX_REQUEST_BYTES
    )
    serve.add_argument(
        "--allow-remote",
        action="store_true",
        help="explicitly permit a non-loopback bind (not recommended)",
    )

    import_parser = subparsers.add_parser(
        "import-profile", help="replace the database profile from badge JSON"
    )
    import_parser.add_argument("input", help="input JSON path")

    export_parser = subparsers.add_parser(
        "export-profile", help="write badge-compatible schema-v4 JSON"
    )
    export_parser.add_argument("output", help="output JSON path, or - for stdout")

    subparsers.add_parser("list-devices", help="print saved device summaries")
    subparsers.add_parser("list-discoveries", help="print discovery records")
    return parser


def _print_json(value, stream=None):
    if stream is None:
        stream = sys.stdout
    json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
    stream.write("\n")


def _read_profile(path):
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_IMPORT_FILE_BYTES:
        raise ProfileValidationError("profile input file is too large")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_profile(path, profile):
    if path == "-":
        _print_json(profile)
        return
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=target.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(profile, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.remove(temporary_name)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        if not 0 <= args.port <= 65535:
            parser.error("--port must be between 0 and 65535")
        if args.max_request_bytes < 1024:
            parser.error("--max-request-bytes must be at least 1024")
        if not args.allow_remote and not is_loopback_host(args.host):
            parser.error(
                "refusing a non-loopback bind; use --allow-remote only with "
                "your own authentication/firewall layer"
            )

    try:
        database = CompanionDatabase(args.db)
        try:
            if args.command == "serve":
                server = create_server(
                    database,
                    host=args.host,
                    port=args.port,
                    max_request_bytes=args.max_request_bytes,
                )
                try:
                    host, port = server.server_address[:2]
                    print("Badge IR companion listening on http://%s:%s" % (host, port))
                    server.serve_forever()
                except KeyboardInterrupt:
                    print("\nStopping companion.")
                finally:
                    server.server_close()
            elif args.command == "import-profile":
                profile = database.import_profile(_read_profile(args.input))
                _print_json(
                    {
                        "imported": True,
                        "active_device": profile["active_device"],
                        "device_count": len(profile["devices"]),
                    }
                )
            elif args.command == "export-profile":
                _write_profile(args.output, database.export_profile())
            elif args.command == "list-devices":
                _print_json(database.list_devices())
            elif args.command == "list-discoveries":
                _print_json(database.list_discoveries())
        finally:
            database.close()
    except (DatabaseError, ProfileValidationError, OSError, ValueError, json.JSONDecodeError) as error:
        print("error: " + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
