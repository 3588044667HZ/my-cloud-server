"""执行器集成测试：清单校验、执行、回滚、断电恢复、签名/时效拒绝。

覆盖 AGENT-MVP.md 要求的 6 个测试场景：
1. 简单清单执行成功
2. 下载失败切换备用源（见 test_actions.py）
3. 执行失败触发回滚，验证文件恢复
4. 断电恢复：从未完成处继续
5. 签名无效被拒绝
6. 过期清单被拒绝
"""

from __future__ import annotations

import io
import json
import os
import sys

from tae import security
from tae.downloader import DownloadManager
from tae.exceptions import (
    ManifestExpiredError,
    SignatureVerificationError,
)
from tae.executor import Executor
from tae.hooks import MockHooks
from tae.models import ActionList
from tae.reporter import Reporter
from tae.state import (
    COMPLETED,
    ROLLED_BACK,
    UPDATE_COMPLETED,
    UPDATE_FAILED,
    StateStore,
)

from .helpers import (
    build_manifest,
    file_add_action,
    file_delete_action,
    file_ensure_action,
    process_exec_action,
    signed_action_list,
    TAETestCase,
)

FAIL_CMD = f'"{sys.executable}" -c "import sys; sys.exit(3)"'
OK_CMD = f'"{sys.executable}" -c "print(42)"'


class ExecutorTestBase(TAETestCase):
    def setUp(self) -> None:
        super().setUp()
        self.state = StateStore(self.path("state.db"))
        self.downloader = DownloadManager(self.path("cache"))
        self.hooks = MockHooks(
            current_version="1.0.0",
            public_key=security.load_public_key(self.pub_path),
        )
        self.reporter = Reporter(stream=io.StringIO(), async_enabled=False)
        self.executor = Executor(
            hooks=self.hooks,
            state=self.state,
            downloader=self.downloader,
            reporter=self.reporter,
            public_key=self.pub_path,
        )
        self.addCleanup(self.state.close)
        self.addCleanup(self.reporter.close)

    def run_manifest(self, manifest: dict, **kwargs) -> bool:
        return self.executor.run_action_list(
            signed_action_list(manifest, self.priv_path), **kwargs
        )


class SimpleManifestTest(ExecutorTestBase):
    """场景 1：包含 file.ensure / file.add / file.delete 的清单执行成功。"""

    def test_simple_manifest_succeeds(self) -> None:
        old_bin = self.write_file("app/main.bin", b"old-version")
        new_bin = self.write_file("src/main.bin", b"new-version-v2")
        added = self.path("app/new.txt")
        added_src = self.write_file("src/new.txt", b"added-content")
        delete_me = self.write_file("app/old.ini", b"to-be-deleted")

        manifest = build_manifest(
            "update-001",
            [
                file_ensure_action(
                    "ensure-main", old_bin,
                    target_hash=self.sha256("src/main.bin"),
                    source_url=self.source_url("src/main.bin"),
                ),
                file_add_action(
                    "add-new", added,
                    source_url=self.source_url("src/new.txt"),
                    source_hash=self.sha256("src/new.txt"),
                    depends_on=["ensure-main"],
                ),
                file_delete_action(
                    "delete-old", delete_me, depends_on=["add-new"]
                ),
            ],
        )

        self.assertTrue(self.run_manifest(manifest))

        with open(old_bin, "rb") as f:
            self.assertEqual(f.read(), b"new-version-v2")
        self.assertTrue(os.path.isfile(added))
        self.assertFalse(os.path.exists(delete_me))

        update = self.state.get_update_status("update-001")
        self.assertEqual(update["status"], UPDATE_COMPLETED)
        for row in self.state.get_all_actions("update-001"):
            self.assertEqual(row["status"], COMPLETED)

    def test_process_exec_in_manifest(self) -> None:
        marker = self.path("app/marker.txt")
        cmd = (
            f'"{sys.executable}" -c '
            f'"import pathlib; p=pathlib.Path(r\'{marker}\'); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(\'ok\')"'
        )
        manifest = build_manifest(
            "update-002",
            [process_exec_action("run-migrate", cmd)],
        )
        self.assertTrue(self.run_manifest(manifest))
        self.assertTrue(os.path.isfile(marker))

    def test_skip_policy_continues(self) -> None:
        exists = self.write_file("app/exists.txt", b"here")
        new_src = self.write_file("src/new.txt", b"content")
        manifest = build_manifest(
            "update-003",
            [
                file_add_action(
                    "add-dup", exists,
                    source_url=self.source_url("src/new.txt"),
                    source_hash=self.sha256("src/new.txt"),
                ),
                file_add_action(
                    "add-ok", self.path("app/ok.txt"),
                    source_url=self.source_url("src/new.txt"),
                    source_hash=self.sha256("src/new.txt"),
                    depends_on=["add-dup"],
                ),
            ],
        )
        # 手动给 add-dup 加上 onFailure=skip
        manifest["actions"][0]["onFailure"] = "skip"
        self.assertTrue(self.run_manifest(manifest))
        rows = {r["action_id"]: r for r in self.state.get_all_actions("update-003")}
        self.assertEqual(rows["add-dup"]["status"], "SKIPPED")
        self.assertEqual(rows["add-ok"]["status"], COMPLETED)


