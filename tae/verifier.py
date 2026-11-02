"""哈希校验工具：文件哈希计算与比对，供下载器与动作实现共用。"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

from .models import HashInfo

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 64 * 1024


def hash_file(path: str, algo: str = "sha256") -> str:
    """计算文件哈希（十六进制小写）。文件不存在抛 OSError。"""
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_file_hash(path: str, hash_info: Optional[HashInfo]) -> bool:
    """校验文件哈希；无哈希要求时视为通过。"""
    if hash_info is None or not hash_info.value:
        return True
    algo = hash_info.algo or "sha256"
    if algo not in hashlib.algorithms_available:
        logger.error("不支持的哈希算法: %s", algo)
        return False
    try:
        return hash_file(path, algo).lower() == hash_info.value.lower()
    except OSError as e:
        logger.error("读取文件失败 %s: %s", path, e)
        return False


def file_matches(
    path: str,
    expected_hash: Optional[HashInfo],
    expected_size: Optional[int] = None,
) -> bool:
    """文件是否存在且哈希与大小均匹配。"""
    if not os.path.isfile(path):
        return False
    if expected_size is not None and os.path.getsize(path) != expected_size:
        return False
    return verify_file_hash(path, expected_hash)


__all__ = ["hash_file", "verify_file_hash", "file_matches"]
