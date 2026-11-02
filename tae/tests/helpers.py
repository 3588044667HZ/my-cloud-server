"""测试共享工具：密钥生成、清单构建与签名、哈希计算。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from typing import Any, Dict, List, Optional

from tae import security
from tae.models import ActionList

FAR_FUTURE = "2099-12-31T23:59:59Z"


class TAETestCase(unittest.TestCase):
    """基类：为每个测试创建独立临时工作区。"""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="tae-test-")
        self.workspace = self._tmp
        self.pub_path = os.path.join(self._tmp, "key.pub")
        self.priv_path = os.path.join(self._tmp, "key.priv")
        security.generate_keypair(self.pub_path, self.priv_path)
        self.addCleanup(self._cleanup_workspace)

    def _cleanup_workspace(self) -> None:
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def path(self, *parts: str) -> str:
        return os.path.join(self.workspace, *parts)

    def write_file(self, rel_path: str, content: bytes) -> str:
        full = self.path(rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(content)
        return full

    def source_url(self, rel_path: str) -> str:
        """生成指向工作区内文件的 file:// URL（用作下载源）。"""
        full = os.path.abspath(self.path(rel_path))
        return "file:///" + full.replace("\\", "/")

    def sha256(self, rel_path: str) -> str:
        return sha256_file(self.path(rel_path))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(
    update_id: str,
    actions: List[Dict[str, Any]],
    *,
    target_version: str = "2.0.0",
    min_client_version: str = "",
    expires_at: str = FAR_FUTURE,
    timestamp: str = "2026-08-11T00:00:00Z",
    schema_version: str = "2.0",
) -> Dict[str, Any]:
    return {
        "schemaVersion": schema_version,
        "updateId": update_id,
        "timestamp": timestamp,
        "expiresAt": expires_at,
        "targetVersion": target_version,
        "minClientVersion": min_client_version,
        "actions": actions,
    }


def signed_action_list(
    manifest: Dict[str, Any], private_key_path: str
) -> ActionList:
    """构建 ActionList 并签名（模拟真实服务端路径）。"""
    action_list = ActionList.from_json(json.dumps(manifest))
    action_list.sign(security.load_private_key(private_key_path))
    return action_list


def file_ensure_action(
    action_id: str,
    target_path: str,
    target_hash: str,
    source_url: str,
    source_hash: Optional[str] = None,
    *,
    depends_on: Optional[List[str]] = None,
    on_failure: str = "abort",
    permissions: Optional[str] = None,
) -> Dict[str, Any]:
    """构造 file.ensure 动作字典。"""
    target: Dict[str, Any] = {"path": target_path}
    if target_hash:
        target["hash"] = {"algo": "sha256", "value": target_hash}
    if permissions:
        target["permissions"] = permissions
    action: Dict[str, Any] = {
        "id": action_id,
        "type": "file.ensure",
        "target": target,
        "download": {
            "urls": [
                {"url": source_url, "priority": 1},
            ],
            "hash": {"algo": "sha256", "value": source_hash or target_hash},
        },
        "rollback": {"backupOriginal": True},
    }
    if depends_on:
        action["dependsOn"] = depends_on
    if on_failure != "abort":
        action["onFailure"] = on_failure
    return action


def file_add_action(
    action_id: str,
    target_path: str,
    source_url: str,
    source_hash: str,
    *,
    depends_on: Optional[List[str]] = None,
) -> Dict[str, Any]:
    action: Dict[str, Any] = {
        "id": action_id,
        "type": "file.add",
        "target": {"path": target_path},
        "download": {
            "urls": [{"url": source_url, "priority": 1}],
            "hash": {"algo": "sha256", "value": source_hash},
        },
    }
    if depends_on:
        action["dependsOn"] = depends_on
    return action


def file_delete_action(
    action_id: str,
    target_path: str,
    *,
    depends_on: Optional[List[str]] = None,
    backup_original: bool = True,
) -> Dict[str, Any]:
    action: Dict[str, Any] = {
        "id": action_id,
        "type": "file.delete",
        "target": {"path": target_path},
    }
    if backup_original:
        action["rollback"] = {"backupOriginal": True}
    if depends_on:
        action["dependsOn"] = depends_on
    return action


def process_exec_action(
    action_id: str,
    command: str,
    *,
    timeout: int = 60,
    on_failure: str = "abort",
    depends_on: Optional[List[str]] = None,
    rollback_command: Optional[str] = None,
) -> Dict[str, Any]:
    action: Dict[str, Any] = {
        "id": action_id,
        "type": "process.exec",
        "command": command,
        "timeout": timeout,
    }
    if on_failure != "abort":
        action["onFailure"] = on_failure
    if depends_on:
        action["dependsOn"] = depends_on
    if rollback_command:
        action["rollback"] = {"command": rollback_command, "timeout": 30}
    return action
