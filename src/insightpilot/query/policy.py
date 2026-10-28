"A closed AST grammar plus scope/column qualification, never keyword stripping."

import hashlib
import json
import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from insightpilot.errors import DomainError
from insightpilot.query.models import ValidatedSql

POLICY_VERSION = "2026-10-v1"
IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")
PARAMETER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,31}$")
NODES = frozenset(
    """Select Subquery With CTE Union From Table TableAlias Identifier Column
Star Alias Join Where Group Having Order Ordered Limit Offset Distinct Literal Null Boolean
Paren Tuple Placeholder And Or Not EQ NEQ GT GTE LT LTE Is In Between Like ILike Escape Exists
Add Sub Mul Div Mod Neg Case If Count Sum Avg Min Max TimestampTrunc DateTrunc Date
Coalesce Nullif Round Cast DataType DataTypeParam Var Extract AtTimeZone""".split()
)
# These expressions are never executed by the gateway. They are recognized
# only to request a bounded rewrite after every other safety check passes.
REWRITE_ONLY_NODES = frozenset({"Window", "WindowSpec", "Lag", "TimeToStr"})
CASTS = frozenset(
    "INT BIGINT SMALLINT DECIMAL DOUBLE FLOAT BOOLEAN TEXT VARCHAR "
    "DATE TIMESTAMP TIMESTAMPTZ".split()
)
UNITS = frozenset(
    "EPOCH YEAR MONTH DAY HOUR MINUTE SECOND WEEK QUARTER DOW DOY ISODOW ISOYEAR".split()
)


def reject(code="sql_policy_forbidden"):
    raise DomainError(code, 422)


def validate_parameters(tree, params):
    if not isinstance(params, dict) or len(params) > 32:
        reject("sql_parameters_invalid")
    for name, value in params.items():
        if not isinstance(name, str) or not PARAMETER.fullmatch(name):
            reject("sql_parameters_invalid")
        if type(value) not in (str, int, bool, type(None)):
            reject("sql_parameters_invalid")
        if isinstance(value, str) and (len(value) > 2048 or "\x00" in value):
            reject("sql_parameters_invalid")
        if type(value) is int and not -(2**63) <= value < 2**63:
            reject("sql_parameters_invalid")
    placeholders = list(tree.find_all(exp.Placeholder))
    if any(
        not node.name or not PARAMETER.fullmatch(node.name) or node.args.get("jdbc")
        for node in placeholders
    ):
        reject("sql_parameters_invalid")
    if {node.name for node in placeholders} != set(params):
        reject("sql_parameters_invalid")


def check_nodes(tree, params):
    nodes = list(tree.walk())
    if len(nodes) > 2000:
        reject("sql_complexity_limit")
    unsupported = {type(node).__name__ for node in nodes if type(node).__name__ not in NODES}
    if unsupported - REWRITE_ONLY_NODES:
        reject()
    for node in nodes:
        if (
            isinstance(node, exp.Identifier)
            and not IDENTIFIER.fullmatch(node.name)
            and not (
                isinstance(node.parent, exp.Alias)
                and node.arg_key == "alias"
                and node.args.get("quoted")
                and node.name.isascii()
                and node.name.isdigit()
            )
        ):
            reject("sql_identifier_invalid")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            reject("sql_recursive_forbidden")
        if isinstance(node, exp.DataType):
            if node.this.name not in CASTS:
                reject("sql_cast_forbidden")
            for index, param in enumerate(node.expressions):
                value = param.this
                if not isinstance(value, exp.Literal) or value.is_string:
                    reject("sql_cast_forbidden")
                try:
                    amount = int(value.this)
                except ValueError:
                    reject("sql_cast_forbidden")
                maximum = 2048 if node.this.name == "VARCHAR" else (38 if index == 0 else 18)
                if amount < 0 or amount > maximum:
                    reject("sql_cast_forbidden")
        if isinstance(node, exp.Var) and node.name.upper() not in UNITS:
            reject("sql_time_unit_forbidden")
        if isinstance(node, exp.Date) and (
            not isinstance(node.this, exp.Expression)
            or any(value for key, value in node.args.items() if key != "this")
        ):
            reject("sql_date_invalid")
        if isinstance(node, exp.AtTimeZone):
            zone = node.args.get("zone")
            if not isinstance(zone, exp.Literal) or zone.this not in ("Asia/Shanghai", "UTC"):
                reject("sql_timezone_forbidden")
        if isinstance(node, (exp.Limit, exp.Offset)):
            count = node.expression
            if isinstance(count, exp.Literal) and not count.is_string:
                try:
                    value = int(count.this)
                except ValueError:
                    reject("sql_limit_invalid")
            elif isinstance(count, exp.Placeholder):
                value = params.get(count.name)
            else:
                reject("sql_limit_invalid")
            maximum = 1000 if isinstance(node, exp.Limit) else 10000
            if type(value) is not int or not 0 <= value <= maximum:
                reject("sql_limit_invalid")
        node.comments = None
    if len(list(tree.find_all(exp.Join))) > 8:
        reject("sql_complexity_limit")
    return bool(unsupported)


