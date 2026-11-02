"""异常存量库自愈测试：alembic_version 声称已迁移但表缺失 → 自动重跑迁移。"""
import os
import sqlite3

import pytest


def _stale_db(path, version):
    """造一个"版本表声称已迁移但表缺失"的异常库。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)')
    conn.execute('INSERT INTO alembic_version VALUES (?)', (version,))
    conn.commit()
    conn.close()


def _tables(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()


class TestCoreStaleRepair:
    def test_init_core_db_repairs(self, tmp_path, monkeypatch):
        import core.bootstrap
        target = str(tmp_path / 'core.db')
        _stale_db(target, '0001_core')  # 复现用户报错场景
        monkeypatch.setattr(core.bootstrap, 'CORE_DB', target)
        core.bootstrap.init_core_db()
        tables = _tables(target)
        assert 'tasks' in tables
        assert 'devices' in tables
        assert 'app_config' in tables

    def test_fresh_db_no_warning(self, tmp_path, monkeypatch, capsys):
        import core.bootstrap
        target = str(tmp_path / 'core.db')
        monkeypatch.setattr(core.bootstrap, 'CORE_DB', target)
        core.bootstrap.init_core_db()
        out = capsys.readouterr().out
        assert '异常库' not in out  # 全新库不应触发自愈


class TestPluginStaleRepair:
    def test_notes_db_repair(self, tmp_path):
        from core.db import ensure_tables
        migrations = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'plugins', 'notes', 'migrations')
        target = str(tmp_path / 'notes.db')
        _stale_db(target, '0001_initial')
        ensure_tables(target, migrations, {'notes', 'notebooks', 'attachments'})
        tables = _tables(target)
        assert {'notes', 'notebooks', 'share_settings', 'attachments'} <= tables

    def test_drive_db_repair(self, tmp_path):
        from core.db import ensure_tables
        migrations = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'plugins', 'drive', 'migrations')
        target = str(tmp_path / 'drive.db')
        _stale_db(target, '0002_shares')
        ensure_tables(target, migrations, {'drive_settings', 'trash_index', 'drive_shares'})
        tables = _tables(target)
        assert {'drive_settings', 'trash_index', 'drive_shares'} <= tables
