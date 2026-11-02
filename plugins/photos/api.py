"""photos 插件 API：register / 上传(整包+分片) / sync / download / thumb / /api/space。

文件统一走 core/file_store（内容寻址 md5），orig/thumb 双桶，永不 404（占位图兜底）。
"""
import io
import json
import os
import shutil
import uuid

from flask import Blueprint, Response, g, request, send_file

from config import (
    DISK_FREE_THRESHOLD, PHOTO_CHUNK_SIZE, PHOTO_MAX_CHUNK_SIZE, PHOTO_MAX_TOTAL_SIZE,
    PHOTOS_DIR, UPLOAD_SESSION_TTL,
)
from core import file_store
from core.auth import now_iso
from core.protocol import error, success
from core.util import generate_id
from plugins.photos.db import get_photos_db, photo_row_to_dict
from plugins.photos.thumb import image_dimensions, make_thumbnail_bytes

photos_bp = Blueprint('photos', __name__)

UPLOADS_DIR = os.path.join(PHOTOS_DIR, '.uploads')

# 磁盘满保护阈值（剩余空间低于该值暂停原图上传）
DISK_THRESHOLD = DISK_FREE_THRESHOLD


def _photo_id():
    return generate_id('p')


def _placeholder_bytes():
    """服务端内置占位图（与 notes 共用逻辑，缺省灰色 WEBP）。"""
    from config import PLACEHOLDER_IMAGE_PATH
    if os.path.isfile(PLACEHOLDER_IMAGE_PATH):
        with open(PLACEHOLDER_IMAGE_PATH, 'rb') as f:
            return f.read()
    from PIL import Image
    img = Image.new('RGB', (480, 360), (235, 235, 235))
    buf = io.BytesIO()
    img.save(buf, 'WEBP', quality=70)
    return buf.getvalue()


_MIME_EXT = {
    'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif',
    'image/webp': '.webp', 'video/mp4': '.mp4', 'video/quicktime': '.mov',
}


def _download_name(row, mimetype, want_thumb):
    """下载文件名：优先照片原始文件名；缺扩展名时按实际类型补；压缩图统一 .webp。"""
    base = (row['filename'] or '').strip() or row['id']
    base = os.path.basename(base.replace('\\', '/'))
    ext = _MIME_EXT.get(mimetype, '')
    if want_thumb:
        stem = os.path.splitext(base)[0] if ext else base
        return f'{stem}.webp'
    if ext and not os.path.splitext(base)[1]:
        return f'{base}{ext}'
    return base


def _send_photo_file(row, file_id, bucket, state, want_thumb=False):
    path = file_store.find_path(file_id, bucket)
    if path:
        mimetype = _sniff_mime(path)
        download_name = _download_name(row, mimetype, want_thumb)
        resp = send_file(path, mimetype=mimetype, download_name=download_name)
        resp.headers['X-Photo-State'] = state
        resp.headers['Cache-Control'] = 'no-cache'
        return resp
    resp = Response(_placeholder_bytes(), mimetype='image/webp')
    resp.headers['X-Photo-State'] = state
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _sniff_mime(filepath):
    with open(filepath, 'rb') as f:
        head = f.read(12)
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if head.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if head.startswith(b'GIF87a') or head.startswith(b'GIF89a'):
        return 'image/gif'
    if len(head) >= 12 and head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp'
    return 'application/octet-stream'


def _check_disk_space():
    """原图上传前磁盘满保护；不足返回错误响应。"""
    free = file_store.disk_free_bytes()
    if free is not None and free < DISK_THRESHOLD:
        return error('Insufficient disk space. Upload paused', code=10507, status=507)
    return None


def _host_url():
    return request.host_url.rstrip('/')


def _photo_with_urls(row):
    photo = photo_row_to_dict(row)
    host = _host_url()
    photo['thumbUrl'] = f'{host}/api/photos/{row["id"]}/download?size=thumb'
    photo['origUrl'] = f'{host}/api/photos/{row["id"]}/download'
    return photo


