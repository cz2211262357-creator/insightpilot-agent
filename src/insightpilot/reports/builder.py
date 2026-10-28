"Evidence is the authority for every displayed number, including chart values."

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import pandas as pd

from insightpilot.errors import DomainError
from insightpilot.reports.charts import chart
from insightpilot.reports.models import ChartSpec, Report

MONEY = {"paid_sales", "net_sales", "refund_amount", "paid_amount", "refund_total"}
TEXT_REFORMATTED_WARNING = "文字说明已按查询证据重新格式化。"
CHART_FALLBACK_WARNING = "图表字段与查询结果不一致，已展示证据表格。"


def preserve_safe_warnings(report, previous):
    # Carry only fixed server-defined warnings across report rebuilding.
    allowed = (TEXT_REFORMATTED_WARNING, CHART_FALLBACK_WARNING)
    extra = [item for item in allowed if item in previous and item not in report.warnings]
    return report.model_copy(update={"warnings": [*report.warnings, *extra]}) if extra else report


def number(value):
    if value is None:
        return None
    try:
        result = Decimal(str(value))
        if type(value) is bool or not result.is_finite():
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError):
        raise DomainError("report_non_numeric_aggregate", 422) from None


class ReportBuilder:
    def __init__(self, chart_specs=None, query_flags=None):
        self.chart_specs = chart_specs or {}
        self.query_flags = query_flags or {}

    def build(self, evidence, claims):
        sources = {str(item.evidence_id): item for item in evidence}
        if len(sources) != len(evidence):
            raise DomainError("report_duplicate_evidence", 422)
        validated, lines = [], []
        warnings = []
        for source in evidence:
            if source.truncated or self.query_flags.get(str(source.evidence_id), {}).get("limited"):
                warnings.append("查询结果为部分数据，不可解释为全量合计或总体占比。")
        for claim in claims:
            source = sources.get(str(claim.evidence_id))
            if source is None or claim.column not in source.columns:
                raise DomainError("report_evidence_unknown", 422)
            if claim.row_index >= len(source.rows):
                raise DomainError("report_cell_unknown", 422)
            template = claim.text_template
            if template.count("{value}") != 1:
                raise DomainError("report_text_invalid", 422)
            prose = template.replace("{value}", "")
            if re.search(r"[0-9零〇一二三四五六七八九十百千万亿{}<>`]", prose):
                raise DomainError("report_text_invalid", 422)
            actual = source.rows[claim.row_index].get(claim.column)
            if claim.operation != "cell":
                if source.truncated or self.query_flags.get(str(source.evidence_id), {}).get(
                    "limited"
                ):
                    raise DomainError("report_partial_aggregate", 422)
                series = pd.Series(
                    [number(row.get(claim.column)) for row in source.rows], dtype=object
                )
                nonnull = series.dropna()
                total = sum(nonnull, Decimal(0)) if len(nonnull) else None
                if claim.operation == "sum":
                    actual = total
                else:
                    denominator = total
                    if claim.denominator_evidence_id:
                        other = sources.get(str(claim.denominator_evidence_id))
                        if (
                            other is None
                            or other.truncated
                            or len(other.rows) != 1
                            or claim.column not in other.columns
                            or self.query_flags.get(str(other.evidence_id), {}).get("limited")
                        ):
                            raise DomainError("report_denominator_invalid", 422)
                        denominator = number(other.rows[0].get(claim.column))
                        if denominator != total:
                            raise DomainError("report_grain_mismatch", 422)
                    numerator = number(actual)
                    actual = (
                        None
                        if numerator is None or denominator in (None, 0)
                        else numerator / denominator
                    )
            if actual is None:
                if claim.value is not None:
                    raise DomainError("report_value_mismatch", 422)
                value, display = None, "暂无数据"
            elif source.types.get(claim.column) in ("decimal", "integer") or isinstance(
                actual, Decimal
            ):
                computed, reported = number(actual), number(claim.value)
                if reported is None or abs(computed - reported) > (
                    Decimal("0.0000005") if claim.operation == "share" else Decimal(0)
                ):
                    raise DomainError("report_value_mismatch", 422)
                if claim.operation == "share":
                    value = format(
                        computed.quantize(Decimal(".000001"), rounding=ROUND_HALF_UP).normalize(),
                        "f",
                    )
                    display = (
                        format(
                            (computed * 100).quantize(Decimal(".01"), rounding=ROUND_HALF_UP), ".2f"
                        )
                        + "%"
                    )
                elif claim.column in MONEY:
                    value = format(computed.quantize(Decimal(".01"), rounding=ROUND_HALF_UP), ".2f")
                    display = value + " 元"
                else:
                    value = (
                        int(computed)
                        if source.types.get(claim.column) == "integer"
                        else format(computed, "f")
                    )
                    display = str(value)
            else:
                if type(actual) is not type(claim.value) or actual != claim.value:
                    raise DomainError("report_value_mismatch", 422)
                value, display = actual, str(actual)
            validated.append({**claim.model_dump(mode="json"), "value": value})
            lines.append(template.replace("{value}", display))
        charts = []
        for identity, spec in self.chart_specs.items():
            if identity not in sources:
                raise DomainError("report_evidence_unknown", 422)
            charts.append(chart(sources[identity], ChartSpec.model_validate(spec)))
        if not claims and any(
            row.get(column) is not None
            for item in evidence
            for row in item.rows
            for column in item.columns
            if item.types.get(column) in ("decimal", "integer")
        ):
            raise DomainError("report_claims_missing", 422)
        return Report(
            summary="\n".join(lines) if lines else "查询已完成；没有可引用的非空结果。",
            claims=validated,
            evidence=[item.model_dump(mode="json") for item in evidence],
            charts=charts,
            chart_specs=self.chart_specs,
            warnings=list(dict.fromkeys(warnings)),
        )
