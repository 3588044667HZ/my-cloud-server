"""drive 插件测试：开关 / shares / list / 沙箱 / 回收站 / 上传。"""
import os

import pytest

import config


def _enable_drive(client, admin_headers, enabled=True):
    r = client.put('/api/admin/drive/enabled', json={'enabled': enabled},
                   headers=admin_headers)
    assert r.status_code == 200


@pytest.fixture(autouse=True)
def _drive_on(client, admin_headers):
    """本模块多数测试假定云盘已开启；每测试前重置为开启（消除跨文件状态泄漏）。"""
    _enable_drive(client, admin_headers, True)


class TestGate:
    def test_disabled_403(self, client, auth_headers, admin_headers):
        _enable_drive(client, admin_headers, False)
        r = client.get('/api/drive/shares', headers=auth_headers)
        assert r.status_code == 403
        assert r.json['code'] == 10403


class TestSharesAndList:
    def test_shares(self, client, auth_headers, admin_headers):
        _enable_drive(client, admin_headers)
        r = client.get('/api/drive/shares', headers=auth_headers)
        assert r.status_code == 200
        names = [s['name'] for s in r.json['data']]
        assert 'TEST' in names

    def test_list(self, client, auth_headers):
        r = client.get('/api/drive/list?share=TEST&path=/', headers=auth_headers)
        assert r.status_code == 200
        entries = r.json['data']['entries']
        assert any(e['name'] == 'docs' and e['isDir'] for e in entries)

    def test_mkdir_and_stat(self, client, auth_headers):
        r = client.post('/api/drive/mkdir', json={'share': 'TEST', 'path': '/newdir'},
                        headers=auth_headers)
        assert r.status_code == 200
        r = client.get('/api/drive/stat?share=TEST&path=/newdir', headers=auth_headers)
        assert r.status_code == 200
        assert r.json['data']['isDir'] is True


class TestSandbox:
    def test_traversal_blocked(self, client, auth_headers):
        for bad in ('/../..', '/..\\..', 'C:/Windows'):
            r = client.get(f'/api/drive/list?share=TEST&path={bad}', headers=auth_headers)
            assert r.status_code in (403, 404), f'{bad} -> {r.status_code}'

    def test_unknown_share(self, client, auth_headers):
        r = client.get('/api/drive/list?share=NOPE&path=/', headers=auth_headers)
        assert r.status_code in (403, 404)


class TestTrash:
    def test_delete_to_trash_and_restore(self, client, auth_headers):
        # 准备文件
        r = client.post('/api/drive/upload/init', json={'share': 'TEST', 'path': '/doc.bin'},
                        headers=auth_headers)
        upload_id = r.json['data']['uploadId']
        client.put(f'/api/drive/upload/{upload_id}/0', data=b'hello', headers=auth_headers)
        r = client.post(f'/api/drive/upload/{upload_id}/complete', headers=auth_headers)
        assert r.status_code == 200

        # 删除进回收站
        r = client.delete('/api/drive/delete', query_string={'share': 'TEST', 'path': '/doc.bin'},
                          headers=auth_headers)
        assert r.status_code == 200

        r = client.get('/api/drive/stat?share=TEST&path=/doc.bin', headers=auth_headers)
        assert r.status_code == 404

        # 回收站列表
        r = client.post('/api/drive/trash/list', json={'share': 'TEST'}, headers=auth_headers)
        assert r.status_code == 200
        items = r.json['data']
        assert len(items) >= 1
        item_id = items[-1]['id']
        assert items[-1]['origPath'] == '/doc.bin'

        # 还原
        r = client.post('/api/drive/trash/restore', json={'id': item_id}, headers=auth_headers)
        assert r.status_code == 200
        r = client.get('/api/drive/stat?share=TEST&path=/doc.bin', headers=auth_headers)
        assert r.status_code == 200


class TestUpload:
    def test_chunked_upload(self, client, auth_headers):
        r = client.post('/api/drive/upload/init', json={'share': 'TEST', 'path': '/up/out.bin'},
                        headers=auth_headers)
        upload_id = r.json['data']['uploadId']
        client.put(f'/api/drive/upload/{upload_id}/0', data=b'AAAA', headers=auth_headers)
        client.put(f'/api/drive/upload/{upload_id}/1', data=b'BBBB', headers=auth_headers)
        r = client.post(f'/api/drive/upload/{upload_id}/complete', headers=auth_headers)
        assert r.status_code == 200

        r = client.get('/api/drive/download?share=TEST&path=/up/out.bin', headers=auth_headers)
        assert r.status_code == 200
        assert r.data == b'AAAABBBB'
        r.close()

    def test_upload_md5_mismatch(self, client, auth_headers):
        r = client.post('/api/drive/upload/init',
                        json={'share': 'TEST', 'path': '/bad.bin', 'md5': '0' * 32},
                        headers=auth_headers)
        upload_id = r.json['data']['uploadId']
        client.put(f'/api/drive/upload/{upload_id}/0', data=b'data', headers=auth_headers)
        r = client.post(f'/api/drive/upload/{upload_id}/complete', headers=auth_headers)
        assert r.status_code == 400
        assert r.json['code'] == 10405

    def test_download_range(self, client, auth_headers):
        # Range 下载（HTTP 206）
        import os
        share_root = os.path.join(config.DATA_DIR, 'share1')
        fpath = os.path.join(share_root, 'range.bin')
        with open(fpath, 'wb') as f:
            f.write(b'0123456789')
        r = client.get('/api/drive/download?share=TEST&path=/range.bin',
                       headers={**auth_headers, 'Range': 'bytes=2-5'})
        assert r.status_code in (206, 200)
        if r.status_code == 206:
            assert r.data == b'2345'
        r.close()
