"""Small, non-blocking Wi-Fi runtime wrapper for the Badge Settings app."""

import gc

try:
    import time
except ImportError:
    time = None

from badge_settings_model import normalize_scan_results


class WiFiRuntimeError(RuntimeError):
    pass


def _ticks_elapsed(now_ms, started_at):
    ticks_diff = None if time is None else getattr(time, "ticks_diff", None)
    if ticks_diff is not None:
        return max(0, int(ticks_diff(int(now_ms), int(started_at))))
    return max(0, int(now_ms) - int(started_at))


class WiFiManager:
    """Own the station interface without retaining raw scan responses."""

    def __init__(self, network_module, timeout_ms=20_000, max_results=16):
        self.network = network_module
        self.timeout_ms = int(timeout_ms)
        self.max_results = int(max_results)
        self.wlan = None
        self.connecting = False
        self.started_at = 0
        self.target_ssid = None

    def ensure_interface(self):
        if self.wlan is None:
            self.wlan = self.network.WLAN(self.network.STA_IF)
        self.wlan.active(True)
        return self.wlan

    def scan(self):
        wlan = self.ensure_interface()
        try:
            raw_results = wlan.scan()
        except Exception:
            # Some drivers include call arguments in exception text. Keep the
            # UI useful without forwarding untrusted low-level details.
            raise WiFiRuntimeError("scan failed")
        try:
            return normalize_scan_results(raw_results, self.max_results)
        finally:
            raw_results = None
            gc.collect()

    def current(self):
        wlan = self.ensure_interface()
        connected = bool(wlan.isconnected())
        ssid = ""
        ip_address = "0.0.0.0"
        if connected:
            try:
                ssid = wlan.config("ssid")
                if isinstance(ssid, (bytes, bytearray)):
                    ssid = ssid.decode("utf-8", "ignore")
                if not isinstance(ssid, str):
                    ssid = str(ssid)
            except Exception:
                ssid = ""
            try:
                ip_address = str(wlan.ifconfig()[0])
            except Exception:
                ip_address = "0.0.0.0"
        return {
            "connected": connected and ip_address not in ("", "0.0.0.0"),
            "ssid": ssid,
            "ip": ip_address,
        }

    def start_connect(self, ssid, password, now_ms):
        wlan = self.ensure_interface()
        try:
            wlan.disconnect()
        except Exception:
            pass
        gc.collect()
        try:
            if password:
                wlan.connect(ssid, password)
            else:
                try:
                    wlan.connect(ssid)
                except TypeError:
                    wlan.connect(ssid, "")
        except Exception:
            self.connecting = False
            self.target_ssid = None
            # Never surface a driver exception here: it may contain the SSID
            # or password passed to WLAN.connect().
            raise WiFiRuntimeError("connection could not start")
        self.connecting = True
        self.started_at = int(now_ms)
        self.target_ssid = ssid

    def _failure_message(self, status):
        mappings = (
            ("STAT_WRONG_PASSWORD", "Password rejected"),
            ("STAT_NO_AP_FOUND", "Network not found"),
            ("STAT_CONNECT_FAIL", "Could not join network"),
        )
        for constant_name, message in mappings:
            if status == getattr(self.network, constant_name, object()):
                return message
        return None

    def poll(self, now_ms):
        if not self.connecting:
            current = self.current()
            return {
                "state": "connected" if current["connected"] else "idle",
                "message": "Connected" if current["connected"] else "Idle",
                "ip": current["ip"],
            }

        wlan = self.ensure_interface()
        if wlan.isconnected():
            current = self.current()
            if current["connected"]:
                self.connecting = False
                self.target_ssid = None
                return {"state": "connected", "message": "Connected", "ip": current["ip"]}

        try:
            status = wlan.status()
        except Exception:
            status = None
        failure = self._failure_message(status)
        if failure is not None:
            self.cancel_attempt()
            return {"state": "failed", "message": failure, "ip": "0.0.0.0"}

        elapsed = _ticks_elapsed(now_ms, self.started_at)
        if elapsed >= self.timeout_ms:
            self.cancel_attempt()
            return {"state": "failed", "message": "Connection timed out", "ip": "0.0.0.0"}

        return {
            "state": "connecting",
            "message": "Connecting %d/%ds" % (elapsed // 1000, self.timeout_ms // 1000),
            "ip": "0.0.0.0",
        }

    def cancel_attempt(self):
        wlan = self.wlan
        self.connecting = False
        self.started_at = 0
        self.target_ssid = None
        if wlan is not None:
            try:
                wlan.disconnect()
            except Exception:
                pass

    def disconnect(self):
        self.cancel_attempt()
        return self.current()

    def close(self, preserve_connection=True):
        if not preserve_connection and self.connecting:
            self.cancel_attempt()
        self.wlan = None
        gc.collect()
