import sqlite3

import pytest
from sqlalchemy.exc import OperationalError

from db import execute_query
from sql_guard import SqlSecurityError, prepare_query

SCHEMA = {"orders": ["id", "amount"]}


@pytest.fixture
def business(tmp_path):
    path = tmp_path / "business.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE orders(id INTEGER, amount REAL)")
        conn.executemany(
            "INSERT INTO orders VALUES (?, ?)", [(i, i * 0.5) for i in range(205)]
        )
    return path


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM sqlite_master",
        "SELECT * FROM orders JOIN hidden ON 1=1",
        "SELECT * FROM (SELECT * FROM hidden)",
        "SELECT * FROM main.orders",
        "DELETE FROM orders",
        "SELECT 1; SELECT 2",
        "SELECT readfile('/tmp/x')",
        "SELECT unknown FROM orders",
        "SELECT * FROM orders LIMIT -1",
        "SELECT * FROM orders LIMIT (SELECT 1)",
        "SELECT randomblob(999999999)",
    ],
)
def test_reject(sql):
    with pytest.raises(SqlSecurityError):
        prepare_query(sql, SCHEMA)


@pytest.mark.parametrize(
    "limit,count,truncated",
    [
        (None, 200, True),
        (0, 0, False),
        (20, 20, False),
        (200, 200, False),
        (201, 200, True),
        (10000, 200, True),
    ],
)
def test_boundaries(business, limit, count, truncated):
    sql = "SELECT * FROM orders" + (f" LIMIT {limit}" if limit is not None else "")
    query = prepare_query(sql, SCHEMA)
    result = execute_query(query.execution_sql, path=business)
    assert result["row_count"] == count and result["truncated"] == truncated


def test_union_and_dates(business):
    query = prepare_query(
        "SELECT id FROM orders WHERE id=1 UNION SELECT id FROM orders WHERE id=2",
        SCHEMA,
    )
    assert execute_query(query.execution_sql, path=business)["rows"] == [[1], [2]]
    query = prepare_query("SELECT DATE('2020-01-01', '+1 day')", SCHEMA)
    assert execute_query(query.execution_sql, path=business)["rows"] == [["2020-01-02"]]


def test_timeout_and_readonly(business):
    with pytest.raises(TimeoutError):
        execute_query(
            "SELECT SUM(a.id*b.id*c.id) FROM orders a,orders b,orders c",
            path=business,
            timeout=0,
        )
    assert execute_query("SELECT COUNT(*) FROM orders", path=business)["rows"] == [
        [205]
    ]
    with pytest.raises(OperationalError):
        execute_query("DELETE FROM orders", path=business)
    with pytest.raises(ValueError, match="resource_limit"):
        execute_query("SELECT printf('%070000d', 1)", path=business)


@pytest.mark.parametrize("count,truncated", [(0, False), (200, False), (201, True)])
def test_exact_row_boundary_without_explicit_limit(business, count, truncated):
    query = prepare_query(f"SELECT id FROM orders WHERE id < {count}", SCHEMA)
    result = execute_query(query.execution_sql, path=business)
    assert result["row_count"] == min(count, 200) and result["truncated"] == truncated


def test_month_grouping_aggregate_and_literal_colon(business):
    query = prepare_query("SELECT strftime('%Y-%m', '2026-09-24'), COUNT(DISTINCT id), "
                          "SUM(CASE WHEN id=1 THEN amount ELSE 0 END), ':literal' FROM orders", SCHEMA)
    result = execute_query(query.execution_sql, path=business)
    assert result["rows"] == [["2026-09", 205, 0.5, ":literal"]]


def test_star_expansion_and_nested_aliases(business):
    query = prepare_query("SELECT o.* FROM (SELECT id FROM orders) o LIMIT 2", SCHEMA)
    assert execute_query(query.execution_sql, path=business)["columns"] == ["id"]


def test_execute_tool_failure_keeps_attempted_sql(monkeypatch):
    import tools
    def fail(sql):
        raise TimeoutError("query_timeout")
    monkeypatch.setattr(tools, "execute_query", fail)
    result = tools.execute_sql("SELECT 1")
    assert result.is_error and result.error_type == "timeout"
    assert result.query["model_sql"] == "SELECT 1"
    assert result.sql.endswith("LIMIT 201") and result.query["status"] == "failed"
