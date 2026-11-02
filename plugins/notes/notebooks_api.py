"""笔记本 CRUD（从 mock-server app/notebooks.py 迁入，鉴权换设备守卫）。"""
from flask import Blueprint, g, request

from core.protocol import error, success
from core.util import generate_id, now_iso
from plugins.notes.db import get_notes_db, notebook_row_to_dict

notebooks_bp = Blueprint('notebooks', __name__)


@notebooks_bp.route('/api/notebooks', methods=['GET'])
def get_notebooks():
    db = get_notes_db()
    rows = db.execute('SELECT * FROM notebooks WHERE user_id = ? ORDER BY created_at ASC',
                      (g.user_id,)).fetchall()
    return success([notebook_row_to_dict(r) for r in rows])


@notebooks_bp.route('/api/notebooks', methods=['POST'])
def create_notebook():
    data = request.get_json(silent=True) or {}
    name = data.get('name', '').strip()
    if not name:
        return error('Notebook name is required')
    nb_id = generate_id('nb')
    color = data.get('color', '#4A90D9')
    created_at = now_iso()
    db = get_notes_db()
    db.execute(
        'INSERT INTO notebooks (id, user_id, name, color, created_at) VALUES (?, ?, ?, ?, ?)',
        (nb_id, g.user_id, name, color, created_at)
    )
    db.commit()
    return success({'id': nb_id, 'name': name, 'color': color, 'createdAt': created_at})


@notebooks_bp.route('/api/notebooks/<nb_id>', methods=['PUT'])
def update_notebook(nb_id):
    db = get_notes_db()
    row = db.execute('SELECT * FROM notebooks WHERE id = ? AND user_id = ?',
                     (nb_id, g.user_id)).fetchone()
    if not row:
        return error('Notebook not found')
    data = request.get_json(silent=True) or {}
    name = data.get('name', row['name'])
    color = data.get('color', row['color'])
    db.execute('UPDATE notebooks SET name = ?, color = ? WHERE id = ? AND user_id = ?',
               (name, color, nb_id, g.user_id))
    db.commit()
    return success({'id': nb_id, 'name': name, 'color': color, 'createdAt': row['created_at']})


@notebooks_bp.route('/api/notebooks/<nb_id>', methods=['DELETE'])
def delete_notebook(nb_id):
    db = get_notes_db()
    row = db.execute('SELECT id FROM notebooks WHERE id = ? AND user_id = ?',
                     (nb_id, g.user_id)).fetchone()
    if not row:
        return error('Notebook not found')
    db.execute('DELETE FROM notebooks WHERE id = ? AND user_id = ?', (nb_id, g.user_id))
    now_ts = now_iso()
    db.execute(
        '''UPDATE notes SET notebook_id = NULL, version = version + 1, updated_at = ?
           WHERE notebook_id = ? AND user_id = ?''',
        (now_ts, nb_id, g.user_id)
    )
    db.commit()
    return success(None)
