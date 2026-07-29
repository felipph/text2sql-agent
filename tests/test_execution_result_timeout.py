from txt2sql.artifacts import Budget
from txt2sql.middleware import compact_result, result_from_rejection, result_from_timeout


def test_result_from_timeout() -> None:
    r = result_from_timeout("query exceeded 30s")
    assert r.status == "timeout"
    assert r.error
    assert "30s" in r.error


def test_result_from_rejection() -> None:
    r = result_from_rejection("DML not allowed")
    assert r.status == "rejected"
    assert "DML" in (r.error or "")


def test_compact_result_truncates_sample() -> None:
    rows = [{"id": i} for i in range(50)]
    budget = Budget(sample_rows=5)
    r = compact_result(rows, budget, schema=[{"name": "id", "type": "INTEGER"}])
    assert r.status == "ok"
    assert r.row_count == 50
    assert len(r.sample) == 5
    assert r.truncated is True
