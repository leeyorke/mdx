"""复习提醒的后台常驻循环

随 FastAPI 一起起停（见 ``app/main.py`` 的 lifespan）：

- 启动时如果功能已开启，先同步一次队列再跑一轮调度；
- 每轮跑完就算「下一个该醒来的时刻」——到期时间由调度算法精确计算，
  免打扰时段会把唤醒时间推后，所以平时几乎不空转；
- 唤醒间隔封顶 10 分钟，保证改了配置（开关、配额、免打扰）能较快生效；
- 关闭时干净地取消任务，不留悬挂的协程。
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.review.constants import TICK_INTERVAL_SECONDS
from app.review.schedule import get_next_wakeup_at
from app.review.service import get_config, run_review_tick, sync_review_queue
from app.review.store import load_schedule

log = logging.getLogger("app.review.scheduler")

#: 单次休眠的上限（秒）。用来兜住「改了配置要尽快生效」。
MAX_SLEEP_SECONDS = 600


class ReviewScheduler:
    """按需唤醒的复习调度器。"""

    def __init__(self, interval_seconds: int = TICK_INTERVAL_SECONDS) -> None:
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        """启动后台循环（幂等）。"""
        if self._task and not self._task.done():
            return

        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="mdx-review-scheduler")
        log.info("复习提醒调度器已启动（最短间隔 %ss）", self._interval)

    async def stop(self) -> None:
        """停止后台循环。"""
        self._stopping.set()

        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                log.warning("停止复习提醒调度器时出错", exc_info=True)
        log.info("复习提醒调度器已停止")

    async def _run(self) -> None:
        # 启动后先等一小会儿，别和 FastAPI 的启动过程抢资源
        await asyncio.sleep(1)

        try:
            await asyncio.to_thread(self._initial_pass)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("复习提醒启动同步失败：%s", exc)

        while not self._stopping.is_set():
            wait_seconds = self._interval
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=wait_seconds)
                return  # 收到停止信号
            except asyncio.TimeoutError:
                pass

            try:
                await asyncio.to_thread(run_review_tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("复习调度轮次出错：%s", exc)

            try:
                wait_seconds = await asyncio.to_thread(self._next_wait_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("计算下次唤醒时间失败：%s", exc)

    def _next_wait_seconds(self) -> int:
        """距离下一次该唤醒还差多少秒。"""
        config = get_config()
        if not config.get("enabled"):
            return MAX_SLEEP_SECONDS

        now_ms = int(time.time() * 1000)
        schedule = load_schedule()
        wake_at = get_next_wakeup_at(schedule, config, now_ms)

        if wake_at is None:
            return MAX_SLEEP_SECONDS

        seconds = max(1, (wake_at - now_ms) // 1000)
        return int(min(max(seconds, self._interval), MAX_SLEEP_SECONDS))

    def _initial_pass(self) -> None:
        """启动后的第一轮：同步队列 + 跑一次调度。"""
        if not get_config().get("enabled"):
            log.info("复习提醒未启用，调度器空转")
            return

        sync_review_queue()
        run_review_tick()


#: 进程内共享的调度器实例（由 lifespan 启停）。
scheduler = ReviewScheduler()
