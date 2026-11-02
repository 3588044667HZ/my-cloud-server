"""动作与下载管理器单元测试。"""

from __future__ import annotations

import os
import sys
import zipfile

from tae.actions import verify_file_hash
from tae.downloader import DownloadManager
from tae.hooks import MockHooks
from tae.models import (
    ArchiveExtractAction,
    FileAddAction,
    FileDeleteAction,
    FileEnsureAction,
    FileReplaceAction,
    HashInfo,
    ManifestEntry,
    ProcessExecAction,
    RollbackSpec,
    TargetSpec,
)
from tae.state import StateStore

from .helpers import TAETestCase, sha256_file

FAIL_CMD = f'"{sys.executable}" -c "import sys; sys.exit(3)"'
OK_CMD = f'"{sys.executable}" -c "print(42)"'


def _hooks() -> MockHooks:
    return MockHooks(current_version="1.0.0")


def _file_url(path: str) -> str:
    return "file:///" + path.replace("\\", "/")


def _spec(src_path: str):
    from tae.models import DownloadItem, DownloadSpec

    return DownloadSpec(
        urls=[DownloadItem(url=_file_url(src_path), priority=1)],
        hash=HashInfo(value=sha256_file(src_path)),
    )


class DownloaderTest(TAETestCase):
    """场景 2：模拟下载失败切换备用源。"""

    def setUp(self) -> None:
        super().setUp()
        self.manager = DownloadManager(self.path("cache"))
        self.content = b"hello world from source-2"

    def test_first_source_fails_then_failover(self) -> None:
        src2 = self.write_file("files/src2.bin", self.content)
        dest = self.path("out.bin")
        from tae.models import DownloadItem

        ok = self.manager.download(
            [
                DownloadItem(url="file:///no/such/file.bin", priority=1),
                DownloadItem(url="file:///" + src2.replace("\\", "/"), priority=2),
            ],
            dest,
            expected_hash=HashInfo(algo="sha256", value=self.sha256("files/src2.bin")),
        )
        self.assertTrue(ok)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), self.content)

    def test_hash_mismatch_triggers_next_source(self) -> None:
        src1 = self.write_file("files/src1.bad", b"tampered")
        src2 = self.write_file("files/src2.good", self.content)
        dest = self.path("out.bin")
        from tae.models import DownloadItem

        ok = self.manager.download(
            [
                DownloadItem(url="file:///" + src1.replace("\\", "/"), priority=1),
                DownloadItem(url="file:///" + src2.replace("\\", "/"), priority=2),
            ],
            dest,
            expected_hash=HashInfo(algo="sha256", value=self.sha256("files/src2.good")),
        )
        self.assertTrue(ok)
        self.assertEqual(self.sha256("out.bin"), self.sha256("files/src2.good"))

    def test_all_sources_fail_returns_false(self) -> None:
        dest = self.path("out.bin")
        from tae.models import DownloadItem

        ok = self.manager.download(
            [
                DownloadItem(url="file:///no/such/a.bin", priority=1),
                DownloadItem(url="file:///no/such/b.bin", priority=2),
            ],
            dest,
            expected_hash=HashInfo(algo="sha256", value="deadbeef"),
        )
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(dest))

    def test_cache_hit_skips_download(self) -> None:
        src = self.write_file("files/src.bin", self.content)
        dest = self.path("out.bin")
        from tae.models import DownloadItem

        spec = DownloadItem(url="file:///" + src.replace("\\", "/"), priority=1)
        ok = self.manager.download(
            [spec], dest, expected_hash=HashInfo(algo="sha256", value=self.sha256("files/src.bin"))
        )
        self.assertTrue(ok)
        mtime = os.path.getmtime(dest)
        ok2 = self.manager.download(
            [spec], dest, expected_hash=HashInfo(algo="sha256", value=self.sha256("files/src.bin"))
        )
        self.assertTrue(ok2)
        self.assertEqual(os.path.getmtime(dest), mtime)


class _ActionTestBase(TAETestCase):
    """动作测试基类：组装执行器所需依赖。"""

    def setUp(self) -> None:
        super().setUp()
        self.hooks = _hooks()
        self.downloader = DownloadManager(self.path("cache"))
        self.state = StateStore(self.path("state.db"))
        self.update_id = "test-update"
        self.addCleanup(self.state.close)

    def make_executor(self, action):
        from tae.actions import create_executor

        return create_executor(
            action, self.hooks, self.downloader, self.state, self.update_id
        )


