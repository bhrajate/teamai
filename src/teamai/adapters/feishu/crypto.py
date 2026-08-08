"""飞书 HTTP 回调的加解密与验签。

lark-oapi 的 dispatcher.do() 内部完成解密 → token 校验 → challenge 直返 → 验签，
但那是同步路径、且要绑 Flask 适配；本模块把其中两个纯函数拆出来（实现与 SDK
源码逐一对照，见 `lark_oapi/core/utils/decryptor.py` 与 `event/dispatcher_handler.py`
的 `_verify_sign`），供 FastAPI 路由在全程 async 的路径上自行调用。

- `decrypt`：key = sha256(encrypt_key)，AES-256-CBC，密文 base64 解码后前
  16 字节为 IV，去 PKCS7 padding。
- `verify_sign`：`sha256(timestamp + nonce + encrypt_key + body).hexdigest()`
  与 `X-Lark-Signature` 常数时间比对。
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def decrypt(encrypt_key: str, ciphertext_b64: str) -> str:
    """解密 Encrypt Key 加密的事件体，返回明文 JSON 字符串。"""
    raw = base64.b64decode(ciphertext_b64)
    iv, ciphertext = raw[:16], raw[16:]
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


def verify_sign(timestamp: str, nonce: str, encrypt_key: str, body: str, signature: str) -> bool:
    """校验 `X-Lark-Signature`。签名缺失/不匹配一律返回 False。"""
    expected = hashlib.sha256(
        (timestamp + nonce + encrypt_key).encode("utf-8") + body.encode("utf-8")
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
