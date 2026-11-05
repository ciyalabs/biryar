"""The worker process.

    python -m biryar.worker            # run until stopped
    python -m biryar.worker --once     # drain what is queued, then exit
    python -m biryar.worker --schedule # queue today's work and exit

Three commands rather than one loop that does everything, because they fail
differently and a deployment usually wants them separated: the worker is a
long-running process, the scheduler is a thing a timer fires, and ``--once``
is what a person runs when they want to see what happens.

## What the loop does between jobs

Nothing clever. It drains what it can claim, reclaims anything abandoned by
a worker that died, and sleeps. There is no long-poll and no notification
channel: a few seconds of latency on a settlement fetch costs nothing, and
``LISTEN``/``NOTIFY`` would be a second mechanism to keep working for a
benefit this does not need.

## Stopping

SIGTERM and SIGINT set a flag rather than raising. A worker killed midway
through a job leaves the row claimed, and although ``reclaim_stuck`` will
eventually free it, that is a quarter of an hour of nothing happening for no
reason. Finishing the job in hand first costs seconds.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from types import FrameType

import httpx

from .amazon import LwaApplication, LwaClient
from .config import Settings, settings
from .db.session import engine_for, session_factory
from .jobs.handlers import register_all
from .jobs.runner import Registry, Runner
from .jobs.schedule import schedule_settlement_fetches

logger = logging.getLogger(__name__)

#: How long to wait when there was nothing to do. Short enough that work
#: starts promptly, long enough that an idle worker is not a busy loop
#: against the database.
IDLE_SECONDS = 5.0

#: How often to look for jobs abandoned by workers that died. Rare, because
#: it is a scan and the situation it handles is not common.
RECLAIM_EVERY_SECONDS = 60.0


@dataclass
class Stopping:
    """Set by a signal, read by the loop."""

    requested: bool = False
    _installed: bool = field(default=False, repr=False)

    def install(self) -> None:
        def handle(signum: int, frame: FrameType | None) -> None:
            if self.requested:
                # Asked twice. Someone wants it gone now.
                logger.warning("worker: second signal, exiting immediately")
                raise SystemExit(1)
            logger.info(
                "worker: signal %s received, finishing the job in hand", signum,
            )
            self.requested = True

        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, handle)
        self._installed = True


def build_runner(
    config: Settings,
    *,
    http: httpx.Client | None = None,
    lwa: LwaClient | None = None,
) -> Runner:
    """Wire a runner with everything its handlers need.

    Both clients are injectable. Leaving one of them to be constructed here
    would mean anything exercising this function still reaches the real
    Amazon for half of what it does, which is not a test — it is an outage
    waiting for someone to run it in the wrong directory.
    """
    engine = engine_for(config)
    factory = session_factory(engine)
    registry = Registry()

    register_all(
        registry.register,
        lwa=lwa
        or LwaClient(
            LwaApplication(
                client_id=config.lwa_client_id,
                client_secret=config.lwa_client_secret.get_secret_value(),
            )
        ),
        keyring=config.keyring(),
        http=http or httpx.Client(timeout=30.0),
        factory=factory,
    )
    return Runner(factory=factory, registry=registry)


def serve(runner: Runner, *, stopping: Stopping | None = None) -> None:
    """Claim and run work until asked to stop."""
    stopping = stopping or Stopping()
    logger.info("worker: %s handling %s", runner.name, runner.registry.kinds())

    last_reclaim = 0.0
    while not stopping.requested:
        now = time.monotonic()
        if now - last_reclaim > RECLAIM_EVERY_SECONDS:
            runner.reclaim()
            last_reclaim = now

        if runner.run_once() is None:
            # Nothing claimable. Sleep in short steps so a signal is noticed
            # promptly rather than after the whole idle period.
            waited = 0.0
            while waited < IDLE_SECONDS and not stopping.requested:
                time.sleep(0.25)
                waited += 0.25

    logger.info("worker: stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="biryar.worker", description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once", action="store_true",
        help="run what is already queued, then exit",
    )
    mode.add_argument(
        "--schedule", action="store_true",
        help="queue the work that is due, then exit",
    )
    parser.add_argument("--limit", type=int, default=None, help="with --once")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    )

    config = settings()
    if not config.secret_key.get_secret_value():
        print(
            "BIRYAR_SECRET_KEY is not set, so no tenant's credentials can be "
            "read. Generate one with:\n"
            "    python -c 'from biryar.crypto import Keyring; "
            "print(\"v1:\" + Keyring.generate_key())'",
            file=sys.stderr,
        )
        return 2

    if args.schedule:
        engine = engine_for(config)
        result = schedule_settlement_fetches(session_factory(engine))
        print(
            f"{result.enqueued} queued, {result.already_queued} already outstanding"
        )
        return 0

    runner = build_runner(config)

    if args.once:
        print(f"{runner.drain(limit=args.limit)} job(s) run")
        return 0

    stopping = Stopping()
    stopping.install()
    serve(runner, stopping=stopping)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
