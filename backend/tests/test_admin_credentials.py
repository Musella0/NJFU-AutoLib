import os
import stat
import tempfile
import unittest

from utils import admin_credentials
from utils import crypto


class AdminCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.old_path = admin_credentials.ADMIN_CREDENTIALS_FILE
        self.old_key = crypto._KEY
        admin_credentials.ADMIN_CREDENTIALS_FILE = os.path.join(
            self.temp_dir.name,
            "admin_credentials.enc",
        )
        crypto._KEY = bytes.fromhex("11" * 32)
        self.addCleanup(self._restore_globals)

    def _restore_globals(self):
        admin_credentials.ADMIN_CREDENTIALS_FILE = self.old_path
        crypto._KEY = self.old_key

    def test_password_policy(self):
        self.assertIsNotNone(admin_credentials.validate_password("short1"))
        self.assertIsNotNone(admin_credentials.validate_password("onlyletters"))
        self.assertIsNotNone(admin_credentials.validate_password("12345678"))
        self.assertIsNone(admin_credentials.validate_password("Strong123"))

    def test_credentials_are_encrypted_and_login_can_be_verified(self):
        record = admin_credentials.save_credentials(
            "admin.user",
            "admin@example.com",
            "Strong123",
        )

        with open(admin_credentials.ADMIN_CREDENTIALS_FILE, encoding="utf-8") as f:
            encrypted = f.read()
        self.assertTrue(encrypted.startswith("enc:"))
        self.assertNotIn("admin.user", encrypted)
        self.assertNotIn("admin@example.com", encrypted)
        self.assertNotIn("Strong123", encrypted)
        mode = stat.S_IMODE(os.stat(admin_credentials.ADMIN_CREDENTIALS_FILE).st_mode)
        self.assertEqual(mode, 0o600)

        valid, loaded = admin_credentials.verify_login("admin.user", "Strong123")
        self.assertTrue(valid)
        self.assertEqual(loaded["auth_version"], record["auth_version"])
        self.assertFalse(admin_credentials.verify_login("admin.user", "wrong123")[0])
        self.assertFalse(admin_credentials.verify_login("other", "Strong123")[0])

    def test_password_reset_rotates_session_version(self):
        original = admin_credentials.save_credentials(
            "admin",
            "admin@example.com",
            "Strong123",
        )
        updated = admin_credentials.update_password(original, "NewPass456")

        self.assertNotEqual(original["auth_version"], updated["auth_version"])
        self.assertFalse(admin_credentials.verify_login("admin", "Strong123")[0])
        self.assertTrue(admin_credentials.verify_login("admin", "NewPass456")[0])


if __name__ == "__main__":
    unittest.main()
