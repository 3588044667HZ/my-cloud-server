-- drive 插件表结构（文档参考；实际建表由 migrations/versions/0001_initial.py 执行）

CREATE TABLE IF NOT EXISTS drive_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trash_index (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    share      TEXT NOT NULL,
    orig_path  TEXT NOT NULL,
    trash_path TEXT NOT NULL,
    deleted_at TEXT NOT NULL
);
