"""端到端集成测试：真实 HTTP 服务 + 客户端执行器闭环。

验证：
- check_update 下发 → 客户端验签 → HTTP 签名 URL 下载 → 执行 → 文件就位
- 主源 404 自动切换备用源（fail_download 注入）
- 断点续传：已下载的 .part 通过 Range 续传完成
- 上报事件到达服务端
- 故障注入：篡改签名/过期清单被客户端拒绝
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from urllib.request import urlopen

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from mock_server.app import create_app  # noqa: E402
from mock_server.catalog import build_default_catalog  # noqa: E402
from tae import security  # noqa: E402
from tae.downloader import DownloadManager  # noqa: E402
from tae.executor import Executor  # noqa: E402
from tae.exceptions import ManifestExpiredError, SignatureVerificationError  # noqa: E402
from tae.hooks import MockHooks  # noqa: E402
from tae.models import ActionList  # noqa: E402
from tae.reporter import Reporter  # noqa: E402
from tae.state import StateStore  # noqa: E402


class ServerThread:
    """后台线程运行真实 HTTP 服务（测试用）。"""

    def __init__(self, app, host="127.0.0.1", port=0):
        self._server = make_server(host, port, app, threaded=True)
        self.port = self._server.server_port
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "ServerThread":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)


class MockServerE2ETest(unittest.TestCase):
    """端到端：完整更新闭环。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.mkdtemp(prefix="tae-e2e-")
        priv = os.path.join(cls._tmp, "server.priv")
        pub = os.path.join(cls._tmp, "client.pub")
        # process.exec 使用当前解释器，保证测试环境可执行
        catalog = build_default_catalog(interpreter=sys.executable)
        app = create_app(
            catalog=catalog, private_key_path=priv, public_key_path=pub, url_ttl=600
        )
        cls.server = ServerThread(app).start()
        cls.public_key = pub
        cls.reported = app.extensions["tae"]["reported_events"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        import shutil

        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self) -> None:
        self.workspace = tempfile.mkdtemp(prefix="tae-e2e-client-")
        self.app_dir = os.path.join(self.workspace, "app")
        os.makedirs(self.app_dir, exist_ok=True)
        # 模拟"已安装"的 v1 应用
        with open(os.path.join(self.app_dir, "main.bin"), "wb") as f:
            f.write(b"TAE demo v1 binary\n")
        with open(os.path.join(self.app_dir, "legacy.cfg"), "wb") as f:
            f.write(b"[legacy]\nold=1\n")
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.workspace, ignore_errors=True)

    # ---- 工具 ----

    def _fetch_manifest(self, **extra) -> ActionList:
        payload = {
            "device_uuid": "e2e-device-001",
            "current_version": "1.0.0",
            "platform": sys.platform,
            "app_root": self.app_dir,
            **extra,
        }
        resp = requests.post(
            f"{self.server.base_url}/api/v2/check_update", json=payload, timeout=10
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        if data.get("no_update"):
            return None  # type: ignore[return-value]
        return ActionList.from_json(json.dumps(data))

    def _make_executor(self) -> Executor:
        hooks = MockHooks(
            device_uuid="e2e-device-001",
            current_version="1.0.0",
            public_key=security.load_public_key(self.public_key),
        )
        return Executor(
            hooks=hooks,
            state=StateStore(os.path.join(self.workspace, "state.db")),
            downloader=DownloadManager(os.path.join(self.workspace, "cache")),
            reporter=Reporter(stream=sys.stderr, async_enabled=False),
            public_key=self.public_key,
        )

    def _assert_v2_state(self) -> None:
        with open(os.path.join(self.app_dir, "main.bin"), "rb") as f:
            self.assertIn(b"v2 binary", f.read())
        self.assertTrue(os.path.exists(os.path.join(self.app_dir, "README.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.app_dir, "legacy.cfg")))

    # ---- 用例 ----

    def test_full_update_roundtrip(self) -> None:
        """场景：真实 HTTP 下发的清单被客户端验签并完整执行。"""
        before = len(self.reported)
        action_list = self._fetch_manifest()
        self.assertIsNotNone(action_list)
        executor = self._make_executor()
        try:
            self.assertTrue(executor.run_action_list(action_list))
        finally:
            executor.state.close()
        self._assert_v2_state()
        # 执行事件应已到达服务端（executor 内 reporter 未连服务端，这里验证 server 侧接收接口可用）
        resp = requests.post(
            f"{self.server.base_url}/api/v2/report",
            json={"events": [{"event_type": "e2e_test", "device_uuid": "e2e-device-001"}]},
            timeout=10,
        )
        self.assertEqual(resp.json()["received"], 1)
        self.assertEqual(len(self.reported), before + 1)

    def test_failover_to_backup_source(self) -> None:
        """主源 404 时自动切换备用源完成下载。"""
        action_list = self._fetch_manifest(fail_download=True)
        executor = self._make_executor()
        try:
            self.assertTrue(executor.run_action_list(action_list))
        finally:
            executor.state.close()
        self._assert_v2_state()

    def test_bad_signature_rejected(self) -> None:
        action_list = self._fetch_manifest(fail_signature=True)
        executor = self._make_executor()
        try:
            with self.assertRaises(SignatureVerificationError):
                executor.run_action_list(action_list)
            # 签名无效不应产生任何动作状态记录
            self.assertEqual(
                executor.state.get_incomplete_actions(action_list.update_id), []
            )
        finally:
            executor.state.close()

    def test_expired_manifest_rejected(self) -> None:
        action_list = self._fetch_manifest(fail_expiry=True)
        executor = self._make_executor()
        try:
            with self.assertRaises(ManifestExpiredError):
                executor.run_action_list(action_list)
        finally:
            executor.state.close()

    def test_resume_with_range(self) -> None:
        """断点续传：先下载部分字节到 .part，再让 downloader 用 Range 续传。"""
        action_list = self._fetch_manifest()
        urls = action_list.actions[0].download.urls
        backup_url = urls[1].url  # /cdn/main.bin 带签名
        full = urlopen(backup_url, timeout=10).read()

        dest = os.path.join(self.workspace, "cache", "main.bin")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".part"
        with open(part, "wb") as f:
            f.write(full[:10])  # 模拟断电前只下了 10 字节

        manager = DownloadManager(os.path.join(self.workspace, "cache"))
        expected_hash = action_list.actions[0].download.primary_hash
        ok = manager.download([urls[1]], dest, expected_hash, len(full))
        self.assertTrue(ok)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), full)


if __name__ == "__main__":
    unittest.main()
