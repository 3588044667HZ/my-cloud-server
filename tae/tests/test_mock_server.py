"""Mock Server 单测：Flask test client 验证接口行为（不启动真实端口）。

覆盖：
- check_update 下发签名清单（客户端验签可通过、URL 为带时效签名 URL）
- no_update 响应
- 故障注入：fail_signature / fail_expiry / fail_download / fail_all_download
- CDN 下载：签名 URL 校验（缺参/篡改/过期 403）、Range 断点续传 206
- 事件上报与公钥分发
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mock_server.app import create_app  # noqa: E402
from tae import security  # noqa: E402
from tae.models import ActionList  # noqa: E402


def _app(keys_dir: str):
    priv = os.path.join(keys_dir, "server.priv")
    pub = os.path.join(keys_dir, "client.pub")
    app = create_app(private_key_path=priv, public_key_path=pub, url_ttl=300)
    app.config["TESTING"] = True
    return app


class MockServerUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="tae-mock-server-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self._tmp, ignore_errors=True))
        self.app = _app(self._tmp)
        self.client = self.app.test_client()

    # ---- 基础 ----

    def test_health(self) -> None:
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json["status"], "ok")

    def test_public_key_distribution(self) -> None:
        resp = self.client.get("/api/v2/public_key")
        self.assertEqual(resp.status_code, 200)
        key = security.load_public_key(resp.data)
        self.assertIsNotNone(key)

    def test_report_receives_events(self) -> None:
        resp = self.client.post(
            "/api/v2/report", json={"events": [{"event_type": "action_completed"}]}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json, {"received": 1})

    def test_report_missing_events_field(self) -> None:
        resp = self.client.post("/api/v2/report", json={})
        self.assertEqual(resp.status_code, 400)

    # ---- check_update ----

    def _check_update(self, **payload) -> dict:
        base = {
            "device_uuid": "unit-test-device",
            "current_version": "1.0.0",
            "platform": "windows",
            "app_root": "C:\\opt\\tae-demo-app",
        }
        base.update(payload)
        resp = self.client.post("/api/v2/check_update", json=base)
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_check_update_returns_signed_manifest(self) -> None:
        data = self._check_update()
        self.assertNotIn("no_update", data)
        self.assertEqual(data["targetVersion"], "2.0.0")
        # 客户端验签必须通过
        action_list = ActionList.from_json(json.dumps(data))
        with open(os.path.join(self._tmp, "client.pub"), "rb") as f:
            public_key = security.load_public_key(f.read())
        self.assertTrue(action_list.validate_signature(public_key))
        # 下载 URL 必须带时效签名参数
        urls = data["actions"][0]["download"]["urls"]
        for item in urls:
            self.assertIn("expires=", item["url"])
            self.assertIn("sig=", item["url"])
            self.assertIn("/cdn", item["url"])
        # 清单未过期
        self.assertFalse(action_list.is_expired())

    def test_check_update_no_update(self) -> None:
        data = self._check_update(current_version="2.0.0")
        self.assertTrue(data.get("no_update"))

    def test_fail_signature_injection(self) -> None:
        data = self._check_update(fail_signature=True)
        action_list = ActionList.from_json(json.dumps(data))
        with open(os.path.join(self._tmp, "client.pub"), "rb") as f:
            public_key = security.load_public_key(f.read())
        # 客户端 fail-closed：验签失败抛异常（而非返回 False 继续）
        from tae.exceptions import SignatureVerificationError

        with self.assertRaises(SignatureVerificationError):
            action_list.validate_signature(public_key)

    def test_fail_expiry_injection(self) -> None:
        data = self._check_update(fail_expiry=True)
        action_list = ActionList.from_json(json.dumps(data))
        self.assertTrue(action_list.is_expired())

    def test_fail_download_injection_uses_broken_primary(self) -> None:
        data = self._check_update(fail_download=True)
        urls = data["actions"][0]["download"]["urls"]
        # 主源指向故障 CDN（404），备源正常
        self.assertIn("/cdn_404/", urls[0]["url"])
        self.assertIn("/cdn/main.bin", urls[1]["url"])

    def test_fail_all_download_injection(self) -> None:
        data = self._check_update(fail_all_download=True)
        for action in data["actions"]:
            for item in action.get("download", {}).get("urls", []):
                self.assertIn("/cdn_404/", item["url"])

    # ---- CDN 下载 ----

    def test_cdn_download_full(self) -> None:
        data = self._check_update()
        url = data["actions"][0]["download"]["urls"][1]["url"]  # /cdn/main.bin
        path = url.split("?")[0] + "?" + url.split("?", 1)[1]
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"v2 binary", resp.data)

    def test_cdn_rejects_missing_signature(self) -> None:
        resp = self.client.get("/cdn/main.bin")
        self.assertEqual(resp.status_code, 403)

    def test_cdn_rejects_tampered_signature(self) -> None:
        data = self._check_update()
        url = data["actions"][0]["download"]["urls"][1]["url"]
        path = url.split("?")[0]
        query = url.split("?", 1)[1]
        params = dict(p.split("=", 1) for p in query.split("&"))
        params["sig"] = "0" * len(params["sig"])
        tampered = path + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        resp = self.client.get(tampered)
        self.assertEqual(resp.status_code, 403)

    def test_cdn_rejects_expired_signature(self) -> None:
        data = self._check_update()
        url = data["actions"][0]["download"]["urls"][1]["url"]
        path = url.split("?")[0]
        query = url.split("?", 1)[1]
        params = dict(p.split("=", 1) for p in query.split("&"))
        params["expires"] = "1"  # 1970 年，早已过期
        expired = path + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        resp = self.client.get(expired)
        self.assertEqual(resp.status_code, 403)

    def test_cdn_range_resume(self) -> None:
        """验证 Range 断点续传返回 206 与正确片段。"""
        data = self._check_update()
        url = data["actions"][0]["download"]["urls"][1]["url"]
        full = self.client.get(url)
        total = len(full.data)
        resp = self.client.get(url, headers={"Range": "bytes=0-9"})
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(len(resp.data), 10)
        # Content-Range 携带总长度
        self.assertEqual(resp.headers.get("Content-Range").split("/")[1], str(total))
        # 前缀 + 续传片段拼接后与完整内容一致
        rest = self.client.get(url, headers={"Range": "bytes=10-"})
        self.assertEqual(resp.data + rest.data, full.data)

    def test_cdn_404_endpoint(self) -> None:
        resp = self.client.get("/cdn_404/main.bin")
        self.assertEqual(resp.status_code, 404)

    def test_cdn_unknown_artifact_rejected(self) -> None:
        # 签名 URL 与路径绑定：改路径后签名失效，先于制品查找被拒绝（fail-closed）
        data = self._check_update()
        url = data["actions"][0]["download"]["urls"][1]["url"]
        resp = self.client.get(url.replace("/main.bin", "/nope.bin"))
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
