"""photos 库初始迁移：photos / uploads（ARCHITECTURE.md §5.3）。"""
from alembic import op

revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''
        CREATE TABLE IF NOT EXISTS photos (
            id            TEXT PRIMARY KEY,
            device_id     TEXT,
            client_id     TEXT UNIQUE,
            media_id      TEXT,
            filename      TEXT NOT NULL DEFAULT '',
            width         INTEGER NOT NULL DEFAULT 0,
            height        INTEGER NOT NULL DEFAULT 0,
            size          INTEGER NOT NULL DEFAULT 0,
            thumb_file_id TEXT NOT NULL DEFAULT '',
            orig_file_id  TEXT NOT NULL DEFAULT '',
            md5           TEXT NOT NULL DEFAULT '',
            version       INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            deleted_at    TEXT
        )
    ''')
    op.execute('CREATE INDEX IF NOT EXISTS idx_photos_updated ON photos(updated_at)')
    op.execute('CREATE INDEX IF NOT EXISTS idx_photos_client ON photos(client_id)')

    op.execute('''
        CREATE TABLE IF NOT EXISTS uploads (
            upload_id    TEXT PRIMARY KEY,
            photo_id     TEXT,
            total_size   INTEGER NOT NULL DEFAULT 0,
            expected_md5 TEXT NOT NULL DEFAULT '',
            seqs         TEXT NOT NULL DEFAULT '[]',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )
    ''')


def downgrade():
    op.execute('DROP TABLE IF EXISTS uploads')
    op.execute('DROP TABLE IF EXISTS photos')
