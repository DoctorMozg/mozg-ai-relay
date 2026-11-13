"""The clock seam: wall time and sleeping, faked in tests, plus UTC timestamps for envelopes."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep(self, s: float) -> None: ...


class SystemClock:
    """Wall time, so readings can be stored and compared across processes."""

    def now(self) -> float:
        return time.time()

    def sleep(self, s: float) -> None:
        if s < 0:
            raise ValueError(f"cannot sleep a negative time: sleep({s})")
        time.sleep(s)


def iso_utc(epoch: float) -> str:
    """Formats epoch seconds as `YYYY-MM-DDTHH:MM:SS.mmmZ`, truncated to the millisecond."""
    if not math.isfinite(epoch):
        raise ValueError(f"epoch must be a finite number, got {epoch!r}")
    stamp = datetime.fromtimestamp(epoch, tz=UTC)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"
