"""Persistent per-platform state and the version-transition state machine.

State (the last-seen GitHub and store versions, whether a warning is currently
outstanding, and when it was last announced) lives in a small SQLite database
so the monitor only raises a warning once, repeats it on a schedule while it
stays unresolved, only sends an all-clear after a real warning, and survives
restarts without re-alerting.

The decision logic in :func:`evaluate` is deliberately pure (no IO), so it can
be unit tested exhaustively.
"""

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Event kinds emitted by evaluate():
WARNING = "warning"                  # out-of-sync divergence detected (tag users)
WARNING_UPDATE = "warning_update"    # already warning, divergence grew (tag users)
WARNING_REMINDER = "warning_reminder"  # still unresolved after the reminder interval
ALL_CLEAR = "all_clear"              # back in sync; warning resolved
INFO_GITHUB = "info_github"          # GitHub advanced (informational)
INFO_STORE = "info_store"            # store advanced (informational)


@dataclass(frozen=True)
class PlatformState:
    github_version: str
    store_version: str
    warning_active: bool
    initialized: bool
    # Epoch seconds of the last warning/update/reminder announced, or None.
    last_warned_at: float = None
    # Highest store version ever observed (see the watermark in evaluate). None
    # for rows written before this column existed -> falls back to store_version.
    store_high: str = None


@dataclass
class Event:
    kind: str
    github_version: str
    store_version: str
    # True when raised while seeding a brand-new platform (already out of sync).
    initial: bool = False


@dataclass
class EvalResult:
    events: list = field(default_factory=list)
    warning_active: bool = False
    initialized: bool = True
    last_warned_at: float = None
    # The store watermark to persist (max store version ever seen).
    store_high: str = None


def evaluate(prev, github_version, store_version, compare,
             *, strict_sync=False, now=0.0, reminder_interval=None):
    """Decide which events to emit given the previous state and new versions.

    ``prev`` is a :class:`PlatformState` or ``None`` for a never-seen platform.
    ``compare(a, b)`` returns -1/0/1.

    ``strict_sync`` — when True (e.g. the APT repo we control), *any* mismatch
    between store and GitHub is a warning, in either direction. When False (app
    stores), only the store being *ahead* of GitHub warns; GitHub being ahead is
    normal and merely informational.

    ``now`` (epoch seconds) and ``reminder_interval`` (seconds) drive repeating
    an unresolved warning: while it stays active, the warning is re-announced
    once every ``reminder_interval``. Returns an :class:`EvalResult`.
    """
    cmp = compare(store_version, github_version)
    store_ahead = cmp > 0
    store_behind = cmp < 0
    warn_condition = (cmp != 0) if strict_sync else store_ahead

    # Highest store version ever observed. App-store version APIs are eventually
    # consistent -- Apple's iTunes lookup in particular briefly re-serves the
    # *previous* version -- so the observed version can dip and then recover. We
    # always persist the true fetched version (so a genuine pull stays visible in
    # state), but only announce an INFO_STORE "updated" when the store reaches a
    # genuinely new high; a dip-and-recover to an already-announced version does
    # not re-fire the notification.
    prev_high = None if prev is None else (prev.store_high or prev.store_version)
    if prev_high is None:
        store_high = store_version
    else:
        store_high = store_version if compare(store_version, prev_high) > 0 else prev_high

    # First observation: seed the baseline silently, but warn immediately if it
    # is already out of sync.
    if prev is None or not prev.initialized:
        events = []
        last_warned = None
        if warn_condition:
            events.append(Event(WARNING, github_version, store_version, initial=True))
            last_warned = now
        return EvalResult(events=events, warning_active=warn_condition,
                          initialized=True, last_warned_at=last_warned,
                          store_high=store_high)

    github_advanced = compare(github_version, prev.github_version) > 0
    store_advanced = compare(store_version, prev.store_version) > 0
    # A genuinely new high -- not merely higher than last cycle, which a recovery
    # from a transient dip also is -- is what gates the "updated" notification.
    store_new_high = compare(store_version, prev_high) > 0

    events = []
    warning_active = prev.warning_active
    last_warned = prev.last_warned_at

    if warn_condition and not warning_active:
        events.append(Event(WARNING, github_version, store_version))
        warning_active = True
        last_warned = now
    elif warn_condition and warning_active:
        if last_warned is None:  # warning latched before reminders existed
            last_warned = now
        # Re-tag if the divergence grew (the leading side pulled further away);
        # otherwise repeat the warning once per reminder interval.
        worsened = (store_ahead and store_advanced) or (store_behind and github_advanced)
        if worsened:
            events.append(Event(WARNING_UPDATE, github_version, store_version))
            last_warned = now
        elif (reminder_interval is not None
              and now - last_warned >= reminder_interval):
            events.append(Event(WARNING_REMINDER, github_version, store_version))
            last_warned = now
    elif not warn_condition and warning_active:
        events.append(Event(ALL_CLEAR, github_version, store_version))
        warning_active = False
        last_warned = None

    # Quiet movement (no warning transition this cycle) -> informational.
    if not events:
        if github_advanced:
            events.append(Event(INFO_GITHUB, github_version, store_version))
        if store_new_high:
            events.append(Event(INFO_STORE, github_version, store_version))

    return EvalResult(events=events, warning_active=warning_active,
                      initialized=True, last_warned_at=last_warned,
                      store_high=store_high)


class StateStore:
    """SQLite-backed store of the last-seen versions per platform."""

    def __init__(self, path):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_state (
                platform       TEXT PRIMARY KEY,
                github_version TEXT NOT NULL,
                store_version  TEXT NOT NULL,
                warning_active INTEGER NOT NULL DEFAULT 0,
                last_warned_at REAL,
                store_high     TEXT,
                updated_at     TEXT NOT NULL
            )
            """
        )
        # Migrate databases created before these columns existed.
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(platform_state)")]
        if "last_warned_at" not in cols:
            self._conn.execute("ALTER TABLE platform_state ADD COLUMN last_warned_at REAL")
        if "store_high" not in cols:
            self._conn.execute("ALTER TABLE platform_state ADD COLUMN store_high TEXT")
        self._conn.commit()

    def get(self, platform):
        row = self._conn.execute(
            "SELECT github_version, store_version, warning_active, last_warned_at, "
            "store_high FROM platform_state WHERE platform = ?",
            (platform,),
        ).fetchone()
        if row is None:
            return None
        return PlatformState(
            github_version=row["github_version"],
            store_version=row["store_version"],
            warning_active=bool(row["warning_active"]),
            initialized=True,
            last_warned_at=row["last_warned_at"],
            store_high=row["store_high"],
        )

    def upsert(self, platform, github_version, store_version, warning_active,
               last_warned_at=None, store_high=None):
        self._conn.execute(
            """
            INSERT INTO platform_state
                (platform, github_version, store_version, warning_active,
                 last_warned_at, store_high, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform) DO UPDATE SET
                github_version = excluded.github_version,
                store_version  = excluded.store_version,
                warning_active = excluded.warning_active,
                last_warned_at = excluded.last_warned_at,
                store_high     = excluded.store_high,
                updated_at     = excluded.updated_at
            """,
            (
                platform,
                github_version,
                store_version,
                1 if warning_active else 0,
                last_warned_at,
                store_high,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def close(self):
        self._conn.close()
