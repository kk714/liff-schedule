from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.worker import compute_reply_deadline


def test_reply_deadline():
    t = datetime(2026, 10, 21, 2, tzinfo=timezone.utc)
    assert compute_reply_deadline(t, 30) == t + timedelta(seconds=30)
    with pytest.raises(ValueError):
        compute_reply_deadline(datetime(2026, 10, 21), 30)
