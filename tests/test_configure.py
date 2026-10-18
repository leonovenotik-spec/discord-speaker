import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest


spec = importlib.util.spec_from_file_location(
    "configure", Path(__file__).resolve().parents[1] / "scripts/configure.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config/bot.env"

    def test_writes_expected_environment(self):
        configure.write_config(self.path, "example.token.for.tests", "123456789012345678")
        self.assertEqual(self.path.read_text(),
                         "DISCORD_TOKEN=example.token.for.tests\nGUILD_ID=123456789012345678\n")

    def test_does_not_overwrite_existing_secret(self):
        configure.write_config(self.path, "original.example.token", "123456789012345678")
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            configure.write_config(self.path, "replacement.example.token", "123456789012345678")
        self.assertEqual(self.path.read_bytes(), before)

    def test_rejects_environment_line_injection(self):
        with self.assertRaises(ValueError):
            configure.write_config(self.path, "example\nGUILD_ID=other", "123456789012345678")
        self.assertFalse(self.path.exists())

    @unittest.skipUnless(os.name == "posix", "Linux file permissions")
    def test_credentials_are_owner_only(self):
        configure.write_config(self.path, "example.token.for.tests", "123456789012345678")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
