"""云服务 Web 前端服务端接口测试（SPEC-WEB-FRONTEND.md）：portal apps / photos list / drive 可选鉴权。"""
import io

from PIL import Image


def _make_jpeg():
    buf = io.BytesIO()
    Image.new('RGB', (60, 40), (0, 200, 100)).save(buf, 'JPEG')
    return buf.getvalue()


class TestPortalApps:
    def test_requires_device_auth(self, client):
        r = client.get('/api/portal/apps')
        assert r.status_code == 401

    def test_returns_apps(self, client, auth_headers):
        r = client.get('/api/portal/apps', headers=auth_headers)
        assert r.status_code == 200
        data = r.json['data']
        assert len(data) >= 3
        ids = [a['id'] for a in data]
        assert 'album' in ids and 'drive' in ids and 'note' in ids


class TestPhotosList:
    def test_pagination_shape(self, client, auth_headers):
        r = client.get('/api/photos/list?page=1&size=5', headers=auth_headers)
        assert r.status_code == 200
        assert 'pagination' in r.json
        assert r.json['pagination']['page'] == 1
        assert r.json['pagination']['size'] == 5
        assert 'thumbUrl' in r.json['data'][0] if r.json['data'] else True

    def test_list_after_upload(self, client, auth_headers):
        client_id = 'list-' + io.BytesIO().read().__str__()
        r = client.post('/api/photos/register', json={'clientId': 'list1', 'md5': ''},
                        headers=auth_headers)
        pid = r.json['data']['id']
        client.put(f'/api/photos/{pid}/file', data=_make_jpeg(), headers=auth_headers)
        r = client.get('/api/photos/list', headers=auth_headers)
        assert any(p['id'] == pid for p in r.json['data'])


class TestDriveOptionalAuth:
    def _setup(self, client, admin_headers, auth_headers, tmp_path):
        import config
        share = tmp_path / 'share'
        share.mkdir(parents=True, exist_ok=True)
        with open(share / 'pic.jpg', 'wb') as f:
            f.write(_make_jpeg())
        config.DRIVE_SHARES = {'WEBT': str(share)}
        client.put('/api/admin/drive/enabled', json={'enabled': True}, headers=admin_headers)

    def test_thumb_optional_auth(self, client, admin_headers, auth_headers, tmp_path):
        """缩略图匿名可读（<img> 预览场景）。"""
        self._setup(client, admin_headers, auth_headers, tmp_path)
        r = client.get('/api/drive/thumb?share=WEBT&path=/pic.jpg')
        assert r.status_code == 200
        assert r.mimetype == 'image/webp'
        r.close()

    def test_download_anonymous_requires_sig(self, client, admin_headers, auth_headers, tmp_path):
        """匿名下载：无签名 → 401；有效签名 → 200（ARCHITECTURE §6.8）。"""
        self._setup(client, admin_headers, auth_headers, tmp_path)
        r = client.get('/api/drive/download?share=WEBT&path=/pic.jpg')
        assert r.status_code == 401

        # 设备签发签名 URL → 匿名可下载
        r = client.get('/api/drive/sign?share=WEBT&path=/pic.jpg&expires=60', headers=auth_headers)
        assert r.status_code == 200
        url = r.json['data']['url']
        assert 'sig=' in url
        r = client.get(url)
        assert r.status_code == 200
        assert r.data == _make_jpeg()
        r.close()

    def test_download_tampered_sig_rejected(self, client, admin_headers, auth_headers, tmp_path):
        self._setup(client, admin_headers, auth_headers, tmp_path)
        r = client.get('/api/drive/sign?share=WEBT&path=/pic.jpg', headers=auth_headers)
        url = r.json['data']['url']
        r = client.get(url.replace('sig=', 'sig=deadbeef'))
        assert r.status_code == 401

    def test_sign_requires_device(self, client, admin_headers, tmp_path):
        import config
        share = tmp_path / 'share'
        share.mkdir(parents=True, exist_ok=True)
        with open(share / 'a.bin', 'wb') as f:
            f.write(b'x')
        config.DRIVE_SHARES = {'WEBT2': str(share)}
        client.put('/api/admin/drive/enabled', json={'enabled': True}, headers=admin_headers)
        r = client.get('/api/drive/sign?share=WEBT2&path=/a.bin')
        assert r.status_code == 401

    def test_list_still_requires_auth(self, client, admin_headers, auth_headers, tmp_path):
        self._setup(client, admin_headers, auth_headers, tmp_path)
        r = client.get('/api/drive/list?share=WEBT&path=/')
        assert r.status_code == 401
