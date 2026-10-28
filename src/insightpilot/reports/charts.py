"Only server-built Plotly traces from approved evidence columns."

import html
import json
from decimal import Decimal, InvalidOperation

import plotly.graph_objects as go

from insightpilot.errors import DomainError


def safe_label(value):
    return html.escape("暂无数据" if value is None else str(value), quote=True)


def chart(evidence, spec):
    if spec.kind == "table":
        figure = go.Figure(
            go.Table(
                header={"values": [safe_label(c) for c in evidence.columns]},
                cells={
                    "values": [
                        [safe_label(r.get(c)) for r in evidence.rows] for c in evidence.columns
                    ]
                },
            )
        )
    else:
        if spec.x_column not in evidence.columns or spec.y_column not in evidence.columns:
            raise DomainError("report_chart_column_invalid", 422)
        values = []
        for row in evidence.rows:
            value = row.get(spec.y_column)
            try:
                number = None if value is None else Decimal(str(value))
                if number is not None and (type(value) is bool or not number.is_finite()):
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                raise DomainError("report_chart_value_invalid", 422) from None
            # Monetary precision remains in evidence. Plotly receives JSON decimal
            # strings and renders its numeric axis without changing cited facts.
            values.append(None if number is None else format(number, "f"))
        args = {
            "x": [safe_label(r.get(spec.x_column)) for r in evidence.rows],
            "y": values,
            "name": safe_label(spec.y_column),
        }
        trace = go.Bar(**args) if spec.kind == "bar" else go.Scatter(**args, mode="lines+markers")
        figure = go.Figure(trace)
    figure.update_layout(template=None, height=320, margin={"l": 48, "r": 20, "t": 20, "b": 48})
    return json.loads(figure.to_json())
