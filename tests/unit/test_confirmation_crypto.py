from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

from pydantic import SecretStr

from insightpilot.query.confirmations import bound_token


def test_same_instant_has_same_confirmation_capability_across_connection_timezones():
    row = {name: uuid4() for name in ("id", "tenant_id", "run_id", "user_id", "query_id")}
    row.update(
        task_revision=1,
        auth_version=1,
        sql_hash="synthetic-sql",
        checkpoint_digest="synthetic-checkpoint",
        budget={"model_used": 1, "query_used": 0, "repair_used": 0, "remaining_seconds": "299.000"},
        expires_at=datetime(2026, 10, 27, tzinfo=UTC),
    )
    key = SecretStr("synthetic-test-key-with-at-least-32-bytes")
    original = bound_token(key, row)
    converted = {**row, "expires_at": row["expires_at"].astimezone(timezone(timedelta(hours=8)))}
    assert bound_token(key, converted) == original
