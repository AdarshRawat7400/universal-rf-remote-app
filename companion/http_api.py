"""Small dependency-free localhost JSON API for the companion database."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from urllib.parse import unquote, urlsplit

from .database import ConflictError, DatabaseError, NotFoundError
from .profile import ProfileValidationError


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_REQUEST_BYTES = 256 * 1024
MAX_PATH_BYTES = 2048


class RequestError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


class CompanionHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying one shared, internally synchronized database."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        database,
        max_request_bytes=DEFAULT_MAX_REQUEST_BYTES,
        handler_class=None,
    ):
        if (
            isinstance(max_request_bytes, bool)
            or not isinstance(max_request_bytes, int)
            or max_request_bytes < 1024
        ):
            raise ValueError("max_request_bytes must be at least 1024")
        self.database = database
        self.max_request_bytes = max_request_bytes
        super().__init__(address, handler_class or CompanionRequestHandler)


class CompanionRequestHandler(BaseHTTPRequestHandler):
    server_version = "BadgeIRCompanion/1"
    sys_version = ""

    def log_message(self, format, *args):
        # Keep normal HTTP access logging, without adding request bodies or
        # other potentially sensitive discovery metadata.
        super().log_message(format, *args)

    def handle_expect_100(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(400, "invalid_length", "invalid Content-Length")
            return False
        if length > self.server.max_request_bytes:
            self._error(413, "payload_too_large", "request body is too large")
            return False
        return super().handle_expect_100()

    def _send_json(self, status, payload):
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def _error(self, status, code, message):
        self._send_json(
            status,
            {"error": {"code": code, "message": message}},
        )

    def _read_json(self):
        if self.headers.get("Transfer-Encoding"):
            raise RequestError(400, "unsupported_transfer_encoding", "use Content-Length")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise RequestError(411, "length_required", "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise RequestError(400, "invalid_length", "invalid Content-Length") from error
        if length < 1:
            raise RequestError(400, "empty_body", "JSON request body is required")
        if length > self.server.max_request_bytes:
            # Drain only a small, bounded overage when it is already buffered.
            # This avoids a TCP reset hiding the JSON 413 response on Windows,
            # while never accepting or allocating an arbitrarily large body.
            if length <= self.server.max_request_bytes + 64 * 1024:
                self.rfile.read(length)
            raise RequestError(413, "payload_too_large", "request body is too large")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise RequestError(400, "incomplete_body", "request body is incomplete")
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise RequestError(
                415, "unsupported_media_type", "Content-Type must be application/json"
            )
        try:
            return json.loads(
                raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise RequestError(400, "invalid_json", "request body is not valid JSON") from error

    def _segments(self):
        if len(self.path.encode("utf-8")) > MAX_PATH_BYTES:
            raise RequestError(414, "uri_too_long", "request path is too long")
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            raise RequestError(400, "query_not_supported", "query strings are not supported")
        if not parsed.path.startswith("/"):
            raise RequestError(400, "invalid_path", "invalid request path")
        if parsed.path == "/":
            return []
        raw_segments = parsed.path[1:].split("/")
        if not raw_segments or any(not segment for segment in raw_segments):
            raise RequestError(404, "not_found", "route does not exist")
        segments = []
        for raw_segment in raw_segments:
            segment = unquote(raw_segment)
            # Route splitting happens before decoding, so an encoded slash can
            # safely be part of a button label. Device ids are independently
            # restricted by profile validation and cannot contain slashes.
            if "\x00" in segment:
                raise RequestError(400, "invalid_path", "invalid path segment")
            segments.append(segment)
        return segments

    @staticmethod
    def _discovery_id(value):
        try:
            result = int(value)
        except ValueError as error:
            raise RequestError(400, "invalid_id", "discovery id must be an integer") from error
        if result < 1 or str(result) != value:
            raise RequestError(400, "invalid_id", "discovery id must be positive")
        return result

    def _dispatch(self):
        method = self.command
        segments = self._segments()
        database = self.server.database

        if method == "GET" and not segments:
            return 200, {
                "name": "GitHub Universe Badge IR companion",
                "api": "/api/v1",
                "health": "/health",
            }
        if method == "GET" and segments == ["health"]:
            return 200, {"status": "ok", "database_schema": 1}
        if not segments or segments[:2] != ["api", "v1"]:
            raise RequestError(404, "not_found", "route does not exist")
        route = segments[2:]

        if route == ["profile"]:
            if method == "GET":
                return 200, {"data": database.export_profile()}
            if method == "PUT":
                stored = database.import_profile(self._read_json())
                return 200, {
                    "data": {
                        "stored": True,
                        "schema": stored["schema"],
                        "active_device": stored["active_device"],
                        "device_count": len(stored["devices"]),
                    }
                }

        if route == ["devices"]:
            if method == "GET":
                return 200, {"data": database.list_devices()}
            if method == "POST":
                return 201, {"data": database.create_device(self._read_json())}

        if len(route) == 2 and route[0] == "devices":
            device_id = route[1]
            if method == "GET":
                return 200, {"data": database.get_device(device_id)}
            if method == "PATCH":
                return 200, {
                    "data": database.update_device(device_id, self._read_json())
                }
            if method == "DELETE":
                return 200, {"data": database.delete_device(device_id)}

        if len(route) == 3 and route[0] == "devices" and route[2] == "buttons":
            if method == "GET":
                return 200, {"data": database.get_buttons(route[1])}

        if len(route) == 4 and route[0] == "devices" and route[2] == "buttons":
            device_id, button_name = route[1], route[3]
            if method == "PUT":
                return 200, {
                    "data": database.put_button(
                        device_id, button_name, self._read_json()
                    )
                }
            if method == "DELETE":
                return 200, {
                    "data": database.delete_button(device_id, button_name)
                }

        if route == ["discoveries"]:
            if method == "GET":
                return 200, {"data": database.list_discoveries()}
            if method == "POST":
                return 200, {"data": database.upsert_discovery(self._read_json())}
            if method == "DELETE":
                return 200, {"data": {"deleted": database.clear_discoveries()}}

        if route == ["discoveries", "save"] and method == "POST":
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise ProfileValidationError("save payload must be an object")
            unknown = set(payload).difference({"ids", "all"})
            if unknown:
                raise ProfileValidationError(
                    "unsupported save field: " + sorted(unknown)[0]
                )
            return 200, {
                "data": database.save_discoveries(
                    payload.get("ids"), payload.get("all", False)
                )
            }

        if len(route) == 2 and route[0] == "discoveries":
            discovery_id = self._discovery_id(route[1])
            if method == "GET":
                return 200, {"data": database.get_discovery(discovery_id)}
            if method == "DELETE":
                return 200, {"data": database.delete_discovery(discovery_id)}

        raise RequestError(404, "not_found", "route does not exist")

    def _handle(self):
        try:
            status, payload = self._dispatch()
        except RequestError as error:
            self._error(error.status, error.code, error.message)
        except ProfileValidationError as error:
            self._error(400, "validation_error", str(error))
        except NotFoundError as error:
            self._error(404, "not_found", str(error))
        except ConflictError as error:
            self._error(409, "conflict", str(error))
        except (DatabaseError, sqlite3.Error):
            self.log_error("database operation failed")
            self._error(500, "database_error", "database operation failed")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self.log_error("unexpected request failure")
            self._error(500, "internal_error", "internal server error")
        else:
            self._send_json(status, payload)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


def create_server(
    database,
    host=DEFAULT_HOST,
    port=DEFAULT_PORT,
    max_request_bytes=DEFAULT_MAX_REQUEST_BYTES,
):
    """Create, but do not start, a companion server."""

    return CompanionHTTPServer(
        (host, port), database, max_request_bytes=max_request_bytes
    )
