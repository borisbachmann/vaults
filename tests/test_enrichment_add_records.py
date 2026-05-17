"""Tests for Enrichment.add_records (Phase 1)."""
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

def test_add_records_returns_results(tmp_vault):
    records = [{"name": "Apple", "title": "Apple"}, {"name": "Banana", "title": "Banana"}]
    results = tmp_vault.enrichment.add_records("Items", records, filename_col="name")
    assert len(results) == 2
    assert all(r.status == "processed" for r in results)
    assert results[0].filename == "Apple"
    assert results[1].filename == "Banana"


def test_add_records_files_on_disk(tmp_vault, tmp_path):
    tmp_vault.enrichment.add_records(
        "Items", [{"name": "NewItem", "title": "New"}], filename_col="name"
    )
    assert (tmp_path / "data" / "Items" / "NewItem.md").exists()


def test_add_records_vault_reloaded(tmp_vault):
    before = len(tmp_vault.records["Items"])
    tmp_vault.enrichment.add_records(
        "Items", [{"name": "Fresh", "title": "Fresh"}], filename_col="name"
    )
    assert len(tmp_vault.records["Items"]) == before + 1
    assert any(r.name == "Fresh" for r in tmp_vault.records["Items"])


def test_add_records_preserves_input_order(tmp_vault):
    records = [
        {"name": "Z", "title": "Z"},
        {"name": "A", "title": "A"},
        {"name": "M", "title": "M"},
    ]
    results = tmp_vault.enrichment.add_records("Items", records, filename_col="name")
    assert [r.filename for r in results] == ["Z", "A", "M"]


# ── Missing filename ───────────────────────────────────────────────────────────

def test_missing_filename_error_default(tmp_vault):
    with pytest.raises(ValueError, match="missing value"):
        tmp_vault.enrichment.add_records(
            "Items", [{"name": None, "title": "Bad"}], filename_col="name"
        )


def test_missing_filename_error_lists_indices(tmp_vault):
    records = [{"name": None}, {"name": "Good"}, {"name": None}]
    with pytest.raises(ValueError) as exc:
        tmp_vault.enrichment.add_records("Items", records, filename_col="name")
    msg = str(exc.value)
    assert "[0]" in msg
    assert "[2]" in msg


def test_missing_filename_skip(tmp_vault):
    records = [{"name": None, "title": "Bad"}, {"name": "Good", "title": "Good"}]
    results = tmp_vault.enrichment.add_records(
        "Items", records, filename_col="name", on_missing_filename="skip"
    )
    assert results[0].status == "skipped"
    assert "skipped_missing_filename" in results[0].warning_types
    assert results[1].status == "processed"
    assert any(r.name == "Good" for r in tmp_vault.records["Items"])


def test_missing_filename_error_atomic_writes_nothing(tmp_vault, tmp_path):
    records = [{"name": "Good"}, {"name": None}]
    with pytest.raises(ValueError):
        tmp_vault.enrichment.add_records("Items", records, filename_col="name")
    assert not (tmp_path / "data" / "Items" / "Good.md").exists()


# ── Collision ──────────────────────────────────────────────────────────────────

def test_collision_error_default(tmp_vault):
    with pytest.raises(ValueError, match="collision"):
        tmp_vault.enrichment.add_records(
            "Items", [{"name": "Existing", "title": "Dup"}], filename_col="name"
        )


def test_collision_error_lists_details(tmp_vault):
    with pytest.raises(ValueError) as exc:
        tmp_vault.enrichment.add_records(
            "Items", [{"name": "Existing"}], filename_col="name"
        )
    assert "Existing" in str(exc.value)


def test_collision_suffix(tmp_vault):
    results = tmp_vault.enrichment.add_records(
        "Items", [{"name": "Existing", "title": "Dup"}],
        filename_col="name", on_collision="suffix",
    )
    assert results[0].filename == "Existing_2"
    assert "collision" in results[0].warning_types
    assert results[0].status == "warning"
    assert any(r.name == "Existing_2" for r in tmp_vault.records["Items"])


def test_collision_suffix_batch_internal(tmp_vault):
    """Two same-named records in one batch both get resolved."""
    records = [{"name": "Dup", "title": "First"}, {"name": "Dup", "title": "Second"}]
    results = tmp_vault.enrichment.add_records(
        "Items", records, filename_col="name", on_collision="suffix"
    )
    filenames = {r.filename for r in results}
    assert "Dup" in filenames
    assert "Dup_2" in filenames


def test_collision_error_batch_internal(tmp_vault):
    records = [{"name": "Dup"}, {"name": "Dup"}]
    with pytest.raises(ValueError, match="collision"):
        tmp_vault.enrichment.add_records("Items", records, filename_col="name")


# ── Sanitization ───────────────────────────────────────────────────────────────

def test_sanitization_obsidian_chars(tmp_vault):
    results = tmp_vault.enrichment.add_records(
        "Items", [{"name": "Bad#Name", "title": "T"}], filename_col="name"
    )
    assert results[0].filename == "Bad_Name"
    assert "sanitization" in results[0].warning_types
    assert results[0].status == "warning"
    assert any(r.name == "Bad_Name" for r in tmp_vault.records["Items"])


def test_sanitization_no_change_no_warning(tmp_vault):
    results = tmp_vault.enrichment.add_records(
        "Items", [{"name": "CleanName", "title": "T"}], filename_col="name"
    )
    assert results[0].warning_types == []
    assert results[0].status == "processed"


# ── allow_new_properties ───────────────────────────────────────────────────────

