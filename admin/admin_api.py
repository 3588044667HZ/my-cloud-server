"""管理后台 /api/admin/*：设备管理 / 配对码 / 孤儿清理 / 云盘开关 / 硬盘管理 / 笔记管理。

沿用 mock-server ADMIN_PASSWORD 鉴权（内存 admin session token），
用户管理改为设备管理（单用户多设备）；笔记管理保留（查看/搜索/强制删除）。
"""
import time as _time
import uuid

from flask import Blueprint, request

from config import (
    ADMIN_PASSWORD, ADMIN_TOKEN_TTL, CORE_DB, DEVICE_ORPHAN_DAYS,
)
from core.auth import generate_pair_code, now_iso
from core.db import get_conn
from core.protocol import error, pagination, success
from plugins.drive.db import get_drive_db

admin_bp = Blueprint('admin', __name__, url_prefix='/api/admin')

_admin_sessions = {}  # token -> {'expires_at': ts}


# ---------- 管理鉴权 ----------

def _require_admin(fn):
    from functools import wraps
    from flask import request as _request

    @wraps(fn)
    def wrapper(*args, **kwargs):
        header = _request.headers.get('Authorization', '')
        token = header[7:] if header.startswith('Bearer ') else ''
        s = _admin_sessions.get(token)
        if not s:
            return error('Unauthorized', code=10401, status=401)
        if s['expires_at'] < _time.time():
            _admin_sessions.pop(token, None)
            return error('Token expired', code=10401, status=401)
        return fn(*args, **kwargs)
    return wrapper


@admin_bp.route('/login', methods=['POST'])
def admin_login():
    data = request.get_json(silent=True) or {}
    password = data.get('password', '').strip()
    if not password:
        return error('Password is required')
    if password != ADMIN_PASSWORD:
        return error('Invalid password', status=401)
    token = uuid.uuid4().hex
    _admin_sessions[token] = {'expires_at': _time.time() + ADMIN_TOKEN_TTL}
    return success({'token': token, 'expiresIn': ADMIN_TOKEN_TTL})


# ---------- 设备管理 ----------

def _device_row_to_dict(r):
    return {
        'deviceId': r['device_id'],
        'name': r['name'],
        'createdAt': r['created_at'],
        'lastActive': r['last_active'],
        'revokedAt': r['revoked_at'],
    }


def _last_active_ts(iso):
    """把 ISO 时间转 unix ts；无法解析返回 0。"""
    if not iso:
        return 0
    try:
        return int(_time.mktime(_time.strptime(iso, '%Y-%m-%dT%H:%M:%SZ')))
    except (ValueError, TypeError):
        return 0


@admin_bp.route('/stats', methods=['GET'])
@_require_admin
def stats():
    db = get_conn(CORE_DB)
    device_count = db.execute('SELECT COUNT(*) as c FROM devices').fetchone()['c']
    active_count = db.execute(
        'SELECT COUNT(*) as c FROM devices WHERE revoked_at IS NULL').fetchone()['c']
    orphan_count = len(_orphan_rows(db, _last_active_ts(now_iso())))
    uploads_row = db.execute(
        "SELECT value FROM app_config WHERE key = 'stats_photo_uploads'").fetchone()
    photo_uploads = int(uploads_row['value']) if uploads_row else 0

    # 磁盘满保护告警（ARCHITECTURE §5.4）
    photos_paused = False
    photos_free = None
    try:
        from core.file_store import disk_free_bytes
        photos_free = disk_free_bytes()
        from config import DISK_FREE_THRESHOLD
        photos_paused = photos_free is not None and photos_free < DISK_FREE_THRESHOLD
    except Exception:
        pass

    # 笔记统计（单用户）
    note_count = trash_count = 0
    try:
        from plugins.notes.db import get_notes_db
        ndb = get_notes_db()
        note_count = ndb.execute(
            'SELECT COUNT(*) as c FROM notes WHERE deleted_at IS NULL').fetchone()['c']
        trash_count = ndb.execute(
            'SELECT COUNT(*) as c FROM notes WHERE deleted_at IS NOT NULL').fetchone()['c']
    except Exception:
        pass

    return success({
        'deviceCount': device_count,
        'activeDeviceCount': active_count,
        'orphanCount': orphan_count,
        'photoUploads': photo_uploads,
        'photosDiskPaused': photos_paused,
        'photosDiskFree': photos_free,
        'noteCount': note_count,
        'trashCount': trash_count,
    })


