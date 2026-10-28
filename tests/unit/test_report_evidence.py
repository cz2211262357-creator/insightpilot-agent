from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from insightpilot.errors import DomainError
from insightpilot.query.evidence import build_evidence
from insightpilot.reports.builder import ReportBuilder
from insightpilot.reports.models import ChartSpec, ReportClaim


def source(rows=None, truncated=False):
    rows = (
        [{"region": "华东", "paid_sales": "70.00"}, {"region": "华南", "paid_sales": "30.00"}]
        if rows is None
        else rows
    )
    query = SimpleNamespace(dataset_version=uuid4(), sql_hash="a" * 64)
    return build_evidence(
        uuid4(),
        query,
        {
            "columns": ["region", "paid_sales"],
            "types": {"region": "text", "paid_sales": "decimal"},
            "rows": rows,
            "truncated": truncated,
        },
        ["paid_sales"],
    )


def claim(evidence, **kwargs):
    return ReportClaim(
        evidence_id=evidence.evidence_id,
        column="paid_sales",
        value="70.00",
        text_template="该分组实付销售额为 {value}",
        **kwargs,
    )


def test_report_refuses_unknown_evidence_and_incorrect_numbers():
    evidence = source()
    for candidate in (
        ReportClaim(evidence_id=uuid4(), column="paid_sales", value="70.00"),
        ReportClaim(evidence_id=evidence.evidence_id, column="paid_sales", value="999"),
    ):
        with pytest.raises(DomainError):
            ReportBuilder().build([evidence], [candidate])


def test_exact_decimal_sum_share_and_actual_plotly_from_approved_columns():
    evidence = source()
    claims = [
        ReportClaim(
            evidence_id=evidence.evidence_id,
            column="paid_sales",
            operation="sum",
            value="100.00",
            text_template="这些分组的实付销售额合计 {value}",
        ),
        ReportClaim(
            evidence_id=evidence.evidence_id,
            column="paid_sales",
            operation="share",
            row_index=0,
            value="0.7",
            text_template="该分组的占比为 {value}",
        ),
    ]
    report = ReportBuilder(
        chart_specs={
            str(evidence.evidence_id): ChartSpec(
                kind="bar", x_column="region", y_column="paid_sales"
            )
        }
    ).build([evidence], claims)
    assert report.claims[0]["value"] == "100.00"
    assert report.claims[1]["value"] == "0.7"
    assert "100.00" in report.summary and "70.00%" in report.summary
    assert report.charts[0]["data"][0]["type"] == "bar"
    assert list(report.charts[0]["data"][0]["x"]) == ["华东", "华南"]


@pytest.mark.parametrize("operation", ["sum", "share"])
def test_truncated_or_sql_limited_detail_is_never_a_full_total(operation):
    evidence = source(truncated=True)
    value = "100" if operation == "sum" else "0.7"
    item = ReportClaim(
        evidence_id=evidence.evidence_id, column="paid_sales", operation=operation, value=value
    )
    with pytest.raises(DomainError):
        ReportBuilder().build([evidence], [item])
    evidence = source()
    item = item.model_copy(update={"evidence_id": evidence.evidence_id})
    with pytest.raises(DomainError):
        ReportBuilder(query_flags={str(evidence.evidence_id): {"limited": True}}).build(
            [evidence], [item]
        )


def test_null_stays_no_data_and_numerical_text_cannot_override_citation():
    evidence = source(rows=[{"region": "华东", "paid_sales": None}])
    item = ReportClaim(
        evidence_id=evidence.evidence_id,
        column="paid_sales",
        value=None,
        text_template="所选范围 {value}",
    )
    report = ReportBuilder().build([evidence], [item])
    assert report.claims[0]["value"] is None and "暂无数据" in report.summary
    assert "0.00" not in report.summary
    bad = item.model_copy(update={"text_template": "销售额是 999 元，{value}"})
    with pytest.raises(DomainError):
        ReportBuilder().build([evidence], [bad])


def test_group_share_rejects_fanout_total_mismatch():
    groups = source(
        rows=[{"region": "华东", "paid_sales": "70.00"}, {"region": "华南", "paid_sales": "60.00"}]
    )
    total = source(rows=[{"region": "全部", "paid_sales": "100.00"}])
    item = ReportClaim(
        evidence_id=groups.evidence_id,
        column="paid_sales",
        operation="share",
        value="0.7",
        denominator_evidence_id=total.evidence_id,
    )
    with pytest.raises(DomainError):
        ReportBuilder().build([groups, total], [item])


def test_plotly_schema_rejects_executable_or_nonwhitelisted_content():
    with pytest.raises(ValidationError):
        ChartSpec(kind="pie")
    with pytest.raises(ValidationError):
        ChartSpec(kind="bar", html="<script>unsafe</script>")
    evidence = source()
    with pytest.raises(DomainError):
        ReportBuilder(
            chart_specs={
                str(evidence.evidence_id): ChartSpec(
                    kind="bar", x_column="region", y_column="unavailable"
                )
            }
        ).build([evidence], [])


def test_nonempty_numeric_evidence_requires_at_least_one_cited_claim():
    with pytest.raises(DomainError) as error:
        ReportBuilder().build([source()], [])
    assert error.value.code == "report_claims_missing"
    report = ReportBuilder().build([source(rows=[])], [])
    assert "没有" in report.summary
