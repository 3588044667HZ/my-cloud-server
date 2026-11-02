"""动作实现：ActionExecutor 抽象基类与具体动作。

回滚约定（文件动作）：
- 替换已存在文件前，将原文件备份为 ``<target>.tae-bak``；
- 若目标原本不存在（新增文件），创建空标记文件 ``<target>.tae-new``；
- 回滚时：有备份则还原，有标记则删除目标。

所有文件替换通过 hooks.atomic_replace 保证原子性。
"""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import tempfile
import time
import zipfile
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

import requests

from .downloader import DownloadManager
from .exceptions import ActionExecutionError, ManifestError
from .hooks import PlatformHooks
from .models import (
    ArchiveExtractAction as _ArchiveExtractActionModel,
)
from .models import (
    FileAddAction as _FileAddActionModel,
)
from .models import (
    FileDeleteAction as _FileDeleteActionModel,
)
from .models import (
    FileEnsureAction as _FileEnsureActionModel,
)
from .models import (
    FileReplaceAction as _FileReplaceActionModel,
)
from .models import (
    ProcessExecAction as _ProcessExecActionModel,
)
from .models import (
    Action, DownloadSpec, FileAction, HashInfo, RollbackSpec, SystemRebootAction,
    SystemWaitAction, TargetSpec, WaitForHealthAction,
)
from .state import StateStore
from .verifier import verify_file_hash  # noqa: F401  (对外复用导出)

logger = logging.getLogger(__name__)

_BACKUP_SUFFIX = ".tae-bak"
_NEW_MARKER_SUFFIX = ".tae-new"
_MIN_REQUIRED_BYTES = 1 * 1024 * 1024  # 无法估算大小时按 1MB 保守计算


# ---- 通用工具 ----

def backup_path(path: str) -> str:
    return path + _BACKUP_SUFFIX


def new_marker_path(path: str) -> str:
    return path + _NEW_MARKER_SUFFIX


def resolve_permissions(perms: Optional[str]) -> Optional[int]:
    """把 "0755" 之类的八进制字符串转为整数权限位。"""
    if perms is None:
        return None
    try:
        return int(perms, 8)
    except ValueError:
        logger.warning("非法权限字符串: %r", perms)
        return None