def test_new_property_allowed(tmp_vault, tmp_path):
    tmp_vault.enrichment.add_records(
        "Items", [{"name": "P", "title": "T", "extra": "val"}], filename_col="name"
    )
    content = (tmp_path / "data" / "Items" / "P.md").read_text()
    assert "extra:" in content


def test_new_property_warning(tmp_vault):
    results = tmp_vault.enrichment.add_records(
        "Items", [{"name": "P", "title": "T", "extra": "val"}], filename_col="name"
    )
    assert "new_property" in results[0].warning_types


def test_new_property_disallowed(tmp_vault, tmp_path):
    tmp_vault.enrichment.add_records(
        "Items", [{"name": "P", "title": "T", "extra": "val"}],
        filename_col="name", allow_new_properties=False,
    )
    content = (tmp_path / "data" / "Items" / "P.md").read_text()
    assert "extra:" not in content


def test_new_property_disallowed_warning(tmp_vault):
    results = tmp_vault.enrichment.add_records(
        "Items", [{"name": "P", "title": "T", "extra": "val"}],
        filename_col="name", allow_new_properties=False,
    )
    assert "dropped_property" in results[0].warning_types


# ── on_invalid ─────────────────────────────────────────────────────────────────

def test_on_invalid_error_writes_nothing(tmp_vault, tmp_path):
    records = [{"name": "Good", "title": "G"}, {"name": None, "title": "Bad"}]
    with pytest.raises(ValueError):
        tmp_vault.enrichment.add_records("Items", records, filename_col="name")
    assert not (tmp_path / "data" / "Items" / "Good.md").exists()


def test_on_invalid_skip_writes_good_records(tmp_vault, tmp_path):
    records = [{"name": "Good", "title": "G"}, {"name": None, "title": "Bad"}]
    results = tmp_vault.enrichment.add_records(
        "Items", records, filename_col="name", on_invalid="skip"
    )
    assert results[0].status in ("processed", "warning")
    assert results[1].status == "skipped"
    assert "skipped_validation" in results[1].warning_types
    assert (tmp_path / "data" / "Items" / "Good.md").exists()


def test_on_invalid_skip_collision(tmp_vault, tmp_path):
    """on_collision='error' + on_invalid='skip' → colliding record skipped."""
    records = [{"name": "Existing"}, {"name": "NewOne"}]
    results = tmp_vault.enrichment.add_records(
        "Items", records, filename_col="name",
        on_collision="error", on_invalid="skip",
    )
    assert results[0].status == "skipped"
    assert "skipped_validation" in results[0].warning_types
    assert results[1].status == "processed"
    assert (tmp_path / "data" / "Items" / "NewOne.md").exists()


# ── Null contract ──────────────────────────────────────────────────────────────

def test_null_renders_as_bare_key(tmp_vault, tmp_path):
    tmp_vault.enrichment.add_records(
        "Items", [{"name": "NullTest", "title": None}], filename_col="name"
    )
    content = (tmp_path / "data" / "Items" / "NullTest.md").read_text()
    assert "null" not in content.lower()
    assert "title:" in content


def test_full_text_written_as_body(tmp_vault, tmp_path):
    tmp_vault.enrichment.add_records(
        "Items", [{"name": "BodyTest", "title": "T", "full_text": "Hello body."}],
        filename_col="name",
    )
    content = (tmp_path / "data" / "Items" / "BodyTest.md").read_text()
    assert "full_text:" not in content
    assert "Hello body." in content


def test_full_text_not_in_frontmatter(tmp_vault, tmp_path):
    tmp_vault.enrichment.add_records(
        "Items", [{"name": "BodyTest2", "full_text": "Some text."}],
        filename_col="name",
    )
    # full_text should be after the closing ---
    content = (tmp_path / "data" / "Items" / "BodyTest2.md").read_text()
    closing = content.index("---\n", 4)  # skip opening ---
    assert "full_text:" not in content[:closing]


# ── Guard conditions ───────────────────────────────────────────────────────────

def test_stale_vault_raises(tmp_vault, tmp_path):
    (tmp_path / "data" / "Items" / "External.md").write_text("---\ntitle: X\n---\n")
    with pytest.raises(RuntimeError, match="stale"):
        tmp_vault.enrichment.add_records(
            "Items", [{"name": "T", "title": "T"}], filename_col="name"
        )


def test_unknown_type_raises(tmp_vault):
    with pytest.raises(ValueError, match="not found in schema"):
        tmp_vault.enrichment.add_records(
            "NoSuchType", [{"name": "X"}], filename_col="name"
        )


# ── Paired links ───────────────────────────────────────────────────────────────

def test_paired_link_reverse_written(tmp_path):
    """Adding a record with a link field updates the reverse side on disk."""
    data = tmp_path / "data"
    (data / "Projects").mkdir(parents=True)
    (data / "People").mkdir(parents=True)
    (data / "Projects" / "Alpha.md").write_text(
        "---\ntitle: Alpha\nMembers:\n---\n"
    )
    (data / "People" / "Anna.md").write_text(
        "---\nname: Anna\nProjects:\n---\n"
    )
    vault = Vault.from_vault(
        tmp_path,
        relationship_pairs=[("Projects.Members", "People.Projects")],
        dangling_refs="stub",
    )

    vault.enrichment.add_records(
        "Projects",
        [{"filename": "Beta", "title": "Beta", "Members": "[[People/Anna]]"}],
        filename_col="filename",
    )

    anna_content = (data / "People" / "Anna.md").read_text()
    assert "[[Projects/Beta]]" in anna_content
