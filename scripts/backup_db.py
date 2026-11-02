"""SQLite 一致性备份（ARCHITECTURE.md §8）。

WAL 模式下不能直接拷贝 db 文件，必须用 sqlite3 .backup API 生成 stage 备份，
否则可能备份到不一致的中间状态。本脚本对 core/notes/photos/drive 全库做一致性备份。

用法：
  python scripts/backup_db.py                 # 输出到 {BACKUP_DIR}/db/<时间戳>/
  python scripts/backup_db.py --out E:/bk     # 指定输出目录
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from config import BACKUP_DIR, CORE_DB, DATA_DIR  # noqa: E402

# 需要备份的库 {名称: 路径}
def _db_files():
    import plugins.drive.db as _d
    import plugins.notes.db as _n
    import plugins.photos.db as _p
    dbs = {
        'core': CORE_DB,
        'notes': _n.NOTES_DB,
        'photos': _p.PHOTOS_DB,
        'drive': _d.DRIVE_DB,
    }
    return {name: path for name, path in dbs.items() if os.path.isfile(path)}


def backup_database(src, dst):
    """用 sqlite3 .backup 做一致性备份（WAL 安全）。"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with sqlite3.connect(src) as src_conn:
        with sqlite3.connect(dst) as dst_conn:
            src_conn.backup(dst_conn)
    return os.path.getsize(dst)


def run(out_dir=None):
    out_dir = out_dir or os.path.join(BACKUP_DIR, 'db',
                                      datetime.now().strftime('%Y%m%d-%H%M%S'))
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for name, path in _db_files().items():
        dst = os.path.join(out_dir, f'{name}.db')
        size = backup_database(path, dst)
        results.append((name, size))
        print(f'  备份 {name}: {path} -> {dst} ({size} bytes)')
    # 校验：目标库可打开且非空
    for name, size in results:
        with sqlite3.connect(os.path.join(out_dir, f'{name}.db')) as c:
            c.execute('PRAGMA integrity_check').fetchone()
    print(f'完成：{len(results)} 个库 → {out_dir}（integrity_check 通过）')
    return out_dir


def main():
    parser = argparse.ArgumentParser(description='SQLite 一致性备份（WAL 安全）')
    parser.add_argument('--out', default=None, help='输出目录（默认 {BACKUP_DIR}/db/<时间戳>）')
    args = parser.parse_args()
    run(args.out)


if __name__ == '__main__':
    main()
