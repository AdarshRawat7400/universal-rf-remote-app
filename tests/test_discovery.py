import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "app"))

from discovery import (
    CAPABILITY_DISCOVERED,
    STATE_COMPLETE,
    STATE_IDLE,
    STATE_PERMISSION_DENIED,
    STATE_SCANNING,
    STATE_UNAVAILABLE,
    NearbyDiscovery,
    UnavailableScanner,
    normalize_ble_result,
    normalize_wifi_result,
    parse_ble_name,
)


def advertisement(name, shortened=False):
    encoded = name.encode("utf-8")
    field_type = 0x08 if shortened else 0x09
    return bytes((len(encoded) + 1, field_type)) + encoded


class FakeScanner:
    def __init__(self, batches=(), start_error=None, poll_error=None):
        self.batches = list(batches)
        self.start_error = start_error
        self.poll_error = poll_error
        self.state = STATE_IDLE
        self.error = None
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1
        if self.start_error is not None:
            raise self.start_error
        self.state = STATE_SCANNING
        return True

    def poll(self):
        if self.poll_error is not None:
            error = self.poll_error
            self.poll_error = None
            raise error
        if self.batches:
            batch = self.batches.pop(0)
            if not self.batches:
                self.state = STATE_COMPLETE
            return batch
        self.state = STATE_COMPLETE
        return ()

    def stop(self):
        self.stops += 1
        self.state = STATE_IDLE


class DiscoveryNormalizationTests(unittest.TestCase):
    def test_normalizes_native_wifi_tuple(self):
        result = normalize_wifi_result(
            (b"Home WiFi", b"\xaa\xbb\xcc\xdd\xee\xff", 6, -47, 3, False)
        )
        self.assertEqual(
            {
                "name": "Home WiFi",
                "address": "AA:BB:CC:DD:EE:FF",
                "transport": "wifi",
                "signal": -47,
                "capability": CAPABILITY_DISCOVERED,
            },
            result,
        )

    def test_normalizes_hidden_wifi_and_dictionary(self):
        result = normalize_wifi_result(
            {"ssid": b"", "bssid": "aa-bb-cc-dd-ee-ff", "rssi": "-81"}
        )
        self.assertEqual("Hidden Wi-Fi", result["name"])
        self.assertEqual("AA:BB:CC:DD:EE:FF", result["address"])
        self.assertEqual(-81, result["signal"])

    def test_extracts_complete_name_over_shortened_name(self):
        payload = advertisement("Short", shortened=True) + advertisement("Full Name")
        self.assertEqual("Full Name", parse_ble_name(payload))

    def test_ble_name_parser_handles_truncated_payload(self):
        self.assertIsNone(parse_ble_name(b"\x08\x09bad"))
        self.assertIsNone(parse_ble_name(None))

    def test_normalizes_native_ble_tuple(self):
        result = normalize_ble_result(
            (0, b"\x01\x02\x03\x04\x05\x06", 0, -63, advertisement("Lamp"))
        )
        self.assertEqual("Lamp", result["name"])
        self.assertEqual("01:02:03:04:05:06", result["address"])
        self.assertEqual("ble", result["transport"])
        self.assertEqual(-63, result["signal"])
        self.assertEqual(CAPABILITY_DISCOVERED, result["capability"])

    def test_normalization_rejects_missing_addresses_and_short_tuples(self):
        self.assertIsNone(normalize_wifi_result((b"name",)))
        self.assertIsNone(normalize_wifi_result({"ssid": "name"}))
        self.assertIsNone(normalize_ble_result((0, b"x")))
        self.assertIsNone(normalize_ble_result({"name": "device"}))


