"""Tests for Expander.add_type."""
import pytest
from pathlib import Path

from vaults import Vault


@pytest.fixture
def tmp_vault(tmp_path):
    data = tmp_path / "data"
    (data / "Items").mkdir(parents=True)
    (data / "Items" / "Existing.md").write_text("---\ntitle: Existing\n---\n")
    return Vault.from_vault(tmp_path)


# ── Happy path ─────────────────────────────────────────────────────────────────

def test_add_type_returns_results(tmp_vault):
    records = [{"name": "Alpha", "color": "red"}, {"name": "Beta", "color": "blue"}]
    results = tmp_vault.expand.add_type("Tags", records, filename_col="name")
    assert len(results) == 2
    assert all(r.status == "processed" for r in results)
    assert {r.filename for r in results} == {"Alpha", "Beta"}


def test_add_type_creates_folder(tmp_vault, tmp_path):
    tmp_vault.expand.add_type(
        "Tags", [{"name": "A", "color": "red"}], filename_col="name"
    )
    assert (tmp_path / "data" / "Tags").is_dir()


def test_add_type_writes_files(tmp_vault, tmp_path):
    tmp_vault.expand.add_type(
        "Tags", [{"name": "A", "color": "red"}], filename_col="name"
    )
    assert (tmp_path / "data" / "Tags" / "A.md").exists()
    content = (tmp_path / "data" / "Tags" / "A.md").read_text()
    assert "color: red" in content


def test_add_type_vault_reloaded(tmp_vault):
    tmp_vault.expand.add_type(
        "Tags", [{"name": "A", "color": "red"}], filename_col="name"
    )
    assert "Tags" in tmp_vault.records
    assert any(r.name == "A" for r in tmp_vault.records["Tags"])


def test_add_type_creates_base_file(tmp_vault, tmp_path):
    tmp_vault.expand.add_type(
        "Tags", [{"name": "A", "color": "red"}], filename_col="name"
    )
    base_path = tmp_path / "bases" / "Tags.base"
    assert base_path.exists()


def test_add_type_base_has_filter(tmp_vault, tmp_path):
    tmp_vault.expand.add_type(
        "Tags", [{"name": "A"}], filename_col="name"
    )
    content = (tmp_path / "bases" / "Tags.base").read_text()
    assert 'file.inFolder("data/Tags")' in content


def test_add_type_base_has_table_view(tmp_vault, tmp_path):
    tmp_vault.expand.add_type(
        "Tags", [{"name": "A", "color": "red"}], filename_col="name"
    )
    content = (tmp_path / "bases" / "Tags.base").read_text()
    assert "type: table" in content
    assert "name: Tags" in content


def test_add_type_base_order_has_file_name(tmp_vault, tmp_path):
    tmp_vault.expand.add_type(
        "Tags", [{"name": "A", "color": "red"}], filename_col="name"
    )
    content = (tmp_path / "bases" / "Tags.base").read_text()
    assert "file.name" in content


def test_add_type_base_order_includes_props(tmp_vault, tmp_path):
    tmp_vault.expand.add_type(
        "Tags", [{"name": "A", "color": "red", "priority": "high"}], filename_col="name"
    )
    content = (tmp_path / "bases" / "Tags.base").read_text()
    assert "color" in content
    assert "priority" in content


def test_add_type_base_file_name_before_props(tmp_vault, tmp_path):
    """file.name must appear before any other properties in the order list."""
    tmp_vault.expand.add_type(
        "Tags", [{"name": "A", "color": "red"}], filename_col="name"
    )
    content = (tmp_path / "bases" / "Tags.base").read_text()
    fn_pos = content.index("file.name")
    color_pos = content.index("color")
    assert fn_pos < color_pos


def test_add_type_preserves_input_order(tmp_vault):
    records = [{"name": "Z"}, {"name": "A"}, {"name": "M"}]
    results = tmp_vault.expand.add_type("Tags", records, filename_col="name")
    assert [r.filename for r in results] == ["Z", "A", "M"]


