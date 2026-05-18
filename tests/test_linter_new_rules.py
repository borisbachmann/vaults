"""Tests for linter rules R-2 (type drift) and R-6 (orphaned file-level links)."""
from pathlib import Path

import pytest

from vaults import Vault

DANGLING = Path(__file__).parent / "fixtures" / "dangling_vault"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rules(violations):
    return {v.rule for v in violations}


def _violations_for(violations, rule):
    return [v for v in violations if v.rule == rule]


def _build_vault(tmp_path, types: dict[str, list[dict]]) -> Vault:
    """Create a minimal vault under tmp_path and load it.

    types: {type_name: [{"_name": filename_stem, **fields}, ...]}
    No bases folder is written; schema is inferred from data.
    """
    data = tmp_path / "data"
    for type_name, records in types.items():
        folder = data / type_name
        folder.mkdir(parents=True)
        for rec in records:
            name = rec.pop("_name")
            lines = ["---"]
            for k, v in rec.items():
                if isinstance(v, list):
                    lines.append(f"{k}:")
                    for item in v:
                        lines.append(f"  - '{item}'")
                elif isinstance(v, bool):
                    lines.append(f"{k}: {'true' if v else 'false'}")
                else:
                    lines.append(f"{k}: {v!r}")
            lines.append("---")
            (folder / f"{name}.md").write_text("\n".join(lines) + "\n")
    return Vault.from_vault(tmp_path)


# ---------------------------------------------------------------------------
# R-2: type drift
# ---------------------------------------------------------------------------

def test_r2_no_violation_on_consistent_type(tmp_path):
    vault = _build_vault(tmp_path, {
        "Items": [
            {"_name": "A", "count": 1},
            {"_name": "B", "count": 2},
        ]
    })
    assert "R-2" not in _rules(vault.lint())


def test_r2_fires_on_int_vs_str(tmp_path):
    vault = _build_vault(tmp_path, {
        "Items": [
            {"_name": "A", "count": 1},
            {"_name": "B", "count": "five"},
        ]
    })
    v2 = _violations_for(vault.lint(), "R-2")
    assert len(v2) == 1
    assert v2[0].field_name == "count"
    assert v2[0].type_name == "Items"


def test_r2_details_list_types_by_python_type(tmp_path):
    vault = _build_vault(tmp_path, {
        "Items": [
            {"_name": "A", "count": 1},
            {"_name": "B", "count": "five"},
        ]
    })
    details = _violations_for(vault.lint(), "R-2")[0].details
    assert "int" in details
    assert "str" in details
    assert "A" in details["int"]
    assert "B" in details["str"]


def test_r2_none_values_not_counted_as_type(tmp_path):
    # Records without the field at all should not be counted as a distinct type.
    vault = _build_vault(tmp_path, {
        "Items": [
            {"_name": "A", "count": 1},
            {"_name": "B", "count": 2},
            {"_name": "C"},  # no count field
        ]
    })
    assert "R-2" not in _rules(vault.lint())


def test_r2_date_vs_str_not_flagged(tmp_path):
    # YAML coerces "2026-11-21" to datetime.date but leaves "2026-05-MM" as str.
    # This split is a YAML artefact; R-2 must not fire for it.
    import datetime
    (tmp_path / "data" / "Events").mkdir(parents=True)
    (tmp_path / "data" / "Events" / "A.md").write_text("---\ndate: 2026-11-21\n---\n")
    (tmp_path / "data" / "Events" / "B.md").write_text("---\ndate: '2026-05-MM'\n---\n")
    vault = Vault.from_vault(tmp_path)
    # Verify the fixture actually produces a date/str split so the test is meaningful.
    recs = vault.records["Events"]
    types = {type(r.fields["date"]).__name__ for r in recs if "date" in r.fields}
    assert "date" in types and "str" in types, "fixture did not produce date/str split"
    assert "R-2" not in _rules(vault.lint())


