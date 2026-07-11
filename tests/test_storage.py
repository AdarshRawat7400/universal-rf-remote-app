import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "app"))

import storage
from storage import ProfileStore, ProfileValidationError, validate_command, validate_profile


def sample_pairs(count=4):
    return [[560, 560 if index % 2 == 0 else 1680] for index in range(count)]


def raw_command(description="Learned", pairs=None):
    return {
        "format": "raw",
        "carrier_hz": 38_000,
        "repeat_count": 1,
        "repeat_gap_us": 40_000,
        "description": description,
        "pulses": pairs or sample_pairs(),
    }


def compact_samsung(command=0x02):
    return {
        "format": "samsung32",
        "carrier_hz": 38_000,
        "repeat_count": 1,
        "repeat_gap_us": 40_000,
        "description": "Samsung",
        "address": 0x07,
        "command": command,
        "decoded": {"protocol": "SAMSUNG32", "address": 7, "command": command},
    }


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(
            self.temporary_directory.name, "missing", "nested", "profiles.json"
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_current_ui_api_creates_missing_parents_and_metadata(self):
        store = ProfileStore(self.path).load()
        self.assertEqual("My Remote", store.device["name"])
        self.assertEqual("generic", store.device["type"])
        self.assertEqual("ir", store.device["transport"])
        self.assertEqual({}, store.device["transport_metadata"])

        saved = store.set_button(
            "Power",
            sample_pairs(),
            "NEC A:45 C:66",
            {"protocol": "NEC", "address": 0x45, "command": 0x66},
        )

        self.assertTrue(os.path.isfile(self.path))
        self.assertEqual(38_000, saved["carrier_hz"])
        self.assertEqual(1, saved["repeat_count"])
        self.assertEqual(40_000, saved["repeat_gap_us"])
        self.assertEqual(saved, ProfileStore(self.path).load().get_button("Power"))

        with open(self.path, "r") as handle:
            persisted = json.load(handle)
        self.assertEqual(storage.SCHEMA_VERSION, persisted["schema"])
        self.assertEqual("device-1", persisted["active_device"])

    def test_batch_update_is_atomic_and_can_name_device(self):
        store = ProfileStore(self.path).load()
        commands = {
            "Power": raw_command("power"),
            "Mute": raw_command("mute"),
        }
        saved = store.set_buttons(commands, device_name="Samsung TV")

        self.assertEqual({"Power", "Mute"}, set(saved))
        loaded = ProfileStore(self.path).load()
        self.assertEqual("Samsung TV", loaded.device["name"])
        self.assertEqual("power", loaded.get_button("Power")["description"])

        before = json.dumps(loaded.data, sort_keys=True)
        with self.assertRaises(ProfileValidationError):
            loaded.set_buttons({"Broken": dict(raw_command(), pulses=[[0, 1]])})
        self.assertEqual(before, json.dumps(loaded.data, sort_keys=True))

    def test_device_ids_use_portable_ascii_validation(self):
        profile = storage._default_data()
        profile["devices"][0]["id"] = "Living_Room-2.0"
        profile["active_device"] = "Living_Room-2.0"
        self.assertEqual("Living_Room-2.0", validate_profile(profile)["active_device"])

        profile["devices"][0]["id"] = "living room"
        profile["active_device"] = "living room"
        with self.assertRaises(ProfileValidationError):
            validate_profile(profile)

    def test_schema_one_is_migrated_without_mutating_input(self):
        legacy = {
            "schema": 1,
            "devices": [
                {
                    "name": "Living Room",
                    "buttons": {
                        "Power": {
                            "format": "raw",
                            "carrier_hz": 38_000,
                            "description": "old",
                            "pulses": sample_pairs(),
                        }
                    },
                }
            ],
        }
        migrated = validate_profile(legacy)

        self.assertEqual(1, legacy["schema"])
        self.assertEqual(storage.SCHEMA_VERSION, migrated["schema"])
        self.assertEqual("device-1", migrated["devices"][0]["id"])
        command = migrated["devices"][0]["buttons"]["Power"]
        self.assertEqual(1, command["repeat_count"])
        self.assertEqual(40_000, command["repeat_gap_us"])
        self.assertEqual("generic", migrated["devices"][0]["type"])
        self.assertEqual("ir", migrated["devices"][0]["transport"])

    def test_schema_two_migrates_transport_fields_and_preserves_active_device(self):
        legacy = {
            "schema": 2,
            "active_device": "soundbar",
            "devices": [
                {
                    "id": "device-1",
                    "name": "TV",
                    "buttons": {},
                },
                {
                    "id": "soundbar",
                    "name": "Soundbar",
                    "buttons": {"Mute": raw_command()},
                },
            ],
        }

        migrated = validate_profile(legacy)
        self.assertEqual(2, legacy["schema"])
        self.assertEqual(storage.SCHEMA_VERSION, migrated["schema"])
        self.assertEqual("soundbar", migrated["active_device"])
        self.assertEqual("generic", migrated["devices"][1]["type"])
        self.assertEqual("ir", migrated["devices"][1]["transport"])
        self.assertEqual({}, migrated["devices"][1]["transport_metadata"])
        self.assertIn("Mute", migrated["devices"][1]["buttons"])

    def test_schema_three_raw_profile_migrates_to_schema_four(self):
        legacy = storage._default_data()
        legacy["schema"] = 3
        legacy["devices"][0]["buttons"]["Power"] = raw_command("v3 raw")
        migrated = validate_profile(legacy)
        self.assertEqual(3, legacy["schema"])
        self.assertEqual(storage.SCHEMA_VERSION, migrated["schema"])
        self.assertEqual(
            "raw", migrated["devices"][0]["buttons"]["Power"]["format"]
        )

    def test_command_bounds_reject_unsafe_values(self):
        cases = (
            dict(raw_command(), carrier_hz=19_999),
            dict(raw_command(), carrier_hz=60_001),
            dict(raw_command(), repeat_count=0),
            dict(raw_command(), repeat_count=21),
            dict(raw_command(), repeat_gap_us=500_001),
            dict(raw_command(), pulses=[[0, 560]]),
            dict(raw_command(), pulses=[[560, storage.MAX_PULSE_US + 1]]),
        )
        for command in cases:
            with self.subTest(command=command):
                with self.assertRaises(ProfileValidationError):
                    validate_command(command)

    def test_compact_samsung_command_avoids_raw_pulse_storage(self):
        canonical = validate_command(compact_samsung())
        self.assertEqual("samsung32", canonical["format"])
        self.assertEqual(7, canonical["address"])
        self.assertEqual(2, canonical["command"])
        self.assertNotIn("pulses", canonical)
        with self.assertRaises(ProfileValidationError):
            validate_command(dict(compact_samsung(), pulses=sample_pairs()))
        with self.assertRaises(ProfileValidationError):
            validate_command(dict(compact_samsung(), command=0x10000))

    def test_learned_samsung_is_persisted_without_raw_pulses(self):
        store = ProfileStore(self.path).load()
        saved = store.set_button(
            "Home",
            sample_pairs(34),
            "SAMSUNG32 A:07 C:79",
            {
                "protocol": "SAMSUNG32",
                "address": 0x07,
                "address_bits": 8,
                "command": 0x79,
                "command_bits": 8,
            },
        )

        self.assertEqual("samsung32", saved["format"])
        self.assertEqual(0x79, saved["command"])
        self.assertNotIn("pulses", saved)
        reloaded = ProfileStore(self.path).load().get_button("Home")
        self.assertEqual("samsung32", reloaded["format"])
        self.assertNotIn("pulses", reloaded)

    def test_extended_samsung_learning_keeps_exact_raw_frame(self):
        store = ProfileStore(self.path).load()
        saved = store.set_button(
            "Extended",
            sample_pairs(34),
            "SAMSUNG32 A:0007 C:0079",
            {
                "protocol": "SAMSUNG32",
                "address": 0x0007,
                "address_bits": 16,
                "command": 0x0079,
                "command_bits": 16,
            },
        )

        self.assertEqual("raw", saved["format"])
        self.assertEqual(16, saved["decoded"]["address_bits"])
        self.assertEqual(sample_pairs(34), saved["pulses"])

    def test_invalid_update_does_not_change_memory_or_disk(self):
        store = ProfileStore(self.path).load()
        store.set_button("Power", sample_pairs(), "working")
        with open(self.path, "r") as handle:
            before = handle.read()

        with self.assertRaises(ProfileValidationError):
            store.set_button("Power", [[0, 10]], "invalid")

        self.assertEqual("working", store.get_button("Power")["description"])
        with open(self.path, "r") as handle:
            self.assertEqual(before, handle.read())

    def test_second_save_keeps_backup_and_corrupt_primary_recovers(self):
        store = ProfileStore(self.path).load()
        store.set_button("Power", sample_pairs(), "first")
        store.set_button("Power", sample_pairs(), "second")
        self.assertTrue(os.path.isfile(self.path + ".bak"))

        with open(self.path, "w") as handle:
            handle.write("{truncated")

        recovered = ProfileStore(self.path).load()
        self.assertEqual("backup", recovered.recovered_from)
        self.assertIn("primary", recovered.last_error)
        self.assertEqual("first", recovered.get_button("Power")["description"])

        # Recovery also repairs the primary, while retaining the good backup.
        reloaded = ProfileStore(self.path).load()
        self.assertIsNone(reloaded.recovered_from)
        self.assertEqual("first", reloaded.get_button("Power")["description"])
        self.assertTrue(os.path.isfile(self.path + ".bak"))

    def test_valid_temporary_file_recovers_interrupted_first_save(self):
        store = ProfileStore(self.path)
        data = storage._default_data()
        data["devices"][0]["buttons"]["Mute"] = raw_command("temporary")
        os.makedirs(os.path.dirname(self.path))
        with open(store.temporary_path, "w") as handle:
            json.dump(validate_profile(data), handle)

        recovered = ProfileStore(self.path).load()
        self.assertEqual("temporary", recovered.recovered_from)
        self.assertEqual("temporary", recovered.get_button("Mute")["description"])
        self.assertTrue(os.path.isfile(self.path))
        self.assertFalse(os.path.exists(store.temporary_path))

    def test_corrupt_primary_and_backup_fall_back_to_safe_default(self):
        os.makedirs(os.path.dirname(self.path))
        for path in (self.path, self.path + ".bak"):
            with open(path, "w") as handle:
                handle.write("not json")

        store = ProfileStore(self.path).load()
        self.assertEqual("default", store.recovered_from)
        self.assertIsNotNone(store.last_error)
        self.assertEqual({}, store.device["buttons"])
        self.assertEqual(storage.SCHEMA_VERSION, store.data["schema"])

    def test_profile_supports_multiple_devices_and_active_selection(self):
        profile = storage._default_data()
        profile["devices"].append(
            {
                "id": "soundbar",
                "name": "Soundbar",
                "type": "soundbar",
                "transport": "ir",
                "transport_metadata": {},
                "buttons": {"Mute": raw_command()},
            }
        )
        profile["active_device"] = "soundbar"

        store = ProfileStore(self.path)
        store.data = profile
        store.save()

        loaded = ProfileStore(self.path).load()
        self.assertEqual("Soundbar", loaded.device["name"])
        self.assertIsNotNone(loaded.get_button("Mute"))

    def test_full_device_crud_uses_unique_ids_and_deterministic_replacement(self):
        store = ProfileStore(self.path).load()
        first = store.create_device(
            "Living Room",
            device_type="tv",
            transport="wifi",
            transport_metadata={"host": "tv.local", "port": 8001},
            make_active=False,
        )
        second = store.create_device(
            "Living Room",
            device_type="soundbar",
            transport="ir",
            make_active=False,
        )

        self.assertEqual("living-room", first["id"])
        self.assertEqual("living-room-2", second["id"])
        self.assertEqual(3, len(store.list_devices()))
        self.assertEqual("device-1", store.data["active_device"])

        renamed = store.rename_device(first["id"], "Main TV")
        self.assertEqual("Main TV", renamed["name"])
        self.assertEqual(first["id"], renamed["id"])

        metadata_updated = store.update_device_metadata(
            first["id"],
            {"host": "tv.local", "power_burst": "strong"},
            device_type="television",
        )
        self.assertEqual("strong", metadata_updated["transport_metadata"]["power_burst"])
        self.assertEqual("television", metadata_updated["type"])

        selected = store.set_active_device(first["id"])
        self.assertTrue(selected["active"])
        self.assertEqual("Main TV", store.device["name"])

        deleted = store.delete_device(first["id"])
        self.assertEqual(first["id"], deleted["id"])
        # The next item at the deleted index wins; if there is no next item,
        # delete_device deterministically chooses the previous item.
        self.assertEqual(second["id"], store.data["active_device"])
        store.delete_device(second["id"])
        self.assertEqual("device-1", store.data["active_device"])
        last_deleted = store.delete_device("device-1")
        self.assertEqual("device-1", last_deleted["id"])
        self.assertEqual("My Remote", store.device["name"])
        self.assertEqual({}, store.device["buttons"])

        reloaded = ProfileStore(self.path).load()
        self.assertEqual(["device-1"], [item["id"] for item in reloaded.list_devices()])

    def test_selecting_already_active_device_does_not_validate_or_write(self):
        store = ProfileStore(self.path).load()
        with mock.patch.object(
            storage,
            "_canonicalize_profile",
            side_effect=AssertionError("no-op selection must not canonicalize"),
        ), mock.patch.object(
            store, "save", side_effect=AssertionError("no-op selection must not save")
        ):
            selected = store.set_active_device("device-1")

        self.assertTrue(selected["active"])
        self.assertEqual("device-1", store.data["active_device"])

    def test_targeted_button_repair_does_not_change_active_device(self):
        store = ProfileStore(self.path).load()
        samsung = store.create_device(
            "Samsung TV", device_type="television", make_active=False
        )

        repaired = store.set_device_buttons(
            samsung["id"], {"Power": compact_samsung(0x02)}
        )

        self.assertEqual("device-1", store.data["active_device"])
        self.assertEqual(0x02, repaired["Power"]["command"])
        self.assertIsNone(store.get_button("Power"))
        reloaded = ProfileStore(self.path).load()
        self.assertEqual("device-1", reloaded.data["active_device"])
        target = reloaded._find_device(reloaded.data, samsung["id"])
        self.assertEqual(0x02, target["buttons"]["Power"]["command"])

    def test_internal_transaction_streams_without_full_profile_copies(self):
        store = ProfileStore(self.path).load()
        original_canonicalize = storage._canonicalize_profile
        original_dump = storage.json.dump
        original_stream = storage._write_profile_stream

        with mock.patch.object(
            storage, "_canonicalize_profile", wraps=original_canonicalize
        ) as canonicalize, mock.patch.object(
            storage,
            "_serialize_profile",
            side_effect=AssertionError("routine save must not build full JSON"),
        ), mock.patch.object(
            storage.json, "dump", wraps=original_dump
        ) as dump, mock.patch.object(
            storage, "_write_profile_stream", wraps=original_stream
        ) as stream, mock.patch.object(
            store,
            "_load_file",
            side_effect=AssertionError("save must not reparse profile files"),
        ):
            store.create_device(
                "Samsung TV",
                device_type="television",
                buttons={"Power": compact_samsung()},
            )

        self.assertEqual(0, canonicalize.call_count)
        self.assertEqual(0, dump.call_count)
        self.assertEqual(1, stream.call_count)

    def test_batch_compaction_uses_copy_on_write_and_one_validation(self):
        store = ProfileStore(self.path).load()
        store.set_buttons(
            {
                "Power": dict(
                    raw_command("legacy power"),
                    decoded={"protocol": "SAMSUNG32", "address": 7, "command": 2},
                ),
                "Mute": dict(
                    raw_command("legacy mute"),
                    decoded={"protocol": "SAMSUNG32", "address": 7, "command": 15},
                ),
            },
            device_name="Samsung TV",
        )
        previous_data = store.data
        previous_power = store.get_button("Power")
        original_canonicalize = storage._canonicalize_profile

        with mock.patch.object(
            storage, "_canonicalize_profile", wraps=original_canonicalize
        ) as canonicalize, mock.patch.object(
            storage,
            "validate_profile",
            side_effect=AssertionError("internal compaction must not make a full copy"),
        ):
            store.set_buttons(
                {"Power": compact_samsung(0x02), "Mute": compact_samsung(0x0F)}
            )

        self.assertEqual(0, canonicalize.call_count)
        self.assertEqual("raw", previous_power["format"])
        self.assertEqual("raw", previous_data["devices"][0]["buttons"]["Power"]["format"])
        self.assertEqual("samsung32", store.get_button("Power")["format"])

    def test_changed_commands_are_detached_before_streaming_commit(self):
        store = ProfileStore(self.path).load()
        submitted = raw_command("original")
        original_pair = list(submitted["pulses"][0])

        store.set_buttons({"Power": submitted})
        submitted["description"] = "mutated"
        submitted["pulses"][0][0] = 1

        saved = store.get_button("Power")
        self.assertEqual("original", saved["description"])
        self.assertEqual(original_pair, saved["pulses"][0])

    def test_streaming_quota_failure_keeps_profile_and_primary(self):
        store = ProfileStore(self.path).load()
        store.save()
        before = json.dumps(store.data, sort_keys=True)
        with open(self.path, "r") as handle:
            primary_before = handle.read()

        with mock.patch.object(storage, "MAX_PROFILE_BYTES", 32):
            with self.assertRaises(ProfileValidationError):
                store.create_device("Will Not Fit")

        self.assertEqual(before, json.dumps(store.data, sort_keys=True))
        with open(self.path, "r") as handle:
            self.assertEqual(primary_before, handle.read())
        self.assertFalse(os.path.exists(store.temporary_path))

    def test_streaming_writer_preserves_unicode_and_scalar_metadata(self):
        store = ProfileStore(self.path).load()
        created = store.create_device(
            "Télévision 家",
            transport_metadata={
                "host": "salon-✓",
                "port": 8001,
                "paired": True,
                "token": None,
            },
            buttons={"Accueil ✓": compact_samsung(0x79)},
        )

        with open(self.path, "r") as handle:
            persisted = json.load(handle)

        self.assertEqual(store.data, persisted)
        saved = store._find_device(persisted, created["id"])
        self.assertEqual("Télévision 家", saved["name"])
        self.assertEqual("salon-✓", saved["transport_metadata"]["host"])
        self.assertEqual(0x79, saved["buttons"]["Accueil ✓"]["command"])

    def test_create_device_with_preset_buttons_is_one_atomic_transaction(self):
        store = ProfileStore(self.path).load()
        created = store.create_device(
            "Samsung TV",
            device_type="television",
            buttons={"Power": compact_samsung()},
        )
        self.assertEqual(1, created["button_count"])
        self.assertEqual("samsung32", store.get_button("Power")["format"])

        before = json.dumps(store.data, sort_keys=True)
        with mock.patch.object(store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.create_device(
                    "Another Samsung",
                    device_type="television",
                    buttons={"Power": compact_samsung()},
                )
        self.assertEqual(before, json.dumps(store.data, sort_keys=True))

    def test_list_summaries_are_detached_and_include_transport_metadata(self):
        store = ProfileStore(self.path).load()
        created = store.create_device(
            "Lamp",
            device_type="light",
            transport="ble",
            transport_metadata={"address": "AA:BB:CC:DD:EE:FF", "rssi": -55},
        )
        self.assertEqual("ble", created["transport"])
        self.assertEqual(0, created["button_count"])

        summaries = store.list_devices()
        lamp = summaries[-1]
        lamp["name"] = "Changed outside store"
        lamp["transport_metadata"]["rssi"] = 0
        self.assertEqual("Lamp", store.device["name"])
        self.assertEqual(-55, store.device["transport_metadata"]["rssi"])

    def test_explicit_duplicate_id_and_invalid_transport_metadata_roll_back(self):
        store = ProfileStore(self.path).load()
        store.create_device("TV", device_id="tv")
        before = json.dumps(store.data, sort_keys=True)

        with self.assertRaises(ProfileValidationError):
            store.create_device("Another TV", device_id="tv")
        with self.assertRaises(ProfileValidationError):
            store.create_device("Radio", transport="rf")
        with self.assertRaises(ProfileValidationError):
            store.create_device(
                "Bad metadata",
                transport="wifi",
                transport_metadata={"network": {"host": "nested"}},
            )
        with self.assertRaises(ProfileValidationError):
            store.create_device("Bad type", device_type="smart television")

        self.assertEqual(before, json.dumps(store.data, sort_keys=True))

    def test_save_discovered_upserts_and_preserves_learned_buttons(self):
        store = ProfileStore(self.path).load()
        discovered = store.save_discovered(
            "Desk Lamp",
            "ble",
            {"address": "AA:BB:CC:DD:EE:FF", "rssi": -60},
            device_type="light",
            device_id="ble-lamp",
            make_active=True,
        )
        self.assertEqual("ble-lamp", discovered["id"])
        store.set_button("Power", sample_pairs(), "learned")

        refreshed = store.save_discovered(
            "Desk Lamp Updated",
            "ble",
            {"address": "AA:BB:CC:DD:EE:FF", "rssi": -48},
            device_type="light",
            device_id="ble-lamp",
        )
        self.assertEqual("Desk Lamp Updated", refreshed["name"])
        self.assertEqual(1, refreshed["button_count"])
        self.assertEqual("learned", store.get_button("Power")["description"])

        count = len(store.list_devices())
        matched = store.save_discovered(
            "Same Lamp Again",
            "ble",
            {"address": "AA:BB:CC:DD:EE:FF", "rssi": -32},
            device_type="light",
        )
        self.assertEqual("ble-lamp", matched["id"])
        self.assertEqual(count, len(store.list_devices()))
        self.assertEqual(-32, store.device["transport_metadata"]["rssi"])

    def test_full_nearby_scan_fits_alongside_the_default_remote(self):
        store = ProfileStore(self.path).load()
        records = []
        for index in range(24):
            records.append(
                {
                    "name": "Nearby " + str(index),
                    "transport": "ble",
                    "transport_metadata": {
                        "address": "AA:BB:CC:DD:EE:%02X" % index,
                        "signal": -40 - index,
                    },
                    "device_type": "ble-advertiser",
                }
            )
        # Reproduce an older build that saved only the first few results before
        # hitting its device cap, then retry Save All with the same scan.
        store.save_discovered_many(records[:8])
        saved = store.save_discovered_many(records)
        self.assertEqual(24, len(saved))
        self.assertEqual(25, len(store.list_devices()))
        self.assertEqual(25, len(ProfileStore(self.path).load().list_devices()))

    def test_batch_discovery_save_is_atomic(self):
        store = ProfileStore(self.path).load()
        records = [
            {
                "name": "One",
                "transport": "wifi",
                "transport_metadata": {"address": "00:00:00:00:00:01"},
            },
            {
                "name": "Two",
                "transport": "ble",
                "transport_metadata": {"address": "00:00:00:00:00:02"},
            },
        ]
        before = json.dumps(store.data, sort_keys=True)
        with mock.patch.object(store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.save_discovered_many(records)
        self.assertEqual(before, json.dumps(store.data, sort_keys=True))

    def test_every_crud_mutator_rolls_memory_back_when_save_fails(self):
        store = ProfileStore(self.path).load()
        second = store.create_device("TV", device_type="tv", make_active=False)
        original = json.dumps(store.data, sort_keys=True)
        operations = (
            lambda: store.create_device("New"),
            lambda: store.rename_device("device-1", "Renamed"),
            lambda: store.update_device_metadata(
                "device-1", {"power_burst": "strong"}
            ),
            lambda: store.set_active_device(second["id"]),
            lambda: store.delete_device(second["id"]),
            lambda: store.save_discovered(
                "Refreshed", "wifi", {"host": "tv.local"}, device_id="device-1"
            ),
        )

        for operation in operations:
            with self.subTest(operation=operation):
                with mock.patch.object(store, "save", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        operation()
                self.assertEqual(original, json.dumps(store.data, sort_keys=True))

    def test_replace_profile_is_atomic_for_companion_restore(self):
        store = ProfileStore(self.path).load()
        replacement = storage._default_data()
        replacement["devices"][0]["name"] = "Restored TV"
        replacement["devices"][0]["buttons"]["Power"] = raw_command("backup")

        with mock.patch.object(
            storage,
            "_serialize_profile",
            side_effect=AssertionError("replace must stream without a full JSON payload"),
        ):
            returned = store.replace_profile(replacement)
        self.assertIs(store, returned)
        self.assertEqual("Restored TV", store.device["name"])
        self.assertEqual("backup", store.get_button("Power")["description"])
        self.assertEqual("Restored TV", ProfileStore(self.path).load().device["name"])

        before = json.dumps(store.data, sort_keys=True)
        with mock.patch.object(store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.replace_profile(storage._default_data())
        self.assertEqual(before, json.dumps(store.data, sort_keys=True))

    def test_aggregate_pulse_quota_is_enforced(self):
        profile = storage._default_data()
        buttons = profile["devices"][0]["buttons"]
        command_count = (storage.MAX_TOTAL_PAIRS // storage.MAX_CAPTURE_PAIRS) + 1
        for index in range(command_count):
            buttons["Button " + str(index)] = raw_command(
                pairs=sample_pairs(storage.MAX_CAPTURE_PAIRS)
            )

        with self.assertRaises(ProfileValidationError):
            validate_profile(profile)

    def test_failed_final_rename_restores_previous_primary(self):
        store = ProfileStore(self.path).load()
        store.set_button("Power", sample_pairs(), "committed")
        real_rename = os.rename

        def fail_new_primary(source, destination):
            if source == store.temporary_path and destination == store.path:
                raise OSError("simulated interruption")
            return real_rename(source, destination)

        with mock.patch.object(storage.os, "rename", side_effect=fail_new_primary):
            with self.assertRaises(OSError):
                store.set_button("Power", sample_pairs(), "uncommitted")

        self.assertEqual("committed", store.get_button("Power")["description"])
        reloaded = ProfileStore(self.path).load()
        self.assertEqual("committed", reloaded.get_button("Power")["description"])
        self.assertFalse(os.path.exists(store.temporary_path))


if __name__ == "__main__":
    unittest.main()
