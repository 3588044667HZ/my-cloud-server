"""drive 库初始迁移：drive_settings（全局开关）/ trash_index（回收站索引）。"""
from alembic import op

revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''
        CREATE TABLE IF NOT EXISTS drive_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')

    op.execute('''
        CREATE TABLE IF NOT EXISTS trash_index (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            share      TEXT NOT NULL,
            orig_path  TEXT NOT NULL,
            trash_path TEXT NOT NULL,
            deleted_at TEXT NOT NULL
        )
    ''')
    op.execute('CREATE INDEX IF NOT EXISTS idx_trash_share ON trash_index(share)')


def downgrade():
    op.execute('DROP TABLE IF EXISTS trash_index')
    op.execute('DROP TABLE IF EXISTS drive_settings')