def _orphan_rows(db, now_ts):
    threshold = now_ts - DEVICE_ORPHAN_DAYS * 86400
    rows = db.execute(
        'SELECT * FROM devices WHERE revoked_at IS NULL'
    ).fetchall()
    orphans = []
    for r in rows:
        if _last_active_ts(r['last_active']) < threshold:
            orphans.append(r)
    return orphans


@admin_bp.route('/devices', methods=['GET'])
@_require_admin
def devices():
    keyword = request.args.get('keyword', '').strip()
    db = get_conn(CORE_DB)
    if keyword:
        rows = db.execute(
            'SELECT * FROM devices WHERE name LIKE ? OR device_id LIKE ? ORDER BY created_at ASC',
            (f'%{keyword}%', f'%{keyword}%')
        ).fetchall()
    else:
        rows = db.execute('SELECT * FROM devices ORDER BY created_at ASC').fetchall()
    return success([_device_row_to_dict(r) for r in rows])


@admin_bp.route('/devices/pair-code', methods=['POST'])
@_require_admin
def pair_code():
    return success({'code': generate_pair_code(), 'expiresIn': 600})


@admin_bp.route('/devices/<device_id>/rename', methods=['POST'])
@_require_admin
def rename_device(device_id):
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return error('name is required')
    db = get_conn(CORE_DB)
    row = db.execute('SELECT * FROM devices WHERE device_id = ?', (device_id,)).fetchone()
    if not row:
        return error('Device not found', code=10404, status=404)
    db.execute('UPDATE devices SET name = ? WHERE device_id = ?', (name, device_id))
    db.commit()
    return success(None)


@admin_bp.route('/devices/<device_id>', methods=['DELETE'])
@_require_admin
def revoke_device(device_id):
    db = get_conn(CORE_DB)
    row = db.execute('SELECT * FROM devices WHERE device_id = ?', (device_id,)).fetchone()
    if not row:
        return error('Device not found', code=10404, status=404)
    db.execute('UPDATE devices SET revoked_at = ? WHERE device_id = ?', (now_iso(), device_id))
    db.commit()
    return success(None)


@admin_bp.route('/devices/orphans', methods=['GET'])
@_require_admin
def orphan_list():
    db = get_conn(CORE_DB)
    orphans = _orphan_rows(db, _last_active_ts(now_iso()))
    return success([_device_row_to_dict(r) for r in orphans])


@admin_bp.route('/devices/orphans/cleanup', methods=['POST'])
@_require_admin
def orphan_cleanup():
    """回收孤儿设备：last_active 超 DEVICE_ORPHAN_DAYS 天未活动 → 撤销。"""
    db = get_conn(CORE_DB)
    orphans = _orphan_rows(db, _last_active_ts(now_iso()))
    for r in orphans:
        db.execute('UPDATE devices SET revoked_at = ? WHERE device_id = ?', (now_iso(), r['device_id']))
    db.commit()
    return success({'revoked': len(orphans)})


# ---------- 云盘开关 ----------

@admin_bp.route('/drive/enabled', methods=['GET', 'PUT'])
@_require_admin
def drive_enabled():
    db = get_drive_db()
    if request.method == 'GET':
        row = db.execute("SELECT value FROM drive_settings WHERE key = 'enabled'").fetchone()
        return success({'enabled': row['value'] == '1' if row else False})
    data = request.get_json(silent=True) or {}
    enabled = 1 if data.get('enabled') else 0
    db.execute("INSERT OR REPLACE INTO drive_settings (key, value) VALUES ('enabled', ?)",
               (str(enabled),))
    db.commit()
    return success({'enabled': bool(enabled)})


