import hashlib
import json
import os
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
RELEASE_ROOT = os.path.join(ROOT, "release")
BUNDLE = os.path.join(RELEASE_ROOT, "badge_settings")
MANIFEST_PATH = os.path.join(RELEASE_ROOT, "badge_settings-manifest.json")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BadgeSettingsReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(MANIFEST_PATH, "r", encoding="utf-8-sig") as handle:
            cls.manifest = json.load(handle)

    def test_bundle_contains_only_deployable_files(self):
        expected = set(self.manifest["files"])
        actual = {
            name
            for name in os.listdir(BUNDLE)
            if os.path.isfile(os.path.join(BUNDLE, name))
        }
        self.assertEqual(actual, expected)
        self.assertEqual(
            sorted(name for name in actual if name.endswith(".py")),
            ["__init__.py"],
        )

    def test_bundle_and_sources_match_manifest(self):
        for name, metadata in self.manifest["files"].items():
            artifact = os.path.join(BUNDLE, name)
            source = os.path.join(ROOT, *metadata["source"].split("/"))
            with self.subTest(name=name):
                self.assertEqual(os.path.getsize(artifact), metadata["size"])
                self.assertEqual(sha256(artifact), metadata["sha256"])
                self.assertEqual(sha256(source), metadata["source_sha256"])

    def test_bytecode_uses_pinned_mpy_abi(self):
        expected_header = bytes.fromhex(self.manifest["target"]["mpy_header_hex"])
        for name in self.manifest["files"]:
            if not name.endswith(".mpy"):
                continue
            with self.subTest(name=name):
                with open(os.path.join(BUNDLE, name), "rb") as handle:
                    self.assertEqual(handle.read(4), expected_header)

    def test_target_is_the_tested_badge_runtime(self):
        target = self.manifest["target"]
        self.assertEqual(target["micropython"], "1.23.0")
        self.assertEqual(target["mpy_abi"], "6.3")
        self.assertEqual(target["architecture"], "armv7m")


if __name__ == "__main__":
    unittest.main()
