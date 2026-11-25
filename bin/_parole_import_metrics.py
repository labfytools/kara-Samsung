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
    deadline = (time() if now is None else now) + max(0, seconds)
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

    def __init__(self, started, notify=None):
        self.started = started
        self.first = self.last = None
        self.initial_bytes = self.bytes = 0
        self.notify = notify
        self.reported_at = started
        self.reported_bytes = 0

    def received(self, size, instant):
        if size <= self.bytes or instant < (self.last if self.last is not None else self.started):
            return
        if self.first is None:
            self.first, self.initial_bytes = instant, size
            if self.notify:
                self.notify("audio-first-byte")
        self.last, self.bytes = instant, size
        if self.notify and instant - self.reported_at >= .35:
            self.report_bytes()

    def report_bytes(self):
        if self.bytes > self.reported_bytes:
            self.notify("audio-bytes", size=self.bytes - self.reported_bytes)
            self.reported_bytes = self.bytes
            self.reported_at = self.last

    def end(self):
        # End the body interval before checksum, conversion or publication.
        # Emit once on EOF, failure or suspension, including a short body
        # whose bytes never reached the reporting interval.
        if self.notify:
            if self.first is not None:
                self.report_bytes()
                self.notify("audio-transfer-ended")
            self.notify = None

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


class DownloadConcurrency:
    """Probe higher admission only with sustained, measured demand."""

    def __init__(self, window=15.):
        self.window = window
        self.sample = None
        self.trial = None
        self.retry_at = 0.
        self.reason = ""
        self.failures = 0

    def update(self, now, ceiling, limit, resources, active, waiting, counters, *, resource_reason="Pression mémoire"):
        # Cohorts compare verified audio completions, actual transferred
        # bytes and published tracks. Low demand cannot qualify a higher cap.
        failures = counters[3] - self.failures
        self.failures = counters[3]
        if resources < limit or failures:
            self.sample = self.trial = None
            self.retry_at = now + 30.
            self.reason = resource_reason if resources < limit else "Erreurs de transfert récentes"
            return min(resources, ceiling, limit, max(3, limit // 2) if failures else limit)
        if now < self.retry_at:
            return limit
        self.reason = ""
        if waiting < 2 or active < max(1, limit - 1):
            self.sample = None
            if self.trial:
                stable, _ = self.trial
                self.trial = None
                return min(stable, ceiling)
            return limit
        if self.sample is None or self.sample[1] != limit:
            self.sample = now, limit, counters
            return limit
        started, _, initial = self.sample
        elapsed = now - started
        transferred, downloaded, integrated = (current - before for current, before in zip(counters[:3], initial[:3]))
        if elapsed < self.window or downloaded < 3 or integrated < 1 or transferred <= 0:
            return limit
        rates = transferred / elapsed, downloaded / elapsed, integrated / elapsed
        self.sample = None
        if self.trial:
            stable, previous = self.trial
            self.trial = None
            if (rates[0] < previous[0] * 1.05 or rates[1] < previous[1] * .95
                    or rates[2] < previous[2] * .95):
                self.reason = "Cadence insuffisante au plafond supérieur"
                self.retry_at = now + 120.
                return min(stable, ceiling)
        effective = min(ceiling, resources, limit + 5)
        if effective > limit:
            self.trial = limit, rates
        return effective
