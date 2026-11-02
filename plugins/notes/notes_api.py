"""笔记 CRUD + 增量同步 + 回收站 + pin + RESTORE 冲突恢复。

从 mock-server app/notes.py 迁入，表逻辑不变；鉴权由 core 设备守卫注入 g.user_id。
"""
from flask import Blueprint, g, request

from config import COLORS, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from core.protocol import error, pagination, success
from core.util import generate_id, now_iso
from plugins.notes.backup import backup_note, remove_backup
from plugins.notes.db import get_notes_db, note_row_to_dict

notes_bp = Blueprint('notes', __name__)


@notes_bp.route('/api/notes', methods=['GET'])
def get_notes():
    keyword = request.args.get('keyword', '').strip()
    notebook_id = request.args.get('notebookId', '').strip()
    color = request.args.get('color', '').strip()
    sort_by = request.args.get('sortBy', 'updatedAt').strip()
    page = max(1, int(request.args.get('page', 1)))
    size = min(MAX_PAGE_SIZE, max(1, int(request.args.get('size', DEFAULT_PAGE_SIZE))))

    order_col = 'created_at' if sort_by == 'createdAt' else 'updated_at'

    db = get_notes_db()
    conditions = ['user_id = ?', 'deleted_at IS NULL']
    params = [g.user_id]

    if keyword:
        conditions.append('(title LIKE ? OR content LIKE ?)')
        kw = f'%{keyword}%'
        params.extend([kw, kw])
    if notebook_id:
        conditions.append('notebook_id = ?')
        params.append(notebook_id)
    if color:
        conditions.append('color = ?')
        params.append(color)

    where = ' AND '.join(conditions)
    total = db.execute(f'SELECT COUNT(*) as cnt FROM notes WHERE {where}', params).fetchone()['cnt']
    rows = db.execute(
        f'SELECT * FROM notes WHERE {where} ORDER BY is_pinned DESC, {order_col} DESC LIMIT ? OFFSET ?',
        params + [size, (page - 1) * size]
    ).fetchall()

    return success([note_row_to_dict(r) for r in rows],
                   extra=pagination(page, size, total))


@notes_bp.route('/api/notes/sync', methods=['GET'])
def sync_notes():
    since = request.args.get('since', '').strip()
    db = get_notes_db()
    if since:
        updated_rows = db.execute(
            'SELECT * FROM notes WHERE user_id = ? AND updated_at > ? AND deleted_at IS NULL',
            (g.user_id, since)
        ).fetchall()
        deleted_rows = db.execute(
            'SELECT id FROM notes WHERE user_id = ? AND deleted_at IS NOT NULL AND deleted_at > ?',
            (g.user_id, since)
        ).fetchall()
        deleted_ids = [r['id'] for r in deleted_rows]
    else:
        updated_rows = db.execute(
            'SELECT * FROM notes WHERE user_id = ? AND deleted_at IS NULL',
            (g.user_id,)
        ).fetchall()
        deleted_ids = []

    return success({
        'updated': [note_row_to_dict(r) for r in updated_rows],
        'deletedIds': deleted_ids,
        'serverTime': now_iso(),
    })


@notes_bp.route('/api/notes/<note_id>', methods=['GET'])
def get_note(note_id):
    db = get_notes_db()
    row = db.execute('SELECT * FROM notes WHERE id = ? AND user_id = ? AND deleted_at IS NULL',
                     (note_id, g.user_id)).fetchone()
    if not row:
        return error('Note not found')
    return success(note_row_to_dict(row))


@notes_bp.route('/api/notes', methods=['POST'])
def create_note():
    data = request.get_json(silent=True) or {}
    note_id = generate_id('n')
    now_ts = now_iso()
    title = data.get('title', '').strip()
    content = data.get('content', '')
    notebook_id = data.get('notebookId')
    color = data.get('color', 'blue')
    is_pinned = 1 if data.get('isPinned', False) else 0

    db = get_notes_db()
    db.execute(
        '''INSERT INTO notes (id, user_id, title, content, notebook_id, color, is_pinned, version, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)''',
        (note_id, g.user_id, title, content, notebook_id, color, is_pinned, now_ts, now_ts)
    )
    db.commit()

    note = {
        'id': note_id, 'title': title, 'content': content,
        'notebookId': notebook_id, 'color': color,
        'isPinned': bool(is_pinned), 'version': 1,
        'createdAt': now_ts, 'updatedAt': now_ts, 'deletedAt': None,
    }
    backup_note(note)
    return success(note)


