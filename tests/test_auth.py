"""鉴权测试：配对 / 撤销 / 401 语义（UNIFIED_DEVICE_TOKEN_AUTH_SPEC §6）。"""
import uuid


class TestPair:
    def test_pair_success(self, client, admin_headers):
        r = client.post('/api/admin/devices/pair-code', headers=admin_headers)
        code = r.json['data']['code']
        r = client.post('/api/devices/pair',
                        json={'code': code, 'name': 'dev1', 'deviceId': str(uuid.uuid4())})
        assert r.status_code == 200
        data = r.json['data']
        assert 'token' in data
        assert 'deviceId' in data
        assert data['name'] == 'dev1'

    def test_pair_code_one_time(self, client, admin_headers):
        r = client.post('/api/admin/devices/pair-code', headers=admin_headers)
        code = r.json['data']['code']
        r1 = client.post('/api/devices/pair', json={'code': code})
        assert r1.status_code == 200
        r2 = client.post('/api/devices/pair', json={'code': code})
        assert r2.status_code == 401

    def test_pair_invalid_code(self, client):
        r = client.post('/api/devices/pair', json={'code': 'XXXX-XXXX'})
        assert r.status_code == 401
        assert r.json['code'] == 10401


class TestRequireDevice:
    def test_no_token_401(self, client):
        r = client.get('/api/notes')
        assert r.status_code == 401
        assert r.json['code'] == 10401

    def test_wrong_token_401(self, client):
        r = client.get('/api/notes', headers={'Authorization': 'Bearer bad'})
        assert r.status_code == 401

    def test_me_ok(self, client, auth_headers):
        r = client.get('/api/devices/me', headers=auth_headers)
        assert r.status_code == 200
        assert 'deviceId' in r.json['data']

    def test_health_public(self, client):
        r = client.get('/api/health')
        assert r.status_code == 200
        assert r.json['status'] == 'ok'


class TestRevoke:
    def test_revoke_invalidates(self, client, admin_headers):
        # 单独配对一台设备
        r = client.post('/api/admin/devices/pair-code', headers=admin_headers)
        r = client.post('/api/devices/pair', json={'code': r.json['data']['code']})
        device_id = r.json['data']['deviceId']
        headers = {'Authorization': f"Bearer {r.json['data']['token']}"}

        r = client.delete(f'/api/devices/{device_id}', headers=headers)
        assert r.status_code == 200

        r = client.get('/api/notes', headers=headers)
        assert r.status_code == 401

    def test_orphan_cleanup(self, client, admin_headers):
        r = client.post('/api/admin/devices/orphans/cleanup', headers=admin_headers)
        assert r.status_code == 200
        assert 'revoked' in r.json['data']