# ---------- register ----------

@photos_bp.route('/api/photos/register', methods=['POST'])
def register():
    data = request.get_json(silent=True) or {}
    client_id = (data.get('clientId') or '').strip()
    media_id = (data.get('mediaId') or '').strip()
    if not client_id:
        return error('clientId is required')
    filename = data.get('filename') or ''
    width = data.get('width') or 0
    height = data.get('height') or 0
    size = data.get('size') or 0
    md5_val = data.get('md5') or ''

    db = get_photos_db()
    row = db.execute('SELECT * FROM photos WHERE client_id = ?', (client_id,)).fetchone()
    if row:
        return success(_photo_with_urls(row))

    photo_id = _photo_id()
    now_ts = now_iso()
    db.execute(
        '''INSERT INTO photos
           (id, device_id, client_id, media_id, filename, width, height, size,
            thumb_file_id, orig_file_id, md5, version, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, 1, ?, ?)''',
        (photo_id, g.device['device_id'], client_id, media_id, filename, width, height, size,
         md5_val, now_ts, now_ts)
    )
    db.commit()
    row = db.execute('SELECT * FROM photos WHERE id = ?', (photo_id,)).fetchone()
    return success(_photo_with_urls(row))


# ---------- 整包上传 ----------

@photos_bp.route('/api/photos/<photo_id>/file', methods=['PUT'])
def upload_file(photo_id):
    db = get_photos_db()
    row = db.execute('SELECT * FROM photos WHERE id = ?', (photo_id,)).fetchone()
    if not row:
        return error('Photo not found. Register first', code=10404, status=404)

    content = request.get_data()
    if not content:
        return error('Empty file', code=10400, status=400)
    if len(content) > PHOTO_MAX_TOTAL_SIZE:
        return error('File too large', code=10413, status=413)

    expected_md5 = row['md5'] or request.headers.get('Content-MD5', '')
    if expected_md5:
        import hashlib
        if hashlib.md5(content).hexdigest() != expected_md5:
            return error('MD5 mismatch', code=10405, status=400)

    disk_err = _check_disk_space()
    if disk_err:
        return disk_err

    # 原图 + 压缩图（内容寻址，跨插件去重）
    orig_file_id = file_store.put(content, bucket='orig')
    thumb_data = make_thumbnail_bytes_from_bytes(content)
    thumb_file_id = file_store.put(thumb_data, bucket='thumb') if thumb_data else ''

    width, height = _image_dims_from_bytes(content)

    now_ts = now_iso()
    version = row['version'] + 1
    db.execute(
        '''UPDATE photos SET thumb_file_id=?, orig_file_id=?, md5=?, size=?, width=?, height=?,
           version=?, updated_at=? WHERE id=?''',
        (thumb_file_id, orig_file_id, expected_md5, len(content), width, height,
         version, now_ts, photo_id)
    )
    db.commit()
    row = db.execute('SELECT * FROM photos WHERE id = ?', (photo_id,)).fetchone()

    # 事件：照片上传完成（ARCHITECTURE §5.2 事件模式）
    from core import event_bus
    event_bus.publish('photo.uploaded', {
        'id': photo_id, 'orig_file_id': orig_file_id, 'thumb_file_id': thumb_file_id,
        'size': len(content), 'source': 'upload',
    })
    return success(_photo_with_urls(row))


def make_thumbnail_bytes_from_bytes(content):
    """从内存字节生成压缩图字节。"""
    try:
        import hashlib, tempfile
        h = hashlib.md5(content).hexdigest()
        tmp_dir = os.path.join(PHOTOS_DIR, '.tmp')
        os.makedirs(tmp_dir, exist_ok=True)
        src = os.path.join(tmp_dir, f'{h}.src')
        with open(src, 'wb') as f:
            f.write(content)
        data = make_thumbnail_bytes(src)
        try:
            os.remove(src)
        except OSError:
            pass
        return data
    except Exception:
        return None


