"""Tests for coerce_types=True on TableAccessor.to_arrow/to_pandas/to_polars.

Records in the vault are always faithful to YAML-parsed disk values. Coercion
only happens at accessor output time.
"""
import datetime
from pathlib import Path

import pytest

from vaults import Vault
from vaults.vault import coerce_target, coerce_value


# ---------------------------------------------------------------------------
# Unit tests for the private helpers (in vault.py)
# ---------------------------------------------------------------------------

def testcoerce_target_single_type():
    assert coerce_target({int}) is None


def testcoerce_target_int_float():
    assert coerce_target({int, float}) is float


def testcoerce_target_bool_int():
    assert coerce_target({bool, int}) is int


def testcoerce_target_bool_float():
    assert coerce_target({bool, float}) is float


def testcoerce_target_str_wins_over_numeric():
    assert coerce_target({int, str}) is str
    assert coerce_target({float, str}) is str


def testcoerce_target_date_plus_str():
    assert coerce_target({datetime.date, str}) is str


def testcoerce_target_date_plus_int():
    assert coerce_target({datetime.date, int}) is str


def testcoerce_target_date_alone():
    assert coerce_target({datetime.date}) is None


def testcoerce_value_date_to_str():
    d = datetime.date(2026, 5, 18)
    assert coerce_value(d, str) == "2026-05-18"


def testcoerce_value_int_to_float():
    assert coerce_value(3, float) == 3.0
    assert isinstance(coerce_value(3, float), float)


def testcoerce_value_bool_to_int():
    assert coerce_value(True, int) == 1
    assert type(coerce_value(True, int)) is int


def testcoerce_value_none_passthrough():
    assert coerce_value(None, str) is None


def testcoerce_value_already_target_type():
    assert coerce_value("hello", str) == "hello"


def testcoerce_value_bad_conversion_returns_original():
    assert coerce_value("hello", float) == "hello"


# ---------------------------------------------------------------------------
# Integration tests — coercion happens in the accessor, vault records unchanged
# ---------------------------------------------------------------------------

def _make_vault(tmp_path, records: list[dict], type_name: str = "Items") -> Vault:
    folder = tmp_path / "data" / type_name
    folder.mkdir(parents=True)
    for rec in records:
        name = rec.pop("_name")
        lines = ["---"]
        for k, v in rec.items():
            lines.append(f"{k}: {v!r}")
        lines.append("---")
        (folder / f"{name}.md").write_text("\n".join(lines) + "\n")
    return Vault.from_vault(tmp_path)


def test_vault_records_unchanged_with_coerce_types(tmp_path):
    # Records in the vault must NOT be coerced regardless of accessor option.
    vault = _make_vault(tmp_path, [
        {"_name": "A", "count": 1},
        {"_name": "B", "count": 2.5},
    ])
    types = {type(r.fields["count"]) for r in vault.records["Items"]}
    assert types == {int, float}


def test_vault_records_not_modified_by_accessor(tmp_path):
    # Calling to_pandas(coerce_types=True) must not modify the in-memory records.
    vault = _make_vault(tmp_path, [
        {"_name": "A", "count": 1},
        {"_name": "B", "count": 2.5},
    ])
    vault.dfs["Items"].to_pandas(coerce_types=True)
    types = {type(r.fields["count"]) for r in vault.records["Items"]}
    assert types == {int, float}


def test_to_pandas_coerce_true_int_float_becomes_float(tmp_path):
    vault = _make_vault(tmp_path, [
        {"_name": "A", "count": 1},
        {"_name": "B", "count": 2.5},
    ])
    df = vault.dfs["Items"].to_pandas(coerce_types=True)
    assert str(df["count"].dtype) == "float64"
    assert set(df["count"].tolist()) == {1.0, 2.5}


def test_to_pandas_coerce_true_str_dominates_int(tmp_path):
    vault = _make_vault(tmp_path, [
        {"_name": "A", "val": "hello"},
        {"_name": "B", "val": 42},
    ])
    df = vault.dfs["Items"].to_pandas(coerce_types=True)
    # Values must be strings; dtype representation varies by pandas version.
    assert all(isinstance(v, str) for v in df["val"].dropna())
    assert set(df["val"].tolist()) == {"hello", "42"}


def test_to_pandas_coerce_true_date_str_mix_becomes_str(tmp_path):
    (tmp_path / "data" / "Events").mkdir(parents=True)
    (tmp_path / "data" / "Events" / "A.md").write_text("---\ndate: 2026-05-18\n---\n")
    (tmp_path / "data" / "Events" / "B.md").write_text("---\ndate: '2026-05-MM'\n---\n")
    vault = Vault.from_vault(tmp_path)

    # Vault records still have the raw split — date object + str.
    raw_types = {type(r.fields["date"]) for r in vault.records["Events"] if "date" in r.fields}
    assert datetime.date in raw_types and str in raw_types

    df = vault.dfs["Events"].to_pandas(coerce_types=True)
    # dtype representation varies; check that all values are plain strings.
    assert all(isinstance(v, str) for v in df["date"].dropna())
    values = set(df["date"].dropna().tolist())
    assert "2026-05-18" in values
    assert "2026-05-MM" in values


def test_to_polars_coerce_true_int_float_becomes_float(tmp_path):
    vault = _make_vault(tmp_path, [
        {"_name": "A", "count": 1},
        {"_name": "B", "count": 2.5},
    ])
    pl = pytest.importorskip("polars")
    df = vault.dfs["Items"].to_polars(coerce_types=True)
    assert df["count"].dtype == pl.Float64


def test_to_arrow_coerce_true_int_float_becomes_float(tmp_path):
    import pyarrow as pa
    vault = _make_vault(tmp_path, [
        {"_name": "A", "count": 1},
        {"_name": "B", "count": 2.5},
    ])
    tbl = vault.dfs["Items"].to_arrow(coerce_types=True)
    assert tbl.schema.field("count").type == pa.float64()


def test_coerce_does_not_pollute_arrow_cache(tmp_path):
    # to_arrow(coerce_types=True) must not write to the cache used by coerce_types=False.
    vault = _make_vault(tmp_path, [
        {"_name": "A", "count": 1},
        {"_name": "B", "count": 2.5},
    ])
    accessor = vault.dfs["Items"]

    # First plain call → populates cache (same object returned on second call).
    tbl1 = accessor.to_arrow(coerce_types=False)
    tbl2 = accessor.to_arrow(coerce_types=False)
    assert tbl1 is tbl2  # cache hit

    # Coerced call → not from cache, returns a distinct object.
    tbl_coerced = accessor.to_arrow(coerce_types=True)
    assert tbl_coerced is not tbl1

    # Cache is still intact after the coerced call.
    tbl3 = accessor.to_arrow(coerce_types=False)
    assert tbl3 is tbl1
