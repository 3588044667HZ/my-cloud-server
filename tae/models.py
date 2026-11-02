"""数据模型：动作 (Action)、清单 (ActionList)、下载项、事件。

所有模型均以 dataclass 承载，JSON 解析通过各自的 ``from_json`` 完成。
动作清单顶层携带 ``signature``（base64），验签时对"不含签名字段的规范 JSON
（键排序 + 紧凑格式）"做 Ed25519 验证，见 :meth:`ActionList.canonical_bytes`。
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .exceptions import (
    DependencyError,
    ManifestError,
    SignatureVerificationError,
    UnsupportedSchemaError,
)

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSION = "2.0"

# 时间格式：ISO 8601 (UTC)，与 action-schema.md 保持一致
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


@dataclass
class HashInfo:
    """哈希信息，algo 支持 sha256 / sha512 / md5 等 hashlib 支持的算法。"""

    algo: str = "sha256"
    value: str = ""

    @classmethod
    def from_json(cls, data: Any) -> "Optional[HashInfo]":
        if not data:
            return None
        if isinstance(data, str):
            # 兼容简化写法："sha256:abc..." 或直接是哈希值
            if ":" in data:
                algo, value = data.split(":", 1)
                return cls(algo=algo.lower(), value=value.lower())
            return cls(algo="sha256", value=data.lower())
        if isinstance(data, dict):
            return cls(
                algo=str(data.get("algo", "sha256")).lower(),
                value=str(data.get("value", "")).lower(),
            )
        raise ManifestError(f"无法解析哈希信息: {data!r}")


@dataclass
class DownloadItem:
    """单个下载源。priority 数字越小优先级越高。"""

    url: str
    priority: int = 1
    hash: Optional[HashInfo] = None
    size: Optional[int] = None

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "DownloadItem":
        try:
            return cls(
                url=str(data["url"]),
                priority=int(data.get("priority", 1)),
                hash=HashInfo.from_json(data.get("hash")),
                size=int(data.get("size")) if data.get("size") is not None else None,
            )
        except (KeyError, ValueError, TypeError) as e:
            raise ManifestError(f"下载源字段非法: {data!r} ({e})")


@dataclass
class DownloadSpec:
    """动作的下载描述：多源 URL + 期望哈希/大小。"""

    urls: List[DownloadItem] = field(default_factory=list)
    hash: Optional[HashInfo] = None
    size: Optional[int] = None

    @classmethod
    def from_json(cls, data: Any) -> "Optional[DownloadSpec]":
        if not data:
            return None
        if not isinstance(data, dict):
            raise ManifestError(f"download 字段必须是对象: {data!r}")
        try:
            urls = [DownloadItem.from_json(u) for u in data.get("urls", [])]
        except TypeError:
            raise ManifestError(f"download.urls 必须是数组: {data!r}")
        return cls(
            urls=sorted(urls, key=lambda u: u.priority),
            hash=HashInfo.from_json(data.get("hash")),
            size=int(data["size"]) if data.get("size") is not None else None,
        )

    @property
    def primary_hash(self) -> Optional[HashInfo]:
        """优先使用 download 级哈希，其次取第一个源的哈希。"""
        return self.hash or (self.urls[0].hash if self.urls else None)


@dataclass
class TargetSpec:
    """动作目标文件/目录信息。"""

    path: str
    hash: Optional[HashInfo] = None
    size: Optional[int] = None
    permissions: Optional[str] = None  # 八进制字符串，如 "0755"

    @classmethod
    def from_json(cls, data: Any) -> "Optional[TargetSpec]":
        if not data:
            return None
        if not isinstance(data, dict) or "path" not in data:
            raise ManifestError(f"target 必须包含 path: {data!r}")
        perms = data.get("permissions")
        return cls(
            path=str(data["path"]),
            hash=HashInfo.from_json(data.get("hash")),
            size=int(data["size"]) if data.get("size") is not None else None,
            permissions=str(perms) if perms is not None else None,
        )


@dataclass
class RollbackSpec:
    """回滚定义：文件动作走备份还原，进程动作走回滚命令。

    detach=True 时回滚命令以后台方式启动（不等待退出），用于回滚到
    "启动服务"类命令（如 start 命令的回滚链）。
    """

    backup_original: bool = False
    command: Optional[str] = None
    timeout: int = 30
    detach: bool = False
    log_file: str = ""  # detach 回滚命令的输出日志路径（空 = 丢弃）

    @classmethod
    def from_json(cls, data: Any) -> "Optional[RollbackSpec]":
        if not data:
            return None
        if isinstance(data, bool):
            # 兼容 "rollback": true 的简写
            return cls(backup_original=data)
        if not isinstance(data, dict):
            raise ManifestError(f"rollback 字段非法: {data!r}")
        return cls(
            backup_original=bool(data.get("backupOriginal", False)),
            command=data.get("command"),
            timeout=int(data.get("timeout", 30)),
            detach=bool(data.get("detach", False)),
            log_file=str(data.get("logFile", "")),
        )


@dataclass
class ManifestEntry:
    """归档解压后的文件清单项（archive.extract 的 manifest）。"""

    path: str
    hash: Optional[HashInfo] = None

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "ManifestEntry":
        try:
            return cls(path=str(data["path"]), hash=HashInfo.from_json(data.get("hash")))
        except (KeyError, ValueError) as e:
            raise ManifestError(f"manifest 项非法: {data!r} ({e})")


@dataclass
class Action:
    """动作基类：通用字段 + 类型分发解析。

    type 相关字段（target/download/command 等）放在各子类中。
    """

    id: str
    type: str
    description: str = ""
    depends_on: List[str] = field(default_factory=list)
    on_failure: str = "abort"  # abort / skip / rollback
    rollback: Optional[RollbackSpec] = None

    def validate(self) -> None:
        if self.on_failure not in ("abort", "skip", "rollback"):
            raise ManifestError(f"动作 {self.id} 的 onFailure 非法: {self.on_failure!r}")

    # ---- JSON 解析 ----

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Action":
        if not isinstance(data, dict):
            raise ManifestError(f"动作必须是对象: {data!r}")
        action_type = data.get("type")
        factory = _ACTION_FACTORIES.get(action_type)
        if factory is None:
            raise ManifestError(f"不支持的动作类型: {action_type!r}")
        try:
            return factory(data)
        except ManifestError:
            raise
        except (KeyError, ValueError, TypeError) as e:
            raise ManifestError(f"动作字段非法: {data!r} ({e})")


@dataclass
class FileAction(Action):
    """文件类动作（ensure / replace / add / delete）通用字段。"""

    target: TargetSpec = None  # type: ignore[assignment]
    download: Optional[DownloadSpec] = None


@dataclass
class FileEnsureAction(FileAction):
    """file.ensure：目标不存在则创建，存在且哈希不符则替换。"""


@dataclass
class FileReplaceAction(FileAction):
    """file.replace：目标必须存在，否则失败。"""


@dataclass
class FileAddAction(FileAction):
    """file.add：目标必须不存在，否则失败。"""


@dataclass
class FileDeleteAction(FileAction):
    """file.delete：删除目标文件，可选备份。"""


@dataclass
class ArchiveExtractAction(Action):
    """archive.extract：下载归档 → 校验 → 解压 → 逐文件校验清单 → 原子安装。"""

    target: TargetSpec = None  # type: ignore[assignment]
    download: Optional[DownloadSpec] = None
    strip_components: int = 0
    manifest: List[ManifestEntry] = field(default_factory=list)


@dataclass
class ProcessExecAction(Action):
    """process.exec：下载脚本（可选）→ 校验 → 执行命令。

    cwd 指定命令的工作目录（如部署目标目录），空字符串表示继承执行器
    进程的工作目录。
    detach=True 时命令以后台方式启动（如启动常驻服务），立即返回成功，
    由后续动作（如健康检查）确认就绪。
    """

    command: str = ""
    timeout: int = 60
    environment: Dict[str, str] = field(default_factory=dict)
    download: Optional[DownloadSpec] = None
    cwd: str = ""
    detach: bool = False
    log_file: str = ""  # detach 模式下命令输出落盘路径（空 = 丢弃）


@dataclass
class SystemRebootAction(Action):
    """system.reboot：MVP 阶段仅记录日志，不真正重启。"""

    delay: int = 5


@dataclass
class SystemWaitAction(Action):
    """system.wait：MVP 阶段仅记录日志，不实现条件等待。"""

    condition: str = ""
    target: str = ""


@dataclass
class WaitForHealthAction(Action):
    """system.wait_for_health：轮询健康检查 URL，直到返回 2xx 或超时。

    用于部署流程末尾验证服务可用（stage2 热更新场景）。
    """

    url: str = ""
    timeout: int = 60


def _build_file_action(action_class: type) -> "Any":
    def _parse(data: Dict[str, Any]) -> Action:
        base = _parse_base(data)
        target = TargetSpec.from_json(data.get("target"))
        if target is None:
            raise ManifestError(f"动作 {data.get('id')!r} 缺少 target")
        return action_class(
            **base,
            target=target,
            download=DownloadSpec.from_json(data.get("download")),
        )

    return _parse


def _parse_base(data: Dict[str, Any]) -> Dict[str, Any]:
    """解析动作通用字段。"""
    try:
        action_id = str(data["id"])
        rollback = RollbackSpec.from_json(data.get("rollback"))
        return dict(
            id=action_id,
            type=str(data["type"]),
            description=str(data.get("description", "")),
            depends_on=[str(d) for d in data.get("dependsOn", [])],
            on_failure=str(data.get("onFailure", "abort")),
            rollback=rollback,
        )
    except KeyError as e:
        raise ManifestError(f"动作缺少必需字段: {e}")


def _parse_archive(data: Dict[str, Any]) -> Action:
    base = _parse_base(data)
    target = TargetSpec.from_json(data.get("target"))
    if target is None:
        raise ManifestError(f"动作 {data.get('id')!r} 缺少 target")
    download = DownloadSpec.from_json(data.get("download"))
    if download is None:
        raise ManifestError(f"动作 {data.get('id')!r} 缺少 download")
    manifest_raw = data.get("manifest", [])
    if not isinstance(manifest_raw, list):
        raise ManifestError(f"动作 {data.get('id')!r} 的 manifest 必须是数组")
    return ArchiveExtractAction(
        **base,
        target=target,
        download=download,
        strip_components=int(data.get("stripComponents", 0)),
        manifest=[ManifestEntry.from_json(m) for m in manifest_raw],
    )


def _parse_process(data: Dict[str, Any]) -> Action:
    base = _parse_base(data)
    command = data.get("command")
    if not command:
        raise ManifestError(f"动作 {data.get('id')!r} 缺少 command")
    env = data.get("environment", {})
    if not isinstance(env, dict):
        raise ManifestError(f"动作 {data.get('id')!r} 的 environment 必须是对象")
    return ProcessExecAction(
        **base,
        command=str(command),
        timeout=int(data.get("timeout", 60)),
        environment={str(k): str(v) for k, v in env.items()},
        download=DownloadSpec.from_json(data.get("download")),
        cwd=str(data.get("cwd", "")),
        detach=bool(data.get("detach", False)),
        log_file=str(data.get("logFile", "")),
    )


def _parse_reboot(data: Dict[str, Any]) -> Action:
    return SystemRebootAction(
        **_parse_base(data), delay=int(data.get("delay", 5))
    )


def _parse_wait(data: Dict[str, Any]) -> Action:
    return SystemWaitAction(
        **_parse_base(data),
        condition=str(data.get("condition", "")),
        target=str(data.get("target", "")),
    )


def _parse_health_check(data: Dict[str, Any]) -> Action:
    base = _parse_base(data)
    url = data.get("target")
    if not isinstance(url, str) or not url.strip():
        raise ManifestError(f"动作 {data.get('id')!r} 的 target 必须是健康检查 URL")
    return WaitForHealthAction(
        **base,
        url=url.strip(),
        timeout=int(data.get("timeout", 60)),
    )


_ACTION_FACTORIES: Dict[str, "Any"] = {
    "file.ensure": _build_file_action(FileEnsureAction),
    "file.replace": _build_file_action(FileReplaceAction),
    "file.add": _build_file_action(FileAddAction),
    "file.delete": _build_file_action(FileDeleteAction),
    "archive.extract": _parse_archive,
    "process.exec": _parse_process,
    "system.reboot": _parse_reboot,
    "system.wait": _parse_wait,
    "system.wait_for_health": _parse_health_check,
}


@dataclass
class ActionList:
    """动作清单。签名域 ``signature`` 不参与规范 JSON 序列化。"""

    schema_version: str
    update_id: str
    timestamp: str
    expires_at: str
    target_version: str
    actions: List[Action]
    min_client_version: str = ""
    signature: Optional[str] = None

    # ---- JSON 解析 ----

    @classmethod
    def from_json(cls, json_str: "Any") -> "ActionList":
        # 边界防御：允许 bytes 输入（HTTP 响应字节直接传入），
        # 统一按 UTF-8 解码，避免上层编码猜测导致内容漂移
        if isinstance(json_str, (bytes, bytearray)):
            json_str = bytes(json_str).decode("utf-8")
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ManifestError(f"清单不是合法 JSON: {e}")
        if not isinstance(data, dict):
            raise ManifestError("清单顶层必须是对象")
        required = ("schemaVersion", "updateId", "timestamp", "expiresAt",
                    "targetVersion", "actions")
        for key in required:
            if key not in data:
                raise ManifestError(f"清单缺少必需字段: {key}")
        if data["schemaVersion"] != SUPPORTED_SCHEMA_VERSION:
            raise UnsupportedSchemaError(
                f"不支持的 schemaVersion: {data['schemaVersion']}",
                update_id=str(data["updateId"]),
            )
        actions_raw = data["actions"]
        if not isinstance(actions_raw, list):
            raise ManifestError("清单 actions 必须是数组")
        action_list = cls(
            schema_version=str(data["schemaVersion"]),
            update_id=str(data["updateId"]),
            timestamp=str(data["timestamp"]),
            expires_at=str(data["expiresAt"]),
            target_version=str(data["targetVersion"]),
            actions=[Action.from_json(a) for a in actions_raw],
            min_client_version=str(data.get("minClientVersion", "")),
            signature=data.get("signature"),
        )
        action_list._validate_ids()
        for action in action_list.actions:
            action.validate()
        return action_list

    @classmethod
    def from_file(cls, path: str) -> "ActionList":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_json(f.read())

    def _validate_ids(self) -> None:
        ids = [a.id for a in self.actions]
        if len(set(ids)) != len(ids):
            raise ManifestError("清单中存在重复的动作 id", update_id=self.update_id)
        id_set = set(ids)
        for a in self.actions:
            for dep in a.depends_on:
                if dep not in id_set:
                    raise DependencyError(
                        f"动作 {a.id} 依赖不存在的动作 {dep}", update_id=self.update_id
                    )

    # ---- 签名 ----

    def to_dict(self, with_signature: bool = True) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "updateId": self.update_id,
            "timestamp": self.timestamp,
            "expiresAt": self.expires_at,
            "targetVersion": self.target_version,
            "minClientVersion": self.min_client_version,
            "actions": [_action_to_dict(a) for a in self.actions],
            **({"signature": self.signature} if with_signature and self.signature else {}),
        }

    def to_json(self, with_signature: bool = True) -> str:
        return json.dumps(self.to_dict(with_signature=with_signature),
                          ensure_ascii=False, indent=2)

    def canonical_bytes(self) -> bytes:
        """验签用的规范字节：不含 signature 字段、键排序、紧凑格式。"""
        return json.dumps(
            self.to_dict(with_signature=False),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    def sign(self, private_key: "Any") -> str:
        """用私钥对规范字节签名，并把 base64 签名写回 self.signature。"""
        from . import security  # 延迟导入避免循环依赖

        raw = security.sign(self.canonical_bytes(), private_key)
        self.signature = base64.b64encode(raw).decode("ascii")
        return self.signature

    def validate_signature(self, public_key: "Any") -> bool:
        """校验签名。无 signature 字段时抛出 SignatureVerificationError。"""
        if not self.signature:
            raise SignatureVerificationError(
                "清单缺少 signature 字段", update_id=self.update_id
            )
        try:
            sig = base64.b64decode(self.signature)
        except ValueError as e:
            raise SignatureVerificationError(
                f"signature 不是合法 base64: {e}", update_id=self.update_id
            )
        from . import security

        if not security.verify(self.canonical_bytes(), sig, public_key):
            # 失败诊断：与 serve 端签名日志（canonical sha256）对比可定位内容差异
            import hashlib

            canonical = self.canonical_bytes()
            logger.error(
                "清单验签失败: update=%s canonical_sha256=%s signature_sha256=%s",
                self.update_id,
                hashlib.sha256(canonical).hexdigest()[:16],
                hashlib.sha256(sig).hexdigest()[:16],
            )
            raise SignatureVerificationError(
                "清单签名验证失败", update_id=self.update_id
            )
        return True

    # ---- 时效 ----

    def is_expired(self, now: Optional[str] = None) -> bool:
        try:
            exp = datetime.strptime(self.expires_at, TIME_FORMAT).replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning("清单 expiresAt 格式无法解析: %s", self.expires_at)
            return True
        if now is None:
            now_dt = datetime.now(timezone.utc)
        else:
            now_dt = datetime.strptime(now, TIME_FORMAT).replace(tzinfo=timezone.utc)
        return now_dt >= exp


def _action_to_dict(action: Action) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "id": action.id,
        "type": action.type,
        "description": action.description,
        "dependsOn": action.depends_on,
        "onFailure": action.on_failure,
    }
    if action.rollback:
        rb: Dict[str, Any] = {}
        if action.rollback.backup_original:
            rb["backupOriginal"] = True
        if action.rollback.command:
            rb["command"] = action.rollback.command
            rb["timeout"] = action.rollback.timeout
            if action.rollback.detach:
                rb["detach"] = True
            if action.rollback.log_file:
                rb["logFile"] = action.rollback.log_file
        # 空回滚定义（如 command=""）序列化为 {}，而 {} 反序列化会变为 None，
        # 造成签名端/验签端规范化字节不一致——因此仅在有实际内容时输出
        if rb:
            d["rollback"] = rb
    if isinstance(action, ArchiveExtractAction):
        # 注意：ArchiveExtractAction 不是 FileAction，需独立序列化分支
        t = {"path": action.target.path}
        if action.target.permissions:
            t["permissions"] = action.target.permissions
        d["target"] = t
        if action.download:
            d["download"] = _download_to_dict(action.download)
        d["stripComponents"] = action.strip_components
        d["manifest"] = [
            {"path": m.path,
             **({"hash": {"algo": m.hash.algo, "value": m.hash.value}} if m.hash else {})}
            for m in action.manifest
        ]
    elif isinstance(action, FileAction):
        t = {"path": action.target.path}
        if action.target.hash:
            t["hash"] = {"algo": action.target.hash.algo, "value": action.target.hash.value}
        if action.target.size is not None:
            t["size"] = action.target.size
        if action.target.permissions:
            t["permissions"] = action.target.permissions
        d["target"] = t
        if action.download:
            d["download"] = _download_to_dict(action.download)
    elif isinstance(action, ProcessExecAction):
        d["command"] = action.command
        d["timeout"] = action.timeout
        if action.environment:
            d["environment"] = action.environment
        if action.download:
            d["download"] = _download_to_dict(action.download)
        if action.cwd:
            d["cwd"] = action.cwd
        if action.detach:
            d["detach"] = True
        if action.log_file:
            d["logFile"] = action.log_file
    elif isinstance(action, SystemRebootAction):
        d["delay"] = action.delay
    elif isinstance(action, SystemWaitAction):
        d["condition"] = action.condition
        d["target"] = action.target
    elif isinstance(action, WaitForHealthAction):
        d["target"] = action.url
        d["timeout"] = action.timeout
    return d


def _download_to_dict(spec: DownloadSpec) -> Dict[str, Any]:
    d: Dict[str, Any] = {"urls": [
        {"url": u.url, "priority": u.priority} for u in spec.urls
    ]}
    if spec.hash:
        d["hash"] = {"algo": spec.hash.algo, "value": spec.hash.value}
    if spec.size is not None:
        d["size"] = spec.size
    return d


@dataclass
class Event:
    """上报事件结构。"""

    event_type: str
    device_uuid: str
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_iso_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "device_uuid": self.device_uuid,
            "payload": self.payload,
            "timestamp": self.timestamp,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


__all__ = [
    "HashInfo", "DownloadItem", "DownloadSpec", "TargetSpec", "RollbackSpec",
    "ManifestEntry", "Action", "FileAction", "FileEnsureAction", "FileReplaceAction",
    "FileAddAction", "FileDeleteAction", "ArchiveExtractAction", "ProcessExecAction",
    "SystemRebootAction", "SystemWaitAction", "WaitForHealthAction",
    "ActionList", "Event",
    "SUPPORTED_SCHEMA_VERSION", "TIME_FORMAT",
]
