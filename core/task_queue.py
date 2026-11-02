"""SQLite 任务队列 + 单 worker 线程。

- tasks 表在 core.db；插件注册 task_handlers（kind → callable）。
- enqueue(kind, payload) 入队；worker 单线程循环：取 PENDING → 执行 → DONE/FAILED
  （失败按 attempt 指数退避重试，最大 MAX_ATTEMPTS 后置 FAILED）。
- 用于缩略图/分片合并/GC 等耗时操作，不阻塞请求路径。
"""
import json
import logging
import threading
import time

from config import CORE_DB
from core.auth import now_iso
from core.db import connect, get_conn

log = logging.getLogger('task_queue')

MAX_ATTEMPTS = 3
BACKOFF_BASE = 2.0

_status = 'stopped'
_handlers = {}
_cond = threading.Condition()
_thread = None
_stop = threading.Event()


def register(kind, handler):
    _handlers[kind] = handler


def enqueue(kind, payload=None):
    """入队一个任务；返回任务 id。"""
    db = get_conn(CORE_DB)
    cur = db.execute(
        'INSERT INTO tasks (kind, payload, status, attempt, created_at, updated_at) '
        'VALUES (?, ?, ?, 0, ?, ?)',
        (kind, json.dumps(payload or {}), 'pending', now_iso(), now_iso())
    )
    db.commit()
    task_id = cur.lastrowid
    _notify()
    return task_id


def _notify():
    with _cond:
        _cond.notify_all()


def _claim(db):
    row = db.execute(
        "SELECT * FROM tasks WHERE status = 'pending' ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    db.execute("UPDATE tasks SET status = 'running', updated_at = ? WHERE id = ?",
               (now_iso(), row['id']))
    db.commit()
    return dict(row)


def _process(task):
    kind = task['kind']
    payload = json.loads(task['payload'] or '{}')
    handler = _handlers.get(kind)
    if handler is None:
        raise RuntimeError(f'no handler for task kind: {kind}')
    handler(payload)


def _worker_loop():
    global _status
    log.info('task worker started')
    while not _stop.is_set():
        db = connect(CORE_DB)  # 独立连接（worker 线程不在请求上下文内）
        task = _claim(db)
        if task is None:
            db.close()
            with _cond:
                _cond.wait(timeout=1.0)
            continue

        attempt = task['attempt'] + 1
        try:
            _process(task)
            db.execute("UPDATE tasks SET status = 'done', attempt = ?, updated_at = ? "
                       'WHERE id = ?', (attempt, now_iso(), task['id']))
            db.commit()
        except Exception as e:  # noqa: BLE001
            log.exception('task %s failed: %s', task['id'], e)
            if attempt >= MAX_ATTEMPTS:
                db.execute("UPDATE tasks SET status = 'failed', attempt = ?, error = ?, "
                           'updated_at = ? WHERE id = ?',
                           (attempt, str(e), now_iso(), task['id']))
            else:
                backoff = BACKOFF_BASE ** (attempt - 1)
                db.execute(
                    "UPDATE tasks SET status = 'pending', attempt = ?, error = ?, "
                    'updated_at = ? WHERE id = ?',
                    (attempt, str(e), now_iso(), task['id']))
                # 指数退避：睡眠期间不持有连接
                db.commit()
                db.close()
                time.sleep(backoff)
                continue
            db.commit()
        db.close()


def start():
    global _thread, _status
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _status = 'running'
    _thread = threading.Thread(target=_worker_loop, name='task-worker', daemon=True)
    _thread.start()


def stop():
    global _status
    _stop.set()
    with _cond:
        _cond.notify_all()
    _status = 'stopped'


def is_running():
    return _status == 'running'
