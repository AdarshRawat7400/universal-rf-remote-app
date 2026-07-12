import os
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "badge_settings"))

from badge_settings_secrets import (
    SecretsIOError,
    SecretsParseError,
    SecretsSizeError,
    SecretsStore,
    parse_supported_assignments,
)


class SecretsParserTests(unittest.TestCase):
    def test_parses_strings_escapes_none_and_documented_weather_tuples(self):
        source = (
            "WIFI_SSID = 'Cafe\\x20WiFi'  # keep\n"
            'GITHUB_USERNAME = "octo\\u002dcat"\n'
            "WEATHER_LOCATION = (51.5074, -0.1278, 'London', 'GB')\n"
            "WLED_IP = None\n"
        )
        values = parse_supported_assignments(source)
        self.assertEqual("Cafe WiFi", values["WIFI_SSID"])
        self.assertEqual("octo-cat", values["GITHUB_USERNAME"])
        self.assertEqual((51.5074, -0.1278, "London", "GB"), values["WEATHER_LOCATION"])
        self.assertIsNone(values["WLED_IP"])
        pair = parse_supported_assignments("WEATHER_LOCATION = ('London', 'GB')\n")
        self.assertEqual(("London", "GB"), pair["WEATHER_LOCATION"])
        supported_forms = (
            ('WEATHER_LOCATION = ("Paris",)\n', ("Paris",)),
            ('WEATHER_LOCATION = [51.5, -0.12, "London"]\n', [51.5, -0.12, "London"]),
            (
                'WEATHER_LOCATION = {"city": "Tokyo", "country": "JP"}\n',
                {"city": "Tokyo", "country": "JP"},
            ),
            (
                'WEATHER_LOCATION = {"lat": 37.77, "lon": -122.42, "name": "SF"}\n',
                {"lat": 37.77, "lon": -122.42, "name": "SF"},
            ),
        )
        for source, expected in supported_forms:
            with self.subTest(source=source):
                self.assertEqual(
                    expected,
                    parse_supported_assignments(source)["WEATHER_LOCATION"],
                )

    def test_parser_never_executes_unrelated_source(self):
        source = 'raise RuntimeError("must not run")\nWIFI_SSID = "Safe"\n'
        self.assertEqual("Safe", parse_supported_assignments(source)["WIFI_SSID"])

    def test_assignment_text_inside_multiline_string_is_not_a_setting(self):
        source = (
            'DOCUMENTATION = """Example only:\n'
            'WIFI_SSID = "phantom"\n'
            'GITHUB_TOKEN = "phantom-token-value-123"\n'
            '"""\n'
            'GITHUB_USERNAME = "octocat"\n'
        )

        values = parse_supported_assignments(source)

        self.assertNotIn("WIFI_SSID", values)
        self.assertNotIn("GITHUB_TOKEN", values)
        self.assertEqual("octocat", values["GITHUB_USERNAME"])

    def test_continued_strings_and_bracketed_calls_do_not_create_settings(self):
        source = (
            'DOCUMENTATION = "Example only: \\\n'
            'WIFI_SSID = \\"phantom\\""\n'
            'OPTIONS = dict(\n'
            '    GITHUB_TOKEN = "phantom-token-value-123",\n'
            ')\n'
            'GITHUB_USERNAME = "octocat"\n'
        )

        compile(source, "<test>", "exec")
        values = parse_supported_assignments(source)

        self.assertNotIn("WIFI_SSID", values)
        self.assertNotIn("GITHUB_TOKEN", values)
        self.assertEqual("octocat", values["GITHUB_USERNAME"])

    def test_rejects_duplicate_dynamic_indented_and_trailing_code(self):
        invalid_sources = (
            'WIFI_SSID = "one"\nWIFI_SSID = "two"\n',
            'WIFI_SSID = __import__("os")\n',
            '  WIFI_SSID = "nested"\n',
            'WIFI_SSID = "safe"; raise RuntimeError()\n',
            'WIFI_PASSWORD = None\n',
            'WEATHER_LOCATION = (open("x"), "GB")\n',
        )
        for source in invalid_sources:
            with self.subTest(source=source):
                with self.assertRaises(SecretsParseError):
                    parse_supported_assignments(source)