# ---------- 硬盘管理（共享目录配置） ----------

_SHARE_NAME_RE = None  # 延迟 import re


def _valid_share_name(name):
    """共享名规则：1~32 位字母/数字/下划线/连字符，禁止以 $ 结尾（避免与隐藏共享混淆）。"""
    import re
    global _SHARE_NAME_RE
    if _SHARE_NAME_RE is None:
        _SHARE_NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,32}$')
    return bool(_SHARE_NAME_RE.match(name)) and not name.endswith('$')


def _disk_usage(path):
    """返回 (free, total, used, exists)；路径不可用时 free/total/used 为 None。"""
    import os
    import shutil
    if not path or not os.path.isdir(path):
        return None, None, None, False
    try:
        usage = shutil.disk_usage(path)
        return usage.free, usage.total, usage.used, True
    except OSError:
        return None, None, None, False


def _share_row_to_dict(row):
    import os
    free, total, used, exists = _disk_usage(row['path'])
    return {
        'name': row['name'],
        'path': row['path'],
        'comment': row['comment'],
        'enabled': bool(row['enabled']),
        'source': 'db',
        'free': free,
        'total': total,
        'used': used,
        'exists': exists,
    }


@admin_bp.route('/drive/shares', methods=['GET'])
@_require_admin
def admin_drive_shares():
    """硬盘列表：后台配置 + 引导配置 + 注册表自动发现，均带余量。"""
    import os

    from plugins.drive.resolver import _read_registry_shares, list_db_shares

    db = get_drive_db()
    rows = db.execute('SELECT * FROM drive_shares ORDER BY name').fetchall()
    out = [_share_row_to_dict(r) for r in rows]
    seen = {r['name'] for r in rows}

    # 引导配置（config.DRIVE_SHARES，未在后台登记）
    from config import DRIVE_SHARES
    for name, path in DRIVE_SHARES.items():
        if name in seen:
            continue
        free, total, used, exists = _disk_usage(path)
        out.append({
            'name': name, 'path': os.path.realpath(path), 'comment': 'configured',
            'enabled': True, 'source': 'config', 'free': free, 'total': total,
            'used': used, 'exists': exists,
        })
        seen.add(name)

    # 注册表自动发现（未覆盖）
    for name, path, remark in _read_registry_shares():
        if name in seen or name.endswith('$'):
            continue
        if not path or not os.path.isdir(path):
            continue
        free, total, used, exists = _disk_usage(path)
        out.append({
            'name': name, 'path': os.path.realpath(path), 'comment': remark,
            'enabled': True, 'source': 'registry', 'free': free, 'total': total,
            'used': used, 'exists': exists,
        })
        seen.add(name)
    return success(out)


@admin_bp.route('/drive/shares', methods=['POST'])
@_require_admin
def admin_drive_add_share():
    """新增硬盘共享。req: { name, path, comment? }"""
    import os
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    path = (data.get('path') or '').strip()
    if not _valid_share_name(name):
        return error('share name must be 1-32 chars of A-Za-z0-9_- (no trailing $)')
    if not path or not os.path.isabs(path):
        return error('path must be an absolute path')
    if not os.path.isdir(path):
        return error(f'path does not exist or is not a directory: {path}')

    db = get_drive_db()
    exists = db.execute('SELECT 1 FROM drive_shares WHERE name = ?', (name,)).fetchone()
    if exists:
        return error('share already exists', status=409)
    now = now_iso()
    db.execute(
        'INSERT INTO drive_shares (name, path, comment, enabled, created_at, updated_at) '
        'VALUES (?, ?, ?, 1, ?, ?)',
        (name, path, (data.get('comment') or '').strip(), now, now)
    )
    db.commit()
    row = db.execute('SELECT * FROM drive_shares WHERE name = ?', (name,)).fetchone()
    return success(_share_row_to_dict(row))


