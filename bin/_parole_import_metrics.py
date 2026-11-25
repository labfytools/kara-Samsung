"""Real acquisition measurements in the existing Parole maintenance index."""
from math import exp, isfinite
import sqlite3
from time import time

LOOKUP = "lookup"
STARTUP = "startup"
THROUGHPUT = "throughput"
TOTAL = "total"
SUCCEEDED = "succeeded"
MEASURES = {LOOKUP, STARTUP, THROUGHPUT, TOTAL, SUCCEEDED}
WEEK = 7 * 86_400


def observe(index, source, values, *, now=None):
    if not source or values.keys() - MEASURES:
        raise ValueError("Mesures de fournisseur invalides.")
    epoch = time() if now is None else now
    samples = {name: float(value) for name, value in values.items()
               if type(value) in (float, int) and isfinite(value) and value >= 0
               and (name != SUCCEEDED or value in (0, 1))}
    if not samples:
        return
    try:
        with index.db(wait=.25) as db:
            # A sibling worker may complete simultaneously. Read and update
            # the exponential mean under the same write transaction.
            db.execute("BEGIN IMMEDIATE")
            for name, value in samples.items():
                saved = db.execute("""SELECT mean,samples,total,updated FROM import_provider_metrics
                                      WHERE source=? AND metric=?""", (source, name)).fetchone()
                if saved is None:
                    row = value, 1, value, epoch
                else:
                    previous, count, total, stamp = saved
                    retention = .75 * exp(-max(0., epoch - stamp) / (2 * WEEK))
                    row = retention * previous + (1 - retention) * value, count + 1, total + value, max(stamp, epoch)
                db.execute("INSERT OR REPLACE INTO import_provider_metrics VALUES(?,?,?,?,?,?)", (source, name, *row))
    except sqlite3.Error:
        # Telemetry must never abort an audio transfer or queue transition.
        return


def limited(index, source, seconds, status, *, now=None):
    if index is None or not source or status != 429 and not 500 <= status < 600:
        return
    deadline = (time() if now is None else now) + min(86_400, max(0, seconds))
    try:
        with index.db(wait=.25) as db:
            db.execute("""INSERT INTO import_provider_limits VALUES(?,?,?)
                          ON CONFLICT(source) DO UPDATE SET until=excluded.until,status=excluded.status
                          WHERE excluded.until>=import_provider_limits.until""",
                       (source, deadline, status))
    except sqlite3.Error:
        return


def snapshot(index, *, now=None):
    epoch = time() if now is None else now
    result = {}
    with index.db(wait=.25) as db:
        for row in db.execute("SELECT * FROM import_provider_metrics ORDER BY source,metric"):
            result.setdefault(row["source"], {})[row["metric"]] = {
                "mean": row["mean"], "samples": row["samples"], "total": row["total"], "updated": row["updated"],
            }
        for row in db.execute("SELECT * FROM import_provider_limits WHERE until>?", (epoch,)):
            result.setdefault(row["source"], {})["limited_until"] = row["until"]
            result[row["source"]]["last_http"] = row["status"]
    return result


class Transfer:
    """Measure startup and throughput from observed bytes, keeping their clocks distinct."""

    def __init__(self, started):
        self.started = started
        self.first = self.last = None
        self.initial_bytes = self.bytes = 0

    def received(self, size, instant):
        if size <= self.bytes or instant < (self.last if self.last is not None else self.started):
            return
        if self.first is None:
            self.first, self.initial_bytes = instant, size
        self.last, self.bytes = instant, size

    def measurements(self):
        if self.first is None:
            return {}
        result = {STARTUP: self.first - self.started}
        later = self.bytes - self.initial_bytes
        seconds = self.last - self.first
        if later >= 64 * 1024 and seconds > 0:
            rate = later / seconds
            if rate <= 2 * 1024**3:
                result[THROUGHPUT] = rate
        return result