def _ensure_parent_dirs(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


class ActionExecutor(ABC):
    """动作执行器抽象基类。"""

    def __init__(
        self,
        action: Action,
        hooks: PlatformHooks,
        downloader: DownloadManager,
        state: StateStore,
        update_id: str,
    ):
        self.action = action
        self.hooks = hooks
        self.downloader = downloader
        self.state = state
        self.update_id = update_id

    # ---- 生命周期 ----

    @abstractmethod
    def can_execute(self) -> bool:
        """执行前置条件检查（依赖完成、磁盘空间等）。"""

    @abstractmethod
    def execute(self) -> bool:
        """执行动作，返回成功/失败。"""

    @abstractmethod
    def rollback(self) -> bool:
        """执行回滚。无回滚需求时直接返回 True。"""

    def has_rollback(self) -> bool:
        """该动作是否具备可执行的回滚。"""
        return self.action.rollback is not None

    # ---- 共享工具 ----

    def _check_disk_space(self, target_path: str, required: Optional[int]) -> bool:
        if required is None:
            required = _MIN_REQUIRED_BYTES
        parent = target_path if os.path.isdir(target_path) else os.path.dirname(target_path)
        free = self.hooks.get_free_disk_space(parent or os.getcwd())
        if free < 0:
            return True  # 无法获取磁盘信息时不阻断
        if free < required:
            logger.error(
                "磁盘空间不足: 需要 %d 字节，剩余 %d 字节 (%s)", required, free, parent
            )
            return False
        return True

    def _download_to_cache(self, spec: DownloadSpec, action_id: str) -> Optional[str]:
        """按确定性缓存路径下载并校验，返回缓存文件路径；失败返回 None。"""
        dest = self.downloader.cache_path(action_id, spec.primary_hash)
        ok = self.downloader.download(spec.urls, dest, spec.primary_hash, spec.size)
        return dest if ok else None

    def _fail(self, message: str) -> bool:
        logger.error("动作 %s 执行失败: %s", self.action.id, message)
        return False


class _FileExecutorMixin:
    """文件类动作共享逻辑（目标备份 / 标记 / 原子替换）。"""

    action: FileAction
    hooks: PlatformHooks
    downloader: DownloadManager
    state: StateStore
    update_id: str

    def _target_path(self) -> str:
        return self.action.target.path

    def has_rollback(self) -> bool:
        # 文件动作通过备份/标记天然支持回滚，无需显式 rollback 定义
        return True

    def _install_from_cache(self, cache_path: str) -> bool:
        """把校验过的缓存文件原子安装到目标，并记录回滚信息。"""
        target = self._target_path()
        _ensure_parent_dirs(target)

        if os.path.exists(target):
            # 原文件存在：先备份再替换（原子性由 os.replace 保证）
            try:
                shutil.copy2(target, backup_path(target))
            except OSError as e:
                logger.error("备份原文件失败 %s: %s", target, e)
                return False
        else:
            # 原文件不存在：打新文件标记，回滚时删除即可
            try:
                open(new_marker_path(target), "w").close()
            except OSError as e:
                logger.error("创建新文件标记失败 %s: %s", target, e)
                return False

        self.hooks.atomic_replace(cache_path, target)

        mode = resolve_permissions(self.action.target.permissions)
        if mode is not None:
            self.hooks.set_permissions(target, mode)
        return True

    def _restore_target(self) -> bool:
        """按备份/标记约定还原目标文件（幂等）。"""
        target = self._target_path()
        bak = backup_path(target)
        marker = new_marker_path(target)

        if os.path.exists(bak):
            try:
                self.hooks.atomic_replace(bak, target)
            except OSError as e:
                logger.error("回滚还原失败 %s: %s", target, e)
                return False
        elif os.path.exists(marker):
            try:
                if os.path.exists(target):
                    os.remove(target)
                os.remove(marker)
            except OSError as e:
                logger.error("回滚删除新文件失败 %s: %s", target, e)
                return False
        return True


class FileEnsureExecutor(_FileExecutorMixin, ActionExecutor):
    """file.ensure：目标不存在则创建，存在且哈希不符则替换。"""

    def can_execute(self) -> bool:
        target = self._target_path()
        if self.action.download:
            required = self.action.download.size or self.action.target.size
            if not self._check_disk_space(target, required):
                return False
        return True

    def execute(self) -> bool:
        target = self._target_path()
        if os.path.isfile(target) and verify_file_hash(
            target, self.action.target.hash
        ):
            logger.info("动作 %s: 目标已满足（幂等跳过）: %s", self.action.id, target)
            return True

        if self.action.download is None:
            return self._fail("目标文件哈希不符且未提供下载源")

        cache = self._download_to_cache(self.action.download, self.action.id)
        if cache is None:
            return self._fail("下载失败，无可用源")
        return self._install_from_cache(cache)

    def rollback(self) -> bool:
        return self._restore_target()


class FileReplaceExecutor(FileEnsureExecutor):
    """file.replace：目标必须存在，否则直接失败。"""

    def execute(self) -> bool:
        target = self._target_path()
        if not os.path.isfile(target):
            return self._fail("目标文件不存在（replace 前置条件）: " + target)
        return super().execute()


class FileAddExecutor(_FileExecutorMixin, ActionExecutor):
    """file.add：目标必须不存在，否则失败。"""

    def can_execute(self) -> bool:
        if self.action.download:
            required = self.action.download.size or self.action.target.size
            if not self._check_disk_space(self._target_path(), required):
                return False
        return True

    def execute(self) -> bool:
        target = self._target_path()
        if os.path.exists(target):
            return self._fail("目标文件已存在（add 前置条件）: " + target)
        if self.action.download is None:
            return self._fail("file.add 缺少下载源")

        cache = self._download_to_cache(self.action.download, self.action.id)
        if cache is None:
            return self._fail("下载失败，无可用源")
        return self._install_from_cache(cache)

    def rollback(self) -> bool:
        return self._restore_target()


class FileDeleteExecutor(_FileExecutorMixin, ActionExecutor):
    """file.delete：删除目标文件；可选备份以便回滚。"""

    def can_execute(self) -> bool:
        return True

    def execute(self) -> bool:
        target = self._target_path()
        if not os.path.exists(target):
            logger.info("动作 %s: 目标不存在，视为成功: %s", self.action.id, target)
            return True
        try:
            if self.action.rollback and self.action.rollback.backup_original:
                _ensure_parent_dirs(target)
                shutil.copy2(target, backup_path(target))
                os.remove(target)
            else:
                os.remove(target)
        except OSError as e:
            return self._fail(f"删除失败 {target}: {e}")
        logger.info("动作 %s: 已删除 %s", self.action.id, target)
        return True

    def rollback(self) -> bool:
        target = self._target_path()
        bak = backup_path(target)
        if os.path.exists(bak):
            try:
                self.hooks.atomic_replace(bak, target)
            except OSError as e:
                logger.error("回滚还原失败 %s: %s", target, e)
                return False
        return True


class ArchiveExtractExecutor(ActionExecutor):
    """archive.extract：下载归档 → 校验 → 解压到临时目录 → 逐文件校验清单 →
    原子安装到目标目录。回滚信息保存在内存 journal 中。"""

    action: _ArchiveExtractActionModel

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._journal: List[Tuple[str, Optional[str]]] = []  # (目标路径, 备份路径|None)

    def can_execute(self) -> bool:
        if self.action.download:
            required = self.action.download.size or self.action.target.size
            if not self._check_disk_space(self.action.target.path, required):
                return False
        return True

    def execute(self) -> bool:
        assert self.action.download is not None
        cache = self._download_to_cache(self.action.download, self.action.id)
        if cache is None:
            return self._fail("下载失败，无可用源")

        extract_root = tempfile.mkdtemp(prefix="tae-extract-", dir=self.downloader.cache_dir)
        try:
            if not self._extract_archive(cache, extract_root):
                return self._fail("归档解压失败")
            if not self._verify_manifest(extract_root):
                return self._fail("解压后清单校验失败")
            if not self._install_tree(extract_root):
                return self._fail("安装到目标目录失败")
        finally:
            shutil.rmtree(extract_root, ignore_errors=True)
        return True

    def _extract_archive(self, archive_path: str, extract_root: str) -> bool:
        try:
            if zipfile.is_zipfile(archive_path):
                with zipfile.ZipFile(archive_path) as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        dst = self._safe_join(extract_root, info.filename)
                        if dst is None:
                            return False
                        _ensure_parent_dirs(dst)
                        with zf.open(info) as src, open(dst, "wb") as out:
                            shutil.copyfileobj(src, out)
                return True
            if tarfile.is_tarfile(archive_path):
                with tarfile.open(archive_path) as tf:
                    for member in tf.getmembers():
                        if not member.isfile():
                            continue
                        dst = self._safe_join(extract_root, member.name)
                        if dst is None:
                            return False
                        _ensure_parent_dirs(dst)
                        src = tf.extractfile(member)
                        if src is None:
                            return False
                        with src, open(dst, "wb") as out:
                            shutil.copyfileobj(src, out)
                return True
        except (zipfile.BadZipFile, tarfile.TarError, OSError) as e:
            logger.error("归档解压异常: %s", e)
            return False
        logger.error("不支持的归档格式: %s", archive_path)
        return False

    def _safe_join(self, root: str, member_name: str) -> Optional[str]:
        """防 zip-slip：成员路径归一化后必须仍位于 root 之内，且支持 stripComponents。"""
        parts = [p for p in member_name.replace("\\", "/").split("/") if p not in ("", ".")]
        parts = parts[self.action.strip_components:] if self.action.strip_components else parts
        if not parts:
            return None  # 目录项或 strip 后为空，跳过
        target = os.path.normpath(os.path.join(root, *parts))
        if os.path.commonpath([os.path.normpath(root), target]) != os.path.normpath(root):
            logger.error("归档成员路径越界被拒绝: %s", member_name)
            return None
        return target

    def _verify_manifest(self, extract_root: str) -> bool:
        for entry in self.action.manifest:
            path = os.path.normpath(os.path.join(extract_root, *entry.path.split("/")))
            if os.path.commonpath([os.path.normpath(extract_root), path]) != os.path.normpath(extract_root):
                logger.error("manifest 路径越界: %s", entry.path)
                return False
            if not os.path.isfile(path):
                logger.error("manifest 文件缺失: %s", entry.path)
                return False
            if not verify_file_hash(path, entry.hash):
                logger.error("manifest 文件哈希不符: %s", entry.path)
                return False
        return True

    def _install_tree(self, extract_root: str) -> bool:
        base = self.action.target.path
        for dirpath, _dirnames, filenames in os.walk(extract_root):
            for name in filenames:
                src = os.path.join(dirpath, name)
                rel = os.path.relpath(src, extract_root)
                dst = os.path.join(base, rel)
                _ensure_parent_dirs(dst)
                if os.path.exists(dst):
                    # 原文件存在：备份（整体移动，原目标被移除），journal 记录备份路径
                    bak = backup_path(dst)
                    try:
                        os.replace(dst, bak)
                    except OSError as e:
                        logger.error("归档替换前备份失败 %s: %s", dst, e)
                        return False
                    self._journal.append((dst, bak))
                else:
                    try:
                        open(new_marker_path(dst), "w").close()
                    except OSError as e:
                        logger.error("归档新增文件打标失败 %s: %s", dst, e)
                        return False
                    self._journal.append((dst, None))
                self.hooks.atomic_replace(src, dst)
        logger.info("归档安装完成，共 %d 个文件", len(self._journal))
        return True

    def rollback(self) -> bool:
        ok = True
        for dst, bak in reversed(self._journal):
            try:
                if bak is not None and os.path.exists(bak):
                    self.hooks.atomic_replace(bak, dst)
                else:
                    if os.path.exists(dst):
                        os.remove(dst)
                    marker = new_marker_path(dst)
                    if os.path.exists(marker):
                        os.remove(marker)
            except OSError as e:
                logger.error("归档回滚失败 %s: %s", dst, e)
                ok = False
        return ok


class ProcessExecExecutor(ActionExecutor):
    """process.exec：下载脚本（可选）→ 校验 → 执行命令；可注册回滚命令。"""

    action: _ProcessExecActionModel

    def can_execute(self) -> bool:
        if self.action.download:
            if not self._check_disk_space(
                self.downloader.cache_dir, self.action.download.size
            ):
                return False
        return True

    def execute(self) -> bool:
        command = self.action.command
        if self.action.download is not None:
            cache = self._download_to_cache(self.action.download, self.action.id)
            if cache is None:
                return self._fail("脚本下载失败，无可用源")
            # 允许命令通过 {script} 占位符引用下载后的脚本路径
            command = command.replace("{script}", cache)

        rc, stdout, stderr = self.hooks.run_command(
            command, self.action.timeout, self.action.environment,
            cwd=self.action.cwd or None, detach=self.action.detach,
            log_file=self.action.log_file or None,
        )
        if rc != 0:
            logger.error(
                "命令执行失败 rc=%d\nstdout: %s\nstderr: %s", rc, stdout, stderr
            )
            return self._fail(f"命令返回非零码 {rc}")
        logger.info("命令执行成功: %s", command)
        return True

    def rollback(self) -> bool:
        spec: Optional[RollbackSpec] = self.action.rollback
        if spec is None or not spec.command:
            logger.info("动作 %s 无回滚命令，跳过回滚", self.action.id)
            return True
        rc, stdout, stderr = self.hooks.run_command(
            spec.command, spec.timeout,
            cwd=self.action.cwd or None,
            detach=spec.detach or self.action.detach,
            log_file=spec.log_file or self.action.log_file or None,
        )
        if rc != 0:
            logger.error(
                "回滚命令失败 rc=%d\nstdout: %s\nstderr: %s", rc, stdout, stderr
            )
            return False
        return True


class RebootExecutor(ActionExecutor):
    """system.reboot：MVP 不真正重启，仅记录日志（避免测试环境副作用）。"""

    action: SystemRebootAction

    def can_execute(self) -> bool:
        return True

    def execute(self) -> bool:
        logger.warning("动作 %s: 系统重启动作在 MVP 中跳过（delay=%ds）",
                       self.action.id, self.action.delay)
        return True

    def rollback(self) -> bool:
        return True


class WaitExecutor(ActionExecutor):
    """system.wait：MVP 不实现条件等待，直接视为成功。"""

    action: SystemWaitAction

    def can_execute(self) -> bool:
        return True

    def execute(self) -> bool:
        logger.warning("动作 %s: 条件等待在 MVP 中跳过（condition=%r）",
                       self.action.id, self.action.condition)
        return True

    def rollback(self) -> bool:
        return True


class WaitForHealthExecutor(ActionExecutor):
    """system.wait_for_health：轮询健康检查 URL 直到返回 2xx 或超时。"""

    action: WaitForHealthAction

    _POLL_INTERVAL = 2.0
    _REQUEST_TIMEOUT = 5.0

    def can_execute(self) -> bool:
        return True

    def execute(self) -> bool:
        deadline = time.monotonic() + self.action.timeout
        last_error = "未发起请求"
        while time.monotonic() < deadline:
            try:
                resp = requests.get(self.action.url, timeout=self._REQUEST_TIMEOUT)
                if resp.ok:
                    logger.info("动作 %s: 健康检查通过 (%s)", self.action.id, resp.status_code)
                    return True
                last_error = f"HTTP {resp.status_code}"
            except requests.RequestException as e:
                last_error = str(e)
            time.sleep(self._POLL_INTERVAL)
        logger.error("动作 %s: 健康检查超时 %s: %s",
                     self.action.id, self.action.url, last_error)
        return False

    def rollback(self) -> bool:
        return True


_EXECUTOR_FACTORIES: Dict[str, "Any"] = {
    "file.ensure": FileEnsureExecutor,
    "file.replace": FileReplaceExecutor,
    "file.add": FileAddExecutor,
    "file.delete": FileDeleteExecutor,
    "archive.extract": ArchiveExtractExecutor,
    "process.exec": ProcessExecExecutor,
    "system.reboot": RebootExecutor,
    "system.wait": WaitExecutor,
    "system.wait_for_health": WaitForHealthExecutor,
}


def create_executor(
    action: Action,
    hooks: PlatformHooks,
    downloader: DownloadManager,
    state: StateStore,
    update_id: str,
) -> ActionExecutor:
    """按动作类型创建执行器。"""
    factory = _EXECUTOR_FACTORIES.get(action.type)
    if factory is None:
        raise ManifestError(f"没有对应的执行器: {action.type}")
    return factory(action, hooks, downloader, state, update_id)


__all__ = [
    "ActionExecutor", "create_executor", "verify_file_hash",
    "backup_path", "new_marker_path",
]
