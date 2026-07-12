import json
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
SETTINGS = os.path.join(ROOT, "badge_settings")
if SETTINGS not in sys.path:
    sys.path.insert(0, SETTINGS)

from badge_settings_wled import (
    MAX_INFO_BYTES,
    WLEDClient,
    WLEDError,
    WLEDScanner,
)


def http_json(payload, status=200):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return (
        ("HTTP/1.0 %d OK\r\nContent-Type: application/json\r\n" % status).encode()
        + ("Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body)).encode()
        + body
    )


class FakeTCPSocket:
    def __init__(self, response=None, failure=None):
        self.response = bytearray(response or b"")
        self.failure = failure
        self.sent = bytearray()
        self.timeout = None
        self.timeout_calls = []
        self.address = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value
        self.timeout_calls.append(value)

    def connect(self, address):
        self.address = address
        if self.failure is not None:
            raise self.failure

    def send(self, payload):
        self.sent.extend(payload)
        return len(payload)

    def recv(self, maximum):
        if not self.response:
            return b""
        chunk = bytes(self.response[:maximum])
        del self.response[:maximum]
        return chunk

    def close(self):
        self.closed = True


class FakeSocketModule:
    AF_INET = 2
    SOCK_STREAM = 1
    SOCK_DGRAM = 2
    SOL_SOCKET = 1
    SO_REUSEADDR = 2

    def __init__(self, responses):
        self.responses = list(responses)
        self.sockets = []

    def socket(self, family=None, socket_type=None):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            sock = FakeTCPSocket(failure=response)
        else:
            sock = FakeTCPSocket(response=response)
        self.sockets.append(sock)
        return sock


def request_payload(sock):
    raw = bytes(sock.sent)
    header_end = raw.find(b"\r\n\r\n")
    return json.loads(raw[header_end + 4 :].decode("utf-8"))


class WLEDClientTests(unittest.TestCase):
    def test_probe_state_and_exact_control_payloads(self):
        responses = [
            http_json(
                {
                    "ver": "0.15.3",
                    "name": "Desk WLED",
                    "fxcount": 120,
                    "leds": {"count": 30},
                }
            ),
            http_json({"on": True, "bri": 128, "seg": [{"fx": 8}]}),
            http_json({"success": True}),
            http_json({"success": True}),
            http_json({"success": True}),
            http_json({"success": True}),
        ]
        sockets = FakeSocketModule(responses)
        client = WLEDClient(sockets, json, timeout=0.5)

        info = client.probe("192.168.1.44")
        toggled = client.toggle_power("192.168.1.44")
        client.set_color("192.168.1.44", 12, 34, 56)
        client.set_effect("192.168.1.44", 42)
        client.set_brightness("192.168.1.44", 200)

        self.assertEqual("Desk WLED", info["name"])
        self.assertEqual("0.15.3", info["version"])
        self.assertEqual({"on": False}, toggled)
        self.assertEqual({"on": False}, request_payload(sockets.sockets[2]))
        self.assertEqual(
            {
                "on": True,
                "seg": [{"id": 0, "fx": 0, "col": [[12, 34, 56]]}],
            },
            request_payload(sockets.sockets[3]),
        )
        self.assertEqual(
            {"on": True, "seg": [{"id": 0, "fx": 42}]},
            request_payload(sockets.sockets[4]),
        )
        self.assertEqual(
            {"on": True, "bri": 200}, request_payload(sockets.sockets[5])
        )
        self.assertTrue(all(sock.closed for sock in sockets.sockets))

    def test_effects_keep_controller_ids_and_filter_reserved_entries(self):
        sockets = FakeSocketModule(
            [http_json(["Solid", "RSVD", "-", "Aurora", "", None])]
        )
        client = WLEDClient(sockets, json)

        effects = client.get_effects("192.168.1.44")

        self.assertEqual([(0, "Solid"), (3, "Aurora")], effects)

    def test_current_large_effect_catalog_keeps_late_ids(self):
        names = ["Effect %d" % index for index in range(223)]
        client = WLEDClient(FakeSocketModule([http_json(names)]), json)

        effects = client.get_effects("192.168.1.44")

        self.assertEqual(223, len(effects))
        self.assertEqual((222, "Effect 222"), effects[-1])

    def test_probe_uses_short_connect_but_longer_response_timeout(self):
        sockets = FakeSocketModule(
            [
                http_json(
                    {
                        "ver": "0.15.3",
                        "name": "Slow WLED",
                        "fxcount": 120,
                        "leds": {},
                    }
                )
            ]
        )
        client = WLEDClient(sockets, json)

        client.probe("192.168.1.44", timeout=0.12, response_timeout=1.0)

        self.assertEqual([0.12, 1.0], sockets.sockets[0].timeout_calls)

    def test_chunked_response_is_decoded_with_bound(self):
        body = json.dumps(
            {
                "ver": "0.15.3",
                "name": "Chunked WLED",
                "fxcount": 3,
                "leds": {},
            },
            separators=(",", ":"),
        ).encode()
        chunked = b"%X\r\n" % len(body) + body + b"\r\n0\r\n\r\n"
        response = (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n"
            + chunked
        )
        client = WLEDClient(FakeSocketModule([response]), json)

        self.assertEqual("Chunked WLED", client.probe("192.168.1.5")["name"])

    def test_malformed_spoof_oversize_and_driver_errors_are_sanitized(self):
        cases = (
            http_json({"name": "Not WLED", "ver": "1"}),
            b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n"
            % (MAX_INFO_BYTES + 1),
            b"BROKEN STATUS\r\nContent-Length: 2\r\n\r\n{}",
            OSError("driver leaked super-secret argument"),
        )
        for response in cases:
            with self.subTest(response=type(response).__name__):
                client = WLEDClient(FakeSocketModule([response]), json)
                with self.assertRaises(WLEDError) as caught:
                    client.probe("192.168.1.8")
                self.assertNotIn("super-secret", str(caught.exception))

    def test_control_bounds_reject_before_opening_socket(self):
        sockets = FakeSocketModule([])
        client = WLEDClient(sockets, json)
        for call in (
            lambda: client.set_color("192.168.1.2", -1, 0, 0),
            lambda: client.set_effect("192.168.1.2", 999),
            lambda: client.set_brightness("192.168.1.2", 0),
            lambda: client.set_power("192.168.1.2", 1),
        ):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual([], sockets.sockets)

    def test_empty_success_response_is_not_reported_as_applied(self):
        client = WLEDClient(FakeSocketModule([http_json({})]), json)

        with self.assertRaisesRegex(WLEDError, "rejected"):
            client.set_power("192.168.1.2", True)


class FakeUDPSocket:
    def __init__(self, packets=None):
        self.packets = list(packets or [])
        self.closed = False
        self.bound = None

    def setsockopt(self, *args):
        return None

    def bind(self, address):
        self.bound = address

    def setblocking(self, value):
        return None

    def recvfrom(self, maximum):
        if not self.packets:
            raise OSError("would block")
        packet = self.packets.pop(0)
        if packet is None:
            raise OSError("not yet")
        return packet, ("192.168.1.1", 65506)

    def close(self):
        self.closed = True


class FakeUDPModule:
    AF_INET = 2
    SOCK_DGRAM = 2
    SOL_SOCKET = 1
    SO_REUSEADDR = 2

    def __init__(self, packets=None):
        self.udp = FakeUDPSocket(packets)

    def socket(self, family=None, socket_type=None):
        return self.udp


class FakeScannerClient:
    def __init__(self, devices=None, packets=None, fail_once=None):
        self.devices = devices or {}
        self.calls = []
        self.socket_module = FakeUDPModule(packets)
        self.fail_once = set(fail_once or ())
        self.attempts = {}

    def _socket_module(self):
        return self.socket_module

    def probe(self, ip_address, timeout=None, response_timeout=None):
        self.calls.append((ip_address, timeout, response_timeout))
        self.attempts[ip_address] = self.attempts.get(ip_address, 0) + 1
        if ip_address in self.fail_once and self.attempts[ip_address] == 1:
            raise WLEDError("first attempt missed")
        device = self.devices.get(ip_address)
        if device is None:
            raise WLEDError("not found")
        return dict(device)


class FakeScannerWLAN:
    def __init__(self, connected=True, ip="192.168.1.2", mask="255.255.255.248"):
        self.connected = connected
        self.ip = ip
        self.mask = mask

    def isconnected(self):
        return self.connected

    def ifconfig(self):
        return (self.ip, self.mask, "192.168.1.1", "192.168.1.1")


def node_packet(ip_address, name="Node WLED"):
    packet = bytearray(44)
    packet[0] = 255
    packet[1] = 1
    for index, part in enumerate(ip_address.split(".")):
        packet[2 + index] = int(part)
    encoded = name.encode("utf-8")[:32]
    packet[6 : 6 + len(encoded)] = encoded
    return bytes(packet)


class WLEDScannerTests(unittest.TestCase):
    def test_announcement_parser_rejects_malformed_and_preserves_bounded_identity(self):
        self.assertIsNone(WLEDScanner.parse_announcement(b"short"))
        malformed = bytearray(node_packet("192.168.1.5"))
        malformed[0] = 0
        self.assertIsNone(WLEDScanner.parse_announcement(malformed))
        parsed = WLEDScanner.parse_announcement(
            node_packet("192.168.1.5", "Living Room WLED")
        )
        self.assertEqual("192.168.1.5", parsed["ip"])
        self.assertEqual("Living Room WLED", parsed["name"])

    def test_incremental_scan_prioritizes_saved_and_udp_then_bounds_subnet(self):
        devices = {
            "192.168.1.5": {
                "ip": "192.168.1.5",
                "name": "Saved",
                "version": "1",
            },
            "192.168.1.6": {
                "ip": "192.168.1.6",
                "name": "Broadcast",
                "version": "1",
            },
            "192.168.1.3": {
                "ip": "192.168.1.3",
                "name": "Scanned",
                "version": "1",
            },
        }
        packets = [node_packet("10.0.0.8", "Outside"), node_packet("192.168.1.6")]
        client = FakeScannerClient(devices, packets)
        scanner = WLEDScanner(
            client,
            FakeScannerWLAN(),
            saved_ip="192.168.1.5",
            probe_timeout=0.1,
        ).start()

        while not scanner.done:
            scanner.step()

        self.assertEqual("192.168.1.5", client.calls[0][0])
        self.assertEqual("192.168.1.6", client.calls[1][0])
        self.assertNotIn("10.0.0.8", [call[0] for call in client.calls])
        self.assertNotIn("192.168.1.2", [call[0] for call in client.calls])
        self.assertEqual(
            ["192.168.1.5", "192.168.1.6", "192.168.1.3"],
            [item["ip"] for item in scanner.results],
        )
        self.assertTrue(client.socket_module.udp.closed)

    def test_udp_announcement_retries_address_missed_by_fast_sweep(self):
        device = {
            "ip": "192.168.1.1",
            "name": "Retry WLED",
            "version": "1",
        }
        client = FakeScannerClient(
            {"192.168.1.1": device},
            packets=[None, node_packet("192.168.1.1")],
            fail_once={"192.168.1.1"},
        )
        scanner = WLEDScanner(client, FakeScannerWLAN(), probe_timeout=0.1).start()

        scanner.step()
        self.assertEqual([], scanner.results)
        scanner.step()

        self.assertEqual([device], scanner.results)
        self.assertEqual(
            [
                ("192.168.1.1", 0.1, 1.0),
                ("192.168.1.1", 0.5, 1.0),
            ],
            client.calls[:2],
        )

    def test_large_subnet_is_capped_to_local_slash_24(self):
        client = FakeScannerClient()
        scanner = WLEDScanner(
            client,
            FakeScannerWLAN(ip="10.20.30.40", mask="255.255.0.0"),
        ).start()

        self.assertEqual(253, scanner.total)
        self.assertEqual(("10.20.30.1", False), scanner._next_address())
        scanner.close()

    def test_disconnected_and_invalid_mask_fail_without_scanning(self):
        for wlan in (
            FakeScannerWLAN(connected=False),
            FakeScannerWLAN(mask="255.0.255.0"),
        ):
            with self.subTest(mask=wlan.mask, connected=wlan.connected):
                scanner = WLEDScanner(FakeScannerClient(), wlan)
                with self.assertRaises(Exception):
                    scanner.start()
                self.assertTrue(scanner.done)


if __name__ == "__main__":
    unittest.main()
