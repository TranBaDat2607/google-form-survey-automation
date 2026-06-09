from gform.distributions import build_quota_column, largest_remainder


def test_quotas_sum_to_n_and_match():
    counts = largest_remainder({"A": 0.6, "B": 0.3, "C": 0.1}, 1000)
    assert sum(counts.values()) == 1000
    assert counts == {"A": 600, "B": 300, "C": 100}


def test_quotas_handle_rounding():
    counts = largest_remainder({"A": 1, "B": 1, "C": 1}, 10)
    assert sum(counts.values()) == 10
    # 3.33 each -> one option gets the extra unit, deterministically.
    assert sorted(counts.values()) == [3, 3, 4]


def test_quotas_deterministic_tie_break():
    assert largest_remainder({"A": 1, "B": 1, "C": 1}, 10) == largest_remainder(
        {"C": 1, "B": 1, "A": 1}, 10
    )


def test_quota_column_is_deterministic_and_complete():
    counts = {"A": 2, "B": 1}
    col1 = build_quota_column(counts, seed=5)
    col2 = build_quota_column(counts, seed=5)
    assert col1 == col2
    assert sorted(col1) == ["A", "A", "B"]


def test_quota_column_changes_with_seed():
    counts = {"A": 50, "B": 50}
    assert build_quota_column(counts, seed=1) != build_quota_column(counts, seed=2)
