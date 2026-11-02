"""事件上报：后台线程 + 队列实现异步上报，避免阻塞执行主流程。

默认输出 JSON 行到 stdout；可替换为网络上报实现（MVP 不实现真实通道）。
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
from typing import Any, Optional, TextIO

from .models import Event

logger = logging.getLogger(__name__)


class Reporter:
    """异步事件上报器。

    事件写入队列，由守护线程逐条写出；close() 时排空队列。
    """

    def __init__(
        self,
        stream: Optional[TextIO] = None,
        async_enabled: bool = True,
        max_queue: int = 1024,
    ):
        self._stream = stream if stream is not None else sys.stdout
        self._async_enabled = async_enabled
        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=max_queue)
        self._thread: Optional[threading.Thread] = None
        if async_enabled:
            self._thread = threading.Thread(target=self._worker, name="tae-reporter", daemon=True)
            self._thread.start()

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._stream.write(json.dumps(item, ensure_ascii=False) + "\n")
                self._stream.flush()
            except Exception as e:  # 上报失败不应影响执行流程
                logger.error("上报写出失败: %s", e)

    def report(self, event: Event) -> None:
        """上报一个事件（fire-and-forget）。"""
        payload = event.to_dict()
        if self._async_enabled and self._thread is not None:
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                logger.warning("上报队列已满，丢弃事件: %s", event.event_type)
        else:
            self._stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._stream.flush()

    def close(self) -> None:
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=5)
            self._thread = None


__all__ = ["Reporter"]
