"""Tests for Vault staleness fingerprint (Step 3)."""
import time
from pathlib import Path

import pytest

from vaults import Vault

FIXTURE = Path(__file__).parent / "fixtures" / "sample_vault"


def test_fingerprint_set_on_load():
    v = Vault.from_vault(FIXTURE)
    assert v.fingerprint != ""


def test_not_stale_immediately_after_load():
    v = Vault.from_vault(FIXTURE)
    assert v.is_stale() is False


def test_stale_after_file_modified(tmp_path):
    import shutil
    shutil.copytree(FIXTURE, tmp_path / "vault")
    vault_path = tmp_path / "vault"

    v = Vault.from_vault(vault_path)
    assert v.is_stale() is False

    md_file = vault_path / "data" / "Projekte" / "Alpha.md"
    md_file.write_text(md_file.read_text() + "\n", encoding="utf-8")

    assert v.is_stale() is True


def test_stale_after_file_added(tmp_path):
    import shutil
    shutil.copytree(FIXTURE, tmp_path / "vault")
    vault_path = tmp_path / "vault"

    v = Vault.from_vault(vault_path)
    new_file = vault_path / "data" / "Projekte" / "Gamma.md"
    new_file.write_text("---\nTitel: Gamma\n---\n", encoding="utf-8")

    assert v.is_stale() is True


def test_stale_returns_false_without_path():
    from vaults import Schema
    v = Vault(schema=Schema())
    assert v.is_stale() is False


def test_fingerprint_not_in_repr():
    v = Vault.from_vault(FIXTURE)
    assert "fingerprint" not in repr(v)
