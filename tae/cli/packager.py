"""项目打包：tar.gz + .taeignore 忽略规则。

.taeignore 支持 gitignore 基本子集：
- 空行与 # 注释
- 以 / 结尾的规则只匹配目录（整棵子树排除）
- 以 / 开头的规则锚定到项目根
- 含 / 的规则按相对根路径匹配；不含 / 的规则匹配任意层级的同名路径段
- 通配符 *、?、[...]（fnmatch 语义，* 不跨 /）
- 以 ! 开头的规则取消忽略（后出现的规则覆盖先出现的）

打包保留文件权限（tarfile），输出 tar.gz 并返回 SHA256。
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import tarfile
import tempfile
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


def parse_ignore_file(path: str) -> List[str]:
    """读取忽略规则文件，返回规范化后的规则列表。"""
    if not os.path.isfile(path):
        return []
    patterns: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    return patterns


def is_ignored(rel_path: str, is_dir: bool, patterns: List[str]) -> bool:
    """判断相对路径是否被忽略；后出现的规则覆盖先出现的（支持 ! 取反）。"""
    rel = rel_path.replace(os.sep, "/").rstrip("/")
    ignored = False
    for raw in patterns:
        negate = raw.startswith("!")
        pattern = raw[1:].strip() if negate else raw.strip()
        dir_only = pattern.endswith("/")
        pattern = pattern.rstrip("/")
        if not pattern:
            continue
        if dir_only and not is_dir:
            # 目录规则不适用于文件本身；但文件位于被忽略目录内由 walk 提前剪枝
            continue
        if _match_pattern(pattern, rel):
            ignored = not negate
    return ignored


def _match_pattern(pattern: str, rel: str) -> bool:
    """匹配单个规则。"""
    if pattern.startswith("/"):
        # 锚定根：只匹配相对根路径
        return fnmatch.fnmatchcase(rel, pattern.lstrip("/"))
    if "/" in pattern:
        # 含斜杠：按相对根路径匹配
        return fnmatch.fnmatchcase(rel, pattern)
    # 不含斜杠：匹配任意层级的路径段
    return any(fnmatch.fnmatchcase(segment, pattern) for segment in rel.split("/"))


def create_package(
    project_dir: str,
    ignore_file: Optional[str] = ".taeignore",
    output_path: Optional[str] = None,
) -> Tuple[str, str]:
    """打包项目目录，返回 (包路径, SHA256)。

    ignore_file 为 None 时不应用忽略规则；output_path 缺省写入临时文件。
    """
    project_dir = os.path.abspath(project_dir)
    if not os.path.isdir(project_dir):
        raise FileNotFoundError(f"项目目录不存在: {project_dir}")

    patterns: List[str] = []
    ignore_file_abs: Optional[str] = None
    if ignore_file:
        ignore_file_abs = os.path.join(project_dir, ignore_file)
        patterns = parse_ignore_file(ignore_file_abs)

    if output_path is None:
        fd, output_path = tempfile.mkstemp(prefix="tae-package-", suffix=".tar.gz")
        os.close(fd)

    added_files = 0
    with tarfile.open(output_path, "w:gz") as tar:
        for root, dirs, files in os.walk(project_dir):
            # 剪枝被忽略的目录（跳过整棵子树，避免二次扫描）
            dirs[:] = [
                d for d in dirs
                if not is_ignored(
                    os.path.relpath(os.path.join(root, d), project_dir), True, patterns
                )
            ]
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, project_dir)
                # 忽略规则文件本身是工具元数据，不进部署包（同 .gitignore 不进 git）
                if ignore_file_abs is not None and full == ignore_file_abs:
                    continue
                if is_ignored(rel, False, patterns):
                    continue
                tar.add(full, arcname=rel, recursive=False)
                added_files += 1

    package_hash = _sha256_file(output_path)
    logger.info(
        "打包完成: %s（%d 个文件，%.1f KB，sha256=%s…）",
        output_path, added_files, os.path.getsize(output_path) / 1024,
        package_hash[:12],
    )
    return output_path, package_hash


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = ["parse_ignore_file", "is_ignored", "create_package"]
