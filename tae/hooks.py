"""平台钩子：核心引擎与操作系统之间的唯一边界。

所有平台相关操作（原子替换、权限、进程执行、磁盘空间等）都必须通过
PlatformHooks 注入，执行器自身不包含任何平台特定代码（解耦原则）。
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple

from . import security

logger = logging.getLogger(__name__)


class PlatformHooks(ABC):
    """平台钩子抽象基类。"""

    @abstractmethod
    def get_device_uuid(self) -> str:
        """返回设备唯一标识，用于上报与定向。"""

    @abstractmethod
    def get_current_version(self) -> str:
        """返回当前客户端版本号。"""

    @abstractmethod
    def verify_signature(self, data: bytes, signature: bytes) -> bool:
        """验证签名。默认实现委托 security 模块。"""

    @abstractmethod
    def atomic_replace(self, src: str, dst: str) -> None:
        """原子替换：os.replace 保证替换过程原子性。"""

    @abstractmethod
    def set_permissions(self, path: str, mode: int) -> None:
        """设置文件权限（POSIX 有效；Windows 上为尽力而为）。"""

    @abstractmethod
    def run_command(
        self,
        cmd: str,
        timeout: int,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        detach: bool = False,
        log_file: Optional[str] = None,
    ) -> Tuple[int, str, str]:
        """执行命令，返回 (返回码, stdout, stderr)。超时视为失败。
        env 为可选的附加环境变量，cwd 为可选的命令工作目录，
        detach=True 时以后台方式启动（如常驻服务），立即返回；
        log_file 为 detach 模式下的输出落盘路径（空则丢弃）。"""

    @abstractmethod
    def get_free_disk_space(self, path: str) -> int:
        """返回路径所在分区的剩余可用字节数。"""

    @abstractmethod
    def report_event(self, event: dict) -> None:
        """上报事件，默认打印到 stdout。"""


class MockHooks(PlatformHooks):
    """Mock 实现：供测试与 demo 注入假环境信息，无平台副作用。"""

    def __init__(
        self,
        device_uuid: str = "test-device-0001",
        current_version: str = "1.0.0",
        public_key=None,
        log_events: bool = True,
    ):
        self.device_uuid = device_uuid
        self.current_version = current_version
        self._public_key = public_key
        self.log_events = log_events
        self.events: list = []  # 测试可检查上报内容

    # ---- 环境信息 ----

    def get_device_uuid(self) -> str:
        return self.device_uuid

    def get_current_version(self) -> str:
        return self.current_version

    # ---- 安全 ----

    def verify_signature(self, data: bytes, signature: bytes) -> bool:
        if self._public_key is None:
            logger.warning("MockHooks 未配置公钥，验签委托 security.verify 将失败")
            return False
        return security.verify(data, signature, self._public_key)

    # ---- 文件 ----

    def atomic_replace(self, src: str, dst: str) -> None:
        os.replace(src, dst)

    def set_permissions(self, path: str, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except OSError as e:
            # Windows 上 chmod 能力有限，失败不阻断更新流程
            logger.warning("设置权限失败 %s: %s", path, e)

    # ---- 进程 ----

    def run_command(
        self,
        cmd: str,
        timeout: int,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        detach: bool = False,
        log_file: Optional[str] = None,
    ) -> Tuple[int, str, str]:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        if detach:
            # 后台启动：脱离父进程会话，不等待退出（常驻服务场景）。
            # 输出落盘到 log_file（默认 DEVNULL，避免管道写阻塞/日志丢失）
            try:
                log_handle = None
                if log_file:
                    log_dir = os.path.dirname(log_file)
                    if log_dir:
                        os.makedirs(log_dir, exist_ok=True)
                    log_handle = open(log_file, "ab")
                subprocess.Popen(
                    cmd, shell=True, cwd=cwd, env=merged_env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle or subprocess.DEVNULL,
                    stderr=log_handle or subprocess.DEVNULL,
                    start_new_session=True,
                )
                return 0, "", ""
            except OSError as e:
                return 1, "", f"后台启动失败: {e}"
        try:
            result = subprocess.run(
                cmd, shell=True, timeout=timeout,
                capture_output=True, text=True, env=merged_env, cwd=cwd,
            )
            return result.returncode, result.stdout or "", result.stderr or ""
        except subprocess.TimeoutExpired as e:
            return -1, "", f"命令超时（{timeout}s）: {e}"

    # ---- 系统 ----

    def get_free_disk_space(self, path: str) -> int:
        try:
            return _disk_free_bytes(path)
        except OSError:
            logger.warning("无法获取磁盘空间: %s", path)
            return -1  # 未知，视为充足

    # ---- 上报 ----

    def report_event(self, event: dict) -> None:
        self.events.append(event)
        if self.log_events:
            logger.info("上报事件: %s", event)


def _disk_free_bytes(path: str) -> int:
    """返回路径所在分区的剩余字节数（跨平台）。"""
    if platform.system() == "Windows":
        import ctypes

        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        avail = ctypes.c_ulonglong(0)
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(os.path.abspath(path)),
            ctypes.byref(avail), ctypes.byref(total), ctypes.byref(free),
        )
        if not ok:
            raise OSError(f"GetDiskFreeSpaceExW 失败: {os.path.abspath(path)}")
        return avail.value
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def wait_seconds(seconds: float) -> None:
    time.sleep(seconds)


__all__ = ["PlatformHooks", "MockHooks"]
