"""从 mock-server 迁移存量数据到 my-cloud-server（SERVER_SKELETON.md §4）。

只读 mock-server，只写 my-cloud-server；可重复执行（幂等）：
  - 已存在的 data/notes.db 不会被覆盖（除非 --force）
  - files/backup 目录合并复制

用法：
  python scripts/migrate_from_mock.py [--mock <mock-server 路径>] [--username <所有者>] [--force]
"""
import argparse
import os
import shutil
import sqlite3
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from config import BACKUP_DIR, DATA_DIR  # noqa: E402
from core.auth import set_single_user  # noqa: E402
from core.bootstrap import init_core_db  # noqa: E402
from core.db import close_local, run_migrations  # noqa: E402

DEFAULT_MOCK = r'E:\PyCharmProjects\mock-server'

# 本脚本真实所在项目根（用于定位插件迁移目录；PROJECT_DIR 作为备份输出目标可被测试覆盖）
_SCRIPT_ROOT = PROJECT_DIR


def _read_users(mock_db):
    """读取 mock-server accounts.db 的用户列表。"""
    if not os.path.isfile(mock_db):
        return []
    conn = sqlite3.connect(mock_db)
    try:
        rows = conn.execute(
            'SELECT id, username, created_at FROM users ORDER BY id ASC'
        ).fetchall()
        return [dict(zip(('id', 'username', 'created_at'), r)) for r in rows]
    finally:
        conn.close()


def _note_counts(notes_db):
    """每个 user_id 的非删除笔记数。"""
    if not os.path.isfile(notes_db):
        return {}
    conn = sqlite3.connect(notes_db)
    try:
        rows = conn.execute(
            'SELECT user_id, COUNT(*) as c FROM notes WHERE deleted_at IS NULL '
            'GROUP BY user_id'
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


def _pick_owner(users, counts, username=None):
    """确定单用户所有者：显式指定 → 仅一个用户 → 多用户时要求显式指定（防选错）。"""
    if username:
        for u in users:
            if u['username'] == username:
                return u
        raise SystemExit(f'用户 {username} 不存在于 mock-server accounts.db')
    if len(users) == 1:
        return users[0]
    if not users:
        raise SystemExit('mock-server accounts.db 无用户，无法确定单用户身份')
    # 多用户：无法安全猜测真实所有者，列出候选并要求 --username 显式指定
    print('mock-server 存在多个用户，需用 --username 指定单用户所有者。候选：')
    for u in sorted(users, key=lambda x: (-counts.get(x['id'], 0), x['id'])):
        print(f'  {u["username"]:<24} id={u["id"]:<4} 非删除笔记={counts.get(u["id"], 0)}')
    raise SystemExit('请以 --username <name> 重新运行迁移脚本')


def _copy_notes_db(mock_dir, force):
    src = os.path.join(mock_dir, 'data', 'notes.db')
    dst = os.path.join(DATA_DIR, 'notes.db')
    if not os.path.isfile(src):
        print(f'跳过：mock-server 无 {src}')
        return
    if os.path.isfile(dst) and not force:
        print(f'跳过：{dst} 已存在（覆盖请加 --force）')
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    shutil.copyfile(src, dst)
    print(f'已复制 notes.db → {dst}')
    # 存量库打迁移标记 / 幂等升级（IF NOT EXISTS 不破坏现有表）
    migrations_dir = os.path.join(_SCRIPT_ROOT, 'plugins', 'notes', 'migrations')
    run_migrations(dst, migrations_dir)
    print('notes.db 迁移已对齐（alembic upgrade head）')


def _copy_dir(src, dst):
    """合并复制目录；源不存在则跳过。"""
    if not os.path.isdir(src):
        print(f'跳过：源目录不存在 {src}')
        return
    os.makedirs(dst, exist_ok=True)
    for entry in os.listdir(src):
        s = os.path.join(src, entry)
        d = os.path.join(dst, entry)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
    print(f'已合并复制 {src} → {dst}')


def run(mock_dir, username=None, force=False):
    """执行迁移；返回生成的配对码。"""
    if not os.path.isdir(mock_dir):
        raise SystemExit(f'mock-server 路径不存在: {mock_dir}')

    init_core_db()

    # 1. 确定单用户身份
    users = _read_users(os.path.join(mock_dir, 'data', 'accounts.db'))
    counts = _note_counts(os.path.join(mock_dir, 'data', 'notes.db'))
    owner = _pick_owner(users, counts, username)
    set_single_user(owner['id'], owner['username'])
    print(f'单用户身份：{owner["username"]} (id={owner["id"]})')

    # 2. notes.db
    _copy_notes_db(mock_dir, force)

    # 3. 附件 data/files/*
    _copy_dir(os.path.join(mock_dir, 'data', 'files'), os.path.join(DATA_DIR, 'files'))

    # 4. txt 冷备 backup/*（目标按 config.BACKUP_DIR，可配置）
    _copy_dir(os.path.join(mock_dir, 'backup'), BACKUP_DIR)

    # 5. 生成首台设备配对码
    from core.auth import generate_pair_code
    code = generate_pair_code()
    print(f'\n首台设备配对码（600s 一次性）：{code}')
    print('三端将 server_url 指向 my-cloud-server 后用该码重新配对即可。')

    close_local()
    return code


def main():
    parser = argparse.ArgumentParser(description='从 mock-server 迁移数据到 my-cloud-server')
    parser.add_argument('--mock', default=DEFAULT_MOCK, help='mock-server 项目路径')
    parser.add_argument('--username', default=None, help='指定单用户所有者')
    parser.add_argument('--force', action='store_true', help='覆盖已存在的 notes.db')
    args = parser.parse_args()
    run(args.mock, args.username, args.force)


if __name__ == '__main__':
    main()
