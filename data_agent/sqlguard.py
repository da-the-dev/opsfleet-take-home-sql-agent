"""Static SQL validation — PII defense layer 1 (docs/ARCHITECTURE.md §4.2).

Never trusts column names in the *output*: a PII source column may not appear
anywhere its value could reach the result set — projection, aliased, or wrapped
in an expression (``CONCAT(first_name, ...)``) — in the outer query *or any CTE
or subquery*. Since PII can never be projected at any level, taint cannot
propagate through renames, which is what makes alias tricks
(``email AS contact_info``) ineffective.

Allowed positions, because values cannot surface through them:
- predicates: WHERE / HAVING / JOIN ON / QUALIFY
- grouping and ordering: GROUP BY / ORDER BY
- counting aggregates only: COUNT(...), APPROX_COUNT_DISTINCT(...) — note that
  MIN/MAX/ARRAY_AGG/STRING_AGG *return actual values* and are therefore NOT safe.

``SELECT *`` is rejected outright (star expansion could smuggle PII columns and
wastes scan budget); the model is told to enumerate columns.
"""

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import build_scope

from . import config

# Aggregates whose output cannot contain the input value.
_SAFE_AGGREGATES = (exp.Count, exp.ApproxDistinct)
# Clauses where a referenced value cannot reach the output.
_SAFE_CLAUSES = (exp.Where, exp.Having, exp.Join, exp.Group, exp.Order, exp.Qualify)

# Flat deny-set. In thelook_ecommerce these names exist only on `users`, so an
# unqualified reference is unambiguous; qualified references are additionally
# resolved to their source table via scope analysis.
_PII_NAMES: set[str] = {c for cols in config.PII_COLUMNS.values() for c in cols}
_PII_TABLES: set[str] = {t for t in config.PII_COLUMNS}


class GuardViolation(Exception):
    """Query rejected by policy; message instructs the LLM how to regenerate."""


@dataclass
class ValidatedQuery:
    sql: str  # possibly rewritten (LIMIT injected)
    touches_pii_table: bool  # drives strictness of result masking (layer 2)


def _is_position_safe(column: exp.Column) -> bool:
    """True if the value of `column` cannot reach the result set from here."""
    node: exp.Expression = column
    while node.parent is not None:
        parent = node.parent
        if isinstance(parent, _SAFE_AGGREGATES) and not isinstance(
            parent.parent, exp.Window
        ):
            return True
        if isinstance(parent, _SAFE_CLAUSES):
            # For JOINs the safe part is the ON condition, not the joined relation.
            if isinstance(parent, exp.Join) and node is not parent.args.get("on"):
                node = parent
                continue
            return True
        if isinstance(parent, (exp.Select, exp.Subquery)):
            return False  # reached a projection without a safe wrapper
        node = parent
    return False


def _column_is_pii(column: exp.Column, scope) -> bool:
    if column.name.lower() not in _PII_NAMES:
        return False
    if column.table:
        source = scope.sources.get(column.table)
        if isinstance(source, exp.Table):
            return column.name.lower() in config.PII_COLUMNS.get(source.name.lower(), set())
        # Source is a CTE/subquery: it cannot legally project a PII value
        # (that projection is itself rejected), so a column merely *named*
        # like PII coming out of it is clean.
        return False
    # Unqualified: unique to `users` in this dataset — treat as PII.
    return True


def validate(sql: str) -> ValidatedQuery:
    """Parse, enforce policy, and return the (possibly rewritten) query.

    Raises GuardViolation with a self-correction message on any breach.
    """
    try:
        statements = sqlglot.parse(sql, read="bigquery")
    except ParseError as e:
        raise GuardViolation(f"SQL does not parse: {e}") from e

    if len(statements) != 1 or statements[0] is None:
        raise GuardViolation("Submit exactly one SQL statement.")
    tree = statements[0]

    if not isinstance(tree, (exp.Select, exp.Union)):
        raise GuardViolation(
            "Only SELECT queries are allowed. This is a read-only analytics agent: "
            "no DML, DDL, scripts, or procedure calls."
        )

    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            if isinstance(projection, exp.Star) or (
                isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
            ):
                raise GuardViolation(
                    "SELECT * is not allowed. Enumerate the specific columns you need "
                    "(this also keeps the scan under the cost budget)."
                )

    root_scope = build_scope(tree)
    if root_scope is None:  # unreachable for SELECT/UNION, but stay total
        raise GuardViolation("Could not analyze query structure; rewrite it as a plain SELECT.")
    touches_pii_table = False
    for scope in root_scope.traverse():
        for source in scope.sources.values():
            if isinstance(source, exp.Table) and source.name.lower() in _PII_TABLES:
                touches_pii_table = True
        for column in scope.columns:
            if _column_is_pii(column, scope) and not _is_position_safe(column):
                raise GuardViolation(
                    f"Column `{column.sql()}` is PII (customer names/emails) and may not "
                    "appear in query output — not even aliased, concatenated, or inside "
                    "value-returning aggregates like MIN/MAX/ARRAY_AGG/STRING_AGG. "
                    "PII columns are allowed only in WHERE/HAVING/JOIN/GROUP BY filters "
                    "or inside COUNT()/APPROX_COUNT_DISTINCT(). To identify customers, "
                    "use the numeric `id`/`user_id` instead."
                )

    if isinstance(tree, exp.Select) and tree.args.get("limit") is None:
        tree = tree.limit(config.DEFAULT_ROW_LIMIT)

    return ValidatedQuery(sql=tree.sql(dialect="bigquery"), touches_pii_table=touches_pii_table)
