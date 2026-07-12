"""Bounded WLED discovery and JSON control for the Badge Settings app."""

import gc

from badge_settings_model import validate_ipv4


HTTP_PORT = 80
WLED_NODE_PORT = 65506
MAX_HEADER_BYTES = 2048
MAX_INFO_BYTES = 4096
MAX_STATE_BYTES = 8192
MAX_EFFECT_BYTES = 16 * 1024
MAX_EFFECTS = 256
MAX_EFFECT_NAME_BYTES = 48
MAX_DEVICE_NAME_BYTES = 48
MAX_DEVICES = 16
MAX_SCAN_HOSTS = 254


class WLEDError(RuntimeError):
    """A sanitized WLED transport, protocol, or validation failure."""


def _is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _bounded_text(value, maximum_bytes, fallback):
    if not isinstance(value, str) or not value:
        return fallback
    for character in value:
        codepoint = ord(character)
        if codepoint < 32 or codepoint == 127:
            return fallback
    try:
        encoded = value.encode("utf-8")
    except (AttributeError, UnicodeError):
        return fallback
    if len(encoded) <= maximum_bytes:
        return value
    end = len(value)
    while end > 0:
        candidate = value[:end]
        if len(candidate.encode("utf-8")) <= maximum_bytes:
            return candidate
        end -= 1
    return fallback


def _ip_to_integer(ip_address):
    canonical = validate_ipv4(ip_address, False)
    result = 0
    for part in canonical.split("."):
        result = (result << 8) | int(part)
    return result


def _integer_to_ip(value):
    return "%d.%d.%d.%d" % (
        (value >> 24) & 255,
        (value >> 16) & 255,
        (value >> 8) & 255,
        value & 255,
    )


def _netmask_to_integer(netmask):
    parts = netmask.split(".") if isinstance(netmask, str) else ()
    if len(parts) != 4:
        raise WLEDError("network mask is unavailable")
    result = 0
    for part in parts:
        if not part or any(character < "0" or character > "9" for character in part):
            raise WLEDError("network mask is invalid")
        number = int(part)
        if number > 255:
            raise WLEDError("network mask is invalid")
        result = (result << 8) | number
    inverse = (~result) & 0xFFFFFFFF
    if inverse & (inverse + 1):
        raise WLEDError("network mask is not contiguous")
    return result


def _load_socket_module():
    try:
        import socket
    except ImportError:
        raise WLEDError("socket support is unavailable")
    return socket


def _load_json_module():
    try:
        import ujson as json_module
    except ImportError:
        try:
            import json as json_module
        except ImportError:
            raise WLEDError("JSON support is unavailable")
    return json_module


def _send_all(sock, payload):
    sent = 0
    while sent < len(payload):
        count = sock.send(payload[sent:])
        if not count:
            raise WLEDError("WLED request could not be sent")
        sent += int(count)


def _content_length(header_bytes):
    lower = header_bytes.lower()
    marker = b"\r\ncontent-length:"
    start = lower.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = lower.find(b"\r\n", start)
    if end < 0:
        return None
    token = lower[start:end].strip()
    if not token or any(byte < 48 or byte > 57 for byte in token):
        raise WLEDError("WLED returned an invalid length")
    return int(token)


def _decode_chunked(payload, maximum_bytes):
    output = bytearray()
    cursor = 0
    while True:
        line_end = payload.find(b"\r\n", cursor)
        if line_end < 0 or line_end - cursor > 16:
            raise WLEDError("WLED returned invalid chunked data")
        token = payload[cursor:line_end].split(b";", 1)[0].strip()
        try:
            size = int(token, 16)
        except (TypeError, ValueError):
            raise WLEDError("WLED returned invalid chunked data")
        cursor = line_end + 2
        if size == 0:
            return bytes(output)
        if size < 0 or len(output) + size > maximum_bytes:
            raise WLEDError("WLED response is too large")
        end = cursor + size
        if end + 2 > len(payload) or payload[end : end + 2] != b"\r\n":
            raise WLEDError("WLED returned incomplete chunked data")
        output.extend(payload[cursor:end])
        cursor = end + 2


