"""Warm pool for pre-initialized sandbox containers.

The warm pool keeps 1–2 sandbox containers/pods ready so that when a run
is claimed, it can be assigned to a warm container instead of starting from
scratch. This reduces cold-start latency significantly.

In Phase 1 (Docker backend), the warm pool pre-creates containers with the
runner image loaded and dependencies cached, but without a RUN_ID assigned.
When a run is claimed, the launcher assigns the run to a warm container by
setting the RUN_ID env var.

In Phase 2 (K8s backend), the warm pool uses a pre-built image cache DaemonSet
on kind nodes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agentbox.settings import settings

logger = logging.getLogger(__name__)


class WarmPool:
    """A pool of pre-initialized sandbox containers ready for hot handoff.

    The pool size is controlled by ``settings.warm_pool_size`` (0 = disabled).
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self._pool: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._maintain_task: asyncio.Task | None = None
        self._started = False

    @property
    def size(self) -> int:
        return len(self._pool)

    @property
    def target_size(self) -> int:
        return settings.warm_pool_size

    async def start(self) -> None:
        """Start maintaining the warm pool."""
        if self.target_size <= 0:
            logger.info("Warm pool disabled (warm_pool_size=%d)", self.target_size)
            return

        self._started = True
        self._maintain_task = asyncio.create_task(self._maintain_loop())
        logger.info("Warm pool started (target_size=%d)", self.target_size)

    async def stop(self) -> None:
        """Stop the warm pool and clean up all warm containers."""
        self._started = False
        if self._maintain_task:
            self._maintain_task.cancel()
            try:
                await self._maintain_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            for entry in self._pool:
                try:
                    await self._backend.kill(entry["container_id"])
                except Exception:
                    logger.exception("Failed to clean up warm container")
            self._pool.clear()
        logger.info("Warm pool stopped")

    async def acquire(self) -> dict[str, Any] | None:
        """Acquire a warm container from the pool.

        Returns the container info dict (including 'container_id') or None
        if the pool is empty.
        """
        if self.target_size <= 0:
            return None

        async with self._lock:
            if not self._pool:
                return None
            entry = self._pool.pop(0)
            cold_start_ms = entry.pop("warm_start_ms", 0)
            logger.info(
                "Acquired warm container %s (pool size now %d)",
                entry.get("container_id", "?"),
                len(self._pool),
            )
            entry["cold_start_ms"] = cold_start_ms
            return entry

    async def _maintain_loop(self) -> None:
        """Periodically refill the pool to target size."""
        while self._started:
            try:
                await self._refill()
            except Exception:
                logger.exception("Error maintaining warm pool")
            await asyncio.sleep(5)

    async def _refill(self) -> None:
        """Refill the pool to the target size."""
        async with self._lock:
            needed = self.target_size - len(self._pool)
            if needed <= 0:
                return

        for _ in range(needed):
            try:
                start = time.monotonic()
                container_id = await self._backend.create_warm_container()
                warm_time_ms = (time.monotonic() - start) * 1000
                async with self._lock:
                    self._pool.append(
                        {
                            "container_id": container_id,
                            "warm_start_ms": warm_time_ms,
                        }
                    )
                logger.info(
                    "Warm container %s created (%.0fms, pool now %d/%d)",
                    container_id,
                    warm_time_ms,
                    len(self._pool),
                    self.target_size,
                )
            except Exception as exc:
                logger.warning("Failed to create warm container: %s", exc)
                break
