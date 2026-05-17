from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ._adapters import _normalize_null, _to_column, _to_records
from ._writers import (
    _check_stale,
    _patch_frontmatter_property,
    _read_md_file,
    _resolve_suffix,
    _write_base_file,
    _write_md_file,
    sanitize_filename,
)

if TYPE_CHECKING:
    from ..vault import Vault

logger = logging.getLogger(__name__)


@dataclass
class EntryResult:
    filename: str
    status: Literal["processed", "warning", "skipped"]
    warning_types: list[str] = field(default_factory=list)


def _update_reverse_links(
    vault: "Vault",
    type_name: str,
    written: list[tuple[str, dict]],
    data_root: Path,
) -> None:
    """Write the reverse side of declared relationship pairs for newly added records."""
    from ..links import parse_wikilink

    for pair in vault.relationship_pairs:
        side_a, side_b = pair
        type_a, field_a = side_a.rsplit(".", 1)
        type_b, field_b = side_b.rsplit(".", 1)

        if type_name == type_a:
            our_field, their_type, their_field = field_a, type_b, field_b
        elif type_name == type_b:
            our_field, their_type, their_field = field_b, type_a, field_a
        else:
            continue

        for filename, fields in written:
            links = fields.get(our_field)
            if links is None:
                continue
            if not isinstance(links, list):
                links = [links]

            back_link = f"[[{type_name}/{filename}]]"

            for link in links:
                parsed = parse_wikilink(link)
                if parsed is None:
                    continue
                _, target_name = parsed
                target_path = data_root / their_type / f"{target_name}.md"
                if not target_path.exists():
                    continue

                existing_fields, existing_body = _read_md_file(target_path)
                existing = existing_fields.get(their_field) or []
                if not isinstance(existing, list):
                    existing = [existing] if existing else []

                if back_link not in existing:
                    existing_fields[their_field] = existing + [back_link]
                    _write_md_file(target_path, existing_fields, existing_body)


def _process_batch(
    normalized: list[dict],
    *,
    type_name: str,
    filename_col: str,
    known_fields: set[str] | None,
    disk_stems: set[str],
    allow_new_properties: bool,
    on_invalid: str,
    on_collision: str,
    on_missing_filename: str,
) -> tuple[
    list[tuple[int, str | None, str]],
    list["EntryResult | None"],
    list[tuple[int, str, dict, str]],
    list[str],
]:
    """Process a batch of normalized records; return (failures, results, to_write, all_props)."""
    batch_stems: set[str] = set()
    failures: list[tuple[int, str | None, str]] = []
    results: list[EntryResult | None] = [None] * len(normalized)
    to_write: list[tuple[int, str, dict, str]] = []
    new_props: dict[str, list[str]] = {}
    dropped_props: dict[str, list[str]] = {}
    seen_props: list[str] = []
    seen_props_set: set[str] = set()

    for idx, rec in enumerate(normalized):
        result = EntryResult(filename="", status="processed")

        raw_name = _normalize_null(rec.get(filename_col))
        if not raw_name:
            if on_missing_filename == "skip":
                result.status = "skipped"
                result.warning_types.append("skipped_missing_filename")
                logger.warning(
                    "add_records[%s]: record %d skipped — missing %r",
                    type_name, idx, filename_col,
                )
            else:
                failures.append((idx, None, f"missing value in filename column {filename_col!r}"))
            results[idx] = result
            continue

        sanitized, changed = sanitize_filename(str(raw_name))
        if changed:
            result.warning_types.append("sanitization")
            logger.warning(
                "add_records[%s]: record %d filename sanitized %r → %r",
                type_name, idx, raw_name, sanitized,
            )

        if sanitized in disk_stems or sanitized in batch_stems:
            if on_collision == "suffix":
                resolved = _resolve_suffix(sanitized, disk_stems | batch_stems)
                logger.warning(
                    "add_records[%s]: record %d collision %r → %r",
                    type_name, idx, sanitized, resolved,
                )
                sanitized = resolved
                result.warning_types.append("collision")
            else:
                failures.append((idx, sanitized, f"filename collision: {sanitized!r} already exists"))
                results[idx] = result
                continue

        batch_stems.add(sanitized)
        result.filename = sanitized

        fields = {k: _normalize_null(v) for k, v in rec.items() if k != filename_col}
        body = str(fields.pop("full_text", "") or "")

        if known_fields is not None:
            unknown = {k for k in fields if k not in known_fields}
            if unknown:
                if allow_new_properties:
                    for prop in unknown:
                        new_props.setdefault(prop, []).append(sanitized)
                    result.warning_types.append("new_property")
                else:
                    for prop in sorted(unknown):
                        dropped_props.setdefault(prop, []).append(sanitized)
                        del fields[prop]
                    result.warning_types.append("dropped_property")

        for prop in fields:
            if prop not in seen_props_set:
                seen_props.append(prop)
                seen_props_set.add(prop)

        if result.warning_types:
            result.status = "warning"

        to_write.append((idx, sanitized, fields, body))
        results[idx] = result

    for prop, names in new_props.items():
        logger.warning(
            "add_records[%s]: property %r is new; exists only in: %s. "
            "Use the janitor to propagate across all records.",
            type_name, prop, ", ".join(names),
        )
    for prop, names in dropped_props.items():
        logger.warning(
            "add_records[%s]: property %r dropped from: %s "
            "(not in schema for type %r).",
            type_name, prop, ", ".join(names), type_name,
        )

    return failures, results, to_write, seen_props


