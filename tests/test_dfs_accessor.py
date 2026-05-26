"""Tests for vault.dfs — DfsAccessor, TableAccessor, ViewsAccessor."""
import sys
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pytest

from vaults import Vault

FIXTURE = Path(__file__).parent / "fixtures" / "sample_vault"


@pytest.fixture(scope="module")
def vault():
    return Vault.from_vault(FIXTURE, dangling_refs="stub")


# ── DfsAccessor dict interface ──────────────────────────────────────────────

def test_dfs_getitem(vault):
    ta = vault.dfs["Projekte"]
    assert ta._type_name == "Projekte"


def test_dfs_getitem_unknown_raises(vault):
    with pytest.raises(KeyError):
        vault.dfs["DoesNotExist"]


def test_dfs_contains(vault):
    assert "Projekte" in vault.dfs
    assert "Personen" in vault.dfs
    assert "Ghost" not in vault.dfs


def test_dfs_iter(vault):
    assert set(vault.dfs) == set(vault.schema.types.keys())


def test_dfs_keys(vault):
    assert "Projekte" in vault.dfs.keys()


def test_dfs_len(vault):
    assert len(vault.dfs) == len(vault.schema.types)


# ── Arrow table structure ───────────────────────────────────────────────────

def test_record_column_first(vault):
    tbl = vault.dfs["Projekte"].to_arrow()
    assert tbl.column_names[0] == "_record"


def test_full_text_column_last(vault):
    tbl = vault.dfs["Projekte"].to_arrow()
    assert tbl.column_names[-1] == "full_text"


def test_record_count(vault):
    tbl = vault.dfs["Projekte"].to_arrow()
    assert tbl.num_rows == 2


def test_string_column_type(vault):
    tbl = vault.dfs["Projekte"].to_arrow()
    assert tbl.schema.field("Titel").type == pa.string()


def test_date_column_type(vault):
    tbl = vault.dfs["Projekte"].to_arrow()
    assert tbl.schema.field("Beginn").type == pa.date32()


def test_boolean_column_type(vault):
    tbl = vault.dfs["Projekte"].to_arrow()
    assert tbl.schema.field("Abgeschlossen").type == pa.bool_()


def test_integer_number_column_type(vault):
    """All-integer NUMBER field → int64 in Arrow."""
    tbl = vault.dfs["Projekte"].to_arrow()
    assert tbl.schema.field("Budget").type == pa.int64()


def test_integer_number_pandas_nullable(vault):
    """int64 Arrow column → pd.Int64Dtype() in pandas (nullable, not float)."""
    import pandas as pd
    df = vault.dfs["Projekte"].to_pandas()
    assert isinstance(df["Budget"].dtype, pd.Int64Dtype)


def test_formula_year_is_nullable_int(vault):
    """Formula year field (integers with potential nulls) → int64 / Int64Dtype."""
    import pandas as pd
    tbl = vault.dfs["Projekte"].to_arrow()
    assert tbl.schema.field("Jahr").type == pa.int64()
    df = vault.dfs["Projekte"].to_pandas()
    assert isinstance(df["Jahr"].dtype, pd.Int64Dtype)


# ── Column ordering convention ──────────────────────────────────────────────

def test_column_order_slots(vault):
    """Strings before booleans before numbers before dates."""
    cols = vault.dfs["Projekte"].to_arrow().column_names
    idx = {c: i for i, c in enumerate(cols)}
    assert idx["Titel"] < idx["Abgeschlossen"]    # STRING < BOOLEAN
    assert idx["Abgeschlossen"] < idx["Budget"]   # BOOLEAN < NUMBER
    assert idx["Budget"] < idx["Beginn"]           # NUMBER < DATE


# ── Link field rendering ────────────────────────────────────────────────────

def test_link_field_as_filename(vault):
    """LINK fields render as bare filename, no folder or brackets."""
    tbl = vault.dfs["Personen"].to_arrow()
    values = tbl.column("Partei").to_pylist()
    non_null = [v for v in values if v is not None]
    assert all("[[" not in v and "/" not in v for v in non_null)


def test_list_links_as_filename_list(vault):
    """LIST_LINKS fields render as lists of bare filenames."""
    tbl = vault.dfs["Projekte"].to_arrow()
    values = tbl.column("Traeger").to_pylist()
    non_null = [v for v in values if v is not None]
    assert all(isinstance(v, list) for v in non_null)
    flat = [item for sublist in non_null for item in sublist]
    assert all("[[" not in item and "/" not in item for item in flat)


# ── Missing fields → null ───────────────────────────────────────────────────

def test_stub_record_has_null_fields(vault):
    """Stub records (dangling link targets) have null values in all columns."""
    tbl = vault.dfs["Personen"].to_arrow()
    names = tbl.column("_record").to_pylist()
    if "Clara" in names:
        idx = names.index("Clara")
        name_val = tbl.column("Name")[idx].as_py()
        assert name_val is None


# ── Arrow caching ───────────────────────────────────────────────────────────

def test_arrow_cache(vault):
    ta = vault.dfs["Projekte"]
    first = ta.to_arrow()
    second = ta.to_arrow()
    assert first is second


# ── pandas / polars ─────────────────────────────────────────────────────────

def test_to_pandas(vault):
    df = vault.dfs["Projekte"].to_pandas()
    assert "_record" in df.columns
    assert len(df) == 2


def test_to_polars(vault):
    pl = pytest.importorskip("polars")
    df = vault.dfs["Projekte"].to_polars()
    assert "_record" in df.columns
    assert len(df) == 2


