"""热更新（stage2）测试：打包、部署服务、客户端端到端、回滚。

覆盖 stage2-pached.md §7 测试要求：
1. 打包与 .taeignore 忽略规则
2. 服务端 HTTP：action_list / package 可访问，token 错误 403
3. 客户端部署：临时目录模拟目标，验证文件替换与命令执行
4. 端到端：真实 HTTP 完整闭环
5. 回滚：健康检查失败自动回滚（文件还原 + 反向命令）
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import unittest

import requests
from flask import Flask, jsonify
from werkzeug.serving import make_server

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tae.cli.config import DeployConfig  # noqa: E402
from tae.cli.packager import create_package, is_ignored, parse_ignore_file  # noqa: E402
from tae.cli.server import DeployServer  # noqa: E402
from tae.client import deploy as client_deploy  # noqa: E402
from tae.exceptions import DowngradeRejectedError  # noqa: E402
from tae.models import ActionList, ArchiveExtractAction  # noqa: E402


def _marker_cmd(path: str, text: str) -> str:
    """生成写标记文件的跨平台命令（测试用于观察 stop/start 是否执行）。"""
    return (
        f'"{sys.executable}" -c '
        f'"import pathlib; pathlib.Path(r\'{path}\').write_text(\'{text}\')"'
    )


class PackagerTest(unittest.TestCase):
    """测试要求 1：打包与忽略规则。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="tae-pack-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_ignore_rules(self) -> None:
        self.assertTrue(is_ignored(".venv", True, [".venv/"]))
        self.assertTrue(is_ignored(".venv/lib", True, [".venv/"]))
        self.assertTrue(is_ignored("sub/app.log", False, ["*.log"]))
        # 目录规则由 walk 剪枝处理：文件在忽略目录内会被跳过
        self.assertTrue(is_ignored("app/__pycache__", True, ["__pycache__/"]))
        self.assertFalse(is_ignored("src/main.py", False, [".venv/"]))
        # ! 取反规则覆盖：keep.txt 最终不被忽略
        self.assertFalse(is_ignored("keep.txt", False, ["*.txt", "!keep.txt"]))

    def test_package_excludes_ignored(self) -> None:
        project = os.path.join(self.tmp, "proj")
        for rel in ("src/main.py", "src/util.py", ".venv/lib/x.py", "debug.log", "data/key.priv"):
            full = os.path.join(project, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(rel)

        ignore_path = os.path.join(project, ".taeignore")
        with open(ignore_path, "w") as f:
            f.write(".venv/\n*.log\n*.priv\n")

        pkg_path, pkg_hash = create_package(project, ".taeignore")
        self.addCleanup(lambda: os.remove(pkg_path) if os.path.exists(pkg_path) else None)
        with tarfile.open(pkg_path, "r:gz") as tar:
            names = set(tar.getnames())
        self.assertIn("src/main.py", names)
        self.assertIn("src/util.py", names)
        self.assertNotIn(".venv/lib/x.py", names)
        self.assertNotIn("debug.log", names)
        self.assertNotIn("data/key.priv", names)
        # .taeignore 本身是工具元数据，不进部署包
        self.assertNotIn(".taeignore", names)
        # 哈希稳定且非空
        self.assertEqual(len(pkg_hash), 64)
        self.assertTrue(os.path.getsize(pkg_path) > 0)


class _DeployServerTestBase(unittest.TestCase):
    """DeployServer 相关测试基类：真实 HTTP 线程。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="tae-deploy-test-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.token = "test-secret-token"

        # 目标目录：模拟目标服务器上的"已安装"应用
        self.target_dir = os.path.join(self.tmp, "target")
        os.makedirs(self.target_dir, exist_ok=True)
        with open(os.path.join(self.target_dir, "app.py"), "w") as f:
            f.write("# v1\n")

        # 打包一个测试项目（源项目）
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(os.path.join(self.project, "src"), exist_ok=True)
        with open(os.path.join(self.project, "src", "app.py"), "w") as f:
            f.write("# v2 - hot deployed\n")
        with open(os.path.join(self.project, "README.md"), "w") as f:
            f.write("readme v2\n")

        self.package_path, self.package_hash = create_package(self.project)
        self.addCleanup(
            lambda: os.remove(self.package_path) if os.path.exists(self.package_path) else None
        )

        self.config = DeployConfig(
            project_dir=self.project,
            target_dir=self.target_dir,
            token=self.token,
            port=0,
            stop_command=_marker_cmd(os.path.join(self.tmp, "stopped.flag"), "stopped"),
            start_command=_marker_cmd(os.path.join(self.tmp, "started.flag"), "started"),
            start_detach=False,  # 测试用同步命令断言 marker 文件
        )
        self.server = DeployServer(self.config, self.package_path, self.package_hash, port=0)
        self.server.start()
        self.addCleanup(self.server.stop)

    def _get(self, path: str, token: str = None):
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return requests.get(
            f"{self.server.base_url}{path}",
            headers=headers,
            timeout=10,
        )


class DeployServerTest(_DeployServerTestBase):
    """测试要求 2：HTTP 服务与 token 校验。"""

    def test_token_required(self) -> None:
        self.assertEqual(self._get("/action_list").status_code, 403)
        self.assertEqual(self._get("/package").status_code, 403)
        self.assertEqual(self._get("/public_key").status_code, 403)

    def test_query_token_rejected(self) -> None:
        """token 不再接受 URL query 传参（防止日志/历史泄露）。"""
        resp = requests.get(
            f"{self.server.base_url}/action_list",
            params={"token": self.token}, timeout=10,
        )
        self.assertEqual(resp.status_code, 403)

    def test_wrong_token_rejected(self) -> None:
        self.assertEqual(self._get("/action_list", token="wrong").status_code, 403)

    def test_action_list_signed_and_complete(self) -> None:
        resp = self._get("/action_list", token=self.token)
        self.assertEqual(resp.status_code, 200)
        action_list = ActionList.from_json(resp.text)
        # targetVersion=local 不应触发降级拒绝（容错已实现）
        self.assertEqual(action_list.target_version, "local")
        # 动作链：stop → deploy → start
        ids = [a.id for a in action_list.actions]
        self.assertEqual(ids, ["stop-service", "deploy-package", "start-service"])
        deploy_action = action_list.actions[1]
        self.assertIsInstance(deploy_action, ArchiveExtractAction)
        self.assertEqual(deploy_action.target.path, self.target_dir)
        # 下载 URL 指向本服务，且不携带 token（鉴权走 Authorization 头）
        url = deploy_action.download.urls[0].url
        self.assertTrue(url.endswith("/package"))
        self.assertNotIn("token", url)
        # 验签通过（客户端公钥来自 /public_key）
        pub = self._get("/public_key", token=self.token)
        action_list.validate_signature(pub.content)

    def test_package_download_matches_hash(self) -> None:
        resp = self._get("/package", token=self.token)
        self.assertEqual(resp.status_code, 200)
        import hashlib

        actual = hashlib.sha256(resp.content).hexdigest()
        self.assertEqual(actual, self.package_hash)

    def test_health_action_included_when_configured(self) -> None:
        self.config.health_check_url = "http://127.0.0.1:1/health"
        self.config.health_check_timeout = 5
        server2 = DeployServer(self.config, self.package_path, self.package_hash, port=0)
        server2.start()
        try:
            resp = requests.get(
                f"{server2.base_url}/action_list",
                headers={"Authorization": f"Bearer {self.token}"}, timeout=10,
            )
            action_list = ActionList.from_json(resp.content)  # bytes 输入（UTF-8 解码防御）
            self.assertEqual(action_list.actions[-1].type, "system.wait_for_health")
        finally:
            server2.stop()

    def test_utf8_content_signature_roundtrip(self) -> None:
        """回归防护：清单含中文内容时，编码链路必须无损（验签通过）。

        曾出现 requests 对无 charset 响应按系统编码猜测导致内容漂移、
        验签失败的问题；服务端已显式 charset=utf-8，客户端强制 UTF-8。
        """
        self.config.stop_command = 'echo "停止服务" && echo ok'
        server2 = DeployServer(self.config, self.package_path, self.package_hash, port=0)
        server2.start()
        try:
            resp = requests.get(
                f"{server2.base_url}/action_list",
                headers={"Authorization": f"Bearer {self.token}"}, timeout=10,
            )
            self.assertIn("charset=utf-8", resp.headers.get("Content-Type", "").lower())
            action_list = ActionList.from_json(resp.content)
            pub = requests.get(
                f"{server2.base_url}/public_key",
                headers={"Authorization": f"Bearer {self.token}"}, timeout=10,
            ).content
            # 中文内容在 UTF-8 解码链路下验签必须成功
            self.assertTrue(action_list.validate_signature(pub))
        finally:
            server2.stop()

    def test_empty_rollback_command_roundtrip(self) -> None:
        """回归防护：stop_command 为空时 start 的 rollback.command 为空字符串。

        空回滚定义曾序列化为 {}，而 {} 反序列化会变为 None，导致签名端与
        验签端规范化字节不一致、验签失败（僵尸进程排查中的真正根因）。
        """
        self.config.stop_command = ""
        self.config.start_command = 'echo "starting"'
        server2 = DeployServer(self.config, self.package_path, self.package_hash, port=0)
        server2.start()
        try:
            resp = requests.get(
                f"{server2.base_url}/action_list",
                headers={"Authorization": f"Bearer {self.token}"}, timeout=10,
            )
            action_list = ActionList.from_json(resp.content)
            pub = requests.get(
                f"{server2.base_url}/public_key",
                headers={"Authorization": f"Bearer {self.token}"}, timeout=10,
            ).content
            self.assertTrue(action_list.validate_signature(pub))
            # 空回滚定义反序列化为 None（两端规范化字节一致）
            start_action = next(a for a in action_list.actions if a.id == "start-service")
            self.assertIsNone(start_action.rollback)
        finally:
            server2.stop()


class DeployEndToEndTest(_DeployServerTestBase):
    """测试要求 3/4：客户端部署端到端。"""

    def test_full_deploy_roundtrip(self) -> None:
        rc = client_deploy.run(
            self.server.base_url, self.token,
            workspace=os.path.join(self.tmp, "client-ws"),
        )
        self.assertEqual(rc, 0)
        # 文件被替换为 v2
        with open(os.path.join(self.target_dir, "src", "app.py")) as f:
            self.assertIn("v2", f.read())
        # stop/start 命令均执行
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "stopped.flag")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "started.flag")))

    def test_health_failure_triggers_rollback(self) -> None:
        """测试要求 5：健康检查失败 → 自动回滚（文件还原 + 反向命令）。"""
        # 健康检查指向不可达端口，注定失败
        self.config.health_check_url = "http://127.0.0.1:1/health"
        self.config.health_check_timeout = 3
        server2 = DeployServer(self.config, self.package_path, self.package_hash, port=0)
        server2.start()
        try:
            rc = client_deploy.run(
                server2.base_url, self.token,
                workspace=os.path.join(self.tmp, "client-ws2"),
            )
            self.assertNotEqual(rc, 0)
        finally:
            server2.stop()

        # 部署包被回滚：新增文件删除（空目录残留无害），原文件恢复 v1
        self.assertFalse(os.path.exists(os.path.join(self.target_dir, "src", "app.py")))
        self.assertFalse(os.path.exists(os.path.join(self.target_dir, "README.md")))
        with open(os.path.join(self.target_dir, "app.py")) as f:
            self.assertIn("v1", f.read())
        # 反向命令执行：start-service 的回滚（stop 命令）
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "stopped.flag")))

    def test_wrong_token_client_exits_nonzero(self) -> None:
        rc = client_deploy.run(self.server.base_url, "wrong-token")
        self.assertEqual(rc, 2)

    def test_detached_start_command_does_not_block(self) -> None:
        """回归防护：start 命令后台启动（如常驻服务）时部署必须立即返回，
        不能等待命令退出（否则 deploy 永不退出、健康检查无法执行）。"""
        pidfile = os.path.join(self.tmp, "server.pid")
        self.config.start_detach = True
        self.config.start_command = (
            f'"{sys.executable}" -c '
            f'"import os, pathlib, time; '
            f'pathlib.Path(r\'{pidfile}\').write_text(str(os.getpid())); '
            f'time.sleep(60)"'
        )
        server2 = DeployServer(self.config, self.package_path, self.package_hash, port=0)
        server2.start()
        try:
            rc = client_deploy.run(
                server2.base_url, self.token,
                workspace=os.path.join(self.tmp, "client-ws3"),
            )
            self.assertEqual(rc, 0)
            # 后台进程已启动（pid 文件写入）且部署未被阻塞；
            # Popen 返回后子进程尚在启动，轮询等待
            deadline = time.time() + 5
            while time.time() < deadline and not os.path.exists(pidfile):
                time.sleep(0.1)
            self.assertTrue(os.path.exists(pidfile))
        finally:
            server2.stop()
            # 清理后台进程
            try:
                with open(pidfile) as f:
                    pid = int(f.read().strip())
                os.system(f"taskkill /F /PID {pid} >nul 2>&1")
            except (OSError, ValueError):
                pass

    def test_detach_flag_in_manifest(self) -> None:
        """生成侧：start 命令带 detach 标志，stop 命令不带。"""
        self.config.start_detach = True
        self.config.stop_command = 'echo "stop"'
        server2 = DeployServer(self.config, self.package_path, self.package_hash, port=0)
        server2.start()
        try:
            resp = requests.get(
                f"{server2.base_url}/action_list",
                headers={"Authorization": f"Bearer {self.token}"}, timeout=10,
            )
            action_list = ActionList.from_json(resp.content)
            by_id = {a.id: a for a in action_list.actions}
            self.assertTrue(by_id["start-service"].detach)
            self.assertFalse(by_id["stop-service"].detach)
            # 客户端往返验签仍一致
            pub = requests.get(
                f"{server2.base_url}/public_key",
                headers={"Authorization": f"Bearer {self.token}"}, timeout=10,
            ).content
            self.assertTrue(action_list.validate_signature(pub))
        finally:
            server2.stop()

    def test_detached_start_writes_log(self) -> None:
        """detach 的 start 命令输出自动落盘（服务日志不丢失）。"""
        log_path = os.path.join(self.tmp, "service.log")
        self.config.start_detach = True
        self.config.start_log = log_path
        self.config.start_command = (
            f'"{sys.executable}" -u -c '
            f'"import time; print(\'SERVICE_READY\', flush=True); time.sleep(30)"'
        )
        server2 = DeployServer(self.config, self.package_path, self.package_hash, port=0)
        server2.start()
        try:
            rc = client_deploy.run(
                server2.base_url, self.token,
                workspace=os.path.join(self.tmp, "client-ws4"),
            )
            self.assertEqual(rc, 0)
            deadline = time.time() + 5
            content = ""
            while time.time() < deadline and "SERVICE_READY" not in content:
                try:
                    with open(log_path, encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except OSError:
                    pass
                time.sleep(0.1)
            self.assertIn("SERVICE_READY", content)
        finally:
            server2.stop()
            # 清理后台进程（sleep 30 的后台 python）
            try:
                import re

                result = os.popen(
                    "wmic process where "
                    '"name=\'python.exe\' and commandline like \'%time.sleep(30)%\'" '
                    "get processid /value"
                ).read()
                for pid in re.findall(r"=(\d+)", result):
                    os.system(f"taskkill /F /PID {pid} >nul 2>&1")
            except OSError:
                pass

    def test_sync_start_command_waits(self) -> None:
        """start_detach=false 时 start 命令作为初始化/同步命令使用：
        必须等待命令完成并校验返回码（不落盘，输出由执行器捕获）。"""
        marker = os.path.join(self.tmp, "init.done")
        self.config.start_detach = False
        self.config.start_command = _marker_cmd(marker, "init-ok")
        server2 = DeployServer(self.config, self.package_path, self.package_hash, port=0)
        server2.start()
        try:
            rc = client_deploy.run(
                server2.base_url, self.token,
                workspace=os.path.join(self.tmp, "client-ws5"),
            )
            self.assertEqual(rc, 0)
            # 同步语义：命令完成时 marker 已写入
            self.assertTrue(os.path.exists(marker))
            # 清单中 start 动作不带 detach 标志
            resp = requests.get(
                f"{server2.base_url}/action_list",
                headers={"Authorization": f"Bearer {self.token}"}, timeout=10,
            )
            action_list = ActionList.from_json(resp.content)
            start_action = next(a for a in action_list.actions if a.id == "start-service")
            self.assertFalse(start_action.detach)
        finally:
            server2.stop()


class CliSmokeTest(unittest.TestCase):
    """CLI 入口冒烟：命令解析与错误处理。"""

    def test_serve_rejects_missing_config(self) -> None:
        from tae.cli import main as cli_main

        tmp = tempfile.mkdtemp(prefix="tae-cfg-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        # 配置文件不存在 → main 捕获 TAEError 返回 2
        rc = cli_main.main(["serve", "--config", os.path.join(tmp, "missing.yaml")])
        self.assertEqual(rc, 2)

    def test_config_validation(self) -> None:
        tmp = tempfile.mkdtemp(prefix="tae-cfg-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        cfg_path = os.path.join(tmp, "tae-config.yaml")
        with open(cfg_path, "w") as f:
            f.write("project_dir: no-such-dir\ntoken: x\n")
        with self.assertRaises(Exception):
            DeployConfig.from_file(cfg_path)


if __name__ == "__main__":
    unittest.main()
