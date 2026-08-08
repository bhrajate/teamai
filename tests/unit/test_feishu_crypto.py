"""飞书回调加解密与验签。

解密对已知密文（fixture 用同样的密钥派生独立生成，非自证自）；验签对
正确/错误/缺失签名各一例；challenge 早于验签的时序在 callback 路由测试里断言。
"""

from __future__ import annotations

from teamai.adapters.feishu.crypto import decrypt, verify_sign

ENCRYPT_KEY = "test-encrypt-key"
# 明文：{"type":"url_verification","challenge":"ajls384kdjx98XX","token":"test-token"}
KNOWN_CIPHERTEXT = (
    "Ri6WGSuGhHtsX7l27CkX1qUfIr52mq/XGfnzPnIdFB6CShcNHFu2/n+i7lqd0Vfk24p4GOvW8L7PfYxIWUxbDYAiqbc"
    "KT6WBoSGC86srjJavUTJAYGdLKGojkLT/BJBj"
)
KNOWN_SIGNATURE = "a0b54ae307272f23b84053fc75d45d78704e5c9b29f835f85f2ca4b4add54460"
BODY = '{"type":"url_verification","challenge":"ajls384kdjx98XX","token":"test-token"}'


class TestDecrypt:
    def test_对已知密文解出明文(self) -> None:
        assert decrypt(ENCRYPT_KEY, KNOWN_CIPHERTEXT) == BODY

    def test_错误key解不出正确明文(self) -> None:
        """错误 key 解出的字节无有效 PKCS7 padding，抛 ValueError 而非出伪明文。"""
        import pytest

        with pytest.raises(ValueError):
            decrypt("another-key", KNOWN_CIPHERTEXT)


class TestVerifySign:
    def test_正确签名通过(self) -> None:
        assert verify_sign("1710000000", "nonce123", ENCRYPT_KEY, BODY, KNOWN_SIGNATURE) is True

    def test_错误签名不通过(self) -> None:
        assert verify_sign("1710000000", "nonce123", ENCRYPT_KEY, BODY, "0" * 64) is False

    def test_签名缺失不通过(self) -> None:
        assert verify_sign("1710000000", "nonce123", ENCRYPT_KEY, BODY, "") is False

    def test_时间戳被篡改不通过(self) -> None:
        """重放防护依赖 timestamp，篡改后签名即失效。"""
        assert verify_sign("1700000000", "nonce123", ENCRYPT_KEY, BODY, KNOWN_SIGNATURE) is False
