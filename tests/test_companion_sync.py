import os
import sys
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "app"))

import storage
from companion_sync import (
    CompanionSync,
    CompanionSyncError,
    load_configured_url,
    validate_base_url,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.closed = False

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class FakeRequests:
    def __init__(self, profile):
        self.profile = profile
        self.last_put = None
        self.responses = []

    def put(self, url, data=None, headers=None):
        self.last_put = (url, data, headers)
        response = FakeResponse(
            {
                "data": {
                    "stored": True,
                    "schema": self.profile["schema"],
                    "active_device": self.profile["active_device"],
                    "device_count": len(self.profile["devices"]),
                }
            }
        )
        self.responses.append(response)
        return response

    def get(self, url):
        response = FakeResponse({"data": self.profile})
        self.responses.append(response)
        return response


class CompanionSyncTests(unittest.TestCase):
    def test_loads_url_from_secrets_setting(self):
        settings = types.SimpleNamespace(
            IR_COMPANION_URL=" http://192.168.1.10:8765/ "
        )
        self.assertEqual(
            "http://192.168.1.10:8765", load_configured_url(settings)
        )
        self.assertIsNone(load_configured_url(types.SimpleNamespace()))

    def test_url_validation_rejects_missing_or_unsafe_values(self):
        for value in (None, "", "192.168.1.10:8765", "http://bad host"):
            with self.subTest(value=value):
                with self.assertRaises(CompanionSyncError):
                    validate_base_url(value)

    def test_push_and_pull_round_trip_current_profile_schema(self):
        profile = storage._default_data()
        fake = FakeRequests(profile)
        client = CompanionSync("http://192.168.1.10:8765", fake)

        pushed = client.push_profile(profile)
        pulled = client.pull_profile()

        self.assertTrue(pushed["stored"])
        self.assertEqual(1, pushed["device_count"])
        self.assertEqual(profile, pulled)
        self.assertEqual(
            "http://192.168.1.10:8765/api/v1/profile", fake.last_put[0]
        )
        self.assertEqual("application/json", fake.last_put[2]["Content-Type"])
        self.assertIsInstance(fake.last_put[1], bytes)
        self.assertTrue(all(response.closed for response in fake.responses))

    def test_unconfigured_and_http_failure_are_clear(self):
        client = CompanionSync(base_url=None, request_module=FakeRequests(storage._default_data()))
        # Explicitly ensure a desktop's standard-library secrets module cannot
        # make this test depend on host environment settings.
        client.base_url = None
        with self.assertRaisesRegex(CompanionSyncError, "secrets.py"):
            client.pull_profile()

        class FailedRequests:
            def get(self, _url):
                return FakeResponse({"error": {}}, status=503)

        failed = CompanionSync("http://127.0.0.1:8765", FailedRequests())
        with self.assertRaisesRegex(CompanionSyncError, "HTTP 503"):
            failed.pull_profile()


if __name__ == "__main__":
    unittest.main()
