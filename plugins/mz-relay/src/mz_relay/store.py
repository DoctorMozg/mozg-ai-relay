"""The SQLite store: messages and their status machine, registrations and contexts."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from mz_relay.clock import iso_utc
from mz_relay.envelope import TERMINAL, Envelope, Kind, Status
from mz_relay.errors import NotFound, StoreError
from mz_relay.roster import Harness, Label

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000

SCHEMA = """
CREATE TABLE messages(source TEXT NOT NULL, id TEXT NOT NULL, recipient TEXT NOT NULL,
  kind TEXT NOT NULL, context_id TEXT NOT NULL, in_reply_to TEXT, hop INTEGER NOT NULL,
  created_at REAL NOT NULL, ttl_s INTEGER NOT NULL, label TEXT NOT NULL,
  ack_required INTEGER NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, via TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL, handed_off_at REAL, lease_until REAL,
  updated_at REAL NOT NULL, notified INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(source, id));
CREATE INDEX messages_id ON messages(id);
CREATE INDEX messages_recipient_status ON messages(recipient, status);
CREATE INDEX messages_due ON messages(status, next_attempt_at);
CREATE TABLE contexts(context_id TEXT PRIMARY KEY, turns INTEGER NOT NULL, turn_cap INTEGER,
  closed_at REAL, closed_by TEXT);
CREATE TABLE registrations(token TEXT PRIMARY KEY, name TEXT NOT NULL, harness TEXT NOT NULL,
  client TEXT NOT NULL, delivery_mode TEXT, binding TEXT, pid INTEGER, created_at REAL NOT NULL,
  last_seen REAL NOT NULL, online INTEGER NOT NULL);
