"""Bounded, non-blocking nearby Wi-Fi and BLE discovery.

Discovery is deliberately not device control. Results only prove that a Wi-Fi
access point or BLE advertiser was observed; they do not imply IR, sub-GHz, or
remote-control compatibility.
"""


CAPABILITY_DISCOVERED = "discovered-not-yet-controllable"
SUPPORTED_TRANSPORTS = ("wifi", "ble")

STATE_IDLE = "idle"
STATE_SCANNING = "scanning"
STATE_COMPLETE = "complete"
STATE_UNAVAILABLE = "unavailable"
STATE_PERMISSION_DENIED = "permission-denied"
STATE_ERROR = "error"

_VALID_STATES = (
    STATE_IDLE,
    STATE_SCANNING,
    STATE_COMPLETE,
    STATE_UNAVAILABLE,
    STATE_PERMISSION_DENIED,
    STATE_ERROR,
)
_MAX_RESULTS_LIMIT = 64
_MAX_NAME_LENGTH = 32
_MAX_ADDRESS_LENGTH = 32
_MAX_ERROR_LENGTH = 96
_IRQ_SCAN_RESULT = 5
_IRQ_SCAN_DONE = 6


def _safe_text(value, fallback):
    if isinstance(value, (bytes, bytearray)):
        try:
            # Non-strict decode error handlers are optional in MicroPython.
            value = value.decode("utf-8")
        except Exception:
            characters = []
            for item in value:
                item = int(item)
                characters.append(chr(item) if 32 <= item <= 126 else "?")
            value = "".join(characters)
    elif value is None:
        value = ""
    else:
        try:
            value = str(value)
        except Exception:
            value = ""
    value = value.replace("\x00", "").strip()
    if not value:
        value = fallback
    return value[:_MAX_NAME_LENGTH]


def _format_address(value):
    if isinstance(value, str):
        address = value.strip().replace("-", ":").upper()
        compact = address.replace(":", "")
        if len(compact) == 12:
            try:
                int(compact, 16)
            except ValueError:
                pass
            else:
                address = ":".join(
                    compact[index : index + 2] for index in range(0, 12, 2)
                )
        return address[:_MAX_ADDRESS_LENGTH] or None

    if isinstance(value, (bytes, bytearray)):
        values = value
    elif isinstance(value, (tuple, list)):
        values = value
    else:
        return None

    if not values:
        return None
    try:
        return ":".join("%02X" % (int(part) & 0xFF) for part in values)[
            :_MAX_ADDRESS_LENGTH
        ]
    except (TypeError, ValueError):
        return None


