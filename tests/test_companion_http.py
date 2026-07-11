import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import quote

from companion.__main__ import is_loopback_host, main
from companion.database import CompanionDatabase
from companion.http_api import create_server


def raw_command():
    return {
        "format": "raw",
        "carrier_hz": 38000,
        "repeat_count": 1,
        "repeat_gap_us": 40000,
        "description": "learned",
        "pulses": [[9000, 4500], [560, 560]],
    }


class CompanionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = CompanionDatabase(
            os.path.join(self.temporary_directory.name, "api.sqlite3")
        )
        self.logging = mock.patch(
            "companion.http_api.CompanionRequestHandler.log_message",
            return_value=None,
        )
        self.logging.start()
        self.server = create_server(
            self.database, host="127.0.0.1", port=0, max_request_bytes=1024
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.logging.stop()
        self.database.close()
        self.temporary_directory.cleanup()

    def request(self, method, path, payload=None, raw=None, headers=None):
        headers = dict(headers or {})
        if payload is not None:
            raw = json.dumps(payload).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request(method, path, body=raw, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_health_device_button_and_profile_endpoints(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", body["status"])

        status, body = self.request(
            "POST",
            "/api/v1/devices",
            {"name": "Bedroom TV", "type": "tv", "transport": "ir"},
        )
        self.assertEqual(201, status)
        device_id = body["data"]["id"]
        button_name = "Power / Standby"
        status, body = self.request(
            "PUT",
            "/api/v1/devices/%s/buttons/%s"
            % (quote(device_id, safe=""), quote(button_name, safe="")),
            raw_command(),
        )
        self.assertEqual(200, status)
        self.assertEqual("raw", body["data"]["format"])

        status, body = self.request("GET", "/api/v1/profile")
        self.assertEqual(200, status)
        self.assertEqual(device_id, body["data"]["active_device"])
        self.assertIn(button_name, body["data"]["devices"][1]["buttons"])

        status, put_body = self.request("PUT", "/api/v1/profile", body["data"])
        self.assertEqual(200, status)
        self.assertTrue(put_body["data"]["stored"])
        self.assertEqual(2, put_body["data"]["device_count"])
        self.assertNotIn("devices", put_body["data"])

        status, body = self.request(
            "PATCH", "/api/v1/devices/" + device_id, {"name": "Samsung Bedroom"}
        )
        self.assertEqual("Samsung Bedroom", body["data"]["name"])
        status, body = self.request("DELETE", "/api/v1/devices/" + device_id)
        self.assertEqual(200, status)
        self.assertEqual(device_id, body["data"]["id"])

    def test_discovery_routes_save_one_and_clear(self):
        payload = {
            "name": "Nearby TV",
            "type": "tv",
            "transport": "ble",
            "transport_metadata": {"address": "AA:BB:CC:DD:EE:FF", "rssi": -60},
        }
        status, body = self.request("POST", "/api/v1/discoveries", payload)
        self.assertEqual(200, status)
        discovery_id = body["data"]["id"]
        status, body = self.request(
            "POST", "/api/v1/discoveries/save", {"ids": [discovery_id]}
        )
        self.assertEqual(200, status)
        self.assertEqual("ble", body["data"][0]["transport"])
        status, body = self.request("DELETE", "/api/v1/discoveries")
        self.assertEqual(1, body["data"]["deleted"])

    def test_validation_content_type_duplicate_json_and_request_limit(self):
        status, body = self.request(
            "POST",
            "/api/v1/devices",
            raw=b'{"name":"one","name":"two"}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_json", body["error"]["code"])

        status, body = self.request(
            "POST",
            "/api/v1/devices",
            raw=b"not json",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(415, status)
        self.assertEqual("unsupported_media_type", body["error"]["code"])

        status, body = self.request(
            "POST",
            "/api/v1/devices",
            raw=b"{" + (b" " * 1100) + b"}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(413, status)
        self.assertEqual("payload_too_large", body["error"]["code"])

        status, body = self.request(
            "GET", "/api/v1/devices/x%2Fy"
        )
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"]["code"])


class CompanionCLITests(unittest.TestCase):
    def test_loopback_detection_and_import_export_cli(self):
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertFalse(is_loopback_host("0.0.0.0"))
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "cli.sqlite3")
            export_path = os.path.join(directory, "profile.json")
            with mock.patch("sys.stdout"):
                self.assertEqual(
                    0,
                    main(
                        [
                            "--db",
                            database_path,
                            "export-profile",
                            export_path,
                        ]
                    ),
                )
            with open(export_path, "r", encoding="utf-8") as handle:
                profile = json.load(handle)
            profile["devices"][0]["name"] = "CLI Remote"
            with open(export_path, "w", encoding="utf-8") as handle:
                json.dump(profile, handle)
            with mock.patch("sys.stdout"):
                self.assertEqual(
                    0,
                    main(
                        [
                            "--db",
                            database_path,
                            "import-profile",
                            export_path,
                        ]
                    ),
                )
            database = CompanionDatabase(database_path)
            try:
                self.assertEqual("CLI Remote", database.list_devices()[0]["name"])
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