@notes_bp.route('/api/notes/<note_id>', methods=['PUT'])
def update_note(note_id):
    db = get_notes_db()
    row = db.execute('SELECT * FROM notes WHERE id = ? AND user_id = ? AND deleted_at IS NULL',
                     (note_id, g.user_id)).fetchone()
    if not row:
        return error('Note not found')

    client_updated_at = request.headers.get('If-Match', '')
    if client_updated_at:
        server_updated_at = row['updated_at']
        if client_updated_at != server_updated_at:
            return error(
                f'Conflict: note was modified by another device. Server version: {server_updated_at}',
                code=409, status=409,
            )

    data = request.get_json(silent=True) or {}
    title = data.get('title', row['title'])
    content = data.get('content', row['content'])
    notebook_id = data.get('notebookId', row['notebook_id'])
    color = data.get('color', row['color'])
    is_pinned = 1 if data.get('isPinned', row['is_pinned']) else 0
    new_version = row['version'] + 1
    now_ts = now_iso()

    db.execute(
        '''UPDATE notes SET title=?, content=?, notebook_id=?, color=?, is_pinned=?,
           version=?, updated_at=? WHERE id=? AND user_id=?''',
        (title, content, notebook_id, color, is_pinned, new_version, now_ts, note_id, g.user_id)
    )
    db.commit()

    note = {
        'id': note_id, 'title': title, 'content': content,
        'notebookId': notebook_id, 'color': color,
        'isPinned': bool(is_pinned), 'version': new_version,
        'createdAt': row['created_at'], 'updatedAt': now_ts,
        'deletedAt': row['deleted_at'],
    }
    backup_note(note)
    return success(note)


@notes_bp.route('/api/notes/<note_id>', methods=['DELETE'])
def delete_note(note_id):
    db = get_notes_db()
    row = db.execute('SELECT * FROM notes WHERE id = ? AND user_id = ? AND deleted_at IS NULL',
                     (note_id, g.user_id)).fetchone()
    if not row:
        return error('Note not found')
    now_ts = now_iso()
    new_version = row['version'] + 1
    db.execute(
        'UPDATE notes SET deleted_at=?, updated_at=?, version=? WHERE id=? AND user_id=?',
        (now_ts, now_ts, new_version, note_id, g.user_id)
    )
    db.commit()

    note = note_row_to_dict(row)
    note['deletedAt'] = now_ts
    note['updatedAt'] = now_ts
    note['version'] = new_version
    backup_note(note)
    return success(None)


@notes_bp.route('/api/notes/<note_id>/pin', methods=['PUT'])
def pin_note(note_id):
    db = get_notes_db()
    row = db.execute('SELECT * FROM notes WHERE id = ? AND user_id = ? AND deleted_at IS NULL',
                     (note_id, g.user_id)).fetchone()
    if not row:
        return error('Note not found')
    new_pinned = 0 if row['is_pinned'] else 1
    new_version = row['version'] + 1
    now_ts = now_iso()
    db.execute(
        'UPDATE notes SET is_pinned=?, version=?, updated_at=? WHERE id=? AND user_id=?',
        (new_pinned, new_version, now_ts, note_id, g.user_id)
    )
    db.commit()

    note = note_row_to_dict(row)
    note['isPinned'] = bool(new_pinned)
    note['version'] = new_version
    note['updatedAt'] = now_ts
    backup_note(note)
    return success(note)


@notes_bp.route('/api/notes/trash', methods=['GET'])
def get_trash_notes():
    db = get_notes_db()
    rows = db.execute(
        'SELECT * FROM notes WHERE user_id = ? AND deleted_at IS NOT NULL ORDER BY deleted_at DESC',
        (g.user_id,)
    ).fetchall()
    from core.trash import remain_days
    out = []
    for r in rows:
        note = note_row_to_dict(r)
        note['remainDays'] = remain_days(r['deleted_at'])
        out.append(note)
    return success(out)


