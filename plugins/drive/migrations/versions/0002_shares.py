"""drive 库迁移 0002：后台硬盘管理——drive_shares（共享配置持久化）。

后台增删硬盘无需改 config.py；存量库从 0001 升级应用本迁移。
"""
from alembic import op

revision = '0002_shares'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''
        CREATE TABLE IF NOT EXISTS drive_shares (
            name       TEXT PRIMARY KEY,
            path       TEXT NOT NULL,
            comment    TEXT NOT NULL DEFAULT '',
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')


def downgrade():
    op.execute('DROP TABLE IF EXISTS drive_shares')
