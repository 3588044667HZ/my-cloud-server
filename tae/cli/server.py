"""临时 HTTP 服务：托管部署包与签名动作清单（开发机端，一次性运行）。

端点（全部要求 Bearer token，错误 token 返回 403）：
- GET /public_key       客户端公钥（每次 serve 临时生成，签名清单用）
- GET /action_list      签名后的 Action List
- GET /package          部署包（tar.gz，支持 Range 断点续传）

动作链：stop（可选）→ archive.extract 部署包 → start（可选）→ 健康检查（可选）。
任一步失败且策略为 rollback 时，客户端按逆序回滚（文件备份还原 + 反向命令）。
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask import Flask, Response, jsonify, request, send_file

from tae.models import ActionList, TIME_FORMAT
from tae.security import public_key_bytes, sign

from .config import DeployConfig

logger = logging.getLogger(__name__)


def _resolve_start_log(config: DeployConfig) -> str:
    """解析 start 命令的输出日志路径。

    - start_log 配置优先（绝对路径或相对 target_dir）
    - 默认 {target_dir}/tae-start.log（服务日志不丢失）
    - "none" 表示丢弃输出
    """
    if config.start_log == "none":
        return ""
    if config.start_log:
        log_path = config.start_log
    else:
        log_path = os.path.join(config.target_dir, "tae-start.log")
    if not os.path.isabs(log_path):
        log_path = os.path.join(config.target_dir, log_path)
    return log_path


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(
        ts if ts is not None else time.time(), timezone.utc
    ).strftime(TIME_FORMAT)


class DeployServer:
    """一次性的部署服务端（生命周期与 tae serve 进程一致）。"""

    def __init__(
        self,
        config: DeployConfig,
        package_path: str,
        package_hash: str,
        port: int = 0,  # 0 = 自动分配（测试用）
    ):
        self.config = config
        self.package_path = package_path
        self.package_hash = package_hash
        self.port = port
        self.base_url = ""

        # 临时密钥对：本次部署有效，客户端通过 /public_key 获取（token 保护）
        self._private_key = Ed25519PrivateKey.generate()
        self._public_key = self._private_key.public_key()

        self._app = Flask(__name__)
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._register_routes()

    # ---- 生命周期 ----

    def start(self) -> "DeployServer":
        """后台线程启动 HTTP 服务；port=0 时自动分配并记录实际端口。"""
        from werkzeug.serving import make_server

        host = self.config.host
        self._server = make_server(host, self.port, self._app, threaded=True)
        self.port = self._server.server_port
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="tae-deploy-server", daemon=True
        )
        self._thread.start()
        logger.info("部署服务已启动: %s:%d 公钥指纹=%s",
                    host, self.port, self.public_key_fingerprint())
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def public_key_fingerprint(self) -> str:
        """公钥指纹（sha256 前 16 位），用于服务端/客户端快速对账。"""
        import hashlib

        raw = public_key_bytes(self._public_key)
        return hashlib.sha256(raw).hexdigest()[:16]

    # ---- 路由 ----

    def _register_routes(self) -> None:
        app = self._app

        @app.get("/public_key")
        def public_key() -> Response:
            if not self._check_token():
                return jsonify({"error": "token 无效"}), 403
            return Response(
                public_key_bytes(self._public_key),
                mimetype="application/octet-stream",
            )

        @app.get("/action_list")
        def action_list() -> Response:
            if not self._check_token():
                return jsonify({"error": "token 无效"}), 403
            data = self._build_action_list_dict()
            # 显式 charset=utf-8 + ensure_ascii=False：避免客户端对响应编码的
            # 猜测（requests 在无 charset 时会用 chardet 检测，中文内容可能被
            # 误判为 GBK 等导致内容漂移、验签失败）
            return Response(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                mimetype="application/json; charset=utf-8",
            )

        @app.get("/package")
        def package() -> Response:
            if not self._check_token():
                return jsonify({"error": "token 无效"}), 403
            return send_file(
                self.package_path,
                mimetype="application/gzip",
                as_attachment=False,
                download_name="package.tar.gz",
                conditional=True,  # 支持 Range 断点续传
            )

    def _check_token(self) -> bool:
        """从 Authorization 头读取 Bearer token（不再接受 URL query 传参，
        避免 token 出现在访问日志与浏览器历史中）。"""
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        token = header[len("Bearer "):].strip()
        return hmac.compare_digest(token, self.config.token)

    # ---- 清单生成 ----

    def _build_action_list_dict(self) -> Dict[str, Any]:
        actions = self._build_actions()
        manifest = {
            "schemaVersion": "2.0",
            "updateId": f"local-deploy-{int(time.time())}-{uuid.uuid4().hex[:6]}",
            "timestamp": _iso(),
            "expiresAt": _iso(time.time() + self.config.package_ttl_seconds),
            "targetVersion": "local",
            "minClientVersion": "",
            "actions": actions,
        }
        action_list = ActionList.from_json(json.dumps(manifest))
        # 诊断日志：签名时的规范字节哈希 + updateId，与客户端验签日志对比可定位内容差异
        import hashlib

        logger.info("清单 canonical sha256: %s (update=%s)",
                    hashlib.sha256(action_list.canonical_bytes()).hexdigest()[:16],
                    action_list.update_id)
        action_list.sign(self._private_key)
        return action_list.to_dict()

    def _build_actions(self) -> List[dict]:
        """生成动作链：stop → deploy → start → health。"""
        config = self.config
        # 包下载 URL 不再携带 token（客户端请求时经 Authorization 头鉴权）
        package_url = f"{request.host_url.rstrip('/')}/package"
        actions: List[dict] = []
        # 生命周期命令在部署目标目录下执行（命令可用相对路径引用目标目录内容）
        lifecycle_cwd = config.target_dir

        def _lifecycle_action(action_id: str, command: str, detach: bool) -> dict:
            """生命周期动作：命令在 target_dir 下执行；detach=True 时
            后台启动（start 常驻服务场景，发令即返回，由健康检查确认就绪），
            输出默认落盘到 {target_dir}/tae-start.log（start_log 可配置）。"""
            action: dict = {
                "id": action_id,
                "type": "process.exec",
                "command": command,
                "timeout": 60,
                "cwd": lifecycle_cwd,
            }
            if detach:
                action["detach"] = True
                log_file = _resolve_start_log(config)
                if log_file:
                    action["logFile"] = log_file
            return action

        # 1. 停止旧服务（可选；回滚时反向执行 start）
        if config.stop_command:
            stop_action = _lifecycle_action("stop-service", config.stop_command, False)
            if config.start_command:
                rollback: Dict[str, Any] = {
                    "command": config.start_command,
                    "timeout": 60,
                }
                if config.start_detach:
                    rollback["detach"] = True
                    log_file = _resolve_start_log(config)
                    if log_file:
                        rollback["logFile"] = log_file
                stop_action["rollback"] = rollback
            actions.append(stop_action)

        # 2. 部署包：下载 → 校验 → 解压到目标目录（原子替换 + 备份可回滚）
        deploy: dict = {
            "id": "deploy-package",
            "type": "archive.extract",
            "target": {"path": config.target_dir},
            "download": {
                "urls": [{"url": package_url, "priority": 1}],
                "hash": {"algo": "sha256", "value": self.package_hash},
            },
            "rollback": {"backupOriginal": True},
        }
        if actions:
            deploy["dependsOn"] = [actions[-1]["id"]]
        actions.append(deploy)

        # 3. 启动新服务（可选；回滚时反向执行 stop）
        if config.start_command:
            start_action = _lifecycle_action(
                "start-service", config.start_command, config.start_detach
            )
            start_action["dependsOn"] = [deploy["id"]]
            if config.stop_command:
                start_action["rollback"] = {
                    "command": config.stop_command,
                    "timeout": 60,
                }
            actions.append(start_action)
        # 4. 健康检查（可选；失败触发整链回滚）
        if config.health_check_url:
            health_action: dict = {
                "id": "wait-health",
                "type": "system.wait_for_health",
                "target": config.health_check_url,
                "timeout": config.health_check_timeout,
                "onFailure": "rollback",
            }
            if actions:
                health_action["dependsOn"] = [actions[-1]["id"]]
            actions.append(health_action)

        return actions


__all__ = ["DeployServer"]