class Enrichment:
    def __init__(self, vault: "Vault") -> None:
        self._vault = vault

    def add_records(
        self,
        type_name: str,
        records,
        *,
        filename_col: str,
        allow_new_properties: bool = True,
        on_invalid: Literal["error", "skip"] = "error",
        on_collision: Literal["error", "suffix"] = "error",
        on_missing_filename: Literal["error", "skip"] = "error",
    ) -> list[EntryResult]:
        _check_stale(self._vault)
        normalized = _to_records(records)

        if type_name not in self._vault.schema.types:
            raise ValueError(
                f"Type {type_name!r} not found in schema. "
                f"Known types: {sorted(self._vault.schema.types)}"
            )

        type_schema = self._vault.schema.types[type_name]
        known_fields = {f.name for f in type_schema.fields}
        data_root = self._vault.path / self._vault.schema.data_folder
        type_dir = data_root / type_name
        disk_stems = {p.stem for p in type_dir.glob("*.md")}

        failures, results, to_write, _ = _process_batch(
            normalized,
            type_name=type_name,
            filename_col=filename_col,
            known_fields=known_fields,
            disk_stems=disk_stems,
            allow_new_properties=allow_new_properties,
            on_invalid=on_invalid,
            on_collision=on_collision,
            on_missing_filename=on_missing_filename,
        )

        if failures:
            if on_invalid == "error":
                lines = [
                    f"  [{idx}] {fname or '(no filename)'}: {reason}"
                    for idx, fname, reason in failures
                ]
                raise ValueError(
                    f"add_records[{type_name!r}]: {len(failures)} record(s) failed validation. "
                    f"Pass on_invalid='skip' to skip failed records.\n" + "\n".join(lines)
                )
            for idx, fname, reason in failures:
                r = results[idx]
                r.status = "skipped"
                r.warning_types.append("skipped_validation")
                logger.warning(
                    "add_records[%s]: record %d skipped — %s",
                    type_name, idx, reason,
                )

        written: list[tuple[str, dict]] = []
        for _, filename, fields, body in to_write:
            _write_md_file(type_dir / f"{filename}.md", fields, body)
            written.append((filename, fields))

        if written and self._vault.relationship_pairs:
            _update_reverse_links(self._vault, type_name, written, data_root)

        self._vault.reload()
        return [r for r in results if r is not None]

    def add_column(
        self,
        type_name: str,
        column,
        *,
        property_name: str,
        on_existing: Literal["error", "skip", "overwrite"] = "error",
        on_missing: Literal["error", "skip"] = "error",
    ) -> list[EntryResult]:
        _check_stale(self._vault)

        if type_name not in self._vault.schema.types:
            raise ValueError(
                f"Type {type_name!r} not found in schema. "
                f"Known types: {sorted(self._vault.schema.types)}"
            )

        col_dict = _to_column(column)
        records = self._vault.records.get(type_name, [])
        data_root = self._vault.path / self._vault.schema.data_folder
        type_dir = data_root / type_name

        missing = [r.name for r in records if r.name not in col_dict]
        existing = [
            r.name for r in records
            if r.name in col_dict and property_name in r.fields
        ]

        if missing and on_missing == "error":
            raise ValueError(
                f"add_column[{type_name!r}]: {len(missing)} record(s) not covered by column mapping. "
                f"Pass on_missing='skip' to leave them unchanged.\n"
                f"Unmatched filenames: {', '.join(missing)}"
            )
        if existing and on_existing == "error":
            raise ValueError(
                f"add_column[{type_name!r}]: property {property_name!r} already exists on "
                f"{len(existing)} record(s). Pass on_existing='skip' or 'overwrite'.\n"
                f"Affected filenames: {', '.join(existing)}"
            )

        results: list[EntryResult] = []

        for rec in records:
            result = EntryResult(filename=rec.name, status="processed")

            if rec.name not in col_dict:
                result.status = "skipped"
                result.warning_types.append("skipped_missing")
                logger.warning(
                    "add_column[%s]: %r skipped — not in column mapping",
                    type_name, rec.name,
                )
                results.append(result)
                continue

            value = _normalize_null(col_dict[rec.name])

            if property_name in rec.fields:
                if on_existing == "skip":
                    result.status = "skipped"
                    result.warning_types.append("skipped_existing")
                    logger.warning(
                        "add_column[%s]: %r skipped — property %r already exists",
                        type_name, rec.name, property_name,
                    )
                    results.append(result)
                    continue
                else:  # overwrite (error case raised above)
                    result.warning_types.append("overwrite")
                    logger.warning(
                        "add_column[%s]: %r overwriting %r",
                        type_name, rec.name, property_name,
                    )

            md_path = type_dir / f"{rec.name}.md"
            _patch_frontmatter_property(
                md_path, property_name, value,
                overwrite=(property_name in rec.fields),
            )

            if result.warning_types:
                result.status = "warning"

            results.append(result)

        self._vault.reload()
        return results

    def add_type(
        self,
        type_name: str,
        records,
        *,
        filename_col: str,
        allow_new_properties: bool = True,
        on_invalid: Literal["error", "skip"] = "error",
        on_collision: Literal["error", "suffix"] = "error",
        on_missing_filename: Literal["error", "skip"] = "error",
    ) -> list[EntryResult]:
        _check_stale(self._vault)
        normalized = _to_records(records)

        if type_name in self._vault.schema.types:
            raise ValueError(
                f"add_type[{type_name!r}]: type already exists in schema. "
                f"Use add_records to add rows to an existing type."
            )

        data_root = self._vault.path / self._vault.schema.data_folder
        bases_root = self._vault.path / self._vault.schema.bases_folder
        type_dir = data_root / type_name
        type_dir.mkdir(parents=True, exist_ok=True)

        failures, results, to_write, all_props = _process_batch(
            normalized,
            type_name=type_name,
            filename_col=filename_col,
            known_fields=None,
            disk_stems=set(),
            allow_new_properties=allow_new_properties,
            on_invalid=on_invalid,
            on_collision=on_collision,
            on_missing_filename=on_missing_filename,
        )

        if failures:
            if on_invalid == "error":
                lines = [
                    f"  [{idx}] {fname or '(no filename)'}: {reason}"
                    for idx, fname, reason in failures
                ]
                raise ValueError(
                    f"add_type[{type_name!r}]: {len(failures)} record(s) failed validation. "
                    f"Pass on_invalid='skip' to skip failed records.\n" + "\n".join(lines)
                )
            for idx, fname, reason in failures:
                r = results[idx]
                r.status = "skipped"
                r.warning_types.append("skipped_validation")
                logger.warning(
                    "add_type[%s]: record %d skipped — %s",
                    type_name, idx, reason,
                )

        _write_base_file(
            bases_root / f"{type_name}.base",
            type_name=type_name,
            props=all_props,
            data_folder=self._vault.schema.data_folder,
        )

        written: list[tuple[str, dict]] = []
        for _, filename, fields, body in to_write:
            _write_md_file(type_dir / f"{filename}.md", fields, body)
            written.append((filename, fields))

        self._vault.reload()
        return [r for r in results if r is not None]
