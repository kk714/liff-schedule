from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.worker import compute_reply_deadline
from app.worker.postgres import advisory_key


def test_advisory_key_is_deterministic_signed_64bit():
    a = advisory_key("ffffffff-ffff-ffff-0000-000000000000")
    assert a == advisory_key("FFFFFFFF-FFFF-FFFF-0000-000000000000")
    assert a == -1
    assert -(2**63) <= advisory_key("123e4567-e89b-12d3-a456-426614174000") < 2**63
    assert advisory_key("00000000-0000-0000-0000-000000000001") != advisory_key(
        "00000000-0000-0001-0000-000000000001"
    )


def test_reply_deadline():
    t = datetime(2026, 10, 21, 2, tzinfo=timezone.utc)
    assert compute_reply_deadline(t, 30) == t + timedelta(seconds=30)
    with pytest.raises(ValueError):
        compute_reply_deadline(datetime(2026, 10, 21), 30)
