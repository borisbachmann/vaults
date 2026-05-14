"""Tests for Vault.to_db() — DuckDB export.

Fixture: sample_vault
  Projekte/Alpha.md — Titel, Beginn (date), Abgeschlossen (bool), Budget (int),
                      Traeger (LIST_LINKS → Personen), Staedte (LIST_STRINGS), Erstellt (datetime),
                      full_text (document body)
  Projekte/Beta.md  — same fields; Traeger links to Clara (missing → stub)
  Personen/Anna.md  — Name, Partei (LINK → Parteien stub), Aktiv
  Personen/Ben.md   — same
"""
from pathlib import Path

import pytest

from vaults import Vault

FIXTURE = Path(__file__).parent / "fixtures" / "sample_vault"


@pytest.fixture
def con():
    v = Vault.from_vault(FIXTURE, dangling_refs="stub")
    return v.to_db()


@pytest.fixture
def con_drop():
    v = Vault.from_vault(FIXTURE, dangling_refs="drop")
    return v.to_db()


@pytest.fixture
def con_pairs():
    v = Vault.from_vault(
        FIXTURE,
        dangling_refs="stub",
        relationship_pairs=[("Projekte.Traeger", "Personen.Projekte")],
    )
    return v.to_db()


# --- table creation ---

def test_type_tables_created(con):
    tables = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_type='BASE TABLE'"
    ).fetchall()}
    assert "Projekte" in tables
    assert "Personen" in tables


def test_join_table_created(con):
    tables = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_type='BASE TABLE'"
    ).fetchall()}
    assert "Projekte__Traeger" in tables
    assert "Personen__Partei" in tables


def test_link_field_excluded_from_main_table(con):
    cols = {r[0] for r in con.execute('DESCRIBE "Projekte"').fetchall()}
    assert "Traeger" not in cols


def test_scalar_fields_in_main_table(con):
    cols = {r[0] for r in con.execute('DESCRIBE "Projekte"').fetchall()}
    assert {"record", "Titel", "Beginn", "Abgeschlossen", "Budget", "Staedte", "Erstellt", "full_text"} <= cols


# --- column types ---

def _col_types(con, table):
    return {r[0]: r[1] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}


def test_string_column_is_varchar(con):
    assert _col_types(con, "Projekte")["Titel"] == "VARCHAR"


def test_date_column_is_date(con):
    assert _col_types(con, "Projekte")["Beginn"] == "DATE"


def test_datetime_column_is_timestamp(con):
    assert _col_types(con, "Projekte")["Erstellt"] == "TIMESTAMP"


def test_boolean_column_is_boolean(con):
    assert _col_types(con, "Projekte")["Abgeschlossen"] == "BOOLEAN"


def test_integer_number_column_is_bigint(con):
    assert _col_types(con, "Projekte")["Budget"] == "BIGINT"


def test_list_strings_column_is_varchar_array(con):
    assert _col_types(con, "Projekte")["Staedte"] == "VARCHAR[]"


def test_record_is_primary_key(con):
    desc = {r[0]: r[3] for r in con.execute('DESCRIBE "Projekte"').fetchall()}
    assert desc["record"] == "PRI"


# --- main table data ---

def test_main_table_record_count(con):
    count = con.execute('SELECT COUNT(*) FROM "Projekte"').fetchone()[0]
    assert count == 2


def test_main_table_values(con):
    row = con.execute('SELECT "Titel", "Budget" FROM "Projekte" WHERE record = \'Alpha\'').fetchone()
    assert row == ("Alpha-Projekt", 50000)


def test_date_value_loaded(con):
    import datetime
    row = con.execute('SELECT "Beginn" FROM "Projekte" WHERE record = \'Alpha\'').fetchone()
    assert row[0] == datetime.date(2021, 3, 1)


def test_list_strings_value_loaded(con):
    row = con.execute('SELECT "Staedte" FROM "Projekte" WHERE record = \'Alpha\'').fetchone()
    assert set(row[0]) == {"Berlin", "Hamburg"}


def test_full_text_value_loaded(con):
    row = con.execute('SELECT "full_text" FROM "Projekte" WHERE record = \'Alpha\'').fetchone()
    assert "Volltext" in row[0]


def test_stub_record_has_null_fields(con):
    row = con.execute('SELECT "Name" FROM "Personen" WHERE record = \'Clara\'').fetchone()
    assert row[0] is None


# --- join table data ---

def test_join_table_rows(con):
    rows = {(r[0], r[1]) for r in con.execute('SELECT source, target FROM "Projekte__Traeger"').fetchall()}
    assert ("Alpha", "Anna") in rows
    assert ("Alpha", "Ben") in rows
    assert ("Beta", "Clara") in rows


def test_join_table_no_duplicates(con):
    count = con.execute('SELECT COUNT(*) FROM "Projekte__Traeger"').fetchone()[0]
    distinct = con.execute('SELECT COUNT(*) FROM (SELECT DISTINCT source, target FROM "Projekte__Traeger")').fetchone()[0]
    assert count == distinct


def test_drop_excludes_dangling_links(con_drop):
    rows = {(r[0], r[1]) for r in con_drop.execute('SELECT source, target FROM "Projekte__Traeger"').fetchall()}
    assert ("Beta", "Clara") not in rows
    assert ("Alpha", "Anna") in rows


# --- relationship pairs ---

def test_pair_creates_only_one_base_table(con_pairs):
    tables = {r[0] for r in con_pairs.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_type='BASE TABLE'"
    ).fetchall()}
    assert "Projekte__Traeger" in tables
    assert "Personen__Projekte" not in tables


def test_pair_creates_reverse_view(con_pairs):
    views = {r[0] for r in con_pairs.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'"
    ).fetchall()}
    assert "Personen__Projekte" in views


def test_pair_view_swaps_source_and_target(con_pairs):
    base = {(r[0], r[1]) for r in con_pairs.execute('SELECT source, target FROM "Projekte__Traeger"').fetchall()}
    view = {(r[0], r[1]) for r in con_pairs.execute('SELECT source, target FROM "Personen__Projekte"').fetchall()}
    assert view == {(t, s) for s, t in base}


# --- formula fields ---

def test_formula_year_column_is_bigint(con):
    """Jahr formula (Beginn.year) extracts an integer → BIGINT column."""
    assert _col_types(con, "Projekte")["Jahr"] == "BIGINT"


def test_formula_year_values(con):
    """Extracted year matches the Beginn date field for each record."""
    rows = {r[0]: r[1] for r in con.execute('SELECT record, "Jahr" FROM "Projekte"').fetchall()}
    assert rows["Alpha"] == 2021
    assert rows["Beta"] == 2022
