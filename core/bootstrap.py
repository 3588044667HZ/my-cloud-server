"""核心平台启动引导：初始化 core.db（迁移 + 异常库自愈）。

scripts（gen_pair_code / migrate_from_mock）与 app.py 共用。
"""
import os

from config import CORE_DB
from core.db import ensure_tables, run_migrations

_MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'migrations')

# core 库关键表（ensure_tables 自愈校验用）
_CORE_TABLES = {'devices', 'app_config', 'pair_codes', 'file_refs', 'tasks'}


def init_core_db():
    """确保 core.db 存在、迁移到 head；存量异常库自动修复。"""
    run_migrations(CORE_DB, _MIGRATIONS_DIR)
    ensure_tables(CORE_DB, _MIGRATIONS_DIR, _CORE_TABLES)
