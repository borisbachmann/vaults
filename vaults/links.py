from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Optional

from .schema import FieldSchema, FieldType, Schema, TypeSchema
from .record import Record

if TYPE_CHECKING:
    from .vault import Vault

logger = logging.getLogger(__name__)

WIKILINK_RE = re.compile(r"^\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]$")

LINK_TYPES = (FieldType.LINK, FieldType.LIST_LINKS, FieldType.LIST_MIXED)
LIST_LINK_TYPES = (FieldType.LIST_LINKS, FieldType.LIST_MIXED)
LINK_TYPES_PAIRED = (FieldType.LINK, FieldType.LIST_LINKS)


# ── Wikilink parsing ──────────────────────────────────────────────────────────


def _match_wikilink(value: str) -> Optional[list[str]]:
    """Match a wikilink and return its path segments, or None."""
    m = WIKILINK_RE.match(str(value).strip())
    if not m:
        return None
    return m.group(1).split("/")


def parse_wikilink(value: str) -> Optional[tuple[str, str]]:
    """
    Parse a wikilink string into its folder and record-name components.

    Uses the last two path segments of the wikilink target, which matches
    Obsidian's disambiguation prefix convention (e.g. ``[[folder/name]]``).
    Pipe aliases (``[[path|alias]]``) are stripped before parsing.

    Parameters
    ----------
    value : str
        A raw wikilink string, e.g. ``"[[Projects/Alpha]]"``.

    Returns
    -------
    tuple of (str, str) or None
        ``(folder, name)`` where ``folder`` is the second-to-last path segment
        and ``name`` is the final segment. Returns None if ``value`` is not a
        valid wikilink or has fewer than two path segments.
    """
    parts = _match_wikilink(value)
    return (parts[-2], parts[-1]) if parts and len(parts) >= 2 else None


def is_wikilink(value: str) -> bool:
    """
    Return True if ``value`` matches the wikilink pattern ``[[...]]``.

    Parameters
    ----------
    value : str
        Any string value from a record's frontmatter.

    Returns
    -------
    bool
    """
    return _match_wikilink(value) is not None


def wikilink_target_folder(value: str) -> Optional[str]:
    """
    Extract the folder component (second-to-last path segment) from a wikilink.

    Parameters
    ----------
    value : str
        A raw wikilink string, e.g. ``"[[Projects/Alpha]]"``.

    Returns
    -------
    str or None
        The folder name, or None if ``value`` is not a valid wikilink or has
        fewer than two path segments.
    """
    parts = _match_wikilink(value)
    return parts[-2] if parts and len(parts) >= 2 else None


def parse_wikilink_name(value: str) -> Optional[str]:
    """
    Extract the record name (last path segment) from a wikilink.

    Parameters
    ----------
    value : str
        A raw wikilink string, e.g. ``"[[Projects/Alpha]]"``.

    Returns
    -------
    str or None
        The final path segment (record name), or None if ``value`` is not a
        valid wikilink.
    """
    parts = _match_wikilink(value)
    return parts[-1] if parts else None


