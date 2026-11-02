-- photos 插件表结构（文档参考；实际建表由 migrations/versions/0001_initial.py 执行）

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
);

CREATE TABLE IF NOT EXISTS uploads (
    upload_id    TEXT PRIMARY KEY,
    photo_id     TEXT,
    total_size   INTEGER NOT NULL DEFAULT 0,
    expected_md5 TEXT NOT NULL DEFAULT '',
    seqs         TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
