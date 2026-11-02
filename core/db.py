"""SQLite 生命周期：连接管理 + alembic 迁移。

- 每个插件一个独立库（core.db 属核心平台自身）。
- 迁移统一走 alembic：每个库对应一个 migrations 目录（含 env.py + versions/），
  在 enable 时执行 `upgrade head`。初始版本使用 CREATE TABLE IF NOT EXISTS，
  保证对从 mock-server 拷贝来的存量库同样幂等。
"""
import os
import sqlite3
import threading

from alembic import command
from alembic.config import Config as AlembicConfig

from flask import g, has_request_context

_local = threading.local()


def connect(db_path):
    """打开一个 SQLite 连接（WAL + 外键）。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _conn_key(db_path):
    return f'_db_{abs(hash(db_path))}'


def get_conn(db_path):
    """按请求缓存连接；请求上下文外用线程本地连接（scripts / worker 线程）。"""
    key = _conn_key(db_path)
    if has_request_context():
        conn = getattr(g, key, None)
        if conn is None:
            conn = connect(db_path)
            setattr(g, key, conn)
        return conn
    conn = getattr(_local, key, None)
    if conn is None:
        conn = connect(db_path)
        setattr(_local, key, conn)
    return conn


def close_all(exc=None):
    """teardown：关闭本请求打开的全部 SQLite 连接。"""
    for key in list(g.__dict__.keys()):
        if key.startswith('_db_'):
            conn = g.__dict__.pop(key)
            try:
                conn.close()
            except Exception:
                pass


def close_local():
    """关闭当前线程的线程本地连接（脚本收尾用）。"""
    for key in list(vars(_local).keys()):
        if key.startswith('_db_'):
            conn = getattr(_local, key, None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                delattr(_local, key)


def run_migrations(db_path, migrations_dir):
    """对指定库执行 alembic upgrade head。

    migrations_dir 必须包含 env.py 与 versions/。env.py 从 alembic 配置读取
    sqlalchemy.url 与 script_location，由本函数注入。
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    cfg = AlembicConfig()
    cfg.set_main_option('script_location', migrations_dir)
    cfg.set_main_option('sqlalchemy.url', f'sqlite:///{db_path}')
    command.upgrade(cfg, 'head')


def stamp_head(db_path, migrations_dir):
    """把存量库标记为已迁移（不执行 DDL），用于从 mock-server 拷贝的 notes.db。"""
    cfg = AlembicConfig()
    cfg.set_main_option('script_location', migrations_dir)
    cfg.set_main_option('sqlalchemy.url', f'sqlite:///{db_path}')
    command.stamp(cfg, 'head')


def ensure_tables(db_path, migrations_dir, expected_tables):
    """幂等兜底：alembic 版本声称已迁移但关键表缺失（存量异常库）时自愈。

    触发条件：如旧版本迁移脚本被修改、表被误删等，导致 alembic_version 已记录 head
    而实际表不存在。修复方式：清空版本标记并重跑迁移——所有迁移使用
    CREATE TABLE IF NOT EXISTS，重跑不破坏现有表与数据。
    """
    if not expected_tables:
        return
    conn = sqlite3.connect(db_path)
    try:
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()
    missing = expected_tables - existing
    if not missing:
        return
    print(f'[db] 检测到异常库：缺少表 {sorted(missing)}，重置迁移标记并重跑迁移')
    conn = sqlite3.connect(db_path)
    try:
        conn.execute('DROP TABLE IF EXISTS alembic_version')
        conn.commit()
    finally:
        conn.close()
    run_migrations(db_path, migrations_dir)


def dict_row(row):
    """sqlite3.Row → dict。"""
    return dict(row) if row is not None else None