"""

MESSAGE_COLUMNS = (
    "source, id, recipient, kind, context_id, in_reply_to, hop, created_at, ttl_s, label, "
    "ack_required, body, status, reason, via, attempts, next_attempt_at, handed_off_at, "
    "lease_until, updated_at"
)
REGISTRATION_COLUMNS = (
    "token, name, harness, client, delivery_mode, binding, pid, created_at, last_seen, online"
)
OPEN_STATUSES = (
    Status.QUEUED,
    Status.DELIVERING,
    Status.HANDED_OFF,
    Status.FALLBACK,
    Status.LEASED,
)
PULLABLE = frozenset({Status.QUEUED, Status.FALLBACK})
AWAITING_ACK = frozenset({Status.DELIVERED, Status.HANDED_OFF, Status.LEASED})
FALLBACK_MODE = "fallback"
UNREACHABLE_PREFIX = "unreachable:"
# Terminal statuses a sender hears about once; unreachable parked rows are notices too.
NOTICE_STATUSES = frozenset({Status.UNDELIVERABLE, Status.EXPIRED, Status.DENIED})


@dataclass(frozen=True)
class StoredMessage:
    envelope: Envelope
    status: Status
    reason: str | None
    via: str | None
    attempts: int
    next_attempt_at: float | None
    handed_off_at: float | None
    lease_until: float | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class StatusChange:
    """A status write; None keeps via, attempts and handed_off_at, and clears the rest."""

    status: Status
    reason: str | None = None
    via: str | None = None
    attempts: int | None = None
    next_attempt_at: float | None = None
    handed_off_at: float | None = None
    lease_until: float | None = None


@dataclass(frozen=True)
class ContextState:
    turns: int
    turn_cap: int | None
    closed_by: str | None


@dataclass(frozen=True)
class Registration:
    token: str
    name: str
    harness: Harness
    client: str
    delivery_mode: str | None
    binding: dict[str, object] | None
    pid: int
    created_at: float
    last_seen: float
    online: bool


class DuplicateMessage(Exception):
    """A message with this source and id is already stored."""


def _placeholders(count: int) -> str:
    return ", ".join("?" * count)


def _status_set(statuses: Status | frozenset[Status], what: str) -> tuple[str, ...]:
    if isinstance(statuses, Status):
        return (statuses.value,)
    if not isinstance(statuses, frozenset) or not statuses:
        raise ValueError(
            f"{what} must be a Status or a non-empty frozenset of them, got {statuses!r}"
        )
    if not all(isinstance(status, Status) for status in statuses):
        raise ValueError(f"{what} holds a value that is not a Status: {statuses!r}")
    return tuple(sorted(status.value for status in statuses))


def _check_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive int, got {limit!r}")


def _check_name(value: str, what: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty string, got {value!r}")


def _check_recipients(recipients: Collection[str]) -> tuple[str, ...]:
    # A bare string is a Collection too, and would be read as one recipient per character.
    if isinstance(recipients, str):
        raise ValueError(f"recipients must be a collection of names, got the string {recipients!r}")
    names = tuple(recipients)
    for name in names:
        _check_name(name, "recipient")
    return names


def _check_change(change: StatusChange, expected: Status | frozenset[Status]) -> tuple[str, ...]:
    if not isinstance(change, StatusChange) or not isinstance(change.status, Status):
        raise ValueError(f"change must be a StatusChange with a Status, got {change!r}")
    return _status_set(expected, "expected")


def _message(row: sqlite3.Row) -> StoredMessage:
    created_at = float(row["created_at"])
    # The schema keeps no time column; the envelope was stamped at insert time.
    env = Envelope(
        id=row["id"],
        source=row["source"],
        to=row["recipient"],
        kind=Kind(row["kind"]),
        context_id=row["context_id"],
        in_reply_to=row["in_reply_to"],
        hop=int(row["hop"]),
        time=iso_utc(created_at),
        ttl_s=int(row["ttl_s"]),
        label=Label(row["label"]),
        ack_required=bool(row["ack_required"]),
        body=row["body"],
    )
    return StoredMessage(
        envelope=env,
        status=Status(row["status"]),
        reason=row["reason"],
        via=row["via"],
        attempts=int(row["attempts"]),
        next_attempt_at=row["next_attempt_at"],
        handed_off_at=row["handed_off_at"],
        lease_until=row["lease_until"],
        created_at=created_at,
        updated_at=float(row["updated_at"]),
    )


def _registration(row: sqlite3.Row) -> Registration:
    binding: dict[str, object] | None = None
    if row["binding"] is not None:
        loaded = json.loads(row["binding"])
        if not isinstance(loaded, dict):
            raise StoreError(f"registration binding is not a JSON object: {row['binding']!r}")
        binding = loaded
    return Registration(
        token=row["token"],
        name=row["name"],
        harness=Harness(row["harness"]),
        client=row["client"],
        delivery_mode=row["delivery_mode"],
        binding=binding,
        pid=int(row["pid"]),
        created_at=float(row["created_at"]),
        last_seen=float(row["last_seen"]),
        online=bool(row["online"]),
    )


class Store:
    """One SQLite connection for all threads; an RLock orders threads, BEGIN IMMEDIATE processes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(
                path, isolation_level=None, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000
            )
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open database {path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._migrate()
        except StoreError:
            self._conn.close()
            raise
        except sqlite3.DatabaseError as exc:
            self._conn.close()
            raise StoreError(f"cannot open database {path}: {exc}") from exc

    def _migrate(self) -> None:
        with self._transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                log.info("created store schema %d in %s", SCHEMA_VERSION, self.path)
                return
            try:
                version = int(row["value"])
            except ValueError as exc:
                raise StoreError(
                    f"database schema version is not a number: {row['value']!r}"
                ) from exc
            if version > SCHEMA_VERSION:
                raise StoreError(
                    f"database schema {version} is newer than supported {SCHEMA_VERSION}"
                )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                # A failed COMMIT may already have rolled back; a second ROLLBACK would mask it.
                if self._conn.in_transaction:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error as exc:
                        # The original failure is the one worth reporting; re-raised below.
                        log.warning("rollback failed in %s: %s", self.path, exc)
                raise

    def _select(
        self, where: str, params: tuple[object, ...], tail: str = ""
    ) -> list[StoredMessage]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE {where} {tail}", params
            ).fetchall()
        return [_message(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def insert_message(
        self,
        env: Envelope,
        status: Status,
        now: float,
        *,
        next_attempt_at: float | None,
        close_by: str | None = None,
    ) -> None:
        """Inserts the row and counts a turn; close_by also closes the context, as a done does."""
        if not isinstance(env, Envelope):
            raise ValueError(f"env must be an Envelope, got {type(env).__name__}")
        if not isinstance(status, Status):
            raise ValueError(f"status must be a Status, got {status!r}")
        with self._transaction() as conn:
            inserted = conn.execute(
                "INSERT INTO messages(source, id, recipient, kind, context_id, in_reply_to, "
                "hop, created_at, ttl_s, label, ack_required, body, status, next_attempt_at, "
                "updated_at) "
                f"VALUES({_placeholders(15)}) ON CONFLICT(source, id) DO NOTHING",
                (
                    env.source,
                    env.id,
                    env.to,
                    env.kind.value,
                    env.context_id,
                    env.in_reply_to,
                    env.hop,
                    now,
                    env.ttl_s,
                    env.label.value,
                    int(env.ack_required),
                    env.body,
                    status.value,
                    next_attempt_at,
                    now,
                ),
            )
            if inserted.rowcount == 0:
                log.warning("duplicate message %s from %s", env.id, env.source)
                raise DuplicateMessage(f"message {env.id} from {env.source} is already stored")
            conn.execute(
                "INSERT INTO contexts(context_id, turns) VALUES(?, 1) "
                "ON CONFLICT(context_id) DO UPDATE SET turns = turns + 1",
                (env.context_id,),
            )
            if close_by is not None:
                self._close_context(env.context_id, close_by, now)

    def get(self, source: str, msg_id: str) -> StoredMessage | None:
        found = self._select("source = ? AND id = ?", (source, msg_id))
        return found[0] if found else None

    def find_by_id(self, msg_id: str, *, recipient: str | None = None) -> StoredMessage | None:
        _check_name(msg_id, "msg_id")
        where: str = "id = ?"
        params: tuple[object, ...] = (msg_id,)
        if recipient is not None:
            where, params = "id = ? AND recipient = ?", (msg_id, recipient)
        found = self._select(where, params, "ORDER BY created_at DESC LIMIT 1")
        return found[0] if found else None

    def update(
        self,
        source: str,
        msg_id: str,
        change: StatusChange,
        now: float,
        *,
        expected: Status | frozenset[Status],
    ) -> bool:
        """Writes change only while the row's status is in expected; False when it no longer is."""
        allowed = _check_change(change, expected)
        with self._transaction():
            return self._apply(source, msg_id, change, now, allowed)

    def update_closing_context(
        self,
        source: str,
        msg_id: str,
        change: StatusChange,
        now: float,
        *,
        expected: Status | frozenset[Status],
    ) -> bool:
        """update, and when it writes, closes the row's context naming its source, as one commit."""
        allowed = _check_change(change, expected)
        with self._transaction() as conn:
            if not self._apply(source, msg_id, change, now, allowed):
                return False
            row = conn.execute(
                "SELECT context_id FROM messages WHERE source = ? AND id = ?", (source, msg_id)
            ).fetchone()
            self._close_context(str(row["context_id"]), source, now)
        return True

    def _apply(
        self, source: str, msg_id: str, change: StatusChange, now: float, allowed: tuple[str, ...]
    ) -> bool:
        sets = [
            "status = ?",
            "reason = ?",
            "next_attempt_at = ?",
            "lease_until = ?",
            "updated_at = ?",
        ]
        # A row reaching a terminal status is news to its sender, even after an earlier notice.
        if change.status in TERMINAL:
            sets.append("notified = 0")
        params: list[object] = [
            change.status.value,
            change.reason,
            change.next_attempt_at,
            change.lease_until,
            now,
        ]
        for column in ("via", "attempts", "handed_off_at"):
            value = getattr(change, column)
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        cursor = self._conn.execute(
            f"UPDATE messages SET {', '.join(sets)} "
            f"WHERE source = ? AND id = ? AND status IN ({_placeholders(len(allowed))})",
            (*params, source, msg_id, *allowed),
        )
        return cursor.rowcount == 1

    def due(self, now: float, recipients: Collection[str], limit: int = 50) -> list[StoredMessage]:
        return self._scheduled(Status.QUEUED, now, recipients, limit)

    def confirm_due(self, now: float, recipients: Collection[str]) -> list[StoredMessage]:
        # HANDED_OFF rows reuse next_attempt_at as their next confirm time.
        return self._scheduled(Status.HANDED_OFF, now, recipients, None)

    def _scheduled(
        self, status: Status, now: float, recipients: Collection[str], limit: int | None
    ) -> list[StoredMessage]:
        names = _check_recipients(recipients)
        if limit is not None:
            _check_limit(limit)
        if not names:
            return []
        where = (
            f"status = ? AND next_attempt_at <= ? AND recipient IN ({_placeholders(len(names))})"
        )
        params: tuple[object, ...] = (status.value, now, *names)
        # rowid breaks created_at ties in insert order; ULIDs minted in one ms sort randomly.
        tail = "ORDER BY created_at, rowid"
        if limit is not None:
            tail += " LIMIT ?"
            params = (*params, limit)
        return self._select(where, params, tail)

    def with_status(
        self, statuses: frozenset[Status], *, recipient: str | None = None
    ) -> list[StoredMessage]:
        allowed = _status_set(statuses, "statuses")
        where = f"status IN ({_placeholders(len(allowed))})"
        params: tuple[object, ...] = allowed
        if recipient is not None:
            where += " AND recipient = ?"
            params = (*allowed, recipient)
        return self._select(where, params, "ORDER BY created_at, rowid")

    def earliest_due(
        self, statuses: frozenset[Status], recipients: Collection[str]
    ) -> float | None:
        """The smallest next_attempt_at among the recipients' rows in the statuses."""
        allowed = _status_set(statuses, "statuses")
        names = _check_recipients(recipients)
        if not names:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT min(next_attempt_at) FROM messages "
                f"WHERE status IN ({_placeholders(len(allowed))}) "
                f"AND recipient IN ({_placeholders(len(names))})",
                (*allowed, *names),
            ).fetchone()
        return None if row[0] is None else float(row[0])

    def open_count(self, recipient: str) -> int:
        """Messages the recipient has not received yet, plus delivered requests still unacked."""
        _check_name(recipient, "recipient")
        open_values = tuple(status.value for status in OPEN_STATUSES)
        with self._lock:
            row = self._conn.execute(
                "SELECT count(*) FROM messages WHERE recipient = ? AND "
                f"(status IN ({_placeholders(len(open_values))}) "
                "OR (status = ? AND ack_required = 1))",
                (recipient, *open_values, Status.DELIVERED.value),
            ).fetchone()
        return int(row[0])

    def past_ttl(self, statuses: frozenset[Status], now: float) -> list[StoredMessage]:
        """Rows in the statuses whose own ttl ended before now."""
        allowed = _status_set(statuses, "statuses")
        return self._select(
            f"status IN ({_placeholders(len(allowed))}) AND created_at + ttl_s < ?",
            (*allowed, now),
            "ORDER BY created_at, rowid",
        )

    def wake_recipient(self, recipient: str, now: float) -> int:
        """Makes the recipient's parked QUEUED rows due now; returns how many."""
        _check_name(recipient, "recipient")
        with self._transaction() as conn:
            woken = conn.execute(
                "UPDATE messages SET next_attempt_at = ?, updated_at = ? "
                "WHERE recipient = ? AND status = ? AND next_attempt_at IS NULL",
                (now, now, recipient, Status.QUEUED.value),
            ).rowcount
        if woken:
            log.info("woke %d parked messages for %s", woken, recipient)
        return woken

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, count(*) FROM messages GROUP BY status"
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def pull_for(self, recipient: str, limit: int, now: float) -> list[StoredMessage]:
        """Delivers the oldest QUEUED and FALLBACK rows via inbox, in one transaction."""
        _check_name(recipient, "recipient")
        _check_limit(limit)
        pullable = _status_set(PULLABLE, "pullable")
        change = StatusChange(Status.DELIVERED, via="inbox")
        with self._transaction() as conn:
            keys = conn.execute(
                "SELECT source, id FROM messages "
                f"WHERE recipient = ? AND status IN ({_placeholders(len(pullable))}) "
                "ORDER BY created_at, rowid LIMIT ?",
                (recipient, *pullable, limit),
            ).fetchall()
            pulled = []
            for source, msg_id in keys:
                if self._apply(source, msg_id, change, now, pullable):
                    pulled.extend(self._select("source = ? AND id = ?", (source, msg_id)))
        if pulled:
            log.info("inbox pulled %d messages for %s", len(pulled), recipient)
        return pulled

    def lease_fallback(self, recipient: str, now: float, lease_s: float) -> StoredMessage | None:
        """Leases the oldest FALLBACK row to the rewake waiter, in one transaction."""
        _check_name(recipient, "recipient")
        if not lease_s > 0:
            raise ValueError(f"lease_s must be positive, got {lease_s!r}")
        fallback = (Status.FALLBACK.value,)
        with self._transaction() as conn:
            key = conn.execute(
                "SELECT source, id FROM messages WHERE recipient = ? AND status = ? "
                "ORDER BY created_at, rowid LIMIT 1",
                (recipient, Status.FALLBACK.value),
            ).fetchone()
            if key is None:
                return None
            source, msg_id = key
            change = StatusChange(Status.LEASED, lease_until=now + lease_s)
            if not self._apply(source, msg_id, change, now, fallback):
                return None
            [leased] = self._select("source = ? AND id = ?", (source, msg_id))
        log.info("leased %s to the waiter of %s until %.3f", msg_id, recipient, now + lease_s)
        return leased

    def requeue_inflight(self, now: float) -> int:
        """Makes rows claimed by a previous daemon due again; run once at daemon start."""
        delivering = (Status.DELIVERING.value,)
        change = StatusChange(Status.QUEUED, next_attempt_at=now)
        with self._transaction() as conn:
            keys = conn.execute(
                "SELECT source, id FROM messages WHERE status = ?", delivering
            ).fetchall()
            requeued = sum(
                self._apply(source, msg_id, change, now, delivering) for source, msg_id in keys
            )
        if requeued:
            log.info("requeued %d messages left delivering by a previous daemon", requeued)
        return requeued

    def awaiting_ack(self, recipient: str) -> list[StoredMessage]:
        _check_name(recipient, "recipient")
        awaiting = _status_set(AWAITING_ACK, "awaiting")
        return self._select(
            f"recipient = ? AND ack_required = 1 AND status IN ({_placeholders(len(awaiting))})",
            (recipient, *awaiting),
            "ORDER BY created_at, rowid",
        )

    def sent_by(self, source: str, limit: int = 20) -> list[StoredMessage]:
        """The sender's newest messages first."""
        _check_name(source, "source")
        _check_limit(limit)
        return self._select(
            "source = ?", (source, limit), "ORDER BY created_at DESC, rowid DESC LIMIT ?"
        )

    def log(self, context_id: str | None, limit: int) -> list[StoredMessage]:
        """The newest `limit` rows of one context, or of all, returned oldest first."""
        _check_limit(limit)
        params: tuple[object, ...]
        if context_id is None:
            where, params = "1", (limit,)
        else:
            _check_name(context_id, "context_id")
            where, params = "context_id = ?", (context_id, limit)
        newest = self._select(where, params, "ORDER BY created_at DESC, rowid DESC LIMIT ?")
        return newest[::-1]

    def _notice_where(self, source: str) -> tuple[str, tuple[object, ...]]:
        _check_name(source, "source")
        statuses = _status_set(NOTICE_STATUSES, "notice statuses")
        # GLOB, unlike LIKE, matches the prefix case-sensitively and without wildcards.
        where = (
            f"source = ? AND notified = 0 AND (status IN ({_placeholders(len(statuses))}) "
            "OR (status = ? AND reason GLOB ?))"
        )
        return where, (source, *statuses, Status.QUEUED.value, UNREACHABLE_PREFIX + "*")

    def notices_for(self, source: str) -> list[StoredMessage]:
        """The sender's rows it has not been told about yet, oldest first."""
        where, params = self._notice_where(source)
        return self._select(where, params, "ORDER BY created_at, rowid")

    def pending_notices(self, source: str) -> int:
        where, params = self._notice_where(source)
        with self._lock:
            row = self._conn.execute(
                f"SELECT count(*) FROM messages WHERE {where}", params
            ).fetchone()
        return int(row[0])

    def mark_notified(self, rows: Sequence[StoredMessage]) -> int:
        """Marks rows shown to their sender; a row that changed since it was read stays unmarked."""
        if not all(isinstance(row, StoredMessage) for row in rows):
            raise ValueError(f"rows must be StoredMessages, got {rows!r}")
        if not rows:
            return 0
        with self._transaction() as conn:
            marked = sum(
                conn.execute(
                    "UPDATE messages SET notified = 1 "
                    "WHERE source = ? AND id = ? AND status = ? AND reason IS ?",
                    (row.envelope.source, row.envelope.id, row.status.value, row.reason),
                ).rowcount
                for row in rows
            )
        if marked:
            log.debug("marked %d notices as shown", marked)
        return marked

    def upsert_registration(self, reg: Registration) -> None:
        if not isinstance(reg, Registration):
            raise ValueError(f"reg must be a Registration, got {type(reg).__name__}")
        _check_name(reg.token, "token")
        binding = None if reg.binding is None else json.dumps(reg.binding, sort_keys=True)
        with self._transaction() as conn:
            conn.execute(
                f"INSERT INTO registrations({REGISTRATION_COLUMNS}) VALUES({_placeholders(10)}) "
                "ON CONFLICT(token) DO UPDATE SET name = excluded.name, "
                "harness = excluded.harness, "
                "client = excluded.client, delivery_mode = excluded.delivery_mode, "
                "binding = excluded.binding, pid = excluded.pid, created_at = excluded.created_at, "
                "last_seen = excluded.last_seen, online = excluded.online",
                (
                    reg.token,
                    reg.name,
                    reg.harness.value,
                    reg.client,
                    reg.delivery_mode,
                    binding,
                    reg.pid,
                    reg.created_at,
                    reg.last_seen,
                    int(reg.online),
                ),
            )
        log.info(
            "registration of %s (%s, %s) mode %s online=%s",
            reg.name,
            reg.harness.value,
            reg.client,
            reg.delivery_mode,
            reg.online,
        )

    def registration(self, token: str) -> Registration | None:
        found = self._registrations("token = ?", (token,))
        return found[0] if found else None

    def latest_delivery_registration(self, name: str) -> Registration | None:
        """The online binding to deliver through; a waiter's fallback never beats a live one."""
        _check_name(name, "name")
        found = self._registrations(
            "name = ? AND online = 1 AND delivery_mode IS NOT NULL",
            (name, FALLBACK_MODE),
            "ORDER BY delivery_mode = ?, created_at DESC LIMIT 1",
        )
        return found[0] if found else None

    def _registrations(
        self, where: str, params: tuple[object, ...], tail: str = ""
    ) -> list[Registration]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {REGISTRATION_COLUMNS} FROM registrations WHERE {where} {tail}", params
            ).fetchall()
        return [_registration(row) for row in rows]

    def set_online(self, token: str, online: bool, now: float) -> None:
        _check_name(token, "token")
        with self._transaction() as conn:
            conn.execute(
                "UPDATE registrations SET online = ?, last_seen = ? WHERE token = ?",
                (int(online), now, token),
            )

    def mark_all_offline(self) -> None:
        with self._transaction() as conn:
            changed = conn.execute("UPDATE registrations SET online = 0 WHERE online = 1").rowcount
        log.info("marked %d registrations offline", changed)

    def online_names(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT name FROM registrations WHERE online = 1"
            ).fetchall()
        return {str(row[0]) for row in rows}

    def context_state(self, context_id: str) -> ContextState:
        """Turns, turn-cap override and closer; a context never written has no turns."""
        _check_name(context_id, "context_id")
        with self._lock:
            row = self._conn.execute(
                "SELECT turns, turn_cap, closed_at, closed_by FROM contexts WHERE context_id = ?",
                (context_id,),
            ).fetchone()
        if row is None:
            return ContextState(turns=0, turn_cap=None, closed_by=None)
        turn_cap = None if row["turn_cap"] is None else int(row["turn_cap"])
        closed_by = None if row["closed_at"] is None else str(row["closed_by"])
        return ContextState(turns=int(row["turns"]), turn_cap=turn_cap, closed_by=closed_by)

    def _close_context(self, context_id: str, by: str, now: float) -> None:
        # Runs inside the caller's transaction; the first closer stays the one named.
        _check_name(by, "closer")
        closed = self._conn.execute(
            "UPDATE contexts SET closed_at = ?, closed_by = ? "
            "WHERE context_id = ? AND closed_at IS NULL",
            (now, by, context_id),
        ).rowcount
        if closed:
            log.info("context %s closed by %s", context_id, by)

    def extend_context(self, context_id: str, by: int, default_cap: int) -> int:
        """Adds `by` to the context's cap, or to default_cap without an override; the new cap."""
        _check_name(context_id, "context_id")
        for value, what in ((by, "by"), (default_cap, "default_cap")):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{what} must be a positive int, got {value!r}")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT turn_cap FROM contexts WHERE context_id = ?", (context_id,)
            ).fetchone()
            if row is None:
                raise NotFound("not_found", f"no context {context_id}")
            base = default_cap if row["turn_cap"] is None else int(row["turn_cap"])
            conn.execute(
                "UPDATE contexts SET turn_cap = ? WHERE context_id = ?", (base + by, context_id)
            )
        log.info("context %s turn cap raised by %d to %d", context_id, by, base + by)
        return base + by
