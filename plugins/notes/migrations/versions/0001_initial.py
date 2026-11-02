"""notes 库初始迁移：notebooks / notes / share_settings / attachments（沿用 mock-server 表结构）。

使用 IF NOT EXISTS，对从 mock-server 拷贝的存量 notes.db 幂等。
"""
from alembic import op

revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''
        CREATE TABLE IF NOT EXISTS notebooks (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#4A90D9',
            created_at TEXT NOT NULL
        )
    ''')
    op.execute('CREATE INDEX IF NOT EXISTS idx_notebooks_user ON notebooks(user_id)')

    op.execute('''
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            notebook_id TEXT,
            color TEXT NOT NULL DEFAULT 'blue',
            is_pinned INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT
        )
    ''')
    op.execute('CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id)')
    op.execute('CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at)')
    op.execute('CREATE INDEX IF NOT EXISTS idx_notes_notebook ON notes(notebook_id)')

    op.execute('''
        CREATE TABLE IF NOT EXISTS share_settings (
            user_id INTEGER PRIMARY KEY,
            logo_text TEXT NOT NULL DEFAULT '分享来自 Open Note',
            watermark TEXT NOT NULL DEFAULT '备忘录'
        )
    ''')

    op.execute('''
        CREATE TABLE IF NOT EXISTS attachments (
            attach_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            note_id TEXT NOT NULL,
            type INTEGER NOT NULL DEFAULT 0,
            file_name TEXT NOT NULL DEFAULT '',
            file_id TEXT NOT NULL,
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            md5 TEXT NOT NULL DEFAULT '',
            state INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    ''')
    op.execute('CREATE INDEX IF NOT EXISTS idx_attachments_note ON attachments(note_id)')
    op.execute('CREATE INDEX IF NOT EXISTS idx_attachments_user ON attachments(user_id)')


def downgrade():
    op.execute('DROP TABLE IF EXISTS attachments')
    op.execute('DROP TABLE IF EXISTS share_settings')
    op.execute('DROP TABLE IF EXISTS notes')
    op.execute('DROP TABLE IF EXISTS notebooks')
