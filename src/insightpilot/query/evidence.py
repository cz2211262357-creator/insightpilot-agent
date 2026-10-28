"Lossless JSON scalars and bounded immutable evidence. Money never becomes float."

import hashlib
import json
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from insightpilot.errors import DomainError
from insightpilot.query.models import EvidenceResult

MAX_RESULT_BYTES = 1048576
MAX_ROWS = 1000
TYPE_NAMES = {
    16: "boolean",
    20: "integer",
    21: "integer",
    23: "integer",
    25: "text",
    1043: "text",
    1700: "decimal",
    700: "decimal",
    701: "decimal",
    1082: "date",
    1114: "local_datetime",
    1184: "instant",
    1186: "interval_seconds",
}


def scalar(value):
    if value is None or type(value) in (bool, int, str):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise DomainError("sql_numeric_nonfinite", 422)
        text = format(value, "f")
        if len(text) > 16384:
            raise DomainError("sql_numeric_too_large", 422)
        return text
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DomainError("sql_numeric_nonfinite", 422)
        return format(Decimal(str(value)), "f")
    if isinstance(value, datetime):
        return (
            value.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if value.utcoffset() is not None
            else value.isoformat()
        )
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return format(
            Decimal(value.days) * 86400 + value.seconds + Decimal(value.microseconds) / 1000000, "f"
        )
    raise DomainError("sql_result_type_unsupported", 422)


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def build_evidence(query_id, query, result, metric_ids):
    content = {
        "query_id": str(query_id),
        "columns": result["columns"],
        "types": result["types"],
        "rows": result["rows"],
        "count": len(result["rows"]),
        "truncated": result["truncated"],
        "metric_ids": metric_ids,
        "dataset_version": str(query.dataset_version),
        "sql_hash": query.sql_hash,
    }
    identity = uuid4()
    while True:
        content["count"] = len(content["rows"])
        sha = hashlib.sha256(encoded(content)).hexdigest()
        evidence = EvidenceResult(evidence_id=identity, sha256=sha, **content)
        if len(evidence.model_dump_json().encode("utf-8")) <= MAX_RESULT_BYTES:
            return evidence
        if not content["rows"]:
            raise DomainError("sql_metadata_too_large", 422)
        content["rows"].pop()
        content["truncated"] = True