class FileEnsureTest(_ActionTestBase):
    def test_replace_when_hash_mismatch(self) -> None:
        target = self.write_file("app/main.bin", b"old-version")
        new_content = b"new-version-v2"
        src = self.write_file("files/main.bin", new_content)
        action = FileEnsureAction(
            id="a1", type="file.ensure",
            target=TargetSpec(path=target, hash=HashInfo(value=sha256_file(src))),
            download=_spec(src),
            rollback=RollbackSpec(backup_original=True),
        )
        self.assertTrue(self.make_executor(action).execute())
        with open(target, "rb") as f:
            self.assertEqual(f.read(), new_content)
        # 原文件被备份，可用于回滚
        self.assertTrue(os.path.exists(target + ".tae-bak"))

    def test_skip_when_already_correct(self) -> None:
        target = self.write_file("app/main.bin", b"good")
        action = FileEnsureAction(
            id="a1", type="file.ensure",
            target=TargetSpec(path=target, hash=HashInfo(value=self.sha256("app/main.bin"))),
        )
        executor = self.make_executor(action)
        self.assertTrue(executor.execute())
        # 内容未变，未创建备份
        self.assertFalse(os.path.exists(target + ".tae-bak"))

    def test_create_when_missing(self) -> None:
        target = self.path("app/new.bin")
        src = self.write_file("files/new.bin", b"brand-new")
        action = FileEnsureAction(
            id="a1", type="file.ensure",
            target=TargetSpec(path=target),
            download=_spec(src),
        )
        self.assertTrue(self.make_executor(action).execute())
        self.assertTrue(os.path.exists(target))
        self.assertTrue(os.path.exists(target + ".tae-new"))

    def test_rollback_restores_original(self) -> None:
        target = self.write_file("app/main.bin", b"old-version")
        src = self.write_file("files/main.bin", b"new-version-v2")
        action = FileEnsureAction(
            id="a1", type="file.ensure",
            target=TargetSpec(path=target, hash=HashInfo(value=sha256_file(src))),
            download=_spec(src),
            rollback=RollbackSpec(backup_original=True),
        )
        executor = self.make_executor(action)
        self.assertTrue(executor.execute())
        self.assertTrue(executor.rollback())
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"old-version")
        self.assertFalse(os.path.exists(target + ".tae-bak"))

    def test_rollback_removes_new_file(self) -> None:
        target = self.path("app/new.bin")
        src = self.write_file("files/new.bin", b"brand-new")
        action = FileEnsureAction(
            id="a1", type="file.ensure",
            target=TargetSpec(path=target),
            download=_spec(src),
        )
        executor = self.make_executor(action)
        self.assertTrue(executor.execute())
        self.assertTrue(executor.rollback())
        self.assertFalse(os.path.exists(target))


class FileAddTest(_ActionTestBase):
    def test_fails_when_target_exists(self) -> None:
        target = self.write_file("app/exists.bin", b"exists")
        src = self.write_file("files/src.bin", b"content")
        action = FileAddAction(
            id="a1", type="file.add",
            target=TargetSpec(path=target),
            download=_spec(src),
        )
        self.assertFalse(self.make_executor(action).execute())

    def test_succeeds_when_absent(self) -> None:
        target = self.path("app/new.bin")
        src = self.write_file("files/src.bin", b"content")
        action = FileAddAction(
            id="a1", type="file.add",
            target=TargetSpec(path=target),
            download=_spec(src),
        )
        self.assertTrue(self.make_executor(action).execute())
        self.assertTrue(os.path.exists(target))


class FileReplaceTest(_ActionTestBase):
    def test_fails_when_target_missing(self) -> None:
        target = self.path("app/missing.bin")
        src = self.write_file("files/src.bin", b"content")
        action = FileReplaceAction(
            id="a1", type="file.replace",
            target=TargetSpec(path=target),
            download=_spec(src),
        )
        self.assertFalse(self.make_executor(action).execute())

    def test_succeeds_when_target_exists(self) -> None:
        target = self.write_file("app/exists.bin", b"old")
        src = self.write_file("files/src.bin", b"new")
        action = FileReplaceAction(
            id="a1", type="file.replace",
            target=TargetSpec(path=target),
            download=_spec(src),
        )
        self.assertTrue(self.make_executor(action).execute())