def _signal(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return -127


def _record(name, address, transport, signal):
    address = _format_address(address)
    if address is None:
        return None
    fallback = "Hidden Wi-Fi" if transport == "wifi" else "Unnamed BLE"
    return {
        "name": _safe_text(name, fallback),
        "address": address,
        "transport": transport,
        "signal": _signal(signal),
        "capability": CAPABILITY_DISCOVERED,
    }


def normalize_wifi_result(result):
    """Normalize a MicroPython ``WLAN.scan()`` entry.

    Native scan entries are ``(ssid, bssid, channel, RSSI, security, hidden)``.
    Dictionaries with equivalent ``ssid``/``bssid``/``rssi`` fields are also
    accepted so adapters and simulators can remain desktop-safe.
    """

    try:
        if isinstance(result, dict):
            name = result.get("name", result.get("ssid"))
            address = result.get("address", result.get("bssid"))
            signal = result.get("signal", result.get("rssi"))
        else:
            if len(result) < 4:
                return None
            name, address, signal = result[0], result[1], result[3]
    except (IndexError, TypeError):
        return None
    return _record(name, address, "wifi", signal)


def parse_ble_name(advertising_data):
    """Extract a complete or shortened local name from BLE AD structures."""

    try:
        data = bytes(advertising_data)
    except (TypeError, ValueError):
        return None

    shortened = None
    index = 0
    while index < len(data):
        field_length = data[index]
        if field_length == 0:
            break
        end = index + field_length + 1
        if end > len(data) or field_length < 1:
            break
        field_type = data[index + 1]
        if field_type in (0x08, 0x09):
            name = _safe_text(data[index + 2 : end], "")
            if name:
                if field_type == 0x09:
                    return name
                shortened = name
        index = end
    return shortened


def normalize_ble_result(result):
    """Normalize a MicroPython BLE ``_IRQ_SCAN_RESULT`` tuple.

    Native tuples are ``(addr_type, addr, adv_type, RSSI, adv_data)``.
    Dictionaries may provide ``name`` and/or ``advertising_data`` directly.
    """

    try:
        if isinstance(result, dict):
            address = result.get("address", result.get("addr"))
            signal = result.get("signal", result.get("rssi"))
            name = result.get("name")
            if not name:
                name = parse_ble_name(
                    result.get("advertising_data", result.get("adv_data", b""))
                )
        else:
            if len(result) < 5:
                return None
            address = result[1]
            signal = result[3]
            name = parse_ble_name(result[4])
    except (IndexError, TypeError):
        return None
    return _record(name, address, "ble", signal)


def _exception_state(error):
    name = error.__class__.__name__.lower()
    errno = getattr(error, "errno", None)
    message = str(error).lower()
    if (
        name == "permissionerror"
        or errno in (1, 13)
        or "permission" in message
        or "denied" in message
    ):
        return STATE_PERMISSION_DENIED
    return STATE_ERROR


def _error_text(error):
    try:
        message = str(error).strip()
    except Exception:
        message = ""
    return (message or error.__class__.__name__)[:_MAX_ERROR_LENGTH]


class UnavailableScanner:
    """Scanner placeholder used when a firmware radio API is missing."""

    def __init__(self, reason):
        self.state = STATE_UNAVAILABLE
        self.error = str(reason)[:_MAX_ERROR_LENGTH]

    def start(self):
        return False

    def poll(self):
        return ()

    def stop(self):
        return None


class _ThreadedWiFiScanner:
    """Run the synchronous WLAN scan away from the MonaOS UI loop."""

    def __init__(self, network_module, thread_module, raw_limit):
        self._network = network_module
        self._thread = thread_module
        self._raw_limit = raw_limit
        self._generation = 0
        self._pending = ()
        self._worker_running = False
        self.state = STATE_IDLE
        self.error = None

    def _worker(self, generation):
        try:
            try:
                wlan = self._network.WLAN(self._network.STA_IF)
                wlan.active(True)
                scanned = wlan.scan()
                if scanned is None:
                    scanned = ()
                # The native driver owns the original scan list. Only a bounded
                # slice is retained by the discovery service.
                pending = tuple(scanned[: self._raw_limit])
            except Exception as error:
                if generation == self._generation:
                    self.error = _error_text(error)
                    self.state = _exception_state(error)
                return

            if generation == self._generation:
                self._pending = pending
                self.error = None
                self.state = STATE_COMPLETE
        finally:
            self._worker_running = False

    def start(self):
        if self.state == STATE_SCANNING:
            return False
        if self._worker_running:
            self.error = "previous Wi-Fi scan is still stopping"
            self.state = STATE_ERROR
            return False
        self._generation += 1
        generation = self._generation
        self._pending = ()
        self.error = None
        self.state = STATE_SCANNING
        self._worker_running = True
        try:
            self._thread.start_new_thread(self._worker, (generation,))
        except Exception as error:
            self._worker_running = False
            self.error = _error_text(error)
            self.state = _exception_state(error)
            return False
        return True

    def poll(self):
        pending = self._pending
        self._pending = ()
        return pending

    def stop(self):
        # MicroPython does not expose cancellation for WLAN.scan(). Invalidating
        # this generation prevents a late worker result from entering the UI.
        self._generation += 1
        self._pending = ()
        self.error = None
        self.state = STATE_IDLE


class _BLEScanner:
    """IRQ-driven BLE advertising scan using MicroPython ``bluetooth.BLE``."""

    def __init__(
        self,
        bluetooth_module,
        raw_limit,
        duration_ms=5000,
        interval_us=30000,
        window_us=30000,
    ):
        self._bluetooth = bluetooth_module
        self._raw_limit = raw_limit
        self._duration_ms = duration_ms
        self._interval_us = interval_us
        self._window_us = window_us
        self._ble = None
        self._pending = {}
        self.state = STATE_IDLE
        self.error = None

    def _on_irq(self, event, data):
        if event == _IRQ_SCAN_RESULT and self.state == STATE_SCANNING:
            try:
                address_type, address, advertisement_type, rssi, payload = data
                address = bytes(address)
                raw = (
                    address_type,
                    address,
                    advertisement_type,
                    int(rssi),
                    bytes(payload),
                )
            except (TypeError, ValueError):
                return

            key = (address_type, address)
            previous = self._pending.get(key)
            if previous is not None:
                if raw[3] > previous[3]:
                    self._pending[key] = raw
                return
            if len(self._pending) < self._raw_limit:
                self._pending[key] = raw
                return

            weakest_key = None
            weakest_signal = None
            for candidate_key, candidate in self._pending.items():
                if weakest_signal is None or candidate[3] < weakest_signal:
                    weakest_key = candidate_key
                    weakest_signal = candidate[3]
            if weakest_key is not None and raw[3] > weakest_signal:
                del self._pending[weakest_key]
                self._pending[key] = raw
        elif event == _IRQ_SCAN_DONE and self.state == STATE_SCANNING:
            self.state = STATE_COMPLETE

    def start(self):
        if self.state == STATE_SCANNING:
            return False
        self._pending = {}
        self.error = None
        self.state = STATE_SCANNING
        try:
            if self._ble is None:
                self._ble = self._bluetooth.BLE()
            self._ble.active(True)
            self._ble.irq(self._on_irq)
            self._ble.gap_scan(
                self._duration_ms,
                self._interval_us,
                self._window_us,
                True,
            )
        except Exception as error:
            self.error = _error_text(error)
            self.state = _exception_state(error)
            return False
        return True

    def poll(self):
        if not self._pending:
            return ()
        pending = tuple(self._pending.values())
        self._pending = {}
        return pending

    def stop(self):
        if self._ble is not None:
            if self.state == STATE_SCANNING:
                try:
                    self._ble.gap_scan(None)
                except Exception:
                    pass
            try:
                self._ble.irq(None)
            except Exception:
                pass
            try:
                self._ble.active(False)
            except Exception:
                pass
        self._pending = {}
        self.error = None
        self.state = STATE_IDLE


def _create_wifi_scanner(raw_limit):
    try:
        import network
    except ImportError:
        return UnavailableScanner("network module unavailable")
    try:
        import _thread
    except ImportError:
        return UnavailableScanner(
            "background thread unavailable; blocking Wi-Fi scan disabled"
        )
    return _ThreadedWiFiScanner(network, _thread, raw_limit)


def _create_ble_scanner(raw_limit):
    try:
        import bluetooth
    except ImportError:
        return UnavailableScanner("bluetooth module unavailable")
    return _BLEScanner(bluetooth, raw_limit)


class NearbyDiscovery:
    """Cooperative Wi-Fi/BLE discovery service for a MonaOS update loop."""

    def __init__(self, max_results=24, wifi_scanner=None, ble_scanner=None):
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            raise ValueError("max_results must be an integer")
        if max_results < 1 or max_results > _MAX_RESULTS_LIMIT:
            raise ValueError("max_results must be between 1 and 64")

        self.max_results = max_results
        raw_limit = max_results * 2
        self._scanners = {
            "wifi": wifi_scanner or _create_wifi_scanner(raw_limit),
            "ble": ble_scanner or _create_ble_scanner(raw_limit),
        }
        self._selected = ()
        self._found = {}
        self._status = {
            transport: {"state": STATE_IDLE, "error": None}
            for transport in SUPPORTED_TRANSPORTS
        }
        for transport in SUPPORTED_TRANSPORTS:
            self._sync_status(transport)

    def _sync_status(self, transport):
        scanner = self._scanners[transport]
        state = getattr(scanner, "state", self._status[transport]["state"])
        if state not in _VALID_STATES:
            state = STATE_ERROR
        error = getattr(scanner, "error", None)
        self._status[transport] = {
            "state": state,
            "error": None if error is None else str(error)[:_MAX_ERROR_LENGTH],
        }

    def _record_error(self, transport, error):
        self._status[transport] = {
            "state": _exception_state(error),
            "error": _error_text(error),
        }

    def start(self, transports=SUPPORTED_TRANSPORTS, clear=True):
        """Start one or both supported scans and return the status snapshot."""

        if isinstance(transports, str):
            transports = (transports,)
        try:
            requested = tuple(transports)
        except TypeError:
            raise ValueError("transports must contain wifi and/or ble")
        if not requested:
            raise ValueError("at least one transport is required")
        for transport in requested:
            if transport not in SUPPORTED_TRANSPORTS:
                raise ValueError(
                    "unsupported discovery transport: %s; use wifi and/or ble"
                    % transport
                )
        requested = tuple(
            transport
            for index, transport in enumerate(requested)
            if transport not in requested[:index]
        )

        self.stop()
        self._selected = requested
        if clear:
            self._found = {}
        for transport in requested:
            scanner = self._scanners[transport]
            try:
                scanner.start()
            except Exception as error:
                self._record_error(transport, error)
            else:
                self._sync_status(transport)
        return self.status

    def _merge(self, record):
        key = (record["transport"], record["address"])
        previous = self._found.get(key)
        if previous is not None:
            if record["signal"] > previous["signal"]:
                previous["signal"] = record["signal"]
            if previous["name"] in ("Hidden Wi-Fi", "Unnamed BLE") and record[
                "name"
            ] not in ("Hidden Wi-Fi", "Unnamed BLE"):
                previous["name"] = record["name"]
            return

        if len(self._found) >= self.max_results:
            weakest_key = None
            weakest_signal = None
            for candidate_key, candidate in self._found.items():
                if weakest_signal is None or candidate["signal"] < weakest_signal:
                    weakest_key = candidate_key
                    weakest_signal = candidate["signal"]
            if weakest_key is None or record["signal"] <= weakest_signal:
                return
            del self._found[weakest_key]
        self._found[key] = record

    def poll(self):
        """Collect available results without waiting and return a snapshot."""

        for transport in self._selected:
            scanner = self._scanners[transport]
            # Failed, denied, unavailable, and explicitly idle scanners have
            # no valid scan to poll. This also preserves their diagnostic
            # state until the caller starts another scan or stops the service.
            if self._status[transport]["state"] not in (
                STATE_SCANNING,
                STATE_COMPLETE,
            ):
                continue
            try:
                available = scanner.poll()
            except Exception as error:
                self._record_error(transport, error)
                continue
            if available:
                normalizer = (
                    normalize_wifi_result
                    if transport == "wifi"
                    else normalize_ble_result
                )
                for raw_result in available:
                    record = normalizer(raw_result)
                    if record is not None:
                        self._merge(record)
            self._sync_status(transport)
        return self.results

    def stop(self):
        """Stop active scans; safe to call repeatedly."""

        for transport in self._selected:
            try:
                self._scanners[transport].stop()
            except Exception as error:
                self._record_error(transport, error)
            else:
                self._sync_status(transport)
        self._selected = ()

    @property
    def results(self):
        ordered = sorted(
            self._found.values(), key=lambda item: item["signal"], reverse=True
        )
        return [dict(item) for item in ordered]

    @property
    def status(self):
        return {
            transport: dict(self._status[transport])
            for transport in SUPPORTED_TRANSPORTS
        }

    @property
    def is_scanning(self):
        return any(
            self._status[transport]["state"] == STATE_SCANNING
            for transport in self._selected
        )