class FailoverTest(ExecutorTestBase):
    """场景 2（下载器级）：主源失败自动切换备用源，见 test_actions.py。"""

    def test_downloader_failover_used_by_ensure(self) -> None:
        new_bin = self.write_file("src/main.bin", b"v2")
        target = self.write_file("app/main.bin", b"v1")
        action = file_ensure_action(
            "ensure-main", target,
            target_hash=self.sha256("src/main.bin"),
            source_url=self.source_url("src/main.bin"),
        )
        # 在第一个位置插入一个不存在的源（优先级 0，会被优先尝试并失败）
        action["download"]["urls"] = [
            {"url": "file:///no/such/main.bin", "priority": 0},
            *action["download"]["urls"],
        ]
        manifest = build_manifest("update-failover", [action])
        self.assertTrue(self.run_manifest(manifest))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"v2")


class RollbackTest(ExecutorTestBase):
    """场景 3：执行失败触发回滚，验证文件恢复。"""

    def test_failure_rolls_back_completed_actions(self) -> None:
        old_bin = self.write_file("app/main.bin", b"original-v1")
        new_bin = self.write_file("src/main.bin", b"broken-v2")
        delete_me = self.write_file("app/old.ini", b"gone")
        added = self.path("app/new.txt")
        added_src = self.write_file("src/new.txt", b"new")

        manifest = build_manifest(
            "update-rollback",
            [
                file_ensure_action(
                    "ensure-main", old_bin,
                    target_hash=self.sha256("src/main.bin"),
                    source_url=self.source_url("src/main.bin"),
                ),
                file_add_action(
                    "add-new", added,
                    source_url=self.source_url("src/new.txt"),
                    source_hash=self.sha256("src/new.txt"),
                    depends_on=["ensure-main"],
                ),
                process_exec_action(
                    "run-db-migrate", FAIL_CMD,
                    on_failure="rollback",
                    depends_on=["add-new"],
                ),
                file_delete_action(
                    "delete-old", delete_me, depends_on=["run-db-migrate"]
                ),
            ],
        )

        self.assertFalse(self.run_manifest(manifest))

        # ensure 被回滚：原文件恢复
        with open(old_bin, "rb") as f:
            self.assertEqual(f.read(), b"original-v1")
        # add 被回滚：新文件删除
        self.assertFalse(os.path.exists(added))
        # delete 尚未执行（依赖失败），文件仍在
        self.assertTrue(os.path.exists(delete_me))

        update = self.state.get_update_status("update-rollback")
        self.assertEqual(update["status"], UPDATE_FAILED)
        rows = {r["action_id"]: r for r in self.state.get_all_actions("update-rollback")}
        self.assertEqual(rows["ensure-main"]["rollback_status"], ROLLED_BACK)
        self.assertEqual(rows["add-new"]["rollback_status"], ROLLED_BACK)
        self.assertEqual(rows["run-db-migrate"]["status"], "FAILED")

    def test_abort_policy_does_not_rollback(self) -> None:
        old_bin = self.write_file("app/main.bin", b"original-v1")
        new_bin = self.write_file("src/main.bin", b"v2")
        manifest = build_manifest(
            "update-abort",
            [
                file_ensure_action(
                    "ensure-main", old_bin,
                    target_hash=self.sha256("src/main.bin"),
                    source_url=self.source_url("src/main.bin"),
                ),
                process_exec_action("run-fail", FAIL_CMD, depends_on=["ensure-main"]),
            ],
        )
        self.assertFalse(self.run_manifest(manifest))
        # abort 策略：不执行回滚，文件保持 v2
        with open(old_bin, "rb") as f:
            self.assertEqual(f.read(), b"v2")


