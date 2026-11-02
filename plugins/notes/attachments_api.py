"""附件两阶段上传 + 缩略图 + 永不 404 下载（从 mock-server app/files.py 迁入）。

鉴权：register/file 由 core 设备守卫注入 g.user_id；download 允许匿名（浏览器 <img>），
但带 Authorization 则必须有效（无效 → 401）。
"""
import base64
import hashlib
import io
import os
import uuid as _uuid

from flask import Blueprint, Response, g, jsonify, request, send_file

from config import (
    ALLOWED_EXTENSIONS, ATTACH_DIR, MAX_UPLOAD_SIZE,
    PLACEHOLDER_IMAGE_PATH, THUMB_MAX_BYTES, THUMB_MAX_EDGE, THUMB_QUALITY,
)
from core.auth import parse_bearer
from core.auth import lookup_device
from core.protocol import error, success
from core.util import generate_id, now_iso
from plugins.notes.db import attachment_row_to_dict, get_notes_db

attachments_bp = Blueprint('attachments', __name__)

ALLOWED_MIME = {
    'image/jpeg', 'image/png', 'image/gif', 'image/webp',
}
MIME_TO_EXT = {
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/gif': '.gif',
    'image/webp': '.webp',
}

_placeholder_cache = None


def _user_files_dir(user_id):
    d = os.path.join(ATTACH_DIR, str(user_id))
    os.makedirs(d, exist_ok=True)
    return d


def _find_main_file(row):
    """按 file_id 在磁盘查找主文件：优先 .bin，其次一步式扩展名文件。"""
    user_dir = _user_files_dir(row['user_id'])
    bin_path = os.path.join(user_dir, f'{row["file_id"]}.bin')
    if os.path.isfile(bin_path):
        return bin_path
    for ext in ALLOWED_EXTENSIONS:
        p = os.path.join(user_dir, f'{row["file_id"]}{ext}')
        if os.path.isfile(p):
            return p
    return None


def _remove_attachment_files(user_id, file_id):
    """删除某附件的全部磁盘文件：主文件 + 压缩图。"""
    user_dir = _user_files_dir(user_id)
    for name in [f'{file_id}.bin', f'{file_id}_thumb.webp'] + [f'{file_id}{e}' for e in ALLOWED_EXTENSIONS]:
        p = os.path.join(user_dir, name)
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass


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


def _get_image_dimensions(filepath):
    try:
        from PIL import Image
        with Image.open(filepath) as img:
            return img.width, img.height
    except Exception:
        return 0, 0


def generate_thumbnail(src_path, thumb_path):
    """生成压缩图：最长边 ≤ THUMB_MAX_EDGE，WEBP，质量递减直至 ≤ THUMB_MAX_BYTES。"""
    try:
        from PIL import Image
        with Image.open(src_path) as img:
            w, h = img.size
            scale = min(1.0, THUMB_MAX_EDGE / max(w, h))
            if scale < 1.0:
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            quality = THUMB_QUALITY
            img.save(thumb_path, 'WEBP', quality=quality)
            while os.path.getsize(thumb_path) > THUMB_MAX_BYTES and quality > 40:
                quality -= 10
                img.save(thumb_path, 'WEBP', quality=quality)
    except Exception:
        pass


def _get_placeholder_bytes():
    """服务端内置占位图：优先读配置路径文件，否则内存生成灰色 WEBP 并缓存。"""
    global _placeholder_cache
    if _placeholder_cache is None:
        if os.path.isfile(PLACEHOLDER_IMAGE_PATH):
            with open(PLACEHOLDER_IMAGE_PATH, 'rb') as f:
                _placeholder_cache = f.read()
        else:
            from PIL import Image
            img = Image.new('RGB', (480, 360), (235, 235, 235))
            buf = io.BytesIO()
            img.save(buf, 'WEBP', quality=70)
            _placeholder_cache = buf.getvalue()
    return _placeholder_cache


def _send_with_state(path, mimetype, state):
    resp = send_file(path, mimetype=mimetype)
    resp.headers['X-Attachment-State'] = state
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


def _send_placeholder(state):
    resp = Response(_get_placeholder_bytes(), mimetype='image/webp')
    resp.headers['X-Attachment-State'] = state
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _compute_md5(filepath):
    h = hashlib.md5()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


# ---------- 一步式上传（兼容旧客户端） ----------