def _image_dims_from_bytes(content):
    try:
        import hashlib, tempfile
        h = hashlib.md5(content).hexdigest()
        tmp_dir = os.path.join(PHOTOS_DIR, '.tmp')
        os.makedirs(tmp_dir, exist_ok=True)
        src = os.path.join(tmp_dir, f'{h}.src')
        with open(src, 'wb') as f:
            f.write(content)
        dims = image_dimensions(src)
        try:
            os.remove(src)
        except OSError:
            pass
        return dims
    except Exception:
        return 0, 0


# ---------- sync ----------

@photos_bp.route('/api/photos/list', methods=['GET'])
def list_photos():
    """分页浏览全部未删除照片（SPEC-WEB-FRONTEND.md §6.2 列表接口）。"""
    keyword = request.args.get('keyword', '').strip()
    page = max(1, int(request.args.get('page', 1)))
    size = min(100, max(1, int(request.args.get('size', 30))))

    db = get_photos_db()
    conditions = ['deleted_at IS NULL']
    params = []
    if keyword:
        conditions.append('filename LIKE ?')
        params.append(f'%{keyword}%')

    where = ' AND '.join(conditions)
    total = db.execute(f'SELECT COUNT(*) as cnt FROM photos WHERE {where}', params).fetchone()['cnt']
    rows = db.execute(
        f'SELECT * FROM photos WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?',
        params + [size, (page - 1) * size]
    ).fetchall()

    return success([_photo_with_urls(r) for r in rows], extra={
        'pagination': {
            'page': page, 'size': size, 'total': total,
            'totalPages': max(1, (total + size - 1) // size),
        }
    })


@photos_bp.route('/api/photos/sync', methods=['GET'])
def sync():
    since = request.args.get('since', '').strip()
    db = get_photos_db()
    if since:
        updated = db.execute(
            'SELECT * FROM photos WHERE updated_at > ? AND deleted_at IS NULL',
            (since,)
        ).fetchall()
        deleted = db.execute(
            'SELECT id FROM photos WHERE deleted_at IS NOT NULL AND deleted_at > ?',
            (since,)
        ).fetchall()
        deleted_ids = [r['id'] for r in deleted]
    else:
        updated = db.execute(
            'SELECT * FROM photos WHERE deleted_at IS NULL ORDER BY updated_at DESC'
        ).fetchall()
        deleted_ids = []
    return success({
        'updated': [_photo_with_urls(r) for r in updated],
        'deletedIds': deleted_ids,
        'serverTime': now_iso(),
    })


# ---------- download ----------

@photos_bp.route('/api/photos/<photo_id>/download', methods=['GET'])
def download(photo_id):
    db = get_photos_db()
    row = db.execute('SELECT * FROM photos WHERE id = ?', (photo_id,)).fetchone()
    want_thumb = request.args.get('size', '') == 'thumb'
    if row:
        if want_thumb:
            if row['thumb_file_id']:
                return _send_photo_file(row, row['thumb_file_id'], 'thumb', 'ready', want_thumb=True)
            return _send_photo_file(row, row['orig_file_id'], 'orig', 'pending')
        if row['orig_file_id']:
            return _send_photo_file(row, row['orig_file_id'], 'orig', 'ready')
        return _send_photo_file(row, '', 'orig', 'pending')
    return _send_photo_file(None, '', 'orig', 'missing')


# ---------- 元数据写回 / 软删 / GC ----------

@photos_bp.route('/api/photos/<photo_id>', methods=['PUT'])
def update_photo(photo_id):
    data = request.get_json(silent=True) or {}
    db = get_photos_db()
    row = db.execute('SELECT * FROM photos WHERE id = ?', (photo_id,)).fetchone()
    if not row:
        return error('Photo not found', code=10404, status=404)

    # If-Match 乐观锁（照抄 notes 模块，ARCHITECTURE §4.1）
    client_updated_at = request.headers.get('If-Match', '')
    if client_updated_at and client_updated_at != row['updated_at']:
        return error(
            f'Conflict: photo was modified by another device. Server version: {row["updated_at"]}',
            code=409, status=409,
        )

    now_ts = now_iso()
    version = row['version'] + 1
    md5_val = data.get('md5', row['md5'])
    size = data.get('size', row['size'])
    db.execute(
        'UPDATE photos SET md5=?, size=?, version=?, updated_at=? WHERE id=?',
        (md5_val, size, version, now_ts, photo_id)
    )
    db.commit()
    row = db.execute('SELECT * FROM photos WHERE id = ?', (photo_id,)).fetchone()
    return success(_photo_with_urls(row))


@photos_bp.route('/api/photos/<photo_id>', methods=['DELETE'])
def delete_photo(photo_id):
    """软删（tombstone）：照片进回收站，保留 30 天；文件不释放（可恢复）。"""
    db = get_photos_db()
    row = db.execute('SELECT * FROM photos WHERE id = ?', (photo_id,)).fetchone()
    if not row:
        return error('Photo not found', code=10404, status=404)
    now_ts = now_iso()
    version = row['version'] + 1
    db.execute(
        'UPDATE photos SET deleted_at=?, version=?, updated_at=? WHERE id=?',
        (now_ts, version, now_ts, photo_id)
    )
    db.commit()
    from core import event_bus
    event_bus.publish('photo.deleted', {'id': photo_id})
    return success(None)


def _unref_photo_files(row):
    """彻底删除照片时释放其文件引用（orig/thumb），引用归零后由 GC 回收。"""
    for fid in (row['orig_file_id'], row['thumb_file_id']):
        if fid:
            file_store.unref(fid)


def _permanently_delete_photo(db, row):
    """物理删除单条照片记录（回收站彻底删除/超期清理调用）：释放文件引用 + 删记录。"""
    _unref_photo_files(row)
    db.execute('DELETE FROM photos WHERE id = ?', (row['id'],))


def _photo_trash_dict(row):
    from core.trash import remain_days
    photo = photo_row_to_dict(row)
    photo['remainDays'] = remain_days(row['deleted_at'])
    return photo


# ---------- 回收站（统一 API 形状，SPEC-CLOUD-RECYCLE-RECOVERY.md §1.3） ----------

@photos_bp.route('/api/photos/trash', methods=['GET'])
def trash_list():
    """回收站列表：含 deletedAt / remainDays，分页。"""
    page = max(1, int(request.args.get('page', 1)))
    size = min(100, max(1, int(request.args.get('size', 30))))
    db = get_photos_db()
    total = db.execute(
        'SELECT COUNT(*) as cnt FROM photos WHERE deleted_at IS NOT NULL').fetchone()['cnt']
    rows = db.execute(
        'SELECT * FROM photos WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT ? OFFSET ?',
        (size, (page - 1) * size)
    ).fetchall()
    from core.protocol import pagination
    return success([_photo_trash_dict(r) for r in rows], extra=pagination(page, size, total))


@photos_bp.route('/api/photos/trash/restore', methods=['POST'])
def trash_restore():
    """恢复（单 id 或 id[] 批量）：清除 deleted_at，文件仍在。"""
    data = request.get_json(silent=True) or {}
    raw = data.get('id') or data.get('ids')
    ids = [raw] if isinstance(raw, str) else (raw or [])
    if not ids:
        return error('id or ids is required')
    db = get_photos_db()
    now_ts = now_iso()
    restored = 0
    for pid in ids:
        cur = db.execute(
            'UPDATE photos SET deleted_at=NULL, version=version+1, updated_at=? '
            'WHERE id=? AND deleted_at IS NOT NULL',
            (now_ts, pid)
        )
        if cur.rowcount:
            restored += 1
    db.commit()
    return success({'restored': restored})


@photos_bp.route('/api/photos/trash/<photo_id>', methods=['DELETE'])
def trash_delete_one(photo_id):
    """回收站内彻底删除（强警告后调用）：物理删除 + 释放文件。"""
    db = get_photos_db()
    row = db.execute('SELECT * FROM photos WHERE id = ? AND deleted_at IS NOT NULL',
                     (photo_id,)).fetchone()
    if not row:
        return error('Photo not found in trash', code=10404, status=404)
    _permanently_delete_photo(db, row)
    db.commit()
    file_store.schedule_gc()
    return success(None)


@photos_bp.route('/api/photos/trash/empty', methods=['DELETE'])
def trash_empty():
    """清空回收站：全部物理删除 + 释放文件 + GC。"""
    db = get_photos_db()
    rows = db.execute('SELECT * FROM photos WHERE deleted_at IS NOT NULL').fetchall()
    for r in rows:
        _permanently_delete_photo(db, r)
    db.commit()
    file_store.schedule_gc()
    return success({'removed': len(rows)})


@photos_bp.route('/api/photos/gc', methods=['POST'])
def trigger_gc():
    """触发后台文件 GC（引用计数为 0 的文件物理回收），入队异步执行。"""
    task_id = file_store.schedule_gc()
    return success({'taskId': task_id})


def sweep_expired():
    """超期自动清理：物理删除超过保留期的回收站照片。返回清除数。"""
    db = get_photos_db()
    rows = db.execute('SELECT * FROM photos WHERE deleted_at IS NOT NULL').fetchall()
    removed = 0
    for r in rows:
        from core.trash import is_expired
        if is_expired(r['deleted_at']):
            _permanently_delete_photo(db, r)
            removed += 1
    if removed:
        db.commit()
        file_store.schedule_gc()
    return removed


# ---------- 分片上传（SPEC-UPLOAD-TRANSPORT.md §3） ----------

@photos_bp.route('/api/photos/upload/init', methods=['POST'])
def upload_init():
    data = request.get_json(silent=True) or {}
    photo_id = (data.get('photoId') or '').strip()
    resume_id = (data.get('uploadId') or '').strip()
    expected_md5 = data.get('md5') or ''

    db = get_photos_db()

    # 秒传：照片已含该内容（orig 就绪）→ 直接完成
    if photo_id:
        row = db.execute('SELECT * FROM photos WHERE id = ?', (photo_id,)).fetchone()
        if row and row['orig_file_id'] and (not expected_md5 or row['md5'] == expected_md5):
            return success({'exists': True, 'id': row['id'],
                            'fileId': row['orig_file_id'], 'size': row['size']})

    # 断点续传：带 uploadId 且会话仍在 → 复用并返回已收块
    if resume_id:
        cur = db.execute('SELECT * FROM uploads WHERE upload_id = ?', (resume_id,)).fetchone()
        if cur and cur['seqs'] != 'done':
            seqs = json.loads(cur['seqs'] or '[]')
            return success({'exists': False, 'uploadId': resume_id,
                            'chunkSize': PHOTO_CHUNK_SIZE, 'haveBlocks': seqs})

    total_size = data.get('totalSize') or 0
    upload_id = str(uuid.uuid4())
    db.execute(
        '''INSERT INTO uploads (upload_id, photo_id, total_size, expected_md5, seqs, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (upload_id, photo_id, total_size, expected_md5, '[]', now_iso(), now_iso())
    )
    db.commit()
    return success({'exists': False, 'uploadId': upload_id,
                    'chunkSize': PHOTO_CHUNK_SIZE, 'haveBlocks': []})


@photos_bp.route('/api/photos/upload/<upload_id>/<int:seq>', methods=['PUT'])
def upload_chunk(upload_id, seq):
    db = get_photos_db()
    row = db.execute('SELECT * FROM uploads WHERE upload_id = ?', (upload_id,)).fetchone()
    if not row:
        return error('Upload session not found', code=10404, status=404)

    content = request.get_data()
    if not content or len(content) > PHOTO_MAX_CHUNK_SIZE:
        return error('Invalid chunk size', code=10413, status=413)

    # 块级 CRC32 校验（SPEC §2/§3.2）：不符返回专属错码，客户端只重传该块
    from core.util import crc32_hex
    expected_crc = request.headers.get('X-Chunk-CRC32', '').strip()
    if expected_crc and expected_crc.lower() != crc32_hex(content):
        return error('Chunk CRC32 mismatch', code=10418, status=400)

    disk_err = _check_disk_space()
    if disk_err:
        return disk_err

    seqs = json.loads(row['seqs'] or '[]')
    if seq not in seqs:
        chunk_dir = os.path.join(UPLOADS_DIR, upload_id)
        os.makedirs(chunk_dir, exist_ok=True)
        chunk_path = os.path.join(chunk_dir, f'{seq:06d}.part')
        with open(chunk_path, 'wb') as f:
            f.write(content)
        seqs.append(seq)
        seqs.sort()
        db.execute(
            'UPDATE uploads SET seqs=?, updated_at=? WHERE upload_id=?',
            (json.dumps(seqs), now_iso(), upload_id)
        )
        db.commit()
    return success({'uploadId': upload_id, 'seq': seq, 'received': len(seqs)})


def _photos_complete_result(db, upload_id):
    """complete 幂等：查 done 会话对应的照片，返回照片 dict（失败返回 None）。"""
    row = db.execute('SELECT * FROM uploads WHERE upload_id = ?', (upload_id,)).fetchone()
    if not row or row['seqs'] != 'done' or not row['photo_id']:
        return None
    p = db.execute('SELECT * FROM photos WHERE id = ?', (row['photo_id'],)).fetchone()
    return p


@photos_bp.route('/api/photos/upload/<upload_id>/complete', methods=['POST'])
def upload_complete(upload_id):
    db = get_photos_db()
    row = db.execute('SELECT * FROM uploads WHERE upload_id = ?', (upload_id,)).fetchone()
    if not row:
        return error('Upload session not found', code=10404, status=404)

    # 幂等：已 complete 直接返回成功
    if row['seqs'] == 'done':
        p = _photos_complete_result(db, upload_id)
        if p:
            return success(_photo_with_urls(p))
        return error('Upload session not found', code=10404, status=404)

    chunk_dir = os.path.join(UPLOADS_DIR, upload_id)
    seqs = json.loads(row['seqs'] or '[]')
    if not os.path.isdir(chunk_dir) or len(seqs) == 0:
        return error('No chunks uploaded', code=10400, status=400)

    # 合并分片到临时文件
    tmp_path = os.path.join(UPLOADS_DIR, f'{upload_id}.merged')
    with open(tmp_path, 'wb') as out:
        for seq in sorted(seqs):
            with open(os.path.join(chunk_dir, f'{seq:06d}.part'), 'rb') as f:
                shutil.copyfileobj(f, out)
    size = os.path.getsize(tmp_path)

    import hashlib
    md5_hash = hashlib.md5()
    with open(tmp_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            md5_hash.update(chunk)
    actual_md5 = md5_hash.hexdigest()
    if row['expected_md5'] and actual_md5 != row['expected_md5']:
        shutil.rmtree(chunk_dir, ignore_errors=True)
        os.remove(tmp_path)
        return error('MD5 mismatch after merge', code=10405, status=400)

    disk_err = _check_disk_space()
    if disk_err:
        shutil.rmtree(chunk_dir, ignore_errors=True)
        os.remove(tmp_path)
        return disk_err

    # 入 file_store（内容寻址）
    orig_file_id = file_store.put_from_path(tmp_path, bucket='orig')
    thumb_data = make_thumbnail_bytes(tmp_path)
    thumb_file_id = file_store.put(thumb_data, bucket='thumb') if thumb_data else ''
    width, height = image_dimensions(tmp_path)

    photo_id = row['photo_id'] or _photo_id()
    now_ts = now_iso()
    if row['photo_id']:
        db.execute(
            '''UPDATE photos SET thumb_file_id=?, orig_file_id=?, md5=?, size=?, width=?, height=?,
               version=version+1, updated_at=? WHERE id=?''',
            (thumb_file_id, orig_file_id, actual_md5, size, width, height, now_ts, photo_id)
        )
    else:
        db.execute(
            '''INSERT INTO photos
               (id, device_id, client_id, media_id, filename, width, height, size,
                thumb_file_id, orig_file_id, md5, version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (photo_id, g.device['device_id'], '', '', '', width, height, size,
             thumb_file_id, orig_file_id, actual_md5, 1, now_ts, now_ts)
        )
    # 保留 done 会话（幂等），TTL 后由后台清理
    db.execute(
        "UPDATE uploads SET seqs='done', photo_id=?, updated_at=? WHERE upload_id=?",
        (photo_id, now_ts, upload_id)
    )
    db.commit()
    shutil.rmtree(chunk_dir, ignore_errors=True)
    try:
        os.remove(tmp_path)
    except OSError:
        pass

    p = db.execute('SELECT * FROM photos WHERE id = ?', (photo_id,)).fetchone()

    # 事件：照片上传完成
    from core import event_bus
    event_bus.publish('photo.uploaded', {
        'id': photo_id, 'orig_file_id': orig_file_id, 'thumb_file_id': thumb_file_id,
        'size': size, 'source': 'chunked',
    })
    return success(_photo_with_urls(p))


@photos_bp.route('/api/photos/upload/batch-check', methods=['POST'])
def upload_batch_check():
    """批量判重（上传文件夹秒传，SPEC §3.4）：{ exists: { [md5]: {fileId, id} } }。"""
    data = request.get_json(silent=True) or {}
    files = data.get('files') or []
    db = get_photos_db()
    exists = {}
    for item in files:
        md5 = (item.get('md5') or '').strip()
        if not md5:
            continue
        row = db.execute(
            'SELECT id, orig_file_id, size FROM photos WHERE md5 = ? AND deleted_at IS NULL '
            'AND orig_file_id != \'\' LIMIT 1',
            (md5,)
        ).fetchone()
        if row:
            exists[md5] = {'fileId': row['orig_file_id'], 'id': row['id'], 'size': row['size']}
    return success({'exists': exists})


def sweep_uploads():
    """孤儿会话 TTL 清理（SPEC §3.5）：删除超期未完成/已完成的 uploads 记录与暂存目录。"""
    import time as _t
    cutoff = _t.time() - UPLOAD_SESSION_TTL
    cutoff_iso = _t.strftime('%Y-%m-%dT%H:%M:%SZ', _t.gmtime(cutoff))
    db = get_photos_db()
    rows = db.execute(
        'SELECT upload_id, updated_at FROM uploads WHERE updated_at < ?', (cutoff_iso,)
    ).fetchall()
    removed = 0
    for r in rows:
        chunk_dir = os.path.join(UPLOADS_DIR, r['upload_id'])
        shutil.rmtree(chunk_dir, ignore_errors=True)
        db.execute('DELETE FROM uploads WHERE upload_id = ?', (r['upload_id'],))
        removed += 1
    if rows:
        db.commit()
    return removed


# ---------- 空间 ----------

@photos_bp.route('/api/space', methods=['GET'])
def space():
    free = file_store.disk_free_bytes()
    total = None
    try:
        total = shutil.disk_usage(PHOTOS_DIR).total
    except OSError:
        pass
    return success({
        'total': total,
        'free': free,
        'threshold': DISK_THRESHOLD,
        'paused': free is not None and free < DISK_THRESHOLD,
    })
