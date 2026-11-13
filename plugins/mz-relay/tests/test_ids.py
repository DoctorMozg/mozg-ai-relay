"""Message ids, the id check shared with TS clients, the clock and UTC timestamps."""

from __future__ import annotations

import re
import time
import uuid
from datetime import UTC, datetime

import pytest
from mz_relay.clock import SystemClock, iso_utc
from mz_relay.ids import new_id, valid_id

CROCKFORD = frozenset("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


class TestNewId:
    def test_is_a_26_char_crockford_ulid(self) -> None:
        value = new_id()
        assert len(value) == 26
        assert set(value) <= CROCKFORD

    def test_two_ids_in_the_same_millisecond_differ(self) -> None:
        assert new_id(now_ms=5) != new_id(now_ms=5)

    def test_ids_sort_by_time(self) -> None:
        assert new_id(now_ms=1) < new_id(now_ms=3)

    @pytest.mark.parametrize(
        ("now_ms", "prefix"),
        [(0, "0000000000"), (31, "000000000Z"), (32, "0000000010")],
    )
    def test_first_ten_chars_encode_the_millisecond(self, now_ms: int, prefix: str) -> None:
        assert new_id(now_ms=now_ms)[:10] == prefix

    def test_default_time_is_now(self) -> None:
        now_ms = int(time.time() * 1000)
        value = new_id()
        assert new_id(now_ms=now_ms - 60_000) < value < new_id(now_ms=now_ms + 60_000)


class TestValidId:
    @pytest.mark.parametrize(
        "value",
        [new_id(), str(uuid.uuid4()), "abcdefgh", "A" + "x" * 63],
        ids=["ulid", "uuid", "8-chars", "64-chars"],
    )
    def test_accepts_ulids_uuids_and_8_to_64_chars(self, value: str) -> None:
        assert valid_id(value) is True

    @pytest.mark.parametrize(
        "value",
        ["", "a b", "-abc12345", "x" * 65, "abcdefg", "abc_defgh", 7, None],
        ids=["empty", "space", "leading-dash", "65-chars", "7-chars", "underscore", "int", "none"],
    )
    def test_rejects_anything_else(self, value: object) -> None:
        assert valid_id(value) is False


class TestClock:
    def test_iso_utc_has_milliseconds_and_z(self) -> None:
        epoch = datetime(2026, 11, 13, 4, 5, 0, 123_000, tzinfo=UTC).timestamp()
        assert iso_utc(epoch) == "2026-11-13T04:05:00.123Z"

    def test_iso_utc_of_the_epoch(self) -> None:
        stamp = iso_utc(0.0)
        assert stamp == "1970-01-01T00:00:00.000Z"
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", stamp)

    def test_system_clock_follows_wall_time(self) -> None:
        clock = SystemClock()
        before = time.time()
        reading = clock.now()
        assert before - 1.0 <= reading <= time.time() + 1.0

    def test_system_clock_sleep_waits(self) -> None:
        clock = SystemClock()
        start = time.monotonic()
        clock.sleep(0.02)
        assert time.monotonic() - start >= 0.02
