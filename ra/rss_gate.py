"""Coordinate Google News traffic across the local search and recovery workers."""
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree


class Deferred(Exception):
    """Google returned 503, or a previous 503 paused the host."""


class FeedTimeout(Deferred, TimeoutError):
    """The feed exhausted its normal timeout retries."""


def retry_delay(value, now):
    if value:
        try:
            return max(0, int(value))
        except ValueError:
            try:
                return max(0, parsedate_to_datetime(value).timestamp() - now)
            except (ValueError, TypeError, OverflowError):
                pass
    return None


class GoogleRSS:
    def __init__(self):
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(2)
        self.until = self.delay = self.failures = self.pace = self.last = 0
        self.persist = False

    def restore(self):
        from . import db
        with self.lock, db.connect() as conn:
            row = conn.execute("SELECT * FROM rss_cooldown WHERE id").fetchone()
            if row:
                self.until = row['until_time'].timestamp()
                self.delay = row['delay_seconds']
            self.persist = True

    def _pause(self, delay):
        self.delay = min(max(self.delay * 2, 300), 3600)
        self.until = max(self.until, time.time() + max(delay or 0, self.delay))
        if self.persist:
            from . import db
            with db.connect() as conn:
                conn.execute("""INSERT INTO rss_cooldown(until_time,delay_seconds) VALUES (%s,%s)
                    ON CONFLICT(id) DO UPDATE SET until_time=excluded.until_time,delay_seconds=excluded.delay_seconds""",
                    (datetime.fromtimestamp(self.until, timezone.utc), self.delay))

    def read(self, fetch, url, probe):
        with self.slots:
            try:
                with self.lock:
                    if time.time() < self.until:
                        raise Deferred("Google News RSS is cooling down")
                    if self.until:
                        # Only one caller probes after a cooldown.
                        try:
                            raw = fetch(probe)
                            if ElementTree.fromstring(raw).tag != 'rss':
                                raise ValueError("Invalid RSS probe response")
                        except (OSError, ValueError, ElementTree.ParseError) as exc:
                            self._pause(retry_delay((getattr(exc, 'headers', None) or {}).get('Retry-After'), time.time()))
                            raise Deferred("Google News RSS recovery probe failed") from exc
                        self.until = self.delay = self.failures = 0
                        self.pace = 2.0
                        self.last = time.monotonic()
                        if self.persist:
                            from . import db
                            with db.connect() as conn:
                                conn.execute("DELETE FROM rss_cooldown WHERE id")
                        if url == probe:
                            return raw
                    if self.pace:
                        wait = self.last + self.pace - time.monotonic()
                        if wait > 0:
                            time.sleep(wait)
                    self.last = time.monotonic()
                raw = fetch(url)
            except HTTPError as exc:
                with self.lock:
                    if exc.code not in (429, 503):
                        self.failures = 0
                        raise
                    self.failures += 1
                    delay = retry_delay((exc.headers or {}).get('Retry-After'), time.time())
                    if exc.code == 429 or self.failures >= 3 or delay is not None:
                        self._pause(delay)
                raise Deferred(f"Google News RSS returned HTTP {exc.code}") from exc
            except OSError as exc:
                cause = exc.reason if isinstance(exc, URLError) else exc
                with self.lock:
                    if isinstance(cause, TimeoutError):
                        self.failures += 1
                        if self.failures >= 3:
                            self._pause(None)
                        raise FeedTimeout("Google News RSS timed out after retries") from exc
                    self.failures = 0
                raise
            with self.lock:
                if not self.until:
                    self.pace = max(0, self.pace - 0.25)
                    self.failures = 0
            return raw


google = GoogleRSS()
