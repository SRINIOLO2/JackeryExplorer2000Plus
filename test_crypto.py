#!/usr/bin/env python3
import unittest
from jackery_api import JackeryAPI

class TestJackeryCrypto(unittest.TestCase):
    def setUp(self):
        self.api = JackeryAPI("test@example.com", "secretpass")

    def test_udid_generation(self):
        udid = self.api._generate_udid()
        self.assertTrue(udid.startswith("2") or udid.startswith("9"))
        self.assertEqual(len(udid), 33)  # 1 prefix + 32-char hex UUID

    def test_aes_encryption(self):
        payload = '{"test": "data"}'
        key = b"1234567890123456"
        encrypted = self.api._encrypt_with_aes(payload, key)
        self.assertTrue(len(encrypted) > 0)

    def test_rsa_encryption(self):
        public_key_b64 = "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCVmzgJy/4XolxPnkfu32YtJqYGFLYqf9/rnVgURJED+8J9J3Pccd6+9L97/+7COZE5OkejsgOkqeLNC9C3r5mhpE4zk/HStss7Q8/5DqkGD1annQ+eoICo3oi0dITZ0Qll56Dowb8lXi6WHViVDdih/oeUwVJY89uJNtTWrz7t7QIDAQAB"
        aes_key = b"1234567890123456"
        encrypted = self.api._encrypt_with_rsa(aes_key, public_key_b64)
        self.assertTrue(len(encrypted) > 0)

if __name__ == "__main__":
    unittest.main()
