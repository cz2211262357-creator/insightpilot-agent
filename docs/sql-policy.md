# SQL policy 2026-10-v1

Only a single readonly SELECT/SELECT UNION is accepted. Every physical source must resolve to one of the six managed analysis views. SQLGlot traverses the complete AST and resolves CTE, subquery, alias and column scopes against the server catalog. Safe stars are expanded; unresolved stars are rejected except COUNT(*).

Allowed functions: COUNT, SUM, AVG, MIN, MAX, DATE_TRUNC, COALESCE, NULLIF, ROUND, EXTRACT, and approved scalar CAST/CASE/arithmetic. Window functions, recursive CTEs, system/user functions, relation functions, file/network operations, writes, locks and SELECT INTO are rejected. Casts exclude arrays, regclass/regproc, JSON, user-defined and system types. Timezone is UTC or Asia/Shanghai. SQL identifiers use ASCII aliases; the UI uses localized catalog labels.

Limits: SQL16000 characters,32 exact named scalar bindings,2048 characters per string parameter,64-bit integers,8 nested scopes/joins,LIMIT1000,OFFSET10000. The execution gateway also enforces row, byte, timeout, permission and query-count budgets; SQL LIMIT does not prove an aggregate is complete.

The bound generator emits psycopg named placeholders from AST nodes. It escapes literal percent signs and the modulo operator; strings containing :name remain strings. Values are never concatenated into SQL. Query fingerprints include canonical SQL, parameters, policy version, catalog version and immutable dataset version.

The machine-readable allow/reject matrix is in evals/sql_policy_matrix.json; tests also cover mismatched and non-scalar parameters, unsafe casts, nested CTE writes and correlated subqueries. tests/integration/test_sql_binding.py runs the actual driver and compares all20 compiled queries with hand-authored goldSQL for both fictional tenants. This does not claim real-model accuracy; online evaluation is a separate acceptance step.
