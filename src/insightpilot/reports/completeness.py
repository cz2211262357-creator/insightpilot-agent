"Conservative completeness for LIMIT/OFFSET at any query depth."

import sqlglot
from sqlglot import exp


def query_is_partial(sql):
    tree = sqlglot.parse_one(sql, read="postgres")
    return any(isinstance(node, (exp.Limit, exp.Offset)) for node in tree.walk())
