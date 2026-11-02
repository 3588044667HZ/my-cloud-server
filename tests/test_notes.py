"""notes 插件测试：CRUD / 增量同步 / 乐观锁 / 回收站 / restore。"""


def _create_note(client, auth_headers, **kw):
    body = {'title': kw.get('title', 'T'), 'content': kw.get('content', 'C'),
            'color': kw.get('color', 'blue')}
    if kw.get('notebookId'):
        body['notebookId'] = kw['notebookId']
    r = client.post('/api/notes', json=body, headers=auth_headers)
    assert r.status_code == 200, r.json
    return r.json['data']


class TestNotesCRUD:
    def test_create_and_get(self, client, auth_headers):
        note = _create_note(client, auth_headers, title='Hello')
        r = client.get(f"/api/notes/{note['id']}", headers=auth_headers)
        assert r.status_code == 200
        assert r.json['data']['title'] == 'Hello'
        assert r.json['data']['version'] == 1

    def test_list_pagination(self, client, auth_headers):
        r = client.get('/api/notes?page=1&size=2', headers=auth_headers)
        assert r.status_code == 200
        assert r.json['pagination']['size'] == 2

    def test_update(self, client, auth_headers):
        note = _create_note(client, auth_headers)
        r = client.put(f"/api/notes/{note['id']}", json={'title': 'U'},
                       headers=auth_headers)
        assert r.status_code == 200
        assert r.json['data']['version'] == 2

    def test_conflict_409(self, client, auth_headers):
        note = _create_note(client, auth_headers)
        r = client.put(f"/api/notes/{note['id']}", json={'title': 'X'},
                       headers={**auth_headers, 'If-Match': '2020-01-01T00:00:00Z'})
        assert r.status_code == 409
        assert r.json['code'] == 409

    def test_pin(self, client, auth_headers):
        note = _create_note(client, auth_headers)
        r = client.put(f"/api/notes/{note['id']}/pin", headers=auth_headers)
        assert r.json['data']['isPinned'] is True


class TestSync:
    def test_full_sync(self, client, auth_headers):
        r = client.get('/api/notes/sync', headers=auth_headers)
        assert r.status_code == 200
        body = r.json['data']
        assert 'updated' in body and 'deletedIds' in body and 'serverTime' in body

    def test_incremental_sync(self, client, auth_headers):
        note = _create_note(client, auth_headers, title='SyncMe')
        r = client.delete(f"/api/notes/{note['id']}", headers=auth_headers)
        assert r.status_code == 200
        r = client.get('/api/notes/sync?since=2020-01-01T00:00:00Z', headers=auth_headers)
        deleted = r.json['data']['deletedIds']
        assert note['id'] in deleted


class TestTrashRestore:
    def test_trash_and_recover(self, client, auth_headers):
        note = _create_note(client, auth_headers, title='TrashMe')
        r = client.delete(f"/api/notes/{note['id']}", headers=auth_headers)
        assert r.status_code == 200
        r = client.get('/api/notes/trash', headers=auth_headers)
        ids = [n['id'] for n in r.json['data']]
        assert note['id'] in ids
        r = client.put(f"/api/notes/{note['id']}/recover", headers=auth_headers)
        assert r.json['data']['deletedAt'] is None

    def test_permanent(self, client, auth_headers):
        note = _create_note(client, auth_headers)
        client.delete(f"/api/notes/{note['id']}", headers=auth_headers)
        r = client.delete(f"/api/notes/{note['id']}/permanent", headers=auth_headers)
        assert r.status_code == 200

    def test_restore_conflict_recovery(self, client, auth_headers):
        note = _create_note(client, auth_headers, title='Orig')
        client.delete(f"/api/notes/{note['id']}", headers=auth_headers)
        r = client.put(f"/api/notes/{note['id']}/restore",
                       json={'title': 'LocalWin', 'content': 'local content',
                             'updatedAt': '2026-08-17T09:30:00Z'},
                       headers=auth_headers)
        assert r.status_code == 200
        data = r.json['data']
        assert data['title'] == 'LocalWin'
        assert data['deletedAt'] is None
        assert data['updatedAt'] == '2026-08-17T09:30:00Z'

    def test_restore_missing_404(self, client, auth_headers):
        r = client.put('/api/notes/nonexist/restore', json={'title': 'a', 'content': 'b'},
                       headers=auth_headers)
        assert r.status_code == 404


class TestNotebooks:
    def test_crud(self, client, auth_headers):
        r = client.post('/api/notebooks', json={'name': 'NB', 'color': '#4A90D9'},
                        headers=auth_headers)
        nb_id = r.json['data']['id']
        r = client.get('/api/notebooks', headers=auth_headers)
        assert any(n['id'] == nb_id for n in r.json['data'])
        r = client.put(f'/api/notebooks/{nb_id}', json={'name': 'NB2'}, headers=auth_headers)
        assert r.json['data']['name'] == 'NB2'
        r = client.delete(f'/api/notebooks/{nb_id}', headers=auth_headers)
        assert r.status_code == 200

    def test_delete_notebook_nulls_notes(self, client, auth_headers):
        r = client.post('/api/notebooks', json={'name': 'DelNB'}, headers=auth_headers)
        nb_id = r.json['data']['id']
        note = _create_note(client, auth_headers, notebookId=nb_id)
        client.delete(f'/api/notebooks/{nb_id}', headers=auth_headers)
        r = client.get(f"/api/notes/{note['id']}", headers=auth_headers)
        assert r.json['data']['notebookId'] is None