def test_add_type_null_renders_as_bare_key(tmp_vault, tmp_path):
    tmp_vault.expand.add_type(
        "Tags", [{"name": "A", "optional": None}], filename_col="name"
    )
    content = (tmp_path / "data" / "Tags" / "A.md").read_text()
    assert "null" not in content.lower()
    assert "optional:" in content


# ── Error: type already exists ─────────────────────────────────────────────────

def test_add_type_raises_if_type_exists(tmp_vault):
    with pytest.raises(ValueError, match="already exists"):
        tmp_vault.expand.add_type(
            "Items", [{"name": "X"}], filename_col="name"
        )


def test_add_type_raises_writes_nothing_if_type_exists(tmp_vault, tmp_path):
    with pytest.raises(ValueError):
        tmp_vault.expand.add_type(
            "Items", [{"name": "X"}], filename_col="name"
        )
    assert not (tmp_path / "data" / "Items" / "X.md").exists()


# ── on_missing_filename ────────────────────────────────────────────────────────

def test_add_type_missing_filename_error(tmp_vault):
    with pytest.raises(ValueError, match="missing value"):
        tmp_vault.expand.add_type(
            "Tags", [{"name": None}], filename_col="name"
        )


def test_add_type_missing_filename_skip(tmp_vault):
    records = [{"name": None}, {"name": "Good"}]
    results = tmp_vault.expand.add_type(
        "Tags", records, filename_col="name", on_missing_filename="skip"
    )
    assert results[0].status == "skipped"
    assert "skipped_missing_filename" in results[0].warning_types
    assert results[1].status == "processed"


# ── on_collision ───────────────────────────────────────────────────────────────

def test_add_type_collision_within_batch_suffix(tmp_vault):
    records = [{"name": "Dup", "x": "1"}, {"name": "Dup", "x": "2"}]
    results = tmp_vault.expand.add_type(
        "Tags", records, filename_col="name", on_collision="suffix"
    )
    filenames = {r.filename for r in results}
    assert "Dup" in filenames
    assert "Dup_2" in filenames


def test_add_type_collision_within_batch_error(tmp_vault):
    records = [{"name": "Dup"}, {"name": "Dup"}]
    with pytest.raises(ValueError, match="collision"):
        tmp_vault.expand.add_type("Tags", records, filename_col="name")


# ── on_invalid ─────────────────────────────────────────────────────────────────

def test_add_type_on_invalid_error_writes_nothing(tmp_vault, tmp_path):
    records = [{"name": "Good"}, {"name": None}]
    with pytest.raises(ValueError):
        tmp_vault.expand.add_type("Tags", records, filename_col="name")
    assert not (tmp_path / "data" / "Tags" / "Good.md").exists()


def test_add_type_on_invalid_skip(tmp_vault, tmp_path):
    records = [{"name": "Good", "x": "1"}, {"name": None}]
    results = tmp_vault.expand.add_type(
        "Tags", records, filename_col="name", on_invalid="skip"
    )
    assert results[0].status == "processed"
    assert results[1].status == "skipped"
    assert (tmp_path / "data" / "Tags" / "Good.md").exists()


# ── Guard conditions ───────────────────────────────────────────────────────────

def test_stale_vault_raises(tmp_vault, tmp_path):
    (tmp_path / "data" / "Items" / "External.md").write_text("---\ntitle: X\n---\n")
    with pytest.raises(RuntimeError, match="stale"):
        tmp_vault.expand.add_type(
            "Tags", [{"name": "A"}], filename_col="name"
        )


# ── pd.Series input ────────────────────────────────────────────────────────────

def test_add_type_pandas_dataframe(tmp_vault, tmp_path):
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame([{"name": "A", "color": "red"}, {"name": "B", "color": "blue"}])
    tmp_vault.expand.add_type("Tags", df, filename_col="name")
    assert (tmp_path / "data" / "Tags" / "A.md").exists()
    assert (tmp_path / "data" / "Tags" / "B.md").exists()
