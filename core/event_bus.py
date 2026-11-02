"""进程内事件总线：publish(topic, payload) + subscribe(topic, handler)。

无持久化；插件跨进程协作的异步解耦点（如 photo.uploaded / file.ref.updated）。
"""
import logging
import threading

log = logging.getLogger('event_bus')

_subscribers = {}
_lock = threading.Lock()


def subscribe(topic, handler):
    with _lock:
        _subscribers.setdefault(topic, []).append(handler)


def unsubscribe(topic, handler):
    with _lock:
        handlers = _subscribers.get(topic)
        if handlers and handler in handlers:
            handlers.remove(handler)


def publish(topic, payload=None):
    """同步分发：handler 异常仅记日志，不打断调用方。"""
    with _lock:
        handlers = list(_subscribers.get(topic, []))
    for h in handlers:
        try:
            h(payload)
        except Exception:  # noqa: BLE001
            log.exception('event handler failed: %s', topic)
