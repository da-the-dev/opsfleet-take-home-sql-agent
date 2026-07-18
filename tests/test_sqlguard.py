"""PII layer 1 + query policy: the adversarial fixtures from the design's safety suite."""

from data_agent import sqlguard
from data_agent.sqlguard import GuardViolation, validate

USERS = "`bigquery-public-data.thelook_ecommerce.users`"
ORDERS = "`bigquery-public-data.thelook_ecommerce.orders`"
PRODUCTS = "`bigquery-public-data.thelook_ecommerce.products`"


def rejected(sql: str) -> str:
    try:
        validate(sql)
    except GuardViolation as err:
        return str(err)
    raise AssertionError(f"expected GuardViolation for: {sql}")


# --- PII cannot reach the output, however phrased -----------------------------

def test_plain_projection_rejected():
    assert "PII" in rejected(f"SELECT email FROM {USERS}")


def test_alias_rename_rejected():
    assert "PII" in rejected(f"SELECT email AS contact_info FROM {USERS}")


def test_derived_expression_rejected():
    assert "PII" in rejected(
        f"SELECT CONCAT(first_name, ' ', last_name) AS who FROM {USERS}"
    )


def test_qualified_via_table_alias_rejected():
    assert "PII" in rejected(
        f"SELECT u.email FROM {USERS} u JOIN {ORDERS} o ON o.user_id = u.id"
    )


def test_cte_smuggling_rejected():
    assert "PII" in rejected(
        f"WITH c AS (SELECT email AS e FROM {USERS}) SELECT e FROM c"
    )


def test_scalar_subquery_rejected():
    assert "PII" in rejected(f"SELECT (SELECT email FROM {USERS} LIMIT 1) AS x")


def test_value_returning_aggregates_rejected():
    assert "PII" in rejected(f"SELECT MIN(email) FROM {USERS}")
    assert "PII" in rejected(f"SELECT ARRAY_AGG(first_name) FROM {USERS}")
    assert "PII" in rejected(f"SELECT STRING_AGG(email, ',') FROM {USERS}")


def test_window_functions_rejected():
    assert "PII" in rejected(
        f"SELECT FIRST_VALUE(email) OVER (ORDER BY id) FROM {USERS}"
    )


# --- Legitimate analytical usage stays possible --------------------------------

def test_counting_aggregates_allowed():
    validate(f"SELECT COUNT(DISTINCT email) AS customers FROM {USERS}")
    validate(f"SELECT APPROX_COUNT_DISTINCT(email) AS customers FROM {USERS}")


def test_predicate_usage_allowed():
    validate(f"SELECT id FROM {USERS} WHERE email LIKE '%@example.com'")


def test_group_by_pii_without_projection_allowed():
    validate(f"SELECT COUNT(1) AS n FROM {USERS} GROUP BY email")


def test_normal_analytics_pass():
    result = validate(
        f"SELECT state, COUNT(DISTINCT id) AS customers FROM {USERS} "
        "GROUP BY state ORDER BY customers DESC LIMIT 10"
    )
    assert result.touches_pii_table


def test_non_pii_table_not_strict():
    result = validate(f"SELECT category, retail_price FROM {PRODUCTS} LIMIT 5")
    assert not result.touches_pii_table


# --- Statement policy ------------------------------------------------------------

def test_dml_rejected():
    for sql in (
        f"DELETE FROM {ORDERS} WHERE 1=1",
        f"UPDATE {ORDERS} SET status = 'x'",
        f"INSERT INTO {ORDERS} (order_id) VALUES (1)",
        f"DROP TABLE {ORDERS}",
    ):
        assert "SELECT" in rejected(sql)


def test_multiple_statements_rejected():
    rejected(f"SELECT id FROM {ORDERS}; SELECT id FROM {ORDERS}")


def test_select_star_rejected():
    assert "SELECT *" in rejected(f"SELECT * FROM {PRODUCTS}")
    assert "SELECT *" in rejected(f"SELECT p.* FROM {PRODUCTS} p")


def test_limit_injected():
    result = validate(f"SELECT id FROM {ORDERS}")
    assert f"LIMIT {sqlguard.config.DEFAULT_ROW_LIMIT}" in result.sql


def test_existing_limit_kept():
    assert "LIMIT 7" in validate(f"SELECT id FROM {ORDERS} LIMIT 7").sql


if __name__ == "__main__":
    import sys
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
