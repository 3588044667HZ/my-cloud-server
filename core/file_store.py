"""统一文件层：内容寻址 md5 + 引用计数 + 统一 GC。

- put(data, bucket) -> file_id（=md5，内容寻址）
- 物理落盘（ARCHITECTURE §5.4）：
    {PHOTOS_DIR}/orig/{ab}/{cd}/{md5}        # 原图：无扩展名（内容寻址，扩展名无意义）
    {PHOTOS_DIR}/thumb/{ab}/{cd}/{md5}.webp  # 压缩图：统一 .webp
- 引用计数在 core.db file_refs 表（全局，跨插件）；删除由各插件管理引用、core 统一 GC。
- 缩略图文件由调用方生成后经 put 入 'thumb' 桶，file_id 即缩略图内容 md5。
"""
import hashlib
import os
import shutil

from config import CORE_DB, PHOTOS_DIR
from core.auth import now_iso
from core.db import get_conn

_THUMB_EXT = '.webp'


def _bucket_path(file_id):
    return os.path.join(file_id[0:2], file_id[2:4], file_id)


def path_for(file_id, bucket='files'):
    """返回 file_id 的规范物理路径（thumb 桶带 .webp；可能不存在）。"""
    base = os.path.join(PHOTOS_DIR, bucket, _bucket_path(file_id))
    return base + _THUMB_EXT if bucket == 'thumb' else base


def find_path(file_id, bucket='files'):
    """返回实际存在的物理路径；兼容旧版 thumb 无后缀文件。不存在返回 None。"""
    p = path_for(file_id, bucket)
    if os.path.isfile(p):
        return p
    if bucket == 'thumb':
        legacy = p[: -len(_THUMB_EXT)]
        if os.path.isfile(legacy):
            return legacy
    return None


def exists(file_id, bucket='files'):
    return find_path(file_id, bucket) is not None


def compute_md5(data):
    return hashlib.md5(data).hexdigest()


def put(data, bucket='files'):
    """写入内容并建立引用计数记录。返回 file_id（=md5）。"""
    file_id = compute_md5(data)
    target = path_for(file_id, bucket)
    if not os.path.isfile(target):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, target)
    _cleanup_legacy(file_id, bucket)

    db = get_conn(CORE_DB)
    row = db.execute('SELECT * FROM file_refs WHERE file_id = ?', (file_id,)).fetchone()
    if row:
        db.execute('UPDATE file_refs SET ref_count = ref_count + 1 WHERE file_id = ?', (file_id,))
    else:
        db.execute(
            'INSERT INTO file_refs (file_id, ref_count, size, created_at) VALUES (?, 1, ?, ?)',
            (file_id, len(data), now_iso())
        )
    db.commit()
    return file_id


def put_from_path(src_path, bucket='files'):
    """按路径读入并内容寻址存储（大文件流式）。返回 file_id。"""
    h = hashlib.md5()
    with open(src_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    file_id = h.hexdigest()
    target = path_for(file_id, bucket)
    if not os.path.isfile(target):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + '.tmp'
        shutil.copyfile(src_path, tmp)
        os.replace(tmp, target)
    _cleanup_legacy(file_id, bucket)

    db = get_conn(CORE_DB)
    row = db.execute('SELECT * FROM file_refs WHERE file_id = ?', (file_id,)).fetchone()
    if row:
        db.execute('UPDATE file_refs SET ref_count = ref_count + 1 WHERE file_id = ?', (file_id,))
    else:
        db.execute(
            'INSERT INTO file_refs (file_id, ref_count, size, created_at) VALUES (?, 1, ?, ?)',
            (file_id, os.path.getsize(src_path), now_iso())
        )
    db.commit()
    return file_id


def _cleanup_legacy(file_id, bucket):
    """升级迁移：thumb 旧版无 .webp 后缀的同内容文件，写入规范路径后清理。"""
    if bucket != 'thumb':
        return
    legacy = path_for(file_id, bucket)[: -len(_THUMB_EXT)]
    try:
        if os.path.isfile(legacy):
            os.remove(legacy)
    except OSError:
        pass


def ref(file_id):
    """显式增加引用（如"笔记引用相册 file_id"场景）。"""
    db = get_conn(CORE_DB)
    row = db.execute('SELECT * FROM file_refs WHERE file_id = ?', (file_id,)).fetchone()
    if row:
        db.execute('UPDATE file_refs SET ref_count = ref_count + 1 WHERE file_id = ?', (file_id,))
    else:
        db.execute(
            'INSERT INTO file_refs (file_id, ref_count, size, created_at) VALUES (?, 1, 0, ?)',
            (file_id, now_iso())
        )
    db.commit()


def unref(file_id):
    """减少引用；引用数为 0 的文件由 gc() 统一回收。"""
    db = get_conn(CORE_DB)
    row = db.execute('SELECT * FROM file_refs WHERE file_id = ?', (file_id,)).fetchone()
    if row:
        db.execute(
            'UPDATE file_refs SET ref_count = MAX(0, ref_count - 1) WHERE file_id = ?',
            (file_id,)
        )
        db.commit()


def gc(bucket=None):
    """批量回收 ref_count == 0 的文件。返回回收文件数。"""
    db = get_conn(CORE_DB)
    rows = db.execute('SELECT file_id FROM file_refs WHERE ref_count <= 0').fetchall()
    buckets = [bucket] if bucket else (os.listdir(PHOTOS_DIR) if os.path.isdir(PHOTOS_DIR) else [])
    removed = 0
    for r in rows:
        for b in buckets:
            # 规范路径 + 旧版 thumb 无后缀路径都清理
            for p in (path_for(r['file_id'], b),):
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                        removed += 1
                    except OSError:
                        pass
            if b == 'thumb':
                legacy = path_for(r['file_id'], b)[: -len(_THUMB_EXT)]
                if os.path.isfile(legacy):
                    try:
                        os.remove(legacy)
                        removed += 1
                    except OSError:
                        pass
        db.execute('DELETE FROM file_refs WHERE file_id = ?', (r['file_id'],))
    db.commit()
    return removed


def disk_free_bytes():
    """PHOTOS_DIR 所在磁盘剩余空间（磁盘满保护）。"""
    try:
        return shutil.disk_usage(PHOTOS_DIR).free
    except OSError:
        return None


def schedule_gc(bucket=None):
    """入队 GC 任务（ARCHITECTURE §5.5/§7.3：删除 GC 走后台批量回收）。"""
    from core.task_queue import enqueue
    return enqueue('file.gc', {'bucket': bucket})


def gc_task_handler(payload):
    """任务队列处理器：file.gc。"""
    removed = gc(payload.get('bucket') if isinstance(payload, dict) else None)
    from core import event_bus
    event_bus.publish('file.gc.done', {'removed': removed})
    return removed