class FailingPromotionStore(SecretsStore):
    def _rename(self, source, destination):
        if source == self.temporary_path and destination == self.path:
            raise OSError("injected promotion failure")
        return os.rename(source, destination)


class SixthValidationOOMStore(SecretsStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.validation_count = 0

    def _validate_text(self, text):
        self.validation_count += 1
        if self.validation_count == 6:
            raise MemoryError("injected post-commit allocation failure")
        return super()._validate_text(text)


class SecretsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temporary.name, "secrets.py")

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, text):
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def read(self, path=None):
        with open(path or self.path, "r", encoding="utf-8", newline="") as handle:
            return handle.read()

    def test_general_update_preserves_unknown_text_comments_and_spacing(self):
        original = (
            "# user comment\r\n"
            "UNRELATED = {'keep': True}\r\n"
            "WIFI_SSID    =    'Old'   # inline\r\n"
            "WEATHER_LOCATION = ('London', 'GB')\r\n"
        )
        self.write(self.path, original)
        values = SecretsStore(self.path).update(
            {
                "WIFI_SSID": 'Cafe "Five" \\ AP',
                "WIFI_PASSWORD": "password123",
                "GITHUB_USERNAME": "octo-cat",
                "GITHUB_TOKEN": "github_pat_" + "a" * 30,
                "WLED_IP": "192.168.1.40",
                "IR_COMPANION_URL": "http://192.168.1.50:8765///",
            }
        )
        installed = self.read()
        self.assertIn("# user comment\r\nUNRELATED = {'keep': True}\r\n", installed)
        self.assertIn("WIFI_SSID    =    ", installed)
        self.assertIn("   # inline\r\n", installed)
        self.assertIn("WEATHER_LOCATION = ('London', 'GB')\r\n", installed)
        self.assertEqual('Cafe "Five" \\ AP', values["WIFI_SSID"])
        self.assertEqual("http://192.168.1.50:8765", values["IR_COMPANION_URL"])

    def test_optional_none_round_trips_as_literal(self):
        values = SecretsStore(self.path).update(
            {"WEATHER_LOCATION": None, "WLED_IP": None, "IR_COMPANION_URL": None}
        )
        for name in ("WEATHER_LOCATION", "WLED_IP", "IR_COMPANION_URL"):
            self.assertIsNone(values[name])
            self.assertIn(name + " = None", self.read())

    def test_any_save_materializes_missing_github_token(self):
        self.write(self.path, 'WIFI_SSID = "Factory"\n')
        values = SecretsStore(self.path).update({})
        self.assertEqual("", values["GITHUB_TOKEN"])

    def test_first_save_materializes_all_factory_import_names(self):
        store = SecretsStore(self.path)

        store.update({"WEATHER_LOCATION": "Paris"})

        values = store.read_values()
        self.assertEqual("", values["WIFI_SSID"])
        self.assertEqual("", values["WIFI_PASSWORD"])
        self.assertEqual("", values["GITHUB_USERNAME"])
        self.assertEqual("", values["GITHUB_TOKEN"])
        self.assertEqual("Paris", values["WEATHER_LOCATION"])
        self.assertIn('GITHUB_TOKEN = ""', self.read())

    def test_verified_backup_matches_new_primary_and_removes_old_secrets(self):
        old_password = "old-password-must-disappear"
        old_token = "ghp_oldtokenmustdisappear123456"
        original = (
            'WIFI_SSID = "Old"\n'
            'WIFI_PASSWORD = "' + old_password + '"\n'
            'GITHUB_USERNAME = "octocat"\n'
            'GITHUB_TOKEN = "' + old_token + '"\n'
        )
        self.write(self.path, original)
        store = SecretsStore(self.path)
        store.update(
            {
                "WIFI_SSID": "",
                "WIFI_PASSWORD": "",
                "GITHUB_TOKEN": "",
            }
        )
        primary = self.read()
        backup = self.read(store.backup_path)
        self.assertEqual(primary, backup)
        self.assertNotIn(old_password, primary + backup)
        self.assertNotIn(old_token, primary + backup)
        self.assertFalse(os.path.exists(store.temporary_path))
        self.assertFalse(os.path.exists(store.recovery_path))
        self.assertEqual("", store.read_values()["WIFI_SSID"])

    def test_late_backup_memory_error_does_not_report_committed_save_as_failed(self):
        self.write(
            self.path,
            'WIFI_SSID = "Old"\nWIFI_PASSWORD = "password"\n'
            'GITHUB_USERNAME = "octocat"\nGITHUB_TOKEN = ""\n',
        )
        store = SixthValidationOOMStore(self.path)

        values = store.update({"WIFI_SSID": "New"})

        self.assertEqual("New", values["WIFI_SSID"])
        self.assertIn('WIFI_SSID = "New"', self.read())
        self.assertGreaterEqual(store.validation_count, 6)

    def test_failed_promotion_rolls_back_without_losing_original(self):
        original = 'WIFI_SSID = "Old"\nGITHUB_TOKEN = ""\n'
        self.write(self.path, original)
        store = FailingPromotionStore(self.path)
        with self.assertRaises(SecretsIOError):
            store.update({"WIFI_SSID": "New"})
        self.assertEqual(original, self.read())
        self.assertFalse(os.path.exists(store.temporary_path))

    def test_recovers_temporary_then_backup_and_cleans_stale_temp(self):
        store = SecretsStore(self.path)
        self.write(store.temporary_path, 'WIFI_SSID = "Temp"\nGITHUB_TOKEN = ""\n')
        self.assertEqual("temporary", store.recover())
        self.assertEqual("Temp", store.read_values()["WIFI_SSID"])

        self.write(self.path, 'WIFI_SSID = broken\n')
        self.write(store.backup_path, 'WIFI_SSID = "Backup"\nGITHUB_TOKEN = ""\n')
        self.assertEqual("backup", store.recover())
        self.assertEqual("Backup", store.read_values()["WIFI_SSID"])

        self.write(store.temporary_path, "not valid Python !!!")
        self.assertEqual("primary", store.recover())
        self.assertFalse(os.path.exists(store.temporary_path))

    def test_duplicate_and_invalid_python_block_update_without_backup(self):
        for source in (
            'WIFI_SSID = "one"\nWIFI_SSID = "two"\n',
            'if :\n    WIFI_SSID = "x"\n',
        ):
            with self.subTest(source=source):
                self.write(self.path, source)
                with self.assertRaises(SecretsParseError):
                    SecretsStore(self.path).update({"GITHUB_USERNAME": "octocat"})
                self.assertEqual(source, self.read())

    def test_size_bound_rejects_before_changing_primary(self):
        original = 'WIFI_SSID = "Old"\nGITHUB_TOKEN = ""\n'
        self.write(self.path, original)
        store = SecretsStore(self.path, max_bytes=96)
        with self.assertRaises(SecretsSizeError):
            store.update({"WEATHER_LOCATION": "x" * 64})
        self.assertEqual(original, self.read())

    def test_errors_never_include_submitted_credential(self):
        secret = "this token contains forbidden spaces"
        self.write(self.path, 'GITHUB_TOKEN = ""\n')
        try:
            SecretsStore(self.path).update({"GITHUB_TOKEN": secret})
        except Exception as error:
            self.assertNotIn(secret, str(error))
            self.assertNotIn(secret, repr(error))
        else:
            self.fail("invalid token unexpectedly accepted")


if __name__ == "__main__":
    unittest.main()