def sweep_expired():
    """回收站超期自动清理（core/trash 分派）：物理删除超过保留期的软删笔记。"""
    from core.trash import is_expired
    db = get_notes_db()
    rows = db.execute(
        'SELECT * FROM notes WHERE deleted_at IS NOT NULL'
    ).fetchall()
    removed = 0
    for r in rows:
        if not is_expired(r['deleted_at']):
            continue
        _delete_note_attachments(db, r['user_id'], r['id'])
        db.execute('DELETE FROM notes WHERE id = ?', (r['id'],))
        remove_backup(r['id'])
        removed += 1
    db.commit()
    return removed


@notes_bp.route('/api/notes/<note_id>/recover', methods=['PUT'])
def recover_note(note_id):
    db = get_notes_db()
    row = db.execute('SELECT * FROM notes WHERE id = ? AND user_id = ? AND deleted_at IS NOT NULL',
                     (note_id, g.user_id)).fetchone()
    if not row:
        return error('Note not found in trash')
    new_version = row['version'] + 1
    now_ts = now_iso()
    db.execute(
        'UPDATE notes SET deleted_at=NULL, updated_at=?, version=? WHERE id=? AND user_id=?',
        (now_ts, new_version, note_id, g.user_id)
    )
    db.commit()

    note = note_row_to_dict(row)
    note['deletedAt'] = None
    note['updatedAt'] = now_ts
    note['version'] = new_version
    backup_note(note)
    return success(note)


@notes_bp.route('/api/notes/<note_id>/restore', methods=['PUT'])
def restore_note(note_id):
    """冲突恢复：笔记处于删除态（软删/回收站）时，一步恢复并覆盖为本地内容。"""
    db = get_notes_db()
    row = db.execute('SELECT * FROM notes WHERE id = ? AND user_id = ?',
                     (note_id, g.user_id)).fetchone()
    if not row:
        return error('Note not found or permanently deleted', code=404, status=404)

    data = request.get_json(silent=True) or {}
    title = data.get('title', '').strip()
    content = data.get('content', '')
    if not title or not content:
        return error('Title and content are required')

    notebook_id = data.get('notebookId')
    if notebook_id:
        nb = db.execute('SELECT id FROM notebooks WHERE id = ? AND user_id = ?',
                        (notebook_id, g.user_id)).fetchone()
        if not nb:
            return error('Notebook not found or not owned by current user')

    color = data.get('color', row['color'])
    is_pinned = 1 if data.get('isPinned', False) else 0
    new_version = row['version'] + 1
    updated_at = data.get('updatedAt') or now_iso()

    db.execute(
        '''UPDATE notes SET deleted_at=NULL, title=?, content=?, notebook_id=?, color=?,
           is_pinned=?, version=?, updated_at=? WHERE id=? AND user_id=?''',
        (title, content, notebook_id, color, is_pinned, new_version, updated_at, note_id, g.user_id)
    )
    db.commit()

    note = {
        'id': note_id, 'title': title, 'content': content,
        'notebookId': notebook_id, 'color': color,
        'isPinned': bool(is_pinned), 'version': new_version,
        'createdAt': row['created_at'], 'updatedAt': updated_at,
        'deletedAt': None,
    }
    backup_note(note)
    return success(note)


@notes_bp.route('/api/notes/<note_id>/permanent', methods=['DELETE'])
def permanent_delete_note(note_id):
    db = get_notes_db()
    row = db.execute('SELECT * FROM notes WHERE id = ? AND user_id = ? AND deleted_at IS NOT NULL',
                     (note_id, g.user_id)).fetchone()
    if not row:
        return error('Note not found in trash')
    _delete_note_attachments(db, g.user_id, note_id)
    db.execute('DELETE FROM notes WHERE id = ? AND user_id = ?', (note_id, g.user_id))
    db.commit()
    remove_backup(note_id)
    return success(None)


def _delete_note_attachments(db, user_id, note_id):
    """删除笔记的全部附件：磁盘文件 + attachments 表记录。"""
    from plugins.notes.attachments_api import _remove_attachment_files
    rows = db.execute('SELECT * FROM attachments WHERE user_id = ? AND note_id = ?',
                      (user_id, note_id)).fetchall()
    if not rows:
        return
    for r in rows:
        _remove_attachment_files(user_id, r['file_id'])
    db.execute('DELETE FROM attachments WHERE user_id = ? AND note_id = ?', (user_id, note_id))
