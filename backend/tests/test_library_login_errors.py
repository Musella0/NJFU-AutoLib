import unittest
import sys
import types
from unittest.mock import Mock, patch

# 这些测试只覆盖登录响应的错误分类，不依赖数据库、VPN 或真实加密库。
fake_pymongo = types.ModuleType("pymongo")
fake_pymongo.MongoClient = Mock(return_value=Mock())
fake_pymongo.ASCENDING = 1
fake_pymongo.DESCENDING = -1
sys.modules.setdefault("pymongo", fake_pymongo)

fake_config = types.ModuleType("utils.config")
fake_config.get_mongo_uri = Mock(return_value="mongodb://test")
sys.modules.setdefault("utils.config", fake_config)

fake_encryptor_module = types.ModuleType("utils.password_encryptor")


class FakePasswordEncryptor:
    set_public_key = Mock(return_value="key")
    encrypt_with_public_key = Mock(return_value="encrypted")


fake_encryptor_module.PasswordEncryptor = FakePasswordEncryptor
sys.modules.setdefault("utils.password_encryptor", fake_encryptor_module)

fake_vpn_module = types.ModuleType("utils.vpn_system")
fake_vpn_module.VPNSystem = Mock()
sys.modules.setdefault("utils.vpn_system", fake_vpn_module)

from utils.library_system import (
    LibraryCredentialsError,
    LibraryLoginError,
    LibrarySystem,
)


class LibraryLoginErrorTests(unittest.TestCase):
    def make_library(self, response):
        library = LibrarySystem.__new__(LibrarySystem)
        library.username = "12345678"
        library.password = "secret"
        library.login_url = "https://example.test/login"
        library.session = Mock()
        library.session.post.return_value = response
        return library

    @patch("utils.library_system.PasswordEncryptor.set_public_key", return_value="key")
    @patch("utils.library_system.PasswordEncryptor.encrypt_with_public_key", return_value="encrypted")
    def test_code_301_keeps_server_authentication_error(self, _encrypt, _set_key):
        response = Mock(status_code=200)
        response.json.return_value = {"code": 301, "message": "认证失败，请联系管理员"}
        library = self.make_library(response)

        with self.assertRaises(LibraryLoginError) as raised:
            library._perform_login("public-key", "nonce")

        self.assertNotIsInstance(raised.exception, LibraryCredentialsError)
        self.assertEqual(str(raised.exception), "code 301：认证失败，请联系管理员")
        self.assertFalse(raised.exception.is_credentials_error)

    @patch("utils.library_system.PasswordEncryptor.set_public_key", return_value="key")
    @patch("utils.library_system.PasswordEncryptor.encrypt_with_public_key", return_value="encrypted")
    def test_explicit_password_error_is_classified_as_credentials_error(self, _encrypt, _set_key):
        response = Mock(status_code=200)
        response.json.return_value = {"code": 400, "message": "用户名或密码错误"}
        library = self.make_library(response)

        with self.assertRaises(LibraryCredentialsError) as raised:
            library._perform_login("public-key", "nonce")

        self.assertTrue(raised.exception.is_credentials_error)

    @patch("utils.library_system.PasswordEncryptor.set_public_key", return_value="key")
    @patch("utils.library_system.PasswordEncryptor.encrypt_with_public_key", return_value="encrypted")
    def test_http_error_is_not_classified_as_password_error(self, _encrypt, _set_key):
        library = self.make_library(Mock(status_code=503))

        with self.assertRaises(LibraryLoginError) as raised:
            library._perform_login("public-key", "nonce")

        self.assertFalse(raised.exception.is_credentials_error)
        self.assertEqual(str(raised.exception), "登录请求失败：HTTP 503")


if __name__ == "__main__":
    unittest.main()
