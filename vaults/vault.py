from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import frontmatter

from .schema import FieldType, Schema, TypeSchema

logger = logging.getLogger(__name__)

_WIKILINK_RE = re.compile(r"^\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]$")
_VALID_DANGLING_REFS = ("drop", "stub")


def _parse_wikilink(value: str) -> Optional[tuple[str, str]]:
    """Return (folder, name) from a wikilink, or None if not a wikilink or has no folder prefix."""
    m = _WIKILINK_RE.match(str(value).strip())
    if not m:
        return None
    parts = m.group(1).split("/")
    return (parts[0], parts[1]) if len(parts) == 2 else None


def _compute_fingerprint(data_root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(data_root.rglob("*.md")):
        h.update(str(p.relative_to(data_root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def _apply_dangling_refs(
    dangling_refs: str,
    schema: Schema,
    records: dict[str, list["Record"]],
) -> dict[str, list["Record"]]:
    # names that actually exist per known type (real records, not stubs)
    existing: dict[str, set[str]] = {
        type_name: {r.name for r in recs}
        for type_name, recs in records.items()
    }

    # link field names per type
    link_fields: dict[str, set[str]] = {
        type_name: {
            f.name for f in ts.fields
            if f.type in (FieldType.LINK, FieldType.LIST_LINKS, FieldType.LIST_MIXED)
        }
        for type_name, ts in schema.types.items()
    }

    record_stubs: dict[tuple[str, str], "Record"] = {}
    type_stubs: dict[str, TypeSchema] = {}

    def _handle_link(folder: str, name: str) -> bool:
        """Process one wikilink target. Returns True if the item should be kept."""
        is_stub_type = folder in type_stubs
        unknown_type = folder not in existing and not is_stub_type
        unknown_record = not unknown_type and not is_stub_type and name not in existing[folder]

        if not (unknown_type or is_stub_type or unknown_record):
            return True

        if dangling_refs == "drop":
            return False

        # stub mode: create type stub if needed, then record stub
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
                    parsed = _parse_wikilink(value)
                    if parsed and not _handle_link(*parsed):
                        record.fields[field_name] = None

                elif isinstance(value, list):
                    new_list = []
                    for item in value:
                        parsed = _parse_wikilink(str(item)) if isinstance(item, str) else None
                        if parsed:
                            if _handle_link(*parsed):
                                new_list.append(item)
                        else:
                            new_list.append(item)
                    record.fields[field_name] = new_list

    for (type_name, _), stub in record_stubs.items():
        records[type_name].append(stub)

    return records


@dataclass
class Record:
    name: str
    type_schema: TypeSchema
    fields: dict[str, Any] = field(default_factory=dict)
    path: Optional[Path] = None

    @property
    def type(self) -> str:
        return self.type_schema.name


@dataclass
class Vault:
    schema: Schema
    records: dict[str, list[Record]] = field(default_factory=dict)
    path: Optional[Path] = None
    relationship_pairs: list[tuple[str, str]] = field(default_factory=list)
    fingerprint: str = field(default="", repr=False)

    def is_stale(self) -> bool:
        if self.path is None:
            return False
        data_root = self.path / self.schema.data_folder
        return _compute_fingerprint(data_root) != self.fingerprint

    @classmethod
    def from_vault(
        cls,
        path: str | Path,
        data_folder: str = "data",
        bases_folder: str = "bases",
        relationship_pairs: Optional[list[tuple[str, str]]] = None,
        ignore_empty: bool = False,
        dangling_refs: str = "drop",
    ) -> "Vault":
        if dangling_refs not in _VALID_DANGLING_REFS:
            raise ValueError(f"dangling_refs must be one of {_VALID_DANGLING_REFS}; got {dangling_refs!r}")

        path = Path(path).resolve()
        data_root = path / data_folder
        schema = Schema.from_vault(path, data_folder=data_folder, bases_folder=bases_folder, ignore_empty=ignore_empty)

        records: dict[str, list[Record]] = {}
        for type_name, type_schema in schema.types.items():
            type_dir = data_root / type_name
            type_records = []
            for md_file in sorted(type_dir.glob("*.md")):
                post = frontmatter.load(md_file)
                fields = dict(post.metadata)
                if post.content.strip():
                    fields["full_text"] = post.content.strip()
                type_records.append(Record(
                    name=md_file.stem,
                    type_schema=type_schema,
                    fields=fields,
                    path=md_file,
                ))
            records[type_name] = type_records

            # flag nested files — they are invisible to the loader
            for nested in type_dir.rglob("*.md"):
                if nested.parent != type_dir:
                    logger.warning(
                        "Nested file ignored: %s (move to %s/)",
                        nested.relative_to(data_root),
                        type_name,
                    )

        records = _apply_dangling_refs(dangling_refs, schema, records)

        return cls(
            schema=schema,
            records=records,
            path=path,
            relationship_pairs=relationship_pairs or [],
            fingerprint=_compute_fingerprint(data_root),
        )
