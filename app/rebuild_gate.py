"""重建期语义闸门（docs/06 v3 §7.1，终审阻塞 3）。

索引重建期间**不得静默返回部分结果**：`/search` 与 `/memories` 必须明确 503，
而不是拿着半张新索引/半张旧索引作答。这里只提供一个线程安全的布尔闸门，
由 `POST /admin/rebuild` 在触发时置位、完成/失败时清除；读端点只做 O(1) 判断。

闸门是 **per-app 实例**（挂在 ``app.state.rebuild_gate``）而不是模块级单例：
同一进程里的多个 app（测试）互不污染，语义上仍是「全局」——一个进程只有一个
重建在跑（部署固定单 worker，见 ``app/main.py``）。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

REBUILDING_ERROR = "rebuilding"
REBUILDING_MESSAGE = "索引重建中，请稍后"


class RebuildingInProgress(Exception):
    """读/写端点命中重建期时抛出的哨兵；由 main 的异常处理器映射成 503。"""

    def __init__(self) -> None:
        super().__init__(REBUILDING_MESSAGE)


class RebuildGate:
    """线程安全的 rebuilding 标志。

    ``/admin/rebuild`` 在**做任何重建工作之前**置位，``finally`` 里清除；
    其它线程（FastAPI 同步端点跑在线程池里）在入口读一次。

    内部用**计数**而不是布尔：两个并发重建重叠时，先结束的那个不会把仍在
    运行的重建提前“解除”（否则读端点会拿到半张索引）。``is_rebuilding`` 仍
    等价于「至少有一个重建在跑」。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._depth = 0

    @property
    def is_rebuilding(self) -> bool:
        with self._lock:
            return self._depth > 0

    def begin(self) -> None:
        with self._lock:
            self._depth += 1

    def end(self) -> None:
        with self._lock:
            if self._depth > 0:
                self._depth -= 1
            else:  # pragma: no cover - 只有错误配对才会到这里
                self._depth = 0

    @contextmanager
    def rebuilding(self):
        """``with gate.rebuilding(): ...`` —— 进入置位，退出（含异常）清除。"""
        self.begin()
        try:
            yield
        finally:
            self.end()

    def raise_if_rebuilding(self) -> None:
        if self.is_rebuilding:
            raise RebuildingInProgress()
