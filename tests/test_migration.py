"""迁移脚本幂等性测试（SERVER_SKELETON.md §4 / §8）：跑两遍结果一致。"""
import importlib.util
import os
import shutil
import sqlite3

import pytest

from core.bootstrap import init_core_db

_MIGRATE_SPEC = importlib.util.spec_from_file_location(
    'migrate_from_mock',
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 'scripts', 'migrate_from_mock.py'),
)
_migrate = importlib.util.module_from_spec(_MIGRATE_SPEC)


def _make_fake_mock(root):
    """构造一个 fake mock-server：accounts.db + notes.db + files + backup。"""
    mock = os.path.join(root, 'mock')
    data = os.path.join(mock, 'data')
    os.makedirs(data, exist_ok=True)

    acct = sqlite3.connect(os.path.join(data, 'accounts.db'))
    acct.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password TEXT, created_at TEXT)')
    acct.execute("INSERT INTO users VALUES (1, 'owner', 'x', '2026-01-01T00:00:00Z')")
    acct.commit()
    acct.close()

    notes = sqlite3.connect(os.path.join(data, 'notes.db'))
    notes.execute('''
        CREATE TABLE notes (
            id TEXT PRIMARY KEY, user_id INTEGER, title TEXT, content TEXT,
            notebook_id TEXT, color TEXT, is_pinned INTEGER, version INTEGER,
            created_at TEXT, updated_at TEXT, deleted_at TEXT)
    ''')
    notes.execute('''
        CREATE TABLE notebooks (id TEXT PRIMARY KEY, user_id INTEGER, name TEXT,
                                color TEXT, created_at TEXT)
    ''')
    notes.execute('''
        CREATE TABLE share_settings (user_id INTEGER PRIMARY KEY, logo_text TEXT, watermark TEXT)
    ''')
    notes.execute('''
        CREATE TABLE attachments (
            attach_id TEXT PRIMARY KEY, user_id INTEGER, note_id TEXT, type INTEGER,
            file_name TEXT, file_id TEXT, width INTEGER, height INTEGER,
            md5 TEXT, state INTEGER, created_at TEXT)
    ''')
    notes.execute(
        "INSERT INTO notes VALUES ('n1', 1, 'Migrated', 'content', NULL, 'blue', 0, 1, "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL)")
    notes.commit()
    notes.close()

    files_dir = os.path.join(data, 'files')
    os.makedirs(os.path.join(files_dir, '1'), exist_ok=True)
    with open(os.path.join(files_dir, '1', 'f_abc.bin'), 'wb') as f:
        f.write(b'file-content')

    backup_dir = os.path.join(mock, 'backup')
    os.makedirs(os.path.join(backup_dir, 'owner'), exist_ok=True)
    with open(os.path.join(backup_dir, 'owner', 'n1.txt'), 'w') as f:
        f.write('{}')
    return mock


@pytest.fixture(scope='module')
def fake_mock(tmp_path_factory):
    return _make_fake_mock(str(tmp_path_factory.mktemp('migrate')))


@pytest.fixture(scope='function')
def migrate_module(monkeypatch, tmp_path):
    target = str(tmp_path)
    _MIGRATE_SPEC.loader.exec_module(_migrate)
    # 重定向到隔离目标（脚本与 core 均指向独立目录，不影响其它测试）
    target_data = os.path.join(target, 'data')
    target_core_db = os.path.join(target_data, 'core.db')
    monkeypatch.setattr(_migrate, 'PROJECT_DIR', target)
    monkeypatch.setattr(_migrate, 'DATA_DIR', target_data)
    monkeypatch.setattr(_migrate, 'BACKUP_DIR', os.path.join(target, 'backup'))
    import core.auth
    import core.bootstrap
    monkeypatch.setattr(core.bootstrap, 'CORE_DB', target_core_db)
    monkeypatch.setattr(core.auth, 'CORE_DB', target_core_db)
    init_core_db()
    return _migrate


def _query_target(target, sql):
    db_path = os.path.join(target, 'data', 'notes.db')
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_migration_idempotent(fake_mock, migrate_module):
    target = migrate_module.PROJECT_DIR
    # 第一次
    migrate_module.run(fake_mock, username='owner')
    # 第二次（幂等：notes.db 已存在则跳过覆盖）
    migrate_module.run(fake_mock, username='owner')

    # 单用户配置
    core = sqlite3.connect(os.path.join(target, 'data', 'core.db'))
    cfg = dict(core.execute('SELECT key, value FROM app_config').fetchall())
    core.close()
    assert cfg['single_user_id'] == '1'
    assert cfg['single_user_name'] == 'owner'

    # notes.db 迁移对齐
    rows = _query_target(target, 'SELECT id, title, user_id FROM notes')
    assert ('n1', 'Migrated', 1) in [tuple(r) for r in rows]
    alembic = _query_target(target, 'SELECT version_num FROM alembic_version')
    assert alembic == [('0001_initial',)]

    # 附件文件复制（不重复）
    file_path = os.path.join(target, 'data', 'files', '1', 'f_abc.bin')
    assert os.path.isfile(file_path)
    assert os.path.getsize(file_path) == len(b'file-content')

    # 冷备
    assert os.path.isfile(os.path.join(target, 'backup', 'owner', 'n1.txt'))
