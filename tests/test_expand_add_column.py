"""Tests for Expander.add_column."""
import pytest
from pathlib import Path

from vaults import Vault


@pytest.fixture
def tmp_vault(tmp_path):
    data = tmp_path / "data"
    (data / "Items").mkdir(parents=True)
    for name, title in [("Alpha", "Alpha Title"), ("Beta", "Beta Title"), ("Gamma", "Gamma Title")]:
        (data / "Items" / f"{name}.md").write_text(f"---\ntitle: {title}\n---\n")
    return Vault.from_vault(tmp_path)


# ── Happy path ─────────────────────────────────────────────────────────────────

def test_add_column_returns_results(tmp_vault):
    col = {"Alpha": "red", "Beta": "blue", "Gamma": "green"}
    results = tmp_vault.expand.add_column("Items", col, property_name="color")
    assert len(results) == 3
    assert all(r.status == "processed" for r in results)
    assert {r.filename for r in results} == {"Alpha", "Beta", "Gamma"}


def test_add_column_written_to_disk(tmp_vault, tmp_path):
    col = {"Alpha": "red", "Beta": "blue", "Gamma": "green"}
    tmp_vault.expand.add_column("Items", col, property_name="color")
    for name, color in [("Alpha", "red"), ("Beta", "blue"), ("Gamma", "green")]:
        content = (tmp_path / "data" / "Items" / f"{name}.md").read_text()
        assert f"color: {color}" in content


def test_add_column_vault_reloaded(tmp_vault):
    col = {"Alpha": "1", "Beta": "2", "Gamma": "3"}
    tmp_vault.expand.add_column("Items", col, property_name="rank")
    recs = {r.name: r for r in tmp_vault.records["Items"]}
    assert recs["Alpha"].fields["rank"] == "1"
    assert recs["Beta"].fields["rank"] == "2"


def test_add_column_preserves_key_order(tmp_vault, tmp_path):
    """New property is appended after existing keys, not reordered."""
    col = {"Alpha": "x", "Beta": "x", "Gamma": "x"}
    tmp_vault.expand.add_column("Items", col, property_name="new_prop")
    content = (tmp_path / "data" / "Items" / "Alpha.md").read_text()
    title_pos = content.index("title:")
    new_pos = content.index("new_prop:")
    assert title_pos < new_pos


def test_add_column_null_renders_as_bare_key(tmp_vault, tmp_path):
    col = {"Alpha": None, "Beta": None, "Gamma": None}
    tmp_vault.expand.add_column("Items", col, property_name="optional")
    content = (tmp_path / "data" / "Items" / "Alpha.md").read_text()
    assert "null" not in content.lower()
    assert "optional:" in content


def test_add_column_result_order_matches_record_order(tmp_vault):
    col = {"Alpha": 1, "Beta": 2, "Gamma": 3}
    results = tmp_vault.expand.add_column("Items", col, property_name="n")
    # Results appear in vault record order, not input dict order
    assert [r.filename for r in results] == [r.name for r in tmp_vault.records["Items"]]


# ── on_existing ────────────────────────────────────────────────────────────────

def test_on_existing_error_default(tmp_vault):
    """Raises on the first call when property already exists on any record."""
    col = {"Alpha": "x", "Beta": "x", "Gamma": "x"}
    with pytest.raises(ValueError, match="already exists"):
        tmp_vault.expand.add_column("Items", col, property_name="title")


def test_on_existing_error_lists_affected(tmp_vault):
    col = {"Alpha": "x", "Beta": "x", "Gamma": "x"}
    with pytest.raises(ValueError) as exc:
        tmp_vault.expand.add_column("Items", col, property_name="title")
    msg = str(exc.value)
    assert "Alpha" in msg or "Beta" in msg


def test_on_existing_error_writes_nothing(tmp_vault, tmp_path):
    """With on_existing='error', nothing is written if any record has the property."""
    col = {"Alpha": "new_alpha", "Beta": "new_beta", "Gamma": "new_gamma"}
    with pytest.raises(ValueError):
        tmp_vault.expand.add_column("Items", col, property_name="title")
    # Original values preserved
    content = (tmp_path / "data" / "Items" / "Alpha.md").read_text()
    assert "Alpha Title" in content
    assert "new_alpha" not in content