def iter_link_names(value: Any) -> list[str]:
    """
    Extract record name stems from a raw link field value.

    Handles both scalar wikilink strings and lists containing wikilink strings.
    Non-wikilink items are silently skipped.

    Parameters
    ----------
    value : str, list, or None
        Raw field value from a record, as stored in ``record.fields``.

    Returns
    -------
    list of str
        Record names (last path segments) parsed from any wikilinks found in
        ``value``. Empty list if ``value`` is None or contains no valid wikilinks.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parsed = parse_wikilink(value)
        return [parsed[1]] if parsed else []
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str):
                parsed = parse_wikilink(item)
                if parsed:
                    result.append(parsed[1])
        return result
    return []


# ── Pair resolution ───────────────────────────────────────────────────────────


def _write_link_field(
    records: list[Record],
    field_name: str,
    record_to_targets: dict[str, set[str]],
    link_target_folder: str,
    field_schema: Optional[FieldSchema],
    expand_to_lists: bool,
) -> None:
    """
    Write reconciled link targets back to a field on each record, mutating in-place.

    Preserves existing wikilinks in their original order and appends any new
    targets discovered from the paired side (sorted for determinism). Raises if
    a singular LINK field would need to hold multiple targets and ``expand_to_lists``
    is False.

    Parameters
    ----------
    records : list of Record
        All records of the type whose field is being written.
    field_name : str
        Name of the link field to update on each record.
    record_to_targets : dict[str, set[str]]
        Mapping of record name to the set of target record names that should
        appear in the field after reconciliation.
    link_target_folder : str
        Folder component to use when constructing new wikilink strings
        (e.g. ``"Projects"`` → ``"[[Projects/Alpha]]"``).
    field_schema : FieldSchema or None
        Schema for the field, used to determine whether it is a list field.
        When None, the field is treated as a list field.
    expand_to_lists : bool
        When True, a singular LINK field that needs multiple targets is silently
        promoted to a list with a warning. When False, that situation raises.

    Raises
    ------
    ValueError
        When a singular LINK field would require multiple targets and
        ``expand_to_lists`` is False.
    """
    is_list_field = field_schema is None or field_schema.type in (FieldType.LIST_LINKS, FieldType.LIST_MIXED)

    for rec in records:
        needed = record_to_targets.get(rec.name)
        if not needed:
            continue

        existing_value = rec.fields.get(field_name)
        existing_wikilinks: list[str] = []
        existing_names: set[str] = set()

        if isinstance(existing_value, str):
            parsed = parse_wikilink(existing_value)
            if parsed:
                existing_wikilinks.append(existing_value)
                existing_names.add(parsed[1])
        elif isinstance(existing_value, list):
            for item in existing_value:
                if isinstance(item, str):
                    parsed = parse_wikilink(item)
                    if parsed:
                        existing_wikilinks.append(item)
                        existing_names.add(parsed[1])

        new_wikilinks = [
            f"[[{link_target_folder}/{name}]]"
            for name in sorted(needed - existing_names)
        ]
        all_wikilinks = existing_wikilinks + new_wikilinks

        if not all_wikilinks:
            continue

        if is_list_field:
            rec.fields[field_name] = all_wikilinks
        elif len(all_wikilinks) == 1:
            rec.fields[field_name] = all_wikilinks[0]
        elif expand_to_lists:
            logger.warning(
                "Pair reconciliation expanded singular LINK field '%s.%s' on record '%s' to a list. "
                "Consider changing the field type to LIST_LINKS in the vault model.",
                rec.type, field_name, rec.name,
            )
            rec.fields[field_name] = all_wikilinks
        else:
            raise ValueError(
                f"Pair reconciliation would produce multiple targets for singular LINK field "
                f"'{rec.type}.{field_name}' on record '{rec.name}'. "
                f"Set expand_to_lists=True or fix the vault model."
            )


def resolve_pairs(vault: Vault, expand_to_lists: bool = True) -> None:
    """
    Reconcile paired link fields across all records, mutating the vault in-place.

    For each declared relationship pair ``(TypeA.field_x, TypeB.field_y)``,
    computes the outer union of edges asserted on either side (snapshot before
    any writes) and writes the complete edge set back to both fields on every
    relevant record. Fields absent from the schema are added to it when new edges
    are discovered.

    Parameters
    ----------
    vault : Vault
        The loaded vault whose ``relationship_pairs`` and ``records`` are used.
        Records are mutated in-place; schema may gain new FieldSchema entries.
    expand_to_lists : bool
        Forwarded to ``_write_link_field``. When True, singular LINK fields that
        acquire multiple targets are promoted to lists with a warning. When False,
        that situation raises instead.

    Raises
    ------
    ValueError
        When ``expand_to_lists`` is False and a singular LINK field would need
        multiple targets after reconciliation.
    """
    if not vault.relationship_pairs:
        return

    field_lookup: dict[tuple[str, str], FieldSchema] = {
        (type_name, f.name): f
        for type_name, type_schema in vault.schema.types.items()
        for f in type_schema.fields
    }

    pair_fields: set[tuple[str, str]] = set()
    for first, second in vault.relationship_pairs:
        type_a, field_a = first.split(".", 1)
        type_b, field_b = second.split(".", 1)
        pair_fields.add((type_a, field_a))
        pair_fields.add((type_b, field_b))

    snapshot: dict[tuple[str, str, str], Any] = {}
    for type_name, field_name in pair_fields:
        for rec in vault.records.get(type_name, []):
            snapshot[(type_name, rec.name, field_name)] = rec.fields.get(field_name)

    for first, second in vault.relationship_pairs:
        type_a, field_a = first.split(".", 1)
        type_b, field_b = second.split(".", 1)

        schema_fa = field_lookup.get((type_a, field_a))
        schema_fb = field_lookup.get((type_b, field_b))

        edges: set[tuple[str, str]] = set()
        for rec in vault.records.get(type_a, []):
            for name in iter_link_names(snapshot.get((type_a, rec.name, field_a))):
                edges.add((rec.name, name))
        for rec in vault.records.get(type_b, []):
            for name in iter_link_names(snapshot.get((type_b, rec.name, field_b))):
                edges.add((name, rec.name))

        if not edges:
            continue

        a_to_bs: dict[str, set[str]] = defaultdict(set)
        b_to_as: dict[str, set[str]] = defaultdict(set)
        for a_name, b_name in edges:
            a_to_bs[a_name].add(b_name)
            b_to_as[b_name].add(a_name)

        link_target_b = (schema_fa.link_target if schema_fa else None) or type_b
        _write_link_field(
            records=vault.records.get(type_a, []),
            field_name=field_a,
            record_to_targets=a_to_bs,
            link_target_folder=link_target_b,
            field_schema=schema_fa,
            expand_to_lists=expand_to_lists,
        )
        if schema_fa is None and a_to_bs and type_a in vault.schema.types:
            vault.schema.types[type_a].fields.append(
                FieldSchema(name=field_a, type=FieldType.LIST_LINKS, link_target=link_target_b)
            )

        link_target_a = (schema_fb.link_target if schema_fb else None) or type_a
        _write_link_field(
            records=vault.records.get(type_b, []),
            field_name=field_b,
            record_to_targets=b_to_as,
            link_target_folder=link_target_a,
            field_schema=schema_fb,
            expand_to_lists=expand_to_lists,
        )
        if schema_fb is None and b_to_as and type_b in vault.schema.types:
            vault.schema.types[type_b].fields.append(
                FieldSchema(name=field_b, type=FieldType.LIST_LINKS, link_target=link_target_a)
            )


# ── Dangling reference handling ───────────────────────────────────────────────


def apply_dangling_refs(
    dangling_refs: str,
    schema: Schema,
    records: dict[str, list[Record]],
) -> dict[str, list[Record]]:
    """
    Resolve wikilinks that point to records or types not present in the vault.

    Iterates every link-typed field across all records. For each link whose
    target record or target type does not exist, either drops the link or
    creates a stub Record (and stub TypeSchema when the target type is unknown),
    according to ``dangling_refs``.

    Stub records carry no fields and are appended to ``records`` so that graph
    and DataFrame accessors have a node to attach edges to, preventing silent
    data loss. Stub types are added to ``schema.types`` with an empty field list.

    Parameters
    ----------
    dangling_refs : str
        Policy for unresolvable links. ``"drop"`` removes them from the field
        value (sets singular links to None, filters them from lists). Any other
        value (e.g. ``"stub"``) creates placeholder records instead.
    schema : Schema
        The vault's schema, mutated in-place when stub types are created.
    records : dict[str, list[Record]]
        Mapping of type name to record list, mutated in-place when stubs are
        appended.

    Returns
    -------
    dict[str, list[Record]]
        The same ``records`` dict, mutated in-place and returned for convenience.
    """
    existing: dict[str, set[str]] = {
        type_name: {r.name for r in recs}
        for type_name, recs in records.items()
    }

    link_fields: dict[str, set[str]] = {
        type_name: {
            f.name for f in ts.fields
            if f.type in (FieldType.LINK, FieldType.LIST_LINKS, FieldType.LIST_MIXED)
        }
        for type_name, ts in schema.types.items()
    }

    record_stubs: dict[tuple[str, str], Record] = {}
    type_stubs: dict[str, TypeSchema] = {}

    def _handle_link(folder: str, name: str) -> bool:
        is_stub_type = folder in type_stubs
        unknown_type = folder not in existing and not is_stub_type
        unknown_record = not unknown_type and not is_stub_type and name not in existing[folder]

        if not (unknown_type or is_stub_type or unknown_record):
            return True

        if dangling_refs == "drop":
            return False

        if unknown_type:
            ts = TypeSchema(name=folder)
            type_stubs[folder] = ts
            schema.types[folder] = ts
            records[folder] = []
            existing[folder] = set()

        record_stubs.setdefault(
            (folder, name),
            Record(name=name, type_schema=type_stubs.get(folder, schema.types[folder])),
        )
        return True

    for type_name, recs in list(records.items()):
        for record in recs:
            for field_name in link_fields.get(type_name, set()):
                if field_name not in record.fields:
                    continue
                value = record.fields[field_name]

                if isinstance(value, str):
                    parsed = parse_wikilink(value)
                    if parsed and not _handle_link(*parsed):
                        record.fields[field_name] = None

                elif isinstance(value, list):
                    new_list = []
                    for item in value:
                        parsed = parse_wikilink(str(item)) if isinstance(item, str) else None
                        if parsed:
                            if _handle_link(*parsed):
                                new_list.append(item)
                        else:
                            new_list.append(item)
                    record.fields[field_name] = new_list

    for (type_name, _), stub in record_stubs.items():
        records[type_name].append(stub)

    return records
