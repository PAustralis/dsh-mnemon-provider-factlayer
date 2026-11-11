"""/health 快照缓存（docs/06 v3 §7.1，终审阻塞 3）。

`/health` 必须 **O(1)**：不跑聚合查询、不加锁、不触发派生。所有昂贵统计
（pending 计数、索引缺口、备份状态、熔断、金丝雀）都由本模块的后台线程每
``HEALTH_REFRESH_SECONDS``（默认 5s）刷新一份快照，请求路径只做一次 dict 拷贝。

- 缓存尚未就绪（进程刚起、或首轮刷新失败）时 ``snapshot()`` 返回 ``None``，
  端点据此返回 ``{"status": "starting"}``。
- ``rebuilding`` 标志在请求时**从闸门现读**（O(1) 的布尔读取，不是缓存陈旧值），
  其余字段全部来自快照。
- 刷新失败保留上一份快照（只记日志），绝不让 /health 变成 500。
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from typing import Callable

logger = logging.getLogger("factlayer.health_cache")


class HealthCache:
    """后台刷新的 /health 快照。

    ``compute`` 必须是无副作用、可重复调用的函数（内部读库），由后台线程持有；
    ``rebuilding`` 只需暴露 ``is_rebuilding`` 属性（``RebuildGate``）。
    """

    def __init__(
        self,
        compute: Callable[[], dict],
        *,
        interval_seconds: float = 5.0,
        rebuilding=None,
        clock: Callable[[], float] = time.time,
        on_thread_stop: Callable[[], None] | None = None,
    ) -> None:
        self._compute = compute
        self._interval = max(0.05, float(interval_seconds))
        self._rebuilding = rebuilding
        self._clock = clock
        self._on_thread_stop = on_thread_stop
        self._refresh_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._snapshot: dict | None = None
        self._snapshot_at_ms: int | None = None
        self._last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 生命周期
    @property
    def is_ready(self) -> bool:
        with self._state_lock:
            return self._snapshot is not None

    def start(self, *, prime: bool = True) -> None:
        """先同步 prime 一份（启动即有事可报），再起后台刷新线程。幂等。"""
        if prime:
            self.refresh_now()
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="health-cache", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        try:
            while not self._stop.wait(self._interval):
                self.refresh_now()
        finally:
            # 后台线程的 SQLite 读连接是 thread-local：必须在自己线程里关闭。
            if self._on_thread_stop is not None:
                try:
                    self._on_thread_stop()
                except Exception:  # 关闭失败不影响退出
                    logger.exception("health cache read connection close failed")

    # ---------------------------------------------------------------- 刷新/读取
    def refresh_now(self) -> dict | None:
        """立即重算一份快照。失败时保留旧快照并返回 None（不抛给调用方）。"""
        with self._refresh_lock:
            try:
                report = self._compute()
            except Exception as exc:  # /health 不能因为一次统计失败而 500
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("health cache refresh failed")
                return None
            if not isinstance(report, dict):
                self._last_error = f"compute 返回了 {type(report).__name__}，不是 dict"
                logger.error("health cache refresh returned non-dict")
                return None
            report = dict(report)
            report["status"] = "ready"
            report["rebuilding"] = self._rebuilding_flag()
            at_ms = int(self._clock() * 1000)
            with self._state_lock:
                self._snapshot = report
                self._snapshot_at_ms = at_ms
                self._last_error = None
            return report

    def snapshot(self) -> dict | None:
        """请求路径：只做拷贝 + 现读 rebuilding，绝不查询数据库。"""
        with self._state_lock:
            snap = self._snapshot
            at_ms = self._snapshot_at_ms
        if snap is None:
            return None
        out = copy.deepcopy(snap)
        out["rebuilding"] = self._rebuilding_flag()
        now_ms = int(self._clock() * 1000)
        out["cache"] = {
            "generated_at_ms": at_ms,
            "age_ms": max(0, now_ms - int(at_ms or now_ms)),
            "refresh_interval_seconds": self._interval,
            "last_error": self._last_error,
        }
        return out

    def _rebuilding_flag(self) -> bool:
        if self._rebuilding is None:
            return False
        return bool(self._rebuilding.is_rebuilding)
