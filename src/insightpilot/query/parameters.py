"AST-generated psycopg bindings; literal percent signs remain literal data."

import sqlglot
from sqlglot.dialects.postgres import Postgres


class BoundGenerator(Postgres.Generator):
    def literal_sql(self, expression):
        return super().literal_sql(expression).replace("%", "%%")

    def mod_sql(self, expression):
        return self.binary(expression, "%%")


def compile_bound(query):
    tree = sqlglot.parse_one(query.sql, read="postgres")
    return BoundGenerator(comments=False).generate(tree), dict(query.params)
