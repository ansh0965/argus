"""A small, dependency-free job scheduler with persistent catch-up.

Three job shapes:
  every(seconds)          - interval jobs
  daily(at="HH:MM")       - once a day, catches up if the machine was asleep
  weekly(day, at="HH:MM") - once a week, same catch-up behaviour

Catch-up matters on a laptop: if the evening wrap is due at 23:30 and the lid
was shut, you still want it when the machine wakes, not silently skipped. Each
job records its last successful run in the state table, so a restart mid-day
does not re-fire everything.
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from . import db
from .util import at_today, log

lg = log("scheduler")


@dataclass
class Job:
    name: str
    fn: Callable[[], object]
    every_seconds: float | None = None
    at: str | None = None
    weekday: int | None = None          # 0=Mon .. 6=Sun, with `at`
    catch_up_hours: float = 6.0         # how late a missed daily job may still fire
    enabled: Callable[[], bool] = field(default=lambda: True)
    jitter_seconds: float = 0.0
    run_on_start: bool = False
    # A job that cannot possibly succeed offline. It is skipped *without* being
    # marked run, so it stays due and fires the instant the link returns.
    requires_net: bool = False
    # Returns True for a summary that means "nothing happened". Such runs are
    # not written to the run log: heartbeat and meeting_sentinel alone logged
    # ~2,400 no-op rows a day, and the dashboard's "What ARGUS did" list showed
    # half an hour of those instead of any decision. Failures always log.
    quiet: Callable[[str], bool] | None = None

    def state_key(self) -> str:
        return f"job_last:{self.name}"

    def last_run(self) -> float:
        return float(db.get_state(self.state_key(), 0) or 0)

    def mark(self, ts: float | None = None) -> None:
        db.set_state(self.state_key(), ts or time.time())

    # ------------------------------------------------------------------ due?

    def due(self, now: datetime) -> bool:
        last = self.last_run()

        if self.every_seconds:
            if last == 0 and self.run_on_start:
                return True
            return (now.timestamp() - last) >= self.every_seconds

        if self.at is None:
            return False

        if self.weekday is not None and now.weekday() != self.weekday:
            # Allow catching up a weekly job the morning after.
            target = at_today(self.at, now - timedelta(days=1))
            if (now - timedelta(days=1)).weekday() != self.weekday:
                return False
        else:
            target = at_today(self.at, now)

        if now < target:
            return False
        if (now - target).total_seconds() > self.catch_up_hours * 3600:
            return False
        return last < target.timestamp()


class Scheduler:
    def __init__(self) -> None:
        self.jobs: list[Job] = []

    def add(self, job: Job) -> Job:
        self.jobs.append(job)
        return job

    def every(self, seconds: float, name: str, fn: Callable, **kw) -> Job:
        return self.add(Job(name=name, fn=fn, every_seconds=seconds, **kw))

    def daily(self, at: str, name: str, fn: Callable, **kw) -> Job:
        return self.add(Job(name=name, fn=fn, at=at, **kw))

    def weekly(self, weekday: int, at: str, name: str, fn: Callable, **kw) -> Job:
        return self.add(Job(name=name, fn=fn, at=at, weekday=weekday, **kw))

    def run_job(self, job: Job, forced: bool = False) -> tuple[bool, str]:
        t0 = time.time()
        try:
            result = job.fn()
            ms = int((time.time() - t0) * 1000)
            summary = "" if result is None else str(result)[:2000]
            quiet = not forced and bool(job.quiet and job.quiet(summary))
            if not quiet:
                db.add_run(job.name, True, ms, summary)
            job.mark()
            # A no-op run ("nothing due", "8/8 sensors alive") stays out of the
            # log file as well as the run log: 1,700 such lines a day were most
            # of argus.log, burying the warnings `argus logs` is read for.
            (lg.debug if quiet else lg.info)(
                "job %s ok in %dms%s", job.name, ms, f" - {summary[:120]}" if summary else "")
            return True, summary
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            db.add_run(job.name, False, ms, "", traceback.format_exc()[-1500:])
            lg.exception("job %s failed", job.name)
            if not forced:
                job.mark()  # do not hot-loop a broken daily job
            return False, str(e)

    def by_name(self, name: str) -> Job | None:
        return next((j for j in self.jobs if j.name == name), None)

    def tick(self) -> int:
        from . import net
        now = datetime.now()
        online = net.is_online()
        ran = deferred = 0
        for job in self.jobs:
            try:
                if not job.enabled():
                    continue
                if not job.due(now):
                    continue
                if job.requires_net and not online:
                    deferred += 1
                    continue
                self.run_job(job)
                ran += 1
            except Exception:
                lg.exception("scheduler tick error on %s", job.name)
        if deferred:
            lg.debug("%d network job(s) deferred until the link returns", deferred)
        return ran

    def run_due_net_jobs(self) -> str:
        """Fire every network job that is currently due. Called on reconnect."""
        now = datetime.now()
        done = []
        for job in self.jobs:
            if not job.requires_net or not job.enabled():
                continue
            if job.due(now):
                ok, _ = self.run_job(job)
                done.append(job.name + ("" if ok else "!"))
        return ", ".join(done) if done else "nothing was due"

    def loop(self, stop, interval: float = 20.0,
             before_tick: Callable[[], object] | None = None) -> None:
        """`before_tick` runs between ticks - never during one - so it may
        safely rebuild self.jobs (the daemon uses it to apply config edits)."""
        lg.info("scheduler started with %d jobs", len(self.jobs))
        while not stop.is_set():
            if before_tick is not None:
                try:
                    before_tick()
                except Exception:
                    lg.exception("before_tick failed")
            self.tick()
            stop.wait(interval)
        lg.info("scheduler stopped")

    def next_due(self, job: Job, now: datetime | None = None) -> float:
        now = now or datetime.now()
        if job.every_seconds:
            last = job.last_run()
            return last + job.every_seconds if last else now.timestamp()
        t = at_today(job.at, now)
        if job.weekday is not None:
            t += timedelta(days=(job.weekday - now.weekday()) % 7)
            if t <= now:
                t += timedelta(days=7)
        elif t <= now:
            t += timedelta(days=1)
        return t.timestamp()

    def describe(self) -> list[dict]:
        now = datetime.now()
        out = []
        for j in list(self.jobs):
            if j.every_seconds:
                spec = f"every {int(j.every_seconds)}s"
            elif j.weekday is not None:
                spec = f"{['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][j.weekday]} {j.at}"
            else:
                spec = f"daily {j.at}"
            try:
                enabled = bool(j.enabled())
            except Exception:
                enabled = False
            out.append({"name": j.name, "spec": spec, "last_run": j.last_run(),
                        "next_due": self.next_due(j, now), "enabled": enabled,
                        "requires_net": j.requires_net})
        return sorted(out, key=lambda d: d["next_due"])
