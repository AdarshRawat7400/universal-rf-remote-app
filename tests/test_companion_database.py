import json
import os
import tempfile
import unittest

from companion.database import CompanionDatabase, NotFoundError
from companion.profile import SCHEMA_VERSION, ProfileValidationError, validate_profile


def raw_command(description="test"):
    return {
        "format": "raw",
        "carrier_hz": 38_000,
        "repeat_count": 2,
        "repeat_gap_us": 40_000,
        "description": description,
        "pulses": [[4500, 4500], [560, 560], [560, 1690]],
        "decoded": {"protocol": "Samsung32", "address": 7, "command": 2},
    }


def compact_samsung_command():
    return {
        "format": "samsung32",
        "carrier_hz": 38_000,
        "repeat_count": 1,
        "repeat_gap_us": 40_000,
        "description": "SAMSUNG32 A:07 C:02",
        "address": 7,
        "command": 2,
        "decoded": {"protocol": "SAMSUNG32", "address": 7, "command": 2},
    }


class CompanionDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temporary_directory.name, "data", "remote.sqlite3")
        self.database = CompanionDatabase(self.path)

    def tearDown(self):
        self.database.close()
        self.temporary_directory.cleanup()

    def test_device_and_command_crud_persists_as_current_schema(self):
        self.assertEqual("device-1", self.database.export_profile()["active_device"])
        created = self.database.create_device(
            {
                "name": "Living Room TV",
                "type": "tv",
                "transport": "ir",
                "active": True,
            }
        )
        self.assertEqual("living-room-tv", created["id"])
        self.assertTrue(created["active"])

        updated = self.database.update_device(
            created["id"], {"name": "Samsung TV", "transport_metadata": {"room": "living"}}
        )
        self.assertEqual("Samsung TV", updated["name"])
        self.database.put_button(created["id"], "Power / Standby", raw_command())
        self.assertEqual(1, self.database.get_device(created["id"])["active"])
        self.assertEqual(
            "Samsung32",
            self.database.get_buttons(created["id"])["Power / Standby"]["decoded"]["protocol"],
        )

        self.database.close()
        self.database = CompanionDatabase(self.path)
        exported = validate_profile(self.database.export_profile())
        self.assertEqual(SCHEMA_VERSION, exported["schema"])
        self.assertEqual(created["id"], exported["active_device"])
        self.assertEqual(1, len(exported["devices"][1]["buttons"]))

        self.database.delete_button(created["id"], "Power / Standby")
        deleted = self.database.delete_device(created["id"])
        self.assertEqual("Samsung TV", deleted["name"])
        self.assertEqual("device-1", self.database.export_profile()["active_device"])
        reset = self.database.delete_device("device-1")
        self.assertEqual("device-1", reset["id"])
        self.assertEqual("My Remote", self.database.get_device("device-1")["name"])

    def test_import_is_validated_and_invalid_import_does_not_replace_data(self):
        original = self.database.export_profile()
        profile = json.loads(json.dumps(original))
        profile["schema"] = 3
        profile["devices"][0]["name"] = "Imported Remote"
        profile["devices"][0]["buttons"]["Power"] = raw_command("imported")
        imported = self.database.import_profile(profile)
        self.assertEqual(SCHEMA_VERSION, imported["schema"])
        self.assertEqual("Imported Remote", imported["devices"][0]["name"])

        invalid = json.loads(json.dumps(profile))
        invalid["devices"][0]["buttons"]["Power"]["pulses"] = [[0, 1]]
        with self.assertRaises(ProfileValidationError):
            self.database.import_profile(invalid)
        self.assertEqual(imported, self.database.export_profile())

        with self.assertRaises(ProfileValidationError):
            self.database.create_device(
                {"id": "x'; DROP TABLE devices;--", "name": "unsafe"}
            )
        self.assertEqual(1, len(self.database.list_devices()))

    def test_compact_samsung_command_round_trips_without_pulses(self):
        profile = self.database.export_profile()
        profile["devices"][0]["buttons"]["Power"] = compact_samsung_command()
        imported = self.database.import_profile(profile)
        command = imported["devices"][0]["buttons"]["Power"]
        self.assertEqual("samsung32", command["format"])
        self.assertEqual(7, command["address"])
        self.assertNotIn("pulses", command)
        self.assertEqual(command, self.database.export_profile()["devices"][0]["buttons"]["Power"])

    def test_discovery_upsert_listing_save_and_delete(self):
        first = self.database.upsert_discovery(
            {
                "name": "Samsung Frame",
                "type": "tv",
                "transport": "wifi",
                "transport_metadata": {
                    "host": "192.168.1.20",
                    "service_id": "_samsungmsf._tcp",
                    "rssi": -42,
                },
            }
        )
        refreshed = self.database.upsert_discovery(
            {
                "name": "Samsung Frame TV",
                "type": "tv",
                "transport": "wifi",
                "transport_metadata": {
                    "host": "192.168.1.20",
                    "service_id": "_samsungmsf._tcp",
                    "rssi": -55,
                },
            }
        )
        self.assertEqual(first["id"], refreshed["id"])
        self.assertEqual(first["first_seen"], refreshed["first_seen"])
        self.assertFalse(refreshed["controllable"])
        self.assertEqual(1, len(self.database.list_discoveries()))

        saved = self.database.save_discoveries([first["id"]])
        self.assertEqual(1, len(saved))
        self.assertEqual("wifi", saved[0]["transport"])
        again = self.database.save_discoveries(save_all=True)
        self.assertEqual(saved[0]["id"], again[0]["id"])
        self.assertEqual(2, len(self.database.list_devices()))

        self.assertEqual(first["id"], self.database.delete_discovery(first["id"])["id"])
        with self.assertRaises(NotFoundError):
            self.database.get_discovery(first["id"])
        self.assertEqual(0, self.database.clear_discoveries())

    def test_create_rejects_non_scalar_metadata_and_bad_identifiers(self):
        with self.assertRaises(ProfileValidationError):
            self.database.create_device(
                {
                    "name": "Bad metadata",
                    "transport_metadata": {"nested": {"not": "allowed"}},
                }
            )
        with self.assertRaises(ProfileValidationError):
            self.database.create_device({"id": ["not", "text"], "name": "Bad id"})


if __name__ == "__main__":
    unittest.main()