def test_on_existing_skip(tmp_vault, tmp_path):
    """Records with existing property are skipped; others untouched."""
    col = {"Alpha": "new_alpha", "Beta": "new_beta", "Gamma": "new_gamma"}
    results = tmp_vault.expand.add_column(
        "Items", col, property_name="title", on_existing="skip",
    )
    assert all(r.status == "skipped" for r in results)
    assert all("skipped_existing" in r.warning_types for r in results)
    # Original titles preserved
    content = (tmp_path / "data" / "Items" / "Alpha.md").read_text()
    assert "Alpha Title" in content


def test_on_existing_overwrite(tmp_vault, tmp_path):
    """Existing property values are replaced."""
    col = {"Alpha": "new_alpha", "Beta": "new_beta", "Gamma": "new_gamma"}
    results = tmp_vault.expand.add_column(
        "Items", col, property_name="title", on_existing="overwrite",
    )
    assert all("overwrite" in r.warning_types for r in results)
    assert all(r.status == "warning" for r in results)
    content = (tmp_path / "data" / "Items" / "Alpha.md").read_text()
    assert "new_alpha" in content


# ── on_missing ─────────────────────────────────────────────────────────────────

def test_on_missing_error_default(tmp_vault):
    col = {"Alpha": "x", "Beta": "x"}  # Gamma missing
    with pytest.raises(ValueError, match="not covered"):
        tmp_vault.expand.add_column("Items", col, property_name="color")


def test_on_missing_error_lists_unmatched(tmp_vault):
    col = {"Alpha": "x"}  # Beta and Gamma missing
    with pytest.raises(ValueError) as exc:
        tmp_vault.expand.add_column("Items", col, property_name="color")
    assert "Beta" in str(exc.value) or "Gamma" in str(exc.value)


def test_on_missing_error_writes_nothing(tmp_vault, tmp_path):
    col = {"Alpha": "x", "Beta": "x"}  # Gamma missing
    with pytest.raises(ValueError):
        tmp_vault.expand.add_column("Items", col, property_name="color")
    content = (tmp_path / "data" / "Items" / "Alpha.md").read_text()
    assert "color:" not in content


def test_on_missing_skip(tmp_vault, tmp_path):
    col = {"Alpha": "red", "Beta": "blue"}  # Gamma missing
    results = tmp_vault.expand.add_column(
        "Items", col, property_name="color", on_missing="skip"
    )
    skipped = [r for r in results if r.status == "skipped"]
    assert len(skipped) == 1
    assert skipped[0].filename == "Gamma"
    assert "skipped_missing" in skipped[0].warning_types
    assert (tmp_path / "data" / "Items" / "Alpha.md").read_text().count("color: red") == 1
    assert "color:" not in (tmp_path / "data" / "Items" / "Gamma.md").read_text()


# ── Guard conditions ───────────────────────────────────────────────────────────

def test_stale_vault_raises(tmp_vault, tmp_path):
    (tmp_path / "data" / "Items" / "External.md").write_text("---\ntitle: X\n---\n")
    with pytest.raises(RuntimeError, match="stale"):
        tmp_vault.expand.add_column(
            "Items", {"Alpha": "x", "Beta": "x", "Gamma": "x"},
            property_name="color",
        )


def test_unknown_type_raises(tmp_vault):
    with pytest.raises(ValueError, match="not found in schema"):
        tmp_vault.expand.add_column(
            "NoSuchType", {"Alpha": "x"}, property_name="color"
        )


# ── pd.Series input ────────────────────────────────────────────────────────────

def test_pandas_series_input(tmp_vault, tmp_path):
    pd = pytest.importorskip("pandas")
    col = pd.Series({"Alpha": "red", "Beta": "blue", "Gamma": "green"})
    tmp_vault.expand.add_column("Items", col, property_name="color")
    content = (tmp_path / "data" / "Items" / "Alpha.md").read_text()
    assert "color: red" in content
