from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .adapters import _normalize_null, _to_column, _to_records
from .io import (
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
    """
    Result for a single record processed by an expander write operation.

    Parameters
    ----------
    filename : str
        The filename stem that was written (or attempted). Empty string when
        the record was skipped before a filename could be determined.
    status : {"processed", "warning", "skipped"}
        Overall outcome. ``"processed"`` means the record was written cleanly;
        ``"warning"`` means it was written but with at least one advisory
        condition (see ``warning_types``); ``"skipped"`` means it was not
        written.
    warning_types : list of str
        Advisory codes describing what happened. May include ``"sanitization"``,
        ``"collision"``, ``"new_property"``, ``"dropped_property"``,
        ``"overwrite"``, ``"skipped_missing"``, ``"skipped_existing"``,
        ``"skipped_missing_filename"``, or ``"skipped_validation"``.
    """

    filename: str
    status: Literal["processed", "warning", "skipped"]
    warning_types: list[str] = field(default_factory=list)


def _update_reverse_links(
    vault: "Vault",
    type_name: str,
    written: list[tuple[str, dict]],
    data_root: Path,
) -> None:
    """
    Write the reverse side of declared relationship pairs for newly added records.

    For each relationship pair involving ``type_name``, reads the link fields of
    the just-written records and appends back-links to the target records on disk.
    Target files that do not exist are silently skipped.

    Parameters
    ----------
    vault : Vault
        The vault providing ``relationship_pairs`` for traversal.
    type_name : str
        The type that was just written to (``add_records`` caller).
    written : list of (str, dict)
        Filename stems and field dicts of the records that were written.
    data_root : Path
        Absolute path to the vault's data folder, used to resolve target file paths.
    """
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
    """
    Validate and prepare a batch of normalised records for writing.

    Handles filename sanitization, collision detection, unknown property
    warnings or drops, and null normalization. Does not perform any disk I/O.

    Parameters
    ----------
    normalized : list of dict
        Row dicts as returned by ``_to_records``. Each dict must contain
        ``filename_col`` as a key.
    type_name : str
        The vault type being written to, used in log messages.
    filename_col : str
        The dict key whose value becomes the filename stem.
    known_fields : set of str or None
        Schema-defined field names for the type. When None (new type), all
        properties are accepted without warnings.
    disk_stems : set of str
        Filename stems already present on disk for this type.
    allow_new_properties : bool
        When True, fields absent from ``known_fields`` are kept and logged as
        warnings. When False, they are dropped and logged.
    on_invalid : str
        ``"error"`` or ``"skip"`` — controls handling of records that fail
        validation (missing filename). Actual error-raising is deferred to
        ``_handle_failures``.
    on_collision : str
        ``"error"`` or ``"suffix"`` — controls handling of filename collisions.
    on_missing_filename : str
        ``"error"`` or ``"skip"`` — controls handling of records with no
        value in ``filename_col``.

    Returns
    -------
    failures : list of (int, str or None, str)
        Tuples of ``(index, filename_or_None, reason)`` for records that
        cannot be written.
    results : list of EntryResult or None
        Per-record results in input order. None at positions not yet resolved.
    to_write : list of (int, str, dict, str)
        Tuples of ``(index, filename, fields, body)`` for records ready to write.
    all_props : list of str
        Ordered unique list of all field names seen across the valid records
        (used by ``add_type`` to populate the ``.base`` file column order).
    """
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


def _handle_failures(
    failures: list[tuple[int, str | None, str]],
    results: list[EntryResult | None],
    on_invalid: str,
    type_name: str,
    method: str,
) -> None:
    """
    Raise or log validation failures collected by ``_process_batch``.

    Parameters
    ----------
    failures : list of (int, str or None, str)
        Tuples of ``(index, filename_or_None, reason)`` as returned by
        ``_process_batch``.
    results : list of EntryResult or None
        Per-record results list, mutated in-place to mark failed records as
        ``"skipped"`` when ``on_invalid="skip"``.
    on_invalid : str
        ``"error"`` raises a ValueError listing all failures. ``"skip"``
        marks each failed record as skipped and logs a warning.
    type_name : str
        The vault type name, used in error and log messages.
    method : str
        The calling method name (``"add_records"`` or ``"add_type"``), used
        in error and log messages.

    Raises
    ------
    ValueError
        When ``on_invalid="error"`` and ``failures`` is non-empty.
    """
    if not failures:
        return
    if on_invalid == "error":
        lines = [
            f"  [{idx}] {fname or '(no filename)'}: {reason}"
            for idx, fname, reason in failures
        ]
        raise ValueError(
            f"{method}[{type_name!r}]: {len(failures)} record(s) failed validation. "
            f"Pass on_invalid='skip' to skip failed records.\n" + "\n".join(lines)
        )
    for idx, fname, reason in failures:
        r = results[idx]
        r.status = "skipped"
        r.warning_types.append("skipped_validation")
        logger.warning(
            "%s[%s]: record %d skipped — %s",
            method, type_name, idx, reason,
        )


class Expander:
    """
    Write layer for expanding a vault with new records, columns, or types.

    All operations check for vault staleness before writing, write only into
    existing or newly created type directories, and call ``vault.reload()``
    on completion so the in-memory state reflects the new disk state.

    Accessed via ``vault.expand``, which lazily constructs and caches this accessor.
    """

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
        """
        Write new Markdown records to an existing type directory.

        Validates, sanitizes, and writes each record as a ``.md`` file. After
        writing, appends back-links on paired relationship fields in target
        records, then reloads the vault.

        Parameters
        ----------
        type_name : str
            The vault type to write into. Must already exist in the schema.
        records : list[dict], dict[str, list], DataFrame, or Series
            Tabular input. Each row becomes one Markdown file.
        filename_col : str
            The column whose values are used as filename stems.
        allow_new_properties : bool
            When True, fields not in the type's schema are written and logged
            as warnings. When False, they are silently dropped.
        on_invalid : {"error", "skip"}
            How to handle records that fail validation (e.g. missing filename).
        on_collision : {"error", "suffix"}
            How to handle filename collisions with existing files.
        on_missing_filename : {"error", "skip"}
            How to handle records with a null or missing ``filename_col`` value.

        Returns
        -------
        list of EntryResult
            One result per input record, in input order.

        Raises
        ------
        ValueError
            When ``type_name`` is not in the schema, or when ``on_invalid="error"``
            and any records fail validation.
        RuntimeError
            When the vault is stale (disk changed since last load).
        """
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

        _handle_failures(failures, results, on_invalid, type_name, "add_records")

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
        """
        Write a new frontmatter property to all records of an existing type.

        Patches each record's ``.md`` file in-place using a surgical insert
        (new property) or a frontmatter round-trip (overwrite). Reloads the
        vault after all writes.

        Parameters
        ----------
        type_name : str
            The vault type whose records are patched. Must already exist in
            the schema.
        column : dict[str, Any], pd.Series, or pl.Series
            Mapping of record name (filename stem) to the value to write.
        property_name : str
            The frontmatter key to insert or update on each record.
        on_existing : {"error", "skip", "overwrite"}
            How to handle records where ``property_name`` already exists.
        on_missing : {"error", "skip"}
            How to handle records not covered by the ``column`` mapping.

        Returns
        -------
        list of EntryResult
            One result per record in the type, in vault record order.

        Raises
        ------
        ValueError
            When ``type_name`` is not in the schema, or when ``on_existing="error"``
            and any records already have the property, or when ``on_missing="error"``
            and any records are not in the column mapping.
        RuntimeError
            When the vault is stale.
        """
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
        """
        Create a new vault type with its directory, records, and ``.base`` file.

        Creates the type directory under the data folder, writes all records as
        ``.md`` files, and generates a minimal ``.base`` file with a table view
        ordered by the fields found in the batch. Raises if the type already exists.
        Reloads the vault after writing.

        Parameters
        ----------
        type_name : str
            Name of the new type. Must not already exist in the schema.
        records : list[dict], dict[str, list], DataFrame, or Series
            Tabular input. Each row becomes one Markdown file.
        filename_col : str
            The column whose values are used as filename stems.
        allow_new_properties : bool
            Passed through to ``_process_batch``. Always effectively True for
            new types since there is no schema to check against, but kept for
            API consistency.
        on_invalid : {"error", "skip"}
            How to handle records that fail validation.
        on_collision : {"error", "suffix"}
            How to handle filename collisions within the batch.
        on_missing_filename : {"error", "skip"}
            How to handle records with a null or missing ``filename_col`` value.

        Returns
        -------
        list of EntryResult
            One result per input record, in input order.

        Raises
        ------
        ValueError
            When ``type_name`` already exists in the schema, or when
            ``on_invalid="error"`` and any records fail validation.
        RuntimeError
            When the vault is stale.
        """
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

        _handle_failures(failures, results, on_invalid, type_name, "add_type")

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