class FileDeleteTest(_ActionTestBase):
    def test_delete_without_backup(self) -> None:
        target = self.write_file("app/trash.bin", b"junk")
        action = FileDeleteAction(
            id="a1", type="file.delete", target=TargetSpec(path=target)
        )
        self.assertTrue(self.make_executor(action).execute())
        self.assertFalse(os.path.exists(target))

    def test_delete_with_backup_and_rollback(self) -> None:
        target = self.write_file("app/keep.bin", b"important")
        action = FileDeleteAction(
            id="a1", type="file.delete",
            target=TargetSpec(path=target),
            rollback=RollbackSpec(backup_original=True),
        )
        executor = self.make_executor(action)
        self.assertTrue(executor.execute())
        self.assertFalse(os.path.exists(target))
        self.assertTrue(executor.rollback())
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"important")


class ArchiveExtractTest(_ActionTestBase):
    def _build_zip(self, entries: dict) -> str:
        path = self.write_file("files/archive.zip", b"")
        with zipfile.ZipFile(path, "w") as zf:
            for name, content in entries.items():
                zf.writestr(name, content)
        return path

    def test_extract_install_and_rollback(self) -> None:
        zip_path = self._build_zip({"bin/app.txt": b"v2-content", "conf.ini": b"x=1"})
        target_dir = self.path("app")
        old = self.write_file("app/bin/app.txt", b"v1-content")

        manifest = [
            ManifestEntry(path="bin/app.txt", hash=HashInfo(value=self._hash_bytes(b"v2-content"))),
        ]
        action = ArchiveExtractAction(
            id="a1", type="archive.extract",
            target=TargetSpec(path=target_dir),
            download=_spec(zip_path),
            manifest=manifest,
        )
        executor = self.make_executor(action)
        self.assertTrue(executor.execute())
        with open(os.path.join(target_dir, "bin", "app.txt"), "rb") as f:
            self.assertEqual(f.read(), b"v2-content")
        with open(os.path.join(target_dir, "conf.ini"), "rb") as f:
            self.assertEqual(f.read(), b"x=1")
        self.assertTrue(executor.rollback())
        with open(os.path.join(target_dir, "bin", "app.txt"), "rb") as f:
            self.assertEqual(f.read(), b"v1-content")
        self.assertFalse(os.path.exists(os.path.join(target_dir, "conf.ini")))

    def test_manifest_hash_mismatch_fails(self) -> None:
        zip_path = self._build_zip({"bin/app.txt": b"v2-content"})
        target_dir = self.path("app")
        action = ArchiveExtractAction(
            id="a1", type="archive.extract",
            target=TargetSpec(path=target_dir),
            download=_spec(zip_path),
            manifest=[
                ManifestEntry(path="bin/app.txt", hash=HashInfo(value="0" * 64)),
            ],
        )
        self.assertFalse(self.make_executor(action).execute())

    def test_zip_slip_rejected(self) -> None:
        zip_path = self._build_zip({"../../evil.txt": b"pwn"})
        target_dir = self.path("app")
        action = ArchiveExtractAction(
            id="a1", type="archive.extract",
            target=TargetSpec(path=target_dir),
            download=_spec(zip_path),
        )
        self.assertFalse(self.make_executor(action).execute())
        self.assertFalse(os.path.exists(self.path("evil.txt")))

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        import hashlib

        return hashlib.sha256(data).hexdigest()


class ProcessExecTest(_ActionTestBase):
    def test_success(self) -> None:
        action = ProcessExecAction(id="a1", type="process.exec", command=OK_CMD)
        self.assertTrue(self.make_executor(action).execute())

    def test_failure(self) -> None:
        action = ProcessExecAction(id="a1", type="process.exec", command=FAIL_CMD)
        self.assertFalse(self.make_executor(action).execute())

    def test_rollback_command(self) -> None:
        action = ProcessExecAction(
            id="a1", type="process.exec", command=OK_CMD,
            rollback=RollbackSpec(command=OK_CMD),
        )
        executor = self.make_executor(action)
        self.assertTrue(executor.execute())
        self.assertTrue(executor.rollback())

    def test_script_download_and_execute(self) -> None:
        script = self.write_file("files/migrate.py", b"print('migrated')\n")
        action = ProcessExecAction(
            id="a1", type="process.exec",
            command=f'"{sys.executable}" {{script}}',
            download=_spec(script),
        )
        self.assertTrue(self.make_executor(action).execute())


class VerifyFileHashTest(TAETestCase):
    def test_verify_file_hash(self) -> None:
        path = self.write_file("a.bin", b"data")
        self.assertTrue(verify_file_hash(path, HashInfo(value=self.sha256("a.bin"))))
        self.assertFalse(verify_file_hash(path, HashInfo(value="0" * 64)))
        self.assertTrue(verify_file_hash(path, None))
