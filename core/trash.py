"""云端回收站共享层（SPEC-CLOUD-RECYCLE-RECOVERY.md）。

- remainDays：`TRASH_RETAIN_DAYS - 已存放天数`，客户端离线可展示倒计时。
- 超期自动清除：各启用插件暴露 `sweep_expired()`，`trash.sweep` 任务每天扫描一次。
"""
import calendar
import logging
import threading
import time

from config import TRASH_RETAIN_DAYS, TRASH_SWEEP_INTERVAL
from core.util import now_iso, utc_ts

log = logging.getLogger('trash')

_registry = None
_sweep_timer = None


def set_registry(registry):
    global _registry
    _registry = registry


def _parse_ts(iso):
    """ISO（%Y-%m-%dT%H:%M:%SZ，UTC）→ unix 秒；解析失败返回 0。"""
    try:
        return calendar.timegm(time.strptime(iso, '%Y-%m-%dT%H:%M:%SZ'))
    except (ValueError, TypeError):
        return 0


def remain_days(deleted_at_iso):
    """软删剩余保留天数（0 = 已可清除）。"""
    if not deleted_at_iso:
        return None
    days = TRASH_RETAIN_DAYS - (utc_ts() - _parse_ts(deleted_at_iso)) / 86400.0
    return max(0, int(days))


def expired_cutoff_ts():
    return utc_ts() - TRASH_RETAIN_DAYS * 86400


def is_expired(deleted_at_iso):
    """某软删记录是否已超期（物理清除）。"""
    if not deleted_at_iso:
        return False
    return _parse_ts(deleted_at_iso) < expired_cutoff_ts()


# ---------- 超期清理 ----------

def sweep_expired():
    """对每个启用插件执行超期回收，返回 {模块名: 清除数}。"""
    removed = {}
    if _registry is None:
        return removed
    for name, plugin in list(_registry.plugins.items()):
        sweep = getattr(plugin, 'sweep_expired', None)
        if not callable(sweep):
            continue
        try:
            removed[name] = int(sweep() or 0)
        except Exception:  # noqa: BLE001 单插件失败不中断
            log.exception('trash sweep failed for plugin %s', name)
    return removed


def sweep_task_handler(payload):
    """task_queue 处理器：trash.sweep。"""
    removed = sweep_expired()
    log.info('trash sweep done: %s', removed)
    return removed


def upload_sweep_task_handler(payload):
    """task_queue 处理器：upload.sweep（孤儿上传会话 TTL 清理）。"""
    removed = {}
    if _registry is None:
        return removed
    for name, plugin in list(_registry.plugins.items()):
        sweep = getattr(plugin, 'sweep_uploads', None)
        if not callable(sweep):
            continue
        try:
            removed[name] = int(sweep() or 0)
        except Exception:  # noqa: BLE001
            log.exception('upload sweep failed for plugin %s', name)
    log.info('upload sweep done: %s', removed)
    return removed


def schedule_sweep(immediate=True):
    """注册每日超期清理：启动即跑一次 + 后台定时器每天入队回收站/上传会话清理。"""
    global _sweep_timer

    def _loop():
        while True:
            time.sleep(TRASH_SWEEP_INTERVAL)
            try:
                from core.task_queue import enqueue
                enqueue('trash.sweep', {})
                enqueue('upload.sweep', {})
            except Exception:  # noqa: BLE001
                log.exception('periodic sweep schedule failed')

    if _sweep_timer and _sweep_timer.is_alive():
        return
    if immediate:
        try:
            from core.task_queue import enqueue
            enqueue('trash.sweep', {})
            enqueue('upload.sweep', {})
        except Exception:  # noqa: BLE001
            log.exception('initial sweep enqueue failed')
    _sweep_timer = threading.Thread(target=_loop, name='trash-sweep-timer', daemon=True)
    _sweep_timer.start()