@admin_bp.route('/drive/shares/<name>', methods=['PUT'])
@_require_admin
def admin_drive_update_share(name):
    """更新共享：{ path?, comment?, enabled? }"""
    import os
    db = get_drive_db()
    row = db.execute('SELECT * FROM drive_shares WHERE name = ?', (name,)).fetchone()
    if not row:
        return error('share not found', code=10404, status=404)
    data = request.get_json(silent=True) or {}
    new_path = (data.get('path') or row['path']).strip()
    new_comment = (data.get('comment') if 'comment' in data else row['comment']).strip()
    enabled = row['enabled']
    if 'enabled' in data:
        enabled = 1 if data.get('enabled') else 0
    if new_path != row['path']:
        if not os.path.isabs(new_path):
            return error('path must be an absolute path')
        if not os.path.isdir(new_path):
            return error(f'path does not exist or is not a directory: {new_path}')
    db.execute(
        'UPDATE drive_shares SET path = ?, comment = ?, enabled = ?, updated_at = ? WHERE name = ?',
        (new_path, new_comment, enabled, now_iso(), name)
    )
    db.commit()
    row = db.execute('SELECT * FROM drive_shares WHERE name = ?', (name,)).fetchone()
    return success(_share_row_to_dict(row))


@admin_bp.route('/drive/shares/<name>', methods=['DELETE'])
@_require_admin
def admin_drive_delete_share(name):
    """删除共享配置（不影响磁盘上文件；该盘的回收站条目将无法还原）。"""
    db = get_drive_db()
    row = db.execute('SELECT * FROM drive_shares WHERE name = ?', (name,)).fetchone()
    if not row:
        return error('share not found', code=10404, status=404)
    db.execute('DELETE FROM drive_shares WHERE name = ?', (name,))
    db.commit()
    return success(None)


# ---------- 笔记管理（单用户，查看/搜索/强制删除） ----------

@admin_bp.route('/notes', methods=['GET'])
@_require_admin
def admin_notes():
    from config import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
    from plugins.notes.db import get_notes_db, note_row_to_dict

    keyword = request.args.get('keyword', '').strip()
    include_trash = request.args.get('includeTrash', '').lower() == 'true'
    page = max(1, int(request.args.get('page', 1)))
    size = min(MAX_PAGE_SIZE, max(1, int(request.args.get('size', DEFAULT_PAGE_SIZE))))

    db = get_notes_db()
    conditions = []
    params = []
    if keyword:
        conditions.append('(title LIKE ? OR content LIKE ?)')
        kw = f'%{keyword}%'
        params.extend([kw, kw])
    if not include_trash:
        conditions.append('deleted_at IS NULL')
    where = ' AND '.join(conditions) if conditions else '1=1'

    total = db.execute(f'SELECT COUNT(*) as cnt FROM notes WHERE {where}', params).fetchone()['cnt']
    rows = db.execute(
        f'SELECT * FROM notes WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?',
        params + [size, (page - 1) * size]
    ).fetchall()

    from core.auth import get_single_user
    username = get_single_user()['name']
    notes = []
    for r in rows:
        note = note_row_to_dict(r)
        note['username'] = username
        notes.append(note)
    return success(notes, extra=pagination(page, size, total))


@admin_bp.route('/notes/<note_id>', methods=['DELETE'])
@_require_admin
def admin_delete_note(note_id):
    """强制删除笔记（含附件文件与冷备），不经过回收站。"""
    from plugins.notes.db import get_notes_db

    db = get_notes_db()
    row = db.execute('SELECT * FROM notes WHERE id = ?', (note_id,)).fetchone()
    if not row:
        return error('Note not found', code=10404, status=404)

    # 与笔记永久删除语义一致：清理附件文件 + 记录 + txt 冷备
    from plugins.notes.notes_api import _delete_note_attachments
    _delete_note_attachments(db, row['user_id'], note_id)
    db.execute('DELETE FROM notes WHERE id = ?', (note_id,))
    db.commit()
    from plugins.notes.backup import remove_backup
    remove_backup(note_id)
    return success(None)