def test_to_pandas_polars_same_shape(vault):
    pl = pytest.importorskip("polars")
    pdf = vault.dfs["Projekte"].to_pandas()
    pldf = vault.dfs["Projekte"].to_polars()
    assert len(pdf) == len(pldf)
    assert set(pdf.columns) == set(pldf.columns)


def test_pandas_missing_raises(vault):
    with patch.dict(sys.modules, {"pandas": None}):
        ta = vault.dfs["Projekte"]
        ta._arrow_cache = None
        with pytest.raises(ImportError, match="pip install vaults\\[pandas\\]"):
            ta.to_pandas()


# ── ViewsAccessor ───────────────────────────────────────────────────────────

def test_views_empty_for_type_without_base(vault):
    va = vault.dfs["Personen"].views
    assert len(va) == 0
    assert list(va) == []


def test_views_contains_expected_names(vault):
    va = vault.dfs["Projekte"].views
    assert "Projekte" in va


def test_views_getitem_unknown_raises(vault):
    with pytest.raises(KeyError):
        vault.dfs["Projekte"].views["NoSuchView"]


def test_view_column_selection(vault):
    """View with explicit order returns only those columns in that order."""
    tbl = vault.dfs["Projekte"].views["Projekte"].to_arrow()
    assert tbl.column_names == ["_record", "Titel", "Beginn", "Jahr", "BudgetLabel"]


def test_view_filter_applied(vault):
    """View with a filter returns only matching rows."""
    tbl = vault.dfs["Projekte"].views["Projekte"].to_arrow()
    assert tbl.num_rows == 2


def test_view_sort_applied(vault):
    """View with sort:DESC on Titel returns Beta before Alpha."""
    tbl = vault.dfs["Projekte"].views["Projekte"].to_arrow()
    records = tbl.column("_record").to_pylist()
    assert records == ["Beta", "Alpha"]


# ── to_parquet (TableAccessor) ─────────────────────────────────────────────

def test_to_parquet_creates_file(vault, tmp_path):
    out = tmp_path / "projekte.parquet"
    result = vault.dfs["Projekte"].to_parquet(out)
    assert result == out
    assert out.exists()
    assert out.stat().st_size > 0


def test_to_parquet_roundtrip(vault, tmp_path):
    import pyarrow.parquet as pq
    out = tmp_path / "projekte.parquet"
    tbl_orig = vault.dfs["Projekte"].to_arrow()
    vault.dfs["Projekte"].to_parquet(out)
    tbl_read = pq.read_table(str(out))
    assert tbl_read.column_names == tbl_orig.column_names
    assert tbl_read.num_rows == tbl_orig.num_rows


def test_to_parquet_preserves_types(vault, tmp_path):
    import pyarrow.parquet as pq
    out = tmp_path / "projekte.parquet"
    vault.dfs["Projekte"].to_parquet(out)
    tbl = pq.read_table(str(out))
    # Beginn is a date field — must survive as date32, not cast to string
    import pyarrow as pa
    assert pa.types.is_date(tbl.schema.field("Beginn").type)


# ── to_csv (TableAccessor) ─────────────────────────────────────────────────

def test_to_csv_creates_file(vault, tmp_path):
    out = tmp_path / "projekte.csv"
    result = vault.dfs["Projekte"].to_csv(out)
    assert result == out
    assert out.exists()
    assert out.stat().st_size > 0


def test_to_csv_readable(vault, tmp_path):
    import pyarrow.csv as pa_csv
    out = tmp_path / "projekte.csv"
    vault.dfs["Projekte"].to_csv(out)
    tbl = pa_csv.read_csv(str(out))
    assert "_record" in tbl.column_names
    assert tbl.num_rows == vault.dfs["Projekte"].to_arrow().num_rows


def test_to_csv_list_column_flattened(vault, tmp_path):
    # Personen has a list-typed link column — must not crash and must be a string in the CSV
    import pyarrow as pa
    import pyarrow.csv as pa_csv
    out = tmp_path / "personen.csv"
    vault.dfs["Personen"].to_csv(out)
    tbl = pa_csv.read_csv(str(out))
    for name in tbl.schema.names:
        assert not pa.types.is_list(tbl.schema.field(name).type)


# ── DfsAccessor.to_arrow ──────────────────────────────────────────────────

def test_dfs_to_arrow_returns_dict(vault):
    import pyarrow as pa
    tables = vault.dfs.to_arrow()
    assert isinstance(tables, dict)
    assert set(tables.keys()) == set(vault.schema.types.keys())
    for tbl in tables.values():
        assert isinstance(tbl, pa.Table)


def test_dfs_to_arrow_contains_records(vault):
    tables = vault.dfs.to_arrow()
    records = tables["Projekte"].column("_record").to_pylist()
    assert set(records) == {"Alpha", "Beta"}


# ── DfsAccessor.to_parquet ────────────────────────────────────────────────

def test_dfs_to_parquet_creates_directory(vault, tmp_path):
    out_dir = tmp_path / "parquet_export"
    result = vault.dfs.to_parquet(out_dir)
    assert out_dir.is_dir()
    assert isinstance(result, dict)
    assert set(result.keys()) == set(vault.schema.types.keys())


def test_dfs_to_parquet_files_exist(vault, tmp_path):
    out_dir = tmp_path / "parquet_export"
    result = vault.dfs.to_parquet(out_dir)
    for type_name, path in result.items():
        assert path == out_dir / f"{type_name}.parquet"
        assert path.exists()


def test_dfs_to_parquet_readable(vault, tmp_path):
    import pyarrow.parquet as pq
    out_dir = tmp_path / "parquet_export"
    vault.dfs.to_parquet(out_dir)
    tbl = pq.read_table(str(out_dir / "Projekte.parquet"))
    assert "_record" in tbl.column_names