class RecoveryTest(ExecutorTestBase):
    """场景 4：断电恢复——终止后重启，从未完成处继续。"""

    def test_resume_from_failed_action(self) -> None:
        target = self.write_file("app/main.bin", b"v1")
        src = self.write_file("src/main.bin", b"v2")
        marker = self.path("app/final.txt")
        cmd = f'"{sys.executable}" -c "import pathlib; p=pathlib.Path(r\'{marker}\'); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(\'ok\')"'

        def make_manifest(failing: bool) -> dict:
            fail_cmd = FAIL_CMD if failing else OK_CMD
            return build_manifest(
                "update-recovery",
                [
                    file_ensure_action(
                        "ensure-main", target,
                        target_hash=self.sha256("src/main.bin"),
                        source_url=self.source_url("src/main.bin"),
                    ),
                    process_exec_action("step-2", fail_cmd, depends_on=["ensure-main"]),
                    process_exec_action("step-3", cmd, depends_on=["step-2"]),
                ],
            )

        # 第一次运行：step-2 失败（模拟断电/崩溃前已执行部分动作）
        self.assertFalse(self.run_manifest(make_manifest(failing=True)))

        rows = {r["action_id"]: r for r in self.state.get_all_actions("update-recovery")}
        self.assertEqual(rows["ensure-main"]["status"], COMPLETED)
        self.assertEqual(rows["step-2"]["status"], "FAILED")

        # 服务端修复后重发同 updateId 的新清单；执行器从未完成处继续
        self.assertTrue(self.run_manifest(make_manifest(failing=False)))

        rows = {r["action_id"]: r for r in self.state.get_all_actions("update-recovery")}
        # 已完成的动作未被重复执行（attempt 保持 1）
        self.assertEqual(rows["ensure-main"]["attempt"], 1)
        self.assertEqual(rows["step-2"]["status"], COMPLETED)
        self.assertEqual(rows["step-3"]["status"], COMPLETED)
        self.assertEqual(
            self.state.get_update_status("update-recovery")["status"], UPDATE_COMPLETED
        )
        self.assertTrue(os.path.isfile(marker))


class SignatureTest(ExecutorTestBase):
    """场景 5：签名无效的清单被拒绝执行。"""

    def test_tampered_manifest_rejected(self) -> None:
        manifest = build_manifest(
            "update-bad-sig",
            [process_exec_action("run", OK_CMD)],
        )
        action_list = signed_action_list(manifest, self.priv_path)
        # 篡改清单内容（绕过签名域）
        action_list.target_version = "9.9.9"
        with self.assertRaises(SignatureVerificationError):
            self.executor.run_action_list(action_list)

    def test_missing_signature_rejected(self) -> None:
        manifest = build_manifest(
            "update-no-sig",
            [process_exec_action("run", OK_CMD)],
        )
        action_list = ActionList.from_json(json.dumps(manifest))
        with self.assertRaises(SignatureVerificationError):
            self.executor.run_action_list(action_list)


class ExpiryTest(ExecutorTestBase):
    """场景 6：过期清单被拒绝。"""

    def test_expired_manifest_rejected(self) -> None:
        manifest = build_manifest(
            "update-expired",
            [process_exec_action("run", OK_CMD)],
            expires_at="2020-01-01T00:00:00Z",
        )
        with self.assertRaises(ManifestExpiredError):
            self.run_manifest(manifest)


class VersionGuardTest(ExecutorTestBase):
    """版本与客户端版本门槛校验（额外防御场景）。"""

    def test_downgrade_rejected_by_default(self) -> None:
        manifest = build_manifest(
            "update-downgrade",
            [process_exec_action("run", OK_CMD)],
            target_version="0.5.0",
        )
        from tae.exceptions import DowngradeRejectedError

        with self.assertRaises(DowngradeRejectedError):
            self.run_manifest(manifest)

    def test_downgrade_allowed_explicitly(self) -> None:
        manifest = build_manifest(
            "update-downgrade-ok",
            [process_exec_action("run", OK_CMD)],
            target_version="0.5.0",
        )
        self.assertTrue(self.run_manifest(manifest, allow_downgrade=True))

    def test_min_client_version_rejected(self) -> None:
        manifest = build_manifest(
            "update-min-client",
            [process_exec_action("run", OK_CMD)],
            min_client_version="99.0.0",
        )
        from tae.exceptions import TAEError

        with self.assertRaises(TAEError):
            self.run_manifest(manifest)

    def test_non_numeric_target_version_skips_downgrade_check(self) -> None:
        """方案 B：非数字目标版本（如 "local"）不参与降级检查。"""
        manifest = build_manifest(
            "update-local",
            [process_exec_action("run", OK_CMD)],
            target_version="local",
        )
        self.assertTrue(self.run_manifest(manifest))

    def test_cycle_dependency_rejected(self) -> None:
        manifest = build_manifest(
            "update-cycle",
            [
                process_exec_action("a", OK_CMD, depends_on=["b"]),
                process_exec_action("b", OK_CMD, depends_on=["a"]),
            ],
        )
        from tae.exceptions import DependencyError

        with self.assertRaises(DependencyError):
            self.run_manifest(manifest)
