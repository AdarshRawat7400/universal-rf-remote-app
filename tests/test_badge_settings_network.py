import os
import sys
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
SETTINGS = os.path.join(ROOT, "badge_settings")
sys.path.insert(0, SETTINGS)

import badge_settings_network as network_impl
from badge_settings_network import WiFiManager, WiFiRuntimeError


class FakeWLAN:
    def __init__(self):
        self.active_value = False
        self.connected = False
        self.status_value = 1
        self.ip = "0.0.0.0"
        self.ssid = ""
        self.scan_results = []
        self.connect_calls = []
        self.disconnect_calls = 0

    def active(self, value=None):
        if value is not None:
            self.active_value = bool(value)
        return self.active_value

    def scan(self):
        if isinstance(self.scan_results, Exception):
            raise self.scan_results
        return self.scan_results

    def isconnected(self):
        return self.connected

    def connect(self, *args):
        self.connect_calls.append(args)
        self.ssid = args[0]

    def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False
        self.ip = "0.0.0.0"

    def status(self):
        return self.status_value

    def ifconfig(self):
        return (self.ip, "255.255.255.0", "192.168.1.1", "192.168.1.1")

    def config(self, name):
        if name == "ssid":
            return self.ssid
        raise ValueError(name)


class FakeNetwork:
    STA_IF = 0
    STAT_IDLE = 0
    STAT_CONNECTING = 1
    STAT_WRONG_PASSWORD = -3
    STAT_NO_AP_FOUND = -2
    STAT_CONNECT_FAIL = -1
    STAT_GOT_IP = 3

    def __init__(self):
        self.interface = FakeWLAN()

    def WLAN(self, interface_id):
        if interface_id != self.STA_IF:
            raise ValueError(interface_id)
        return self.interface


class WiFiManagerTests(unittest.TestCase):
    def setUp(self):
        self.network = FakeNetwork()
        self.manager = WiFiManager(self.network, timeout_ms=20_000, max_results=2)

    def test_scan_activates_station_and_returns_bounded_normalized_results(self):
        self.network.interface.scan_results = [
            (b"Weak", b"1", 1, -80, 3, 0),
            (b"Strong", b"2", 6, -35, 0, 0),
            (b"Middle", b"3", 11, -55, 3, 0),
        ]

        results = self.manager.scan()

        self.assertTrue(self.network.interface.active_value)
        self.assertEqual(["Strong", "Middle"], [item["ssid"] for item in results])

    def test_scan_failure_is_sanitized(self):
        self.network.interface.scan_results = OSError("radio busy")
        with self.assertRaisesRegex(WiFiRuntimeError, "scan failed"):
            self.manager.scan()

    def test_open_and_secured_connections_use_correct_signature(self):
        self.manager.start_connect("Cafe", "", 100)
        self.assertEqual(("Cafe",), self.network.interface.connect_calls[-1])
        self.manager.start_connect("Home", "password", 200)
        self.assertEqual(("Home", "password"), self.network.interface.connect_calls[-1])

    def test_connect_driver_error_never_echoes_credentials(self):
        secret = "do-not-display-this-password"

        def failed_connect(*args):
            raise OSError("driver args: %r" % (args,))

        self.network.interface.connect = failed_connect
        with self.assertRaises(WiFiRuntimeError) as caught:
            self.manager.start_connect("PrivateNetwork", secret, 100)

        message = str(caught.exception)
        self.assertEqual("connection could not start", message)
        self.assertNotIn(secret, message)

    def test_poll_reports_dhcp_success(self):
        self.manager.start_connect("Home", "password", 100)
        self.network.interface.connected = True
        self.network.interface.ip = "192.168.1.42"

        result = self.manager.poll(500)

        self.assertEqual("connected", result["state"])
        self.assertEqual("192.168.1.42", result["ip"])
        self.assertFalse(self.manager.connecting)

    def test_known_failures_are_specific_and_cancel_driver_retry(self):
        cases = (
            (self.network.STAT_WRONG_PASSWORD, "Password rejected"),
            (self.network.STAT_NO_AP_FOUND, "Network not found"),
            (self.network.STAT_CONNECT_FAIL, "Could not join network"),
        )
        for status, message in cases:
            with self.subTest(status=status):
                self.manager.start_connect("Home", "password", 0)
                self.network.interface.status_value = status
                result = self.manager.poll(50)
                self.assertEqual("failed", result["state"])
                self.assertEqual(message, result["message"])
                self.assertFalse(self.manager.connecting)

    def test_timeout_disconnects_to_stop_indefinite_retries(self):
        self.manager.start_connect("Home", "password", 1_000)
        result = self.manager.poll(21_000)
        self.assertEqual("failed", result["state"])
        self.assertEqual("Connection timed out", result["message"])
        self.assertGreaterEqual(self.network.interface.disconnect_calls, 2)

    def test_timeout_uses_wrap_safe_tick_difference(self):
        original_time = network_impl.time
        network_impl.time = types.SimpleNamespace(
            ticks_diff=lambda newer, older: (
                newer - older if newer >= older else newer + 1024 - older
            )
        )
        try:
            manager = WiFiManager(self.network, timeout_ms=100)
            manager.start_connect("Home", "password", 1000)
            self.assertEqual("connecting", manager.poll(50)["state"])
            self.assertEqual("failed", manager.poll(76)["state"])
        finally:
            network_impl.time = original_time

    def test_current_requires_nonzero_ip(self):
        self.network.interface.connected = True
        self.network.interface.ssid = b"Home"
        self.assertFalse(self.manager.current()["connected"])
        self.network.interface.ip = "10.0.0.9"
        current = self.manager.current()
        self.assertTrue(current["connected"])
        self.assertEqual("Home", current["ssid"])


if __name__ == "__main__":
    unittest.main()
