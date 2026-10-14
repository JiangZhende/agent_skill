"""
worker：从数据库的运行队列里领取并执行运行。

    python -m core.worker                # 单独起一个 worker 进程（WORKER_MODE=external 时用）
    python -m core.worker --threads 4

WORKER_MODE=embedded（默认）时，Web UI 和 API 进程会各自在进程内启动 worker 线程，不用单独起。
worker 是唯一需要 Docker 权限的组件：WORKER_MODE=external 时，入口进程不执行任何脚本。
"""
import argparse
import logging
import signal
import threading
import time

from core import config

logger = logging.getLogger("agent.worker")

POLL_INTERVAL = 0.5


class Worker:
    def __init__(self, service, threads: int = None):
        self.service = service
        self.threads = threads or config.WORKER_THREADS
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self):
        for i in range(self.threads):
            t = threading.Thread(target=self._loop, daemon=True, name=f"worker-{i}")
            t.start()
            self._threads.append(t)
        threading.Thread(target=self._sweep_loop, daemon=True, name="worker-sweeper").start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            try:
                worked = self.service.run_next()
            except Exception:
                logger.exception("worker loop failed")
                worked = False
            if not worked:
                self._stop.wait(POLL_INTERVAL)

    def _sweep_loop(self):
        while not self._stop.wait(15):
            try:
                self.service.reap()
            except Exception:
                logger.exception("reap failed")

    def stop(self, timeout: float = 10):
        """不再领取新的运行；等正在执行的运行结束（最多 timeout 秒）。"""
        self._stop.set()
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))


def main(argv=None):
    from core import db
    from core.sessions import SessionService

    parser = argparse.ArgumentParser(prog="python -m core.worker", description="运行队列的 worker")
    parser.add_argument("--threads", type=int, default=None, help=f"同时执行几个运行，默认 {config.WORKER_THREADS}")
    parser.add_argument("--skills-dir", default=None, help="skill 目录，默认项目下的 skills/")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    db.get_engine()
    if config.USE_DOCKER:
        from core.executor import cleanup_orphans
        cleanup_orphans()
    service = SessionService(skills_dir=args.skills_dir)
    service.startup()
    worker = Worker(service, args.threads).start()
    logger.info("worker started: %d threads", worker.threads)

    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    while not stopping.wait(15):
        try:
            service.sweep()
        except Exception:
            logger.exception("sweep failed")
    logger.info("worker stopping: waiting for running tasks")
    worker.stop(timeout=config.RUN_TIME_BUDGET or 60)
    service.close()


if __name__ == "__main__":
    main()