def check_aggregate_grain(tree):
    # Check each SELECT scope independently. A one-to-many join can multiply
    # order/refund values even though the SQL and database permissions are valid.
    for scope in traverse_scope(tree):
        sources = {alias: source for alias, (_, source) in scope.selected_sources.items()}
        tables = {
            alias: source.name for alias, source in sources.items() if isinstance(source, exp.Table)
        }
        names = set(tables.values())
        unknown = any(isinstance(source, Scope) for source in sources.values())
        many_order = bool({"order_items", "aftersales"} & names) or unknown
        for aggregate in scope.expression.find_all(exp.Sum, exp.Avg, exp.Count):
            if aggregate.find_ancestor(exp.Select) is not scope.expression:
                continue
            columns = list(aggregate.find_all(exp.Column))
            if isinstance(aggregate, (exp.Sum, exp.Avg)):
                for column in columns:
                    origin = tables.get(column.table)
                    if (
                        (origin == "orders" and column.name == "paid_amount" and many_order)
                        or (
                            origin == "aftersales"
                            and column.name == "refund_amount"
                            and ("order_items" in names or unknown)
                        )
                        or (
                            origin == "order_items"
                            and column.name == "line_paid_amount"
                            and ("aftersales" in names or unknown)
                        )
                    ):
                        reject("sql_unsafe_grain")
            if isinstance(aggregate, exp.Count) and not isinstance(aggregate.this, exp.Distinct):
                order_column = any(
                    tables.get(column.table) == "orders" and column.name == "order_id"
                    for column in columns
                )
                order_alias = (
                    isinstance(aggregate.parent, exp.Alias)
                    and aggregate.parent.alias == "order_count"
                )
                if "orders" in names and many_order and (order_column or order_alias):
                    reject("sql_unsafe_grain")


class SqlPolicy:
    def validate(self, sql, params, catalog):
        if not isinstance(sql, str) or not sql.strip() or len(sql) > 16000 or "\x00" in sql:
            reject("sql_input_invalid")
        try:
            statements = sqlglot.parse(sql, read="postgres")
        except (sqlglot.errors.SqlglotError, RecursionError, ValueError):
            reject("sql_parse_invalid")
        if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
            reject("sql_select_required")
        tree = normalize_identifiers(statements[0], dialect="postgres")
        validate_parameters(tree, params)
        needs_rewrite = check_nodes(tree, params)
        scopes = list(traverse_scope(tree))
        sources = set()
        for scope in scopes:
            depth = 0
            parent = scope.parent
            while parent is not None:
                depth += 1
                parent = parent.parent
            if depth > 8:
                reject("sql_complexity_limit")
            for source in scope.sources.values():
                if isinstance(source, Scope):
                    continue
                if not isinstance(source, exp.Table) or not isinstance(source.this, exp.Identifier):
                    reject("sql_relation_forbidden")
                if (
                    source.catalog
                    or source.db not in ("", "analysis")
                    or source.name not in catalog.views
                ):
                    reject("sql_relation_forbidden")
                source.set("db", exp.Identifier(this="analysis"))
                sources.add(source.name)
        schema = {
            "analysis": {
                name: {column: "UNKNOWN" for column in columns}
                for name, columns in catalog.views.items()
            }
        }
        try:
            tree = qualify(
                tree,
                dialect="postgres",
                db="analysis",
                schema=schema,
                expand_stars=True,
                validate_qualify_columns=True,
                allow_partial_qualification=False,
                quote_identifiers=False,
                identify=False,
            )
        except (sqlglot.errors.SqlglotError, ValueError, RecursionError):
            reject("sql_column_forbidden")
        needs_rewrite = check_nodes(tree, params) or needs_rewrite
        check_aggregate_grain(tree)
        if needs_rewrite:
            reject("sql_expression_unsupported")
        if any(not isinstance(star.parent, exp.Count) for star in tree.find_all(exp.Star)):
            reject("sql_star_unresolved")
        canonical = tree.sql(dialect="postgres", pretty=False, comments=False)
        identity = {
            "sql": canonical,
            "params": params,
            "dataset_version": str(catalog.dataset_version),
            "catalog_version": catalog.version,
            "policy_version": POLICY_VERSION,
        }
        fingerprint = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        columns = sorted({column.sql(dialect="postgres") for column in tree.find_all(exp.Column)})
        return ValidatedSql(
            sql=canonical,
            params=dict(params),
            sql_hash=fingerprint,
            views=sorted(sources),
            columns=columns,
            dataset_version=catalog.dataset_version,
            catalog_version=catalog.version,
        )
