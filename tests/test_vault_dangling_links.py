"""Tests for dangling reference handling in Vault.from_vault() via dangling_refs parameter.

Fixture: dangling_vault
  Projekte/Alpha.md — Traeger: Anna (exists), Ghost (missing record in known type Personen)
                     — Partner: Phantom (missing scalar link in known type Personen)
  Projekte/Beta.md  — Foerderer: links to Foerderprogramme/ which is an unknown type entirely
  Personen/Anna.md  — exists
"""
from pathlib import Path

import pytest

from vaults import Vault

FIXTURE = Path(__file__).parent / "fixtures" / "dangling_vault"


def _alpha(vault):
    return vault.records["Projekte"][0]


def _beta(vault):
    return next(r for r in vault.records["Projekte"] if r.name == "Beta")


# --- default (drop) ---

def test_drop_is_default():
    v = Vault.from_vault(FIXTURE)
    traeger = _alpha(v).fields["Traeger"]
    assert not any("Ghost" in item for item in traeger)


def test_drop_removes_dangling_list_items():
    v = Vault.from_vault(FIXTURE, dangling_refs="drop")
    traeger = _alpha(v).fields["Traeger"]
    assert not any("Ghost" in item for item in traeger)


def test_drop_keeps_valid_list_items():
    v = Vault.from_vault(FIXTURE, dangling_refs="drop")
    traeger = _alpha(v).fields["Traeger"]
    assert any("Anna" in item for item in traeger)


def test_drop_nulls_dangling_scalar_link():
    v = Vault.from_vault(FIXTURE, dangling_refs="drop")
    assert _alpha(v).fields["Partner"] is None


def test_drop_removes_unknown_type_links():
    v = Vault.from_vault(FIXTURE, dangling_refs="drop")
    assert _beta(v).fields["Foerderer"] == []


def test_drop_does_not_add_unknown_type_to_schema():
    v = Vault.from_vault(FIXTURE, dangling_refs="drop")
    assert "Foerderprogramme" not in v.schema.types


# --- stub ---

def test_stub_adds_missing_record_in_known_type():
    v = Vault.from_vault(FIXTURE, dangling_refs="stub")
    names = {r.name for r in v.records["Personen"]}
    assert "Ghost" in names
    assert "Phantom" in names


def test_stub_record_has_empty_fields():
    v = Vault.from_vault(FIXTURE, dangling_refs="stub")
    ghost = next(r for r in v.records["Personen"] if r.name == "Ghost")
    assert ghost.fields == {}
    assert ghost.path is None


def test_stub_record_has_correct_type_schema():
    v = Vault.from_vault(FIXTURE, dangling_refs="stub")
    ghost = next(r for r in v.records["Personen"] if r.name == "Ghost")
    assert ghost.type == "Personen"
    assert ghost.type_schema is v.schema.types["Personen"]


def test_stub_does_not_duplicate():
    v = Vault.from_vault(FIXTURE, dangling_refs="stub")
    names = [r.name for r in v.records["Personen"]]
    assert names.count("Ghost") == 1


def test_stub_keeps_link_value_intact():
    v = Vault.from_vault(FIXTURE, dangling_refs="stub")
    traeger = _alpha(v).fields["Traeger"]
    assert any("Ghost" in item for item in traeger)


def test_stub_adds_unknown_type_to_schema():
    v = Vault.from_vault(FIXTURE, dangling_refs="stub")
    assert "Foerderprogramme" in v.schema.types


def test_stub_unknown_type_has_no_fields():
    v = Vault.from_vault(FIXTURE, dangling_refs="stub")
    assert v.schema.types["Foerderprogramme"].fields == []


def test_stub_adds_records_for_unknown_type():
    v = Vault.from_vault(FIXTURE, dangling_refs="stub")
    names = {r.name for r in v.records["Foerderprogramme"]}
    assert "NSP" in names
    assert "BKM" in names


def test_stub_unknown_type_record_has_empty_fields():
    v = Vault.from_vault(FIXTURE, dangling_refs="stub")
    nsp = next(r for r in v.records["Foerderprogramme"] if r.name == "NSP")
    assert nsp.fields == {}
    assert nsp.path is None


# --- nested file logging ---

def test_nested_file_triggers_warning(tmp_path, caplog):
    import shutil
    shutil.copytree(FIXTURE, tmp_path / "vault")
    nested_dir = tmp_path / "vault" / "data" / "Projekte" / "subdir"
    nested_dir.mkdir()
    (nested_dir / "Hidden.md").write_text("---\nTitel: Hidden\n---\n")

    import logging
    with caplog.at_level(logging.WARNING, logger="vaults.vault"):
        Vault.from_vault(tmp_path / "vault")

    assert any("Hidden.md" in r.message for r in caplog.records)


# --- invalid mode ---

def test_invalid_dangling_refs_raises():
    with pytest.raises(ValueError):
        Vault.from_vault(FIXTURE, dangling_refs="keep")


def test_invalid_dangling_refs_raises_on_garbage():
    with pytest.raises(ValueError):
        Vault.from_vault(FIXTURE, dangling_refs="invalid")
