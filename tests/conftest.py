"""pytest 夹具：隔离数据目录 + 应用 + 已配对设备。"""
import os
import shutil
import sys
import tempfile

import pytest

# 测试隔离：把数据目录指向临时目录（必须在导入 app/plugins 之前完成）
_TMP = tempfile.mkdtemp(prefix='mycloud_test_')

import config
config.DATA_DIR = _TMP
config.CORE_DB = os.path.join(_TMP, 'core.db')
config.ATTACH_DIR = os.path.join(_TMP, 'files')
config.PHOTOS_DIR = os.path.join(_TMP, 'photos')
config.BACKUP_DIR = os.path.join(_TMP, 'backup')

# drive 测试共享
_SHARE = os.path.join(_TMP, 'share1')
os.makedirs(os.path.join(_SHARE, 'docs'), exist_ok=True)
config.DRIVE_SHARES = {'TEST': _SHARE}

from core.auth import set_single_user
from core.bootstrap import init_core_db

init_core_db()
set_single_user('1', 'tester')


@pytest.fixture(scope='session')
def client():
    from app import create_app
    app = create_app(test_config={'TASK_QUEUE_ENABLED': False})
    with app.test_client() as c:
        yield c


@pytest.fixture(scope='session')
def admin_headers(client):
    r = client.post('/api/admin/login', json={'password': 'admin123'})
    assert r.status_code == 200, r.json
    return {'Authorization': f"Bearer {r.json['data']['token']}"}


@pytest.fixture(scope='session')
def auth_headers(client, admin_headers):
    """生成配对码并配对一台设备，返回请求头。"""
    r = client.post('/api/admin/devices/pair-code', headers=admin_headers)
    code = r.json['data']['code']
    r = client.post('/api/devices/pair', json={'code': code, 'name': 'pytest-device'})
    assert r.status_code == 200, r.json
    token = r.json['data']['token']
    return {'Authorization': f'Bearer {token}'}
