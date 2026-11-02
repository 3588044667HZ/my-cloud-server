"""tae-config.yaml 解析。

使用 PyYAML 的 safe_load（拒绝 Python 对象反序列化，防中毒 YAML），
字段与 stage2-pached.md §3.1 对齐，缺失字段提供默认值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import yaml

from ..exceptions import TAEError


@dataclass
class DeployConfig:
    """热部署配置（开发机端）。"""

    project_dir: str = "."
    target_dir: str = ""
    stop_command: str = ""
    start_command: str = ""
    ignore_file: str = ".taeignore"
    health_check_url: str = ""
    health_check_timeout: int = 60
    host: str = "0.0.0.0"
    port: int = 8000
    token: str = ""
    package_ttl_seconds: int = 3600  # 清单/签名 URL 有效期
    start_detach: bool = True  # start 命令后台启动（服务常驻场景，发令即返回）
    start_log: str = ""  # start 命令输出日志路径（空 = 默认 {target_dir}/tae-start.log；"none" = 丢弃）

    @classmethod
    def from_file(cls, path: str) -> "DeployConfig":
        """从 YAML 文件加载配置（safe_load）。"""
        if not os.path.isfile(path):
            raise TAEError(f"配置文件不存在: {path}")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data: Dict[str, Any] = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise TAEError(f"配置文件语法错误: {e}") from e
        if not isinstance(data, dict):
            raise TAEError(f"配置根节点必须是映射: {path}")

        def _get(key: str, default: Any) -> Any:
            value = data.get(key, default)
            if value is None:
                return default
            return value

        config = cls(
            project_dir=str(_get("project_dir", ".")),
            target_dir=str(_get("target_dir", "")).rstrip("/") or "/",
            stop_command=str(_get("stop_command", "")),
            start_command=str(_get("start_command", "")),
            ignore_file=str(_get("ignore_file", ".taeignore")),
            health_check_url=str(_get("health_check_url", "")),
            health_check_timeout=int(_get("health_check_timeout", 60)),
            host=str(_get("host", "0.0.0.0")),
            port=int(_get("port", 8000)),
            token=str(_get("token", "")),
            package_ttl_seconds=int(_get("package_ttl_seconds", 3600)),
            start_detach=bool(_get("start_detach", True)),
            start_log=str(_get("start_log", "")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        """边界防御：关键字段必须满足约束，缺省值可能导致部署到错误位置。"""
        if not os.path.isdir(self.project_dir):
            raise TAEError(f"project_dir 不存在: {self.project_dir}")
        if not self.target_dir or self.target_dir == "/":
            raise TAEError("target_dir 必须指定（如 /opt/myapp）")
        if not self.token:
            raise TAEError("token 不能为空（客户端部署的安全凭证）")
        if self.port < 0 or self.port > 65535:
            raise TAEError(f"port 非法: {self.port}")
        if self.health_check_timeout <= 0:
            raise TAEError("health_check_timeout 必须大于 0")


__all__ = ["DeployConfig"]
