"""验签功能：Ed25519 公钥加载与签名验证。

安全边界说明：
- 公钥内嵌/部署于客户端，禁止从网络或清单中获取；
- 验签失败一律拒绝执行（fail-closed）。
"""

from __future__ import annotations

import logging
import os
from typing import Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger(__name__)

PublicKeyLike = Union[Ed25519PublicKey, bytes, str]
PrivateKeyLike = Union[Ed25519PrivateKey, bytes, str]


def load_public_key(key: PublicKeyLike) -> Ed25519PublicKey:
    """加载 Ed25519 公钥，接受密钥对象 / 原始字节 / 文件路径。"""
    if isinstance(key, Ed25519PublicKey):
        return key
    if isinstance(key, str):
        return _load_from_file(key, is_private=False)
    if isinstance(key, (bytes, bytearray)):
        return Ed25519PublicKey.from_public_bytes(bytes(key))
    raise TypeError(f"无法识别的公钥类型: {type(key)!r}")


def load_private_key(key: PrivateKeyLike) -> Ed25519PrivateKey:
    """加载 Ed25519 私钥（用于 demo / 测试签名，客户端产品代码不应持有私钥）。"""
    if isinstance(key, Ed25519PrivateKey):
        return key
    if isinstance(key, str):
        return _load_from_file(key, is_private=True)
    if isinstance(key, (bytes, bytearray)):
        return Ed25519PrivateKey.from_private_bytes(bytes(key))
    raise TypeError(f"无法识别的私钥类型: {type(key)!r}")


def _load_from_file(path: str, is_private: bool) -> "Any":
    if not os.path.isfile(path):
        raise FileNotFoundError(f"密钥文件不存在: {path}")
    with open(path, "rb") as f:
        data = f.read()  # 原始二进制密钥，禁止 strip（可能截断密钥字节）
    if is_private:
        return Ed25519PrivateKey.from_private_bytes(data)
    return Ed25519PublicKey.from_public_bytes(data)


def generate_keypair(public_path: str, private_path: str) -> None:
    """生成 Ed25519 密钥对并写入文件（仅测试与 demo 使用）。"""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    _write_private_key(private_key, private_path)
    _write_public_key(public_key, public_path)
    logger.info("已生成密钥对: pub=%s priv=%s", public_path, private_path)


def _write_private_key(key: Ed25519PrivateKey, path: str) -> None:
    data = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(path, "wb") as f:
        f.write(data)


def _write_public_key(key: Ed25519PublicKey, path: str) -> None:
    data = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    with open(path, "wb") as f:
        f.write(data)


def public_key_bytes(key: Ed25519PublicKey) -> bytes:
    """导出公钥原始字节（供分发/校验）。"""
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def verify(data: bytes, signature: bytes, public_key: PublicKeyLike) -> bool:
    """验证 Ed25519 签名，任何异常均视为验证失败（fail-closed）。"""
    import hashlib

    try:
        key = load_public_key(public_key)
        key.verify(signature, data)
        return True
    except InvalidSignature:
        # 失败诊断：公钥指纹 + 数据哈希 + 签名指纹，便于与服务端签名日志对账
        logger.warning(
            "签名不匹配: 公钥指纹=%s 数据sha256=%s 签名长度=%d",
            _fingerprint(key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )),
            hashlib.sha256(data).hexdigest()[:16],
            len(signature),
        )
        return False
    except Exception as e:  # 边界防御：验签路径不允许任何未捕获异常泄露
        logger.error("验签过程异常: %s", e)
        return False


def _fingerprint(raw: bytes) -> str:
    """公钥/数据指纹（sha256 前 16 位）。"""
    import hashlib

    return hashlib.sha256(raw).hexdigest()[:16]


def sign(data: bytes, private_key: PrivateKeyLike) -> bytes:
    """对数据签名（仅测试与 demo 使用）。"""
    key = load_private_key(private_key)
    return key.sign(data)


__all__ = [
    "load_public_key", "load_private_key", "generate_keypair",
    "public_key_bytes", "verify", "sign",
]