class NearbyDiscoveryTests(unittest.TestCase):
    def test_scans_wifi_and_ble_together_and_reports_completion(self):
        wifi = FakeScanner(
            [[(b"Router", b"\x10\x11\x12\x13\x14\x15", 1, -55, 3, False)]]
        )
        ble = FakeScanner(
            [[(0, b"\x20\x21\x22\x23\x24\x25", 0, -42, advertisement("TV"))]]
        )
        discovery = NearbyDiscovery(wifi_scanner=wifi, ble_scanner=ble)

        status = discovery.start()
        self.assertEqual(STATE_SCANNING, status["wifi"]["state"])
        self.assertEqual(STATE_SCANNING, status["ble"]["state"])
        results = discovery.poll()

        self.assertEqual(["ble", "wifi"], [item["transport"] for item in results])
        self.assertEqual(STATE_COMPLETE, discovery.status["wifi"]["state"])
        self.assertEqual(STATE_COMPLETE, discovery.status["ble"]["state"])
        self.assertFalse(discovery.is_scanning)

    def test_can_scan_each_transport_independently(self):
        wifi = FakeScanner([[]])
        ble = FakeScanner([[]])
        discovery = NearbyDiscovery(wifi_scanner=wifi, ble_scanner=ble)

        discovery.start(("wifi",))
        self.assertEqual(1, wifi.starts)
        self.assertEqual(0, ble.starts)
        discovery.poll()

        discovery.start("ble")
        self.assertEqual(1, wifi.stops)
        self.assertEqual(1, ble.starts)

    def test_rejects_ir_and_subghz_as_discovery_transports(self):
        discovery = NearbyDiscovery(
            wifi_scanner=FakeScanner(), ble_scanner=FakeScanner()
        )
        for transport in ("ir", "subghz", "rf"):
            with self.assertRaisesRegex(ValueError, "unsupported discovery"):
                discovery.start((transport,))

    def test_deduplicates_and_keeps_strongest_observation(self):
        address = b"\xaa\xbb\xcc\xdd\xee\xff"
        wifi = FakeScanner(
            [
                [(b"", address, 1, -88, 3, False)],
                [(b"Office", address, 1, -41, 3, False)],
            ]
        )
        discovery = NearbyDiscovery(wifi_scanner=wifi, ble_scanner=FakeScanner())
        discovery.start(("wifi",))
        discovery.poll()
        results = discovery.poll()

        self.assertEqual(1, len(results))
        self.assertEqual("Office", results[0]["name"])
        self.assertEqual(-41, results[0]["signal"])

    def test_result_limit_retains_strongest_and_returns_copies(self):
        rows = [
            (b"Weak", b"\x00\x00\x00\x00\x00\x01", 1, -90, 3, False),
            (b"Mid", b"\x00\x00\x00\x00\x00\x02", 1, -65, 3, False),
            (b"Strong", b"\x00\x00\x00\x00\x00\x03", 1, -30, 3, False),
        ]
        discovery = NearbyDiscovery(
            max_results=2,
            wifi_scanner=FakeScanner([rows]),
            ble_scanner=FakeScanner(),
        )
        discovery.start(("wifi",))
        results = discovery.poll()

        self.assertEqual(["Strong", "Mid"], [item["name"] for item in results])
        results[0]["name"] = "changed"
        self.assertEqual("Strong", discovery.results[0]["name"])

    def test_unavailable_radio_and_permission_error_are_reported(self):
        discovery = NearbyDiscovery(
            wifi_scanner=UnavailableScanner("network module unavailable"),
            ble_scanner=FakeScanner(start_error=PermissionError("BLE denied")),
        )
        initial = discovery.status
        self.assertEqual(STATE_UNAVAILABLE, initial["wifi"]["state"])

        status = discovery.start()
        self.assertEqual(STATE_UNAVAILABLE, status["wifi"]["state"])
        self.assertEqual(STATE_PERMISSION_DENIED, status["ble"]["state"])
        self.assertIn("denied", status["ble"]["error"])
        discovery.poll()
        self.assertEqual(
            STATE_PERMISSION_DENIED, discovery.status["ble"]["state"]
        )

    def test_poll_permission_error_is_reported_without_raising(self):
        discovery = NearbyDiscovery(
            wifi_scanner=FakeScanner(poll_error=OSError(13, "permission denied")),
            ble_scanner=FakeScanner(),
        )
        discovery.start(("wifi",))
        self.assertEqual([], discovery.poll())
        self.assertEqual(STATE_PERMISSION_DENIED, discovery.status["wifi"]["state"])

    def test_stop_is_idempotent_and_clears_scanning_state(self):
        wifi = FakeScanner()
        discovery = NearbyDiscovery(wifi_scanner=wifi, ble_scanner=FakeScanner())
        discovery.start(("wifi",))
        self.assertTrue(discovery.is_scanning)
        discovery.stop()
        discovery.stop()
        self.assertFalse(discovery.is_scanning)
        self.assertEqual(STATE_IDLE, discovery.status["wifi"]["state"])

    def test_max_results_is_strictly_bounded(self):
        with self.assertRaises(ValueError):
            NearbyDiscovery(max_results=0)
        with self.assertRaises(ValueError):
            NearbyDiscovery(max_results=65)


if __name__ == "__main__":
    unittest.main()