class WLEDClient:
    """Small raw-HTTP WLED client with strict memory and timeout bounds."""

    def __init__(self, socket_module=None, json_module=None, timeout=1.2):
        if not isinstance(timeout, (int, float)) or timeout < 0.05 or timeout > 5:
            raise ValueError("WLED timeout is out of range")
        self.socket_module = socket_module
        self.json_module = json_module
        self.timeout = timeout

    def __repr__(self):
        return "<WLEDClient bounded>"

    def _socket_module(self):
        if self.socket_module is None:
            self.socket_module = _load_socket_module()
        return self.socket_module

    def _json_module(self):
        if self.json_module is None:
            self.json_module = _load_json_module()
        return self.json_module

    def _request(
        self,
        ip_address,
        method,
        path,
        payload=None,
        maximum_bytes=4096,
        timeout=None,
        response_timeout=None,
    ):
        ip_address = validate_ipv4(ip_address, False)
        if method not in ("GET", "POST") or not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("invalid WLED request")
        if not _is_integer(maximum_bytes) or maximum_bytes < 32 or maximum_bytes > MAX_EFFECT_BYTES:
            raise ValueError("WLED response bound is invalid")
        connect_timeout = self.timeout if timeout is None else timeout
        read_timeout = self.timeout if response_timeout is None else response_timeout
        for timeout_value in (connect_timeout, read_timeout):
            if (
                not isinstance(timeout_value, (int, float))
                or timeout_value < 0.02
                or timeout_value > 5
            ):
                raise ValueError("WLED request timeout is invalid")

        body = b""
        if payload is not None:
            try:
                serialized = self._json_module().dumps(payload)
                if not isinstance(serialized, str):
                    serialized = str(serialized)
                body = serialized.encode("utf-8")
            except Exception:
                raise WLEDError("WLED command could not be encoded")
            if len(body) > 1024:
                raise WLEDError("WLED command is too large")

        request = (
            method
            + " "
            + path
            + " HTTP/1.0\r\nHost: "
            + ip_address
            + "\r\nAccept: application/json\r\nConnection: close\r\n"
        ).encode("ascii")
        if body:
            request += (
                "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(body)
            ).encode("ascii")
        request += b"\r\n" + body

        socket_module = self._socket_module()
        sock = None
        raw = bytearray()
        try:
            family = getattr(socket_module, "AF_INET", None)
            socket_type = getattr(socket_module, "SOCK_STREAM", None)
            sock = (
                socket_module.socket(family, socket_type)
                if family is not None and socket_type is not None
                else socket_module.socket()
            )
            sock.settimeout(connect_timeout)
            sock.connect((ip_address, HTTP_PORT))
            # A short connect timeout keeps subnet sweeps responsive, while a
            # separate response budget lets a real ESP device produce JSON.
            sock.settimeout(read_timeout)
            _send_all(sock, request)

            header_end = -1
            expected_total = None
            while len(raw) <= maximum_bytes + MAX_HEADER_BYTES:
                chunk = sock.recv(512)
                if not chunk:
                    break
                raw.extend(chunk)
                if header_end < 0:
                    header_end = raw.find(b"\r\n\r\n")
                    if header_end >= 0:
                        length = _content_length(bytes(raw[:header_end]))
                        if length is not None:
                            if length > maximum_bytes:
                                raise WLEDError("WLED response is too large")
                            expected_total = header_end + 4 + length
                    elif len(raw) > MAX_HEADER_BYTES:
                        raise WLEDError("WLED returned oversized headers")
                if expected_total is not None and len(raw) >= expected_total:
                    break
            if len(raw) > maximum_bytes + MAX_HEADER_BYTES:
                raise WLEDError("WLED response is too large")
        except WLEDError:
            raise
        except Exception:
            raise WLEDError("WLED connection failed")
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

        try:
            header_end = raw.find(b"\r\n\r\n")
            if header_end < 0:
                raise WLEDError("WLED returned an incomplete response")
            header = bytes(raw[:header_end])
            status_line_end = header.find(b"\r\n")
            status_line = header if status_line_end < 0 else header[:status_line_end]
            parts = status_line.split(b" ", 2)
            if len(parts) < 2:
                raise WLEDError("WLED returned an invalid HTTP status")
            status = int(parts[1])
            if status < 200 or status >= 300:
                raise WLEDError("WLED returned HTTP %d" % status)
            response_body = bytes(raw[header_end + 4 :])
            if b"transfer-encoding: chunked" in header.lower():
                response_body = _decode_chunked(response_body, maximum_bytes)
            length = _content_length(header)
            if length is not None:
                if len(response_body) < length:
                    raise WLEDError("WLED returned an incomplete response")
                response_body = response_body[:length]
            if len(response_body) > maximum_bytes:
                raise WLEDError("WLED response is too large")
            return response_body
        except WLEDError:
            raise
        except Exception:
            raise WLEDError("WLED returned an invalid HTTP response")
        finally:
            raw = None
            request = None
            body = None
            gc.collect()

    def _json_request(
        self,
        ip_address,
        method,
        path,
        payload=None,
        maximum_bytes=4096,
        timeout=None,
        response_timeout=None,
    ):
        body = self._request(
            ip_address,
            method,
            path,
            payload,
            maximum_bytes,
            timeout,
            response_timeout,
        )
        try:
            text = body.decode("utf-8")
            result = self._json_module().loads(text)
        except Exception:
            raise WLEDError("WLED returned invalid JSON")
        finally:
            body = None
        return result

    def probe(self, ip_address, timeout=None, response_timeout=None):
        info = self._json_request(
            ip_address,
            "GET",
            "/json/info",
            maximum_bytes=MAX_INFO_BYTES,
            timeout=timeout,
            response_timeout=response_timeout,
        )
        if not isinstance(info, dict):
            raise WLEDError("host is not a WLED controller")
        version = info.get("ver")
        name = info.get("name")
        leds = info.get("leds")
        effect_count = info.get("fxcount")
        if (
            not isinstance(version, str)
            or not version
            or not isinstance(name, str)
            or not isinstance(leds, dict)
            or not _is_integer(effect_count)
            or effect_count < 1
            or effect_count > 512
        ):
            raise WLEDError("host is not a WLED controller")
        return {
            "ip": validate_ipv4(ip_address, False),
            "name": _bounded_text(name, MAX_DEVICE_NAME_BYTES, "WLED"),
            "version": _bounded_text(version, 32, "Unknown"),
            "fxcount": effect_count,
        }

    def get_state(self, ip_address):
        state = self._json_request(
            ip_address, "GET", "/json/state", maximum_bytes=MAX_STATE_BYTES
        )
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("on"), bool)
            or not _is_integer(state.get("bri"))
            or state["bri"] < 0
            or state["bri"] > 255
        ):
            raise WLEDError("WLED state is malformed")
        return state

    def get_effects(self, ip_address):
        effects = self._json_request(
            ip_address, "GET", "/json/eff", maximum_bytes=MAX_EFFECT_BYTES
        )
        if not isinstance(effects, list):
            raise WLEDError("WLED effect list is malformed")
        result = []
        for effect_id in range(min(len(effects), 512)):
            name = effects[effect_id]
            if not isinstance(name, str):
                continue
            cleaned = _bounded_text(name.strip(), MAX_EFFECT_NAME_BYTES, "")
            if not cleaned or cleaned.upper() == "RSVD" or cleaned == "-":
                continue
            result.append((effect_id, cleaned))
            if len(result) >= MAX_EFFECTS:
                break
        effects = None
        gc.collect()
        return result

    def _post_state(self, ip_address, payload):
        result = self._json_request(
            ip_address,
            "POST",
            "/json/state",
            payload=payload,
            maximum_bytes=MAX_STATE_BYTES,
        )
        if not isinstance(result, dict) or result.get("success") is not True:
            raise WLEDError("WLED rejected the command")
        return result

    def set_power(self, ip_address, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("power state must be boolean")
        self._post_state(ip_address, {"on": enabled})
        return {"on": enabled}

    def toggle_power(self, ip_address):
        current = self.get_state(ip_address)
        return self.set_power(ip_address, not current["on"])

    def set_color(self, ip_address, red, green, blue):
        values = (red, green, blue)
        if any(not _is_integer(value) or value < 0 or value > 255 for value in values):
            raise ValueError("RGB value is out of range")
        return self._post_state(
            ip_address,
            {
                "on": True,
                "seg": [{"id": 0, "fx": 0, "col": [[red, green, blue]]}],
            },
        )

    def set_effect(self, ip_address, effect_id):
        if not _is_integer(effect_id) or effect_id < 0 or effect_id > 511:
            raise ValueError("effect ID is out of range")
        return self._post_state(
            ip_address, {"on": True, "seg": [{"id": 0, "fx": effect_id}]}
        )

    def set_brightness(self, ip_address, brightness):
        if not _is_integer(brightness) or brightness < 1 or brightness > 255:
            raise ValueError("brightness is out of range")
        return self._post_state(ip_address, {"on": True, "bri": brightness})

    def close(self):
        return None


class WLEDScanner:
    """Incrementally verify WLED controllers on the badge's local subnet."""

    def __init__(
        self,
        client,
        wlan,
        saved_ip=None,
        max_devices=MAX_DEVICES,
        probe_timeout=0.12,
    ):
        if not isinstance(client, WLEDClient) and not hasattr(client, "probe"):
            raise ValueError("WLED client is invalid")
        if not _is_integer(max_devices) or max_devices < 1 or max_devices > MAX_DEVICES:
            raise ValueError("WLED result limit is invalid")
        if not isinstance(probe_timeout, (int, float)) or probe_timeout < 0.02 or probe_timeout > 1:
            raise ValueError("WLED probe timeout is invalid")
        self.client = client
        self.wlan = wlan
        self.saved_ip = saved_ip
        self.max_devices = max_devices
        self.probe_timeout = probe_timeout
        self.results = []
        self.done = False
        self.error = None
        self.scanned = 0
        self.total = 0
        self.current_ip = None
        self._own = 0
        self._start = 0
        self._end = -1
        self._next = 0
        self._pending = []
        self._probed = {}
        self._udp = None
        self._started = False

    @staticmethod
    def parse_announcement(packet):
        if not isinstance(packet, (bytes, bytearray)) or len(packet) < 40:
            return None
        if packet[0] != 255 or packet[1] != 1:
            return None
        ip_address = "%d.%d.%d.%d" % (
            packet[2],
            packet[3],
            packet[4],
            packet[5],
        )
        try:
            validate_ipv4(ip_address, False)
            name = bytes(packet[6:38]).split(b"\x00", 1)[0].decode("utf-8", "ignore").strip()
        except Exception:
            return None
        return {
            "ip": ip_address,
            "name": _bounded_text(name, MAX_DEVICE_NAME_BYTES, "WLED"),
        }

    def _same_scan_range(self, ip_address):
        try:
            value = _ip_to_integer(ip_address)
        except Exception:
            return False
        return self._start <= value <= self._end and value != self._own

    def _queue(self, ip_address, priority=False):
        if not self._same_scan_range(ip_address):
            return
        previous = self._probed.get(ip_address)
        if previous in ("priority", "found") or (previous == "sweep" and not priority):
            return
        for pending_ip, unused_priority in self._pending:
            if pending_ip == ip_address:
                return
        if len(self._pending) >= MAX_DEVICES:
            return
        self._pending.append((ip_address, bool(priority)))

    def _open_udp(self):
        socket_module = self.client._socket_module()
        sock = None
        try:
            family = getattr(socket_module, "AF_INET", None)
            socket_type = getattr(socket_module, "SOCK_DGRAM", None)
            sock = (
                socket_module.socket(family, socket_type)
                if family is not None and socket_type is not None
                else socket_module.socket()
            )
            reuse = getattr(socket_module, "SO_REUSEADDR", None)
            socket_level = getattr(socket_module, "SOL_SOCKET", None)
            if reuse is not None and socket_level is not None:
                try:
                    sock.setsockopt(socket_level, reuse, 1)
                except Exception:
                    pass
            sock.bind(("", WLED_NODE_PORT))
            setblocking = getattr(sock, "setblocking", None)
            if setblocking is not None:
                setblocking(False)
            else:
                sock.settimeout(0)
            self._udp = sock
        except Exception:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            self._udp = None

    def start(self):
        self.close()
        self.done = False
        self.error = None
        self.results = []
        self.scanned = 0
        self._pending = []
        self._probed = {}
        self.current_ip = None
        try:
            if not self.wlan.isconnected():
                raise WLEDError("badge is not connected to Wi-Fi")
            configuration = self.wlan.ifconfig()
            own_ip = validate_ipv4(configuration[0], False)
            own_value = _ip_to_integer(own_ip)
            mask_value = _netmask_to_integer(configuration[1])
            network_value = own_value & mask_value
            broadcast_value = network_value | ((~mask_value) & 0xFFFFFFFF)
            start = network_value + 1
            end = broadcast_value - 1
            if end < start:
                raise WLEDError("local subnet has no scannable hosts")
            if end - start + 1 > MAX_SCAN_HOSTS:
                start = (own_value & 0xFFFFFF00) + 1
                end = start + MAX_SCAN_HOSTS - 1
            self._own = own_value
            self._start = start
            self._end = end
            self._next = start
            self.total = end - start + 1 - (1 if start <= own_value <= end else 0)
            if self.saved_ip:
                try:
                    saved = validate_ipv4(self.saved_ip, False)
                except Exception:
                    saved = None
                if saved:
                    self._queue(saved, priority=True)
            self._open_udp()
            self._started = True
            return self
        except Exception as error:
            self.done = True
            self.error = str(error)[:72]
            self._started = False
            raise

    def _poll_udp(self):
        if self._udp is None:
            return
        for unused in range(4):
            try:
                packet, unused_address = self._udp.recvfrom(64)
            except Exception:
                break
            announcement = self.parse_announcement(packet)
            if announcement is not None:
                # Native WLED announcements are strong evidence. Permit one
                # longer retry even if the fast subnet sweep tried this IP.
                self._queue(announcement["ip"], priority=True)

    def _next_address(self):
        if self._pending:
            return self._pending.pop(0)
        while self._next <= self._end:
            value = self._next
            self._next += 1
            if value == self._own:
                continue
            self.scanned += 1
            ip_address = _integer_to_ip(value)
            if ip_address not in self._probed:
                return ip_address, False
        return None

    def step(self):
        if self.done:
            return False
        if not self._started:
            raise WLEDError("WLED scan has not started")
        self._poll_udp()
        candidate = self._next_address()
        if candidate is None:
            self.done = True
            self._close_udp()
            return False
        ip_address, priority = candidate
        self.current_ip = ip_address
        self._probed[ip_address] = "priority" if priority else "sweep"
        try:
            device = self.client.probe(
                ip_address,
                timeout=0.5 if priority else self.probe_timeout,
                response_timeout=1.0,
            )
        except Exception:
            device = None
        if device is not None and len(self.results) < self.max_devices:
            self._probed[ip_address] = "found"
            duplicate = False
            for existing in self.results:
                if existing.get("ip") == device.get("ip"):
                    duplicate = True
                    break
            if not duplicate:
                self.results.append(device)
        if len(self.results) >= self.max_devices:
            self.done = True
            self._close_udp()
        elif self._next > self._end and not self._pending:
            self.done = True
            self._close_udp()
        gc.collect()
        return True

    def _close_udp(self):
        if self._udp is not None:
            try:
                self._udp.close()
            except Exception:
                pass
        self._udp = None

    def close(self):
        self._close_udp()
        self._started = False
        self.done = True
        self.current_ip = None
        gc.collect()
