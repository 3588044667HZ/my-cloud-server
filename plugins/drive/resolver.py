"""ShareResolver 抽象（ARCHITECTURE.md §6.3）：share 名 → 本地根路径。

共享来源（优先级从高到低，同名去重）：
  1. 后台配置（drive.db drive_shares，管理员经 /api/admin/drive/shares 管理）
  2. config.DRIVE_SHARES / DRIVE_SHARES_JSON（引导/测试兜底）
  3. Windows 注册表 SMB 共享（自动发现，过滤隐藏共享与系统盘）
- RemoteSmbResolver：未来 SMB 迁走后换 smbprotocol 实现（占位）。
"""
import os
import shutil

import config


class Share:
    def __init__(self, name, path, comment='', hidden=False):
        self.name = name
        self.path = path
        self.comment = comment
        self.hidden = hidden


class ShareResolver:
    def list_shares(self):
        raise NotImplementedError

    def resolve(self, share):
        raise NotImplementedError


def _parse_registry_share_data(data):
    """解析注册表共享值：可能是 REG_SZ 字符串（换行分隔）或 REG_MULTI_SZ 列表。
    键统一转小写，兼容 Windows 注册表里 'Path'/'path' 的大小写差异。"""
    if isinstance(data, list):
        lines = data
    elif isinstance(data, str):
        lines = data.split('\n')
    else:
        lines = []
    info = dict()
    for line in lines:
        if '=' in line:
            k, _, v = line.partition('=')
            info[k.strip().lower()] = v.strip()
    return info


def _read_registry_shares():
    """读取本机 SMB 共享（Windows）。返回 [(name, path, comment), ...]。"""
    shares = []
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r'SYSTEM\CurrentControlSet\Services\LanmanServer\Shares')
        try:
            i = 0
            while True:
                name, data, _ = winreg.EnumValue(key, i)
                i += 1
                info = _parse_registry_share_data(data)
                shares.append((name, info.get('path', ''), info.get('remark', '')))
        except OSError:
            pass
        finally:
            key.Close()
    except OSError:
        pass
    return shares


def list_db_shares(enabled_only=True):
    """读取后台配置的共享（drive.db drive_shares）。返回 sqlite3.Row 列表。"""
    try:
        from core.db import get_conn
        from plugins.drive.db import DRIVE_DB
        db = get_conn(DRIVE_DB)
    except Exception:
        return []
    sql = 'SELECT * FROM drive_shares'
    if enabled_only:
        sql += ' WHERE enabled = 1'
    sql += ' ORDER BY name'
    return db.execute(sql).fetchall()


class LocalShareResolver(ShareResolver):
    def __init__(self, explicit=None):
        # explicit：config.DRIVE_SHARES {share名: 本地路径}，优先级最高的引导配置
        self.explicit = explicit if explicit is not None else dict(config.DRIVE_SHARES)

    def list_shares(self):
        result = []
        seen = set()
        # 1) 后台配置（管理员管理，可持久化）
        for row in list_db_shares(enabled_only=True):
            result.append(Share(row['name'], os.path.realpath(row['path']), row['comment']))
            seen.add(row['name'])
        # 2) 引导配置
        for name, path in self.explicit.items():
            if name in seen:
                continue
            result.append(Share(name, os.path.realpath(path), comment='configured'))
            seen.add(name)
        # 3) 注册表共享
        for name, path, remark in _read_registry_shares():
            if name in seen:
                continue
            hidden = name.endswith('$')
            real = os.path.realpath(path) if path else ''
            if not real or not os.path.isdir(real):
                continue
            if hidden:
                continue
            if _is_system_drive(real):
                continue
            result.append(Share(name, real, remark))
            seen.add(name)
        return result

    def resolve(self, share):
        shares = {s.name: s.path for s in self.list_shares()}
        path = shares.get(share)
        if not path:
            raise ValueError(f'unknown share: {share}')
        return path


def _is_system_drive(path):
    """判断路径是否落在系统盘（默认排除，防误删系统文件）。"""
    system = os.environ.get('SystemDrive', 'C:').lower()
    drive = os.path.splitdrive(os.path.realpath(path))[0].lower()
    return drive == system.lower() and system


class RemoteSmbResolver(ShareResolver):
    """未来：SMB 迁走后用 smbprotocol 实现。"""

    def list_shares(self):
        raise NotImplementedError

    def resolve(self, share):
        raise NotImplementedError


def get_resolver():
    """按当前 config.DRIVE_SHARES 构造解析器（每次调用重建，便于测试覆盖）。"""
    return LocalShareResolver()


def share_info(share):
    """单个共享信息（含剩余空间）。"""
    res = get_resolver()
    root = res.resolve(share)
    free = total = used = None
    try:
        usage = shutil.disk_usage(root)
        total, used, free = usage.total, usage.used, usage.free
    except OSError:
        pass
    return {
        'name': share,
        'path': root,
        'free': free,
        'total': total,
        'used': used,
    }


def is_within_root(real, real_root):
    """判断 real 路径是否落在 real_root 沙箱内。

    Windows 下 os.path.realpath 对盘符根目录会保留尾部分隔符（如 'E:\\\\'），
    直接 real_root + os.sep 会产生双分隔符导致所有子路径被误判越界。
    """
    if real == real_root:
        return True
    root = real_root.rstrip(os.sep)
    return real.startswith(root + os.sep)
