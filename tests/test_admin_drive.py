"""后台硬盘管理测试：/api/admin/drive/shares CRUD + 余量 + 设备侧生效。"""
import os
import tempfile


def _enable_drive(client, admin_headers):
    r = client.put('/api/admin/drive/enabled', json={'enabled': True}, headers=admin_headers)
    assert r.status_code == 200


class TestAdminSharesGate:
    def test_requires_admin(self, client, auth_headers):
        r = client.get('/api/admin/drive/shares', headers=auth_headers)
        assert r.status_code == 401

    def test_requires_admin_write(self, client, auth_headers):
        r = client.post('/api/admin/drive/shares', json={'name': 'X', 'path': 'C:/'},
                        headers=auth_headers)
        assert r.status_code == 401


class TestAdminSharesCRUD:
    def test_list_includes_config_share(self, client, admin_headers):
        r = client.get('/api/admin/drive/shares', headers=admin_headers)
        assert r.status_code == 200
        shares = {s['name']: s for s in r.json['data']}
        assert 'TEST' in shares
        assert shares['TEST']['source'] == 'config'
        assert 'free' in shares['TEST']

    def test_add_share(self, client, admin_headers):
        share_dir = tempfile.mkdtemp(prefix='addshare_')
        r = client.post('/api/admin/drive/shares',
                        json={'name': 'DATA1', 'path': share_dir, 'comment': '数据盘'},
                        headers=admin_headers)
        assert r.status_code == 200, r.json
        data = r.json['data']
        assert data['name'] == 'DATA1'
        assert data['enabled'] is True
        assert data['source'] == 'db'
        assert data['exists'] is True

    def test_add_invalid_name(self, client, admin_headers):
        for bad in ('', 'with space', 'a$', 'x/y'):
            r = client.post('/api/admin/drive/shares',
                            json={'name': bad, 'path': 'C:/'}, headers=admin_headers)
            assert r.status_code == 400, f'{bad} -> {r.status_code}'

    def test_add_invalid_path(self, client, admin_headers):
        for path in ('not-a-path', 'Z:/definitely/not/exist'):
            r = client.post('/api/admin/drive/shares',
                            json={'name': 'BADPATH', 'path': path}, headers=admin_headers)
            assert r.status_code == 400, path

    def test_add_duplicate(self, client, admin_headers):
        share_dir = tempfile.mkdtemp(prefix='dup_')
        client.post('/api/admin/drive/shares', json={'name': 'DUP1', 'path': share_dir},
                    headers=admin_headers)
        r = client.post('/api/admin/drive/shares', json={'name': 'DUP1', 'path': share_dir},
                        headers=admin_headers)
        assert r.status_code == 409

    def test_update_share(self, client, admin_headers):
        share_dir = tempfile.mkdtemp(prefix='upd_')
        client.post('/api/admin/drive/shares', json={'name': 'UPD1', 'path': share_dir},
                    headers=admin_headers)
        new_dir = tempfile.mkdtemp(prefix='upd2_')
        r = client.put('/api/admin/drive/shares/UPD1',
                       json={'path': new_dir, 'comment': 'updated', 'enabled': False},
                       headers=admin_headers)
        assert r.status_code == 200, r.json
        data = r.json['data']
        assert data['path'].replace('\\', '/') == new_dir.replace('\\', '/')
        assert data['comment'] == 'updated'
        assert data['enabled'] is False

    def test_update_not_found(self, client, admin_headers):
        r = client.put('/api/admin/drive/shares/NOPE', json={'comment': 'x'},
                       headers=admin_headers)
        assert r.status_code == 404

    def test_delete_share(self, client, admin_headers):
        share_dir = tempfile.mkdtemp(prefix='del_')
        client.post('/api/admin/drive/shares', json={'name': 'DEL1', 'path': share_dir},
                    headers=admin_headers)
        r = client.delete('/api/admin/drive/shares/DEL1', headers=admin_headers)
        assert r.status_code == 200
        r = client.get('/api/admin/drive/shares', headers=admin_headers)
        assert 'DEL1' not in [s['name'] for s in r.json['data']]


class TestAdminShareAffectsDevice:
    def test_device_sees_db_share(self, client, admin_headers, auth_headers):
        _enable_drive(client, admin_headers)
        share_dir = tempfile.mkdtemp(prefix='dev_')
        client.post('/api/admin/drive/shares', json={'name': 'DEVDISK', 'path': share_dir},
                    headers=admin_headers)
        r = client.get('/api/drive/shares', headers=auth_headers)
        assert r.status_code == 200
        names = [s['name'] for s in r.json['data']]
        assert 'DEVDISK' in names

    def test_disabled_share_hidden_from_device(self, client, admin_headers, auth_headers):
        _enable_drive(client, admin_headers)
        share_dir = tempfile.mkdtemp(prefix='dis_')
        client.post('/api/admin/drive/shares', json={'name': 'HIDEDISK', 'path': share_dir},
                    headers=admin_headers)
        client.put('/api/admin/drive/shares/HIDEDISK', json={'enabled': False},
                   headers=admin_headers)
        r = client.get('/api/drive/shares', headers=auth_headers)
        assert 'HIDEDISK' not in [s['name'] for s in r.json['data']]