def test_r2_bool_vs_int_detected(tmp_path):
    vault = _build_vault(tmp_path, {
        "Flags": [
            {"_name": "A", "active": True},
            {"_name": "B", "active": 1},
        ]
    })
    v2 = _violations_for(vault.lint(), "R-2")
    assert len(v2) == 1
    assert v2[0].field_name == "active"


def test_r2_no_violation_for_consistent_str_values(tmp_path):
    # Different string values but same type → no R-2.
    vault = _build_vault(tmp_path, {
        "Items": [
            {"_name": "A", "label": "alpha"},
            {"_name": "B", "label": "beta"},
        ]
    })
    assert "R-2" not in _rules(vault.lint())


# ---------------------------------------------------------------------------
# R-6: orphaned file-level links
# ---------------------------------------------------------------------------

def test_r6_no_violation_on_clean_vault():
    # sample_vault has no dangling links (it uses dangling_refs="drop" default).
    from pathlib import Path
    sample = Path(__file__).parent / "fixtures" / "sample_vault"
    vault = Vault.from_vault(sample)
    assert "R-6" not in _rules(vault.lint())


def test_r6_fires_for_missing_file_in_known_type():
    # With stub mode the dangling links are kept in memory but files don't exist.
    vault = Vault.from_vault(DANGLING, dangling_refs="stub")
    v6 = _violations_for(vault.lint(), "R-6")
    assert len(v6) > 0


def test_r6_identifies_correct_type():
    vault = Vault.from_vault(DANGLING, dangling_refs="stub")
    v6 = _violations_for(vault.lint(), "R-6")
    type_names = {v.type_name for v in v6}
    # Projekte/Alpha has Traeger → Ghost/Phantom in Personen (both missing)
    assert "Projekte" in type_names


def test_r6_details_contain_affected_records_and_links():
    vault = Vault.from_vault(DANGLING, dangling_refs="stub")
    v6 = _violations_for(vault.lint(), "R-6")
    projekte_v = next(v for v in v6 if v.type_name == "Projekte")
    inner = projekte_v.details["Projekte"]
    # Alpha links to Ghost and Phantom which don't exist on disk.
    # inner["Alpha"] is {field_name: [links]} — flatten values to get all links.
    assert "Alpha" in inner
    all_links = [link for links in inner["Alpha"].values() for link in links]
    assert any("Ghost" in link or "Phantom" in link for link in all_links)


def test_r6_not_emitted_for_links_dropped_from_memory():
    # With the default drop mode the dangling links are removed before lint sees them.
    vault = Vault.from_vault(DANGLING, dangling_refs="drop")
    assert "R-6" not in _rules(vault.lint())


def test_r6_not_fired_for_existing_link_target():
    # Anna.md exists on disk; the link to her must not produce R-6.
    vault = Vault.from_vault(DANGLING, dangling_refs="stub")
    v6 = _violations_for(vault.lint(), "R-6")
    # Flatten all orphaned link values from all violations
    orphaned_links = [
        link
        for v in v6
        for rec_fields in v.details.values()
        for field_links in rec_fields.values()
        for links in field_links.values()
        for link in links
    ]
    assert not any("Anna" in link for link in orphaned_links)


def test_r4_fires_pre_processing_r6_fires_post_processing():
    # R-4 is captured during from_vault (before dangling-ref processing), so it
    # appears in vault.violations. R-6 is only visible in a manual lint() call
    # after loading with stub mode. They do not overlap on the same link.
    vault_drop = Vault.from_vault(DANGLING, dangling_refs="drop")
    # R-4 was captured at load time (pre-drop) for links to unknown Foerderprogramme.
    assert "R-4" in _rules(vault_drop.violations)
    # Manual lint() after drop sees no dangling links → no R-6.
    assert "R-6" not in _rules(vault_drop.lint())

    vault_stub = Vault.from_vault(DANGLING, dangling_refs="stub")
    # With stub mode unknown types are added to schema, so R-4 does not fire
    # in the manual lint(); R-6 fires instead for the missing files.
    assert "R-6" in _rules(vault_stub.lint())
    assert "R-4" not in _rules(vault_stub.lint())