@attachments_bp.route('/api/files/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return error('No file provided')
    file = request.files['file']
    if not file.filename:
        return error('No file selected')

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS and file.mimetype not in ALLOWED_MIME:
        return error(f'Unsupported file type. Allowed: {", ".join(ALLOWED_EXTENSIONS)}')

    if ext not in ALLOWED_EXTENSIONS:
        ext = MIME_TO_EXT.get(file.mimetype, '.jpg')

    content = file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        return error(f'File too large. Max: {MAX_UPLOAD_SIZE // (1024 * 1024)} MB')

    note_id = request.form.get('noteId', '').strip()

    file_id = f'f_{_uuid.uuid4().hex[:16]}'
    user_dir = _user_files_dir(g.user_id)
    filepath = os.path.join(user_dir, f'{file_id}{ext}')
    with open(filepath, 'wb') as f:
        f.write(content)

    width, height = _get_image_dimensions(filepath)
    md5_hash = _compute_md5(filepath)

    attach_id = generate_id('a')
    now_ts = now_iso()
    db = get_notes_db()
    db.execute(
        '''INSERT INTO attachments
           (attach_id, user_id, note_id, type, file_name, file_id, width, height, md5, state, created_at)
           VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, 1, ?)''',
        (attach_id, g.user_id, note_id, file.filename, file_id, width, height, md5_hash, now_ts)
    )
    db.commit()

    host = request.host_url.rstrip('/')
    url = f'{host}/api/attachments/{attach_id}/download'

    return success({
        'fileId': file_id,
        'url': url,
        'width': width,
        'height': height,
        'md5': md5_hash,
    })


# ---------- 两阶段上传 ----------

@attachments_bp.route('/api/attachments', methods=['POST'])
def register_attachment():
    """注册附件位置（申请回执）。attachId 幂等：已存在且匹配 → 200；冲突 → 10402。"""
    data = request.get_json(silent=True) or {}

    attach_id = (data.get('attachId') or '').strip() or str(_uuid.uuid4())
    note_id = (data.get('noteId') or '').strip()
    atype = data.get('type', 0)
    if atype not in (0, 1, 2):
        return error('Unsupported attachment type', code=10415, status=400)
    file_name = data.get('fileName') or ''
    width = data.get('width') or 0
    height = data.get('height') or 0
    md5_val = data.get('md5') or ''

    db = get_notes_db()
    row = db.execute('SELECT * FROM attachments WHERE attach_id = ?', (attach_id,)).fetchone()

    if row:
        # user_id 在库中为 INTEGER、g.user_id 为 TEXT，统一转 str 比较
        if str(row['user_id']) != str(g.user_id) or row['type'] != atype:
            return error('attachId already exists with different content', code=10402, status=409)
        state_str = 'ready' if _find_main_file(row) else 'pending'
        status_code = 200
    else:
        now_ts = now_iso()
        db.execute(
            '''INSERT INTO attachments
               (attach_id, user_id, note_id, type, file_name, file_id, width, height, md5, state, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)''',
            (attach_id, g.user_id, note_id, atype, file_name, attach_id, width, height, md5_val, now_ts)
        )
        db.commit()
        state_str = 'pending'
        status_code = 201

    host = request.host_url.rstrip('/')
    return jsonify({
        'code': 0,
        'msg': 'success',
        'data': {
            'attachId': attach_id,
            'url': f'{host}/api/attachments/{attach_id}/download?size=thumb',
            'originalUrl': f'{host}/api/attachments/{attach_id}/download',
            'state': state_str,
        }
    }), status_code


@attachments_bp.route('/api/attachments/<attach_id>/file', methods=['PUT'])
def upload_attachment_file(attach_id):
    """上传文件内容（凭回执补传）。幂等：重复 PUT 覆盖；主文件落盘 {file_id}.bin。"""
    db = get_notes_db()
    row = db.execute(
        'SELECT * FROM attachments WHERE attach_id = ? AND user_id = ?',
        (attach_id, g.user_id)
    ).fetchone()
    if not row:
        return error('Attachment not found. Register first', code=10404, status=404)

    content = request.get_data()
    if not content:
        return error('Empty file', code=10400, status=400)
    if len(content) > MAX_UPLOAD_SIZE:
        return error(f'File too large. Max: {MAX_UPLOAD_SIZE // (1024 * 1024)} MB', code=10413, status=413)

    expected_md5 = request.headers.get('Content-MD5', '')
    if expected_md5:
        actual = hashlib.md5(content).hexdigest()
        try:
            given = base64.b64decode(expected_md5).hex()
        except Exception:
            given = expected_md5
        if given.lower() != actual:
            return error('Content-MD5 mismatch', code=10405, status=400)

    user_dir = _user_files_dir(g.user_id)
    _remove_attachment_files(g.user_id, row['file_id'])
    main_path = os.path.join(user_dir, f'{row["file_id"]}.bin')
    with open(main_path, 'wb') as f:
        f.write(content)

    width, height = _get_image_dimensions(main_path)
    md5_hash = hashlib.md5(content).hexdigest()
    size = len(content)

    generate_thumbnail(main_path, os.path.join(user_dir, f'{row["file_id"]}_thumb.webp'))

    db.execute(
        'UPDATE attachments SET width=?, height=?, md5=?, state=1 WHERE attach_id=? AND user_id=?',
        (width, height, md5_hash, attach_id, g.user_id)
    )
    db.commit()

    host = request.host_url.rstrip('/')
    return success({
        'attachId': attach_id,
        'url': f'{host}/api/attachments/{attach_id}/download',
        'md5': md5_hash,
        'size': size,
        'state': 'ready',
    })


@attachments_bp.route('/api/attachments/<attach_id>/download', methods=['GET'])
def download_attachment(attach_id):
    """下载附件（永不 404）。?size=thumb → 压缩图；无参 → 原图。

    可选认证：携带 Authorization 头则必须有效（无效 → 401）；无头允许匿名。
    状态经 X-Attachment-State 头告知：ready / pending / missing。
    """
    token = parse_bearer()
    if token and lookup_device(token) is None:
        return error('Unauthorized', code=10401, status=401)

    db = get_notes_db()
    row = db.execute('SELECT * FROM attachments WHERE attach_id = ?', (attach_id,)).fetchone()
    # 兼容旧内容 URL：存量笔记图片可能直接引用 fileId（f_xxx）而非 attachId
    if not row:
        row = db.execute('SELECT * FROM attachments WHERE file_id = ?', (attach_id,)).fetchone()

    want_thumb = request.args.get('size', '') == 'thumb'

    if row:
        main_path = _find_main_file(row)
        if main_path:
            if want_thumb:
                thumb_path = os.path.join(_user_files_dir(row['user_id']), f'{row["file_id"]}_thumb.webp')
                if os.path.isfile(thumb_path):
                    return _send_with_state(thumb_path, 'image/webp', 'ready')
            return _send_with_state(main_path, _sniff_mime(main_path), 'ready')
        return _send_placeholder('pending')

    return _send_placeholder('missing')


# ---------- 既有接口 ----------

@attachments_bp.route('/api/notes/<note_id>/attachments', methods=['GET'])
def get_note_attachments(note_id):
    db = get_notes_db()
    rows = db.execute(
        'SELECT * FROM attachments WHERE note_id = ? AND user_id = ? ORDER BY created_at DESC',
        (note_id, g.user_id)
    ).fetchall()
    return success([attachment_row_to_dict(r) for r in rows])


@attachments_bp.route('/api/attachments/<attach_id>', methods=['PUT'])
def update_attachment(attach_id):
    data = request.get_json(silent=True) or {}
    db = get_notes_db()
    row = db.execute(
        'SELECT * FROM attachments WHERE attach_id = ? AND user_id = ?',
        (attach_id, g.user_id)
    ).fetchone()
    if not row:
        return error('Attachment not found')

    new_md5 = data.get('md5', row['md5'])
    new_state = data.get('state', row['state'])

    db.execute(
        'UPDATE attachments SET md5 = ?, state = ? WHERE attach_id = ? AND user_id = ?',
        (new_md5, new_state, attach_id, g.user_id)
    )
    db.commit()

    updated = attachment_row_to_dict(row)
    updated['md5'] = new_md5
    updated['state'] = new_state
    return success(updated)


@attachments_bp.route('/api/attachments/<attach_id>', methods=['DELETE'])
def delete_attachment(attach_id):
    db = get_notes_db()
    row = db.execute(
        'SELECT * FROM attachments WHERE attach_id = ? AND user_id = ?',
        (attach_id, g.user_id)
    ).fetchone()
    if not row:
        return error('Attachment not found')

    _remove_attachment_files(g.user_id, row['file_id'])
    db.execute('DELETE FROM attachments WHERE attach_id = ? AND user_id = ?', (attach_id, g.user_id))
    db.commit()
    return success(None)
