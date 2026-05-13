from __future__ import annotations

import datetime
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import frontmatter


class FieldType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    LINK = "link"
    LIST_STRINGS = "list[string]"
    LIST_LINKS = "list[link]"
    LIST_MIXED = "list[mixed]"
    UNKNOWN = "unknown"


_WIKILINK_RE = re.compile(r"^\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]$")


def _is_wikilink(value: str) -> bool:
    return bool(_WIKILINK_RE.match(str(value).strip()))


def _wikilink_target_folder(value: str) -> Optional[str]:
    m = _WIKILINK_RE.match(str(value).strip())
    if not m:
        return None
    parts = m.group(1).split("/")
    return parts[0] if len(parts) > 1 else None


def infer_field_type(values: list) -> FieldType:
    non_null = [v for v in values if v is not None and v != "" and v != []]
    if not non_null:
        return FieldType.UNKNOWN
    if all(isinstance(v, bool) for v in non_null):
        return FieldType.BOOLEAN
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null):
        return FieldType.NUMBER
    # datetime before date — datetime.datetime is a subclass of datetime.date
    if all(isinstance(v, datetime.datetime) for v in non_null):
        return FieldType.DATETIME
    if all(isinstance(v, datetime.date) and not isinstance(v, datetime.datetime) for v in non_null):
        return FieldType.DATE
    if all(isinstance(v, list) for v in non_null):
        flat = [item for sub in non_null for item in sub if item is not None and item != ""]
        if not flat:
            return FieldType.LIST_STRINGS
        if all(_is_wikilink(str(item)) for item in flat):
            return FieldType.LIST_LINKS
        if any(_is_wikilink(str(item)) for item in flat):
            return FieldType.LIST_MIXED
        return FieldType.LIST_STRINGS
    if all(isinstance(v, str) for v in non_null):
        if all(_is_wikilink(v) for v in non_null):
            return FieldType.LINK
        return FieldType.STRING
    return FieldType.UNKNOWN


def infer_link_target(values: list) -> Optional[str]:
    folders: set[str] = set()
    for v in values:
        if v is None or v == "" or v == []:
            continue
        items = v if isinstance(v, list) else [v]
        for item in items:
            folder = _wikilink_target_folder(str(item))
            if folder:
                folders.add(folder)
    return folders.pop() if len(folders) == 1 else None


@dataclass
class FieldSchema:
    name: str
    type: FieldType
    link_target: Optional[str] = None


@dataclass
class TypeSchema:
    name: str
    fields: list[FieldSchema] = field(default_factory=list)


@dataclass
class Schema:
    types: dict[str, TypeSchema] = field(default_factory=dict)
    data_folder: str = "data"
    bases_folder: str = "bases"

    @classmethod
    def from_vault(
        cls,
        path: str | Path,
        data_folder: str = "data",
        bases_folder: str = "bases",
        ignore_empty: bool = False,
    ) -> "Schema":
        root = Path(path) / data_folder

        schema = cls(data_folder=data_folder, bases_folder=bases_folder)

        for type_dir in sorted(root.iterdir()):
            if not type_dir.is_dir() or type_dir.name.startswith("_"):
                continue

            # field_name → list of all values seen across all records
            field_values: dict[str, list] = defaultdict(list)

            for md_file in sorted(type_dir.glob("*.md")):
                post = frontmatter.load(md_file)
                for key, value in post.metadata.items():
                    field_values[key].append(value)
                if post.content.strip():
                    field_values["full_text"].append(post.content.strip())

            if ignore_empty and not field_values:
                continue

            fields = []
            for field_name, values in field_values.items():
                ftype = infer_field_type(values)
                link_target = None
                if ftype in (FieldType.LINK, FieldType.LIST_LINKS, FieldType.LIST_MIXED):
                    link_target = infer_link_target(values)
                fields.append(FieldSchema(name=field_name, type=ftype, link_target=link_target))

            schema.types[type_dir.name] = TypeSchema(name=type_dir.name, fields=fields)

        return schema

    def diff(self, other: "Schema") -> dict:
        added_types = sorted(set(other.types) - set(self.types))
        removed_types = sorted(set(self.types) - set(other.types))
        changed_fields: dict[str, dict] = {}

        for type_name in sorted(set(self.types) & set(other.types)):
            a = {f.name: f for f in self.types[type_name].fields}
            b = {f.name: f for f in other.types[type_name].fields}
            added = sorted(set(b) - set(a))
            removed = sorted(set(a) - set(b))
            changed = sorted(
                name for name in set(a) & set(b)
                if a[name].type != b[name].type or a[name].link_target != b[name].link_target
            )
            if added or removed or changed:
                changed_fields[type_name] = {
                    "added_fields": added,
                    "removed_fields": removed,
                    "changed_fields": changed,
                }

        return {
            "added_types": added_types,
            "removed_types": removed_types,
            "changed_fields": changed_fields,
        }

    def to_json(self, path: str | Path) -> None:
        payload = {
            "data_folder": self.data_folder,
            "bases_folder": self.bases_folder,
            "types": {
                type_name: [
                    {"name": f.name, "type": f.type.value, "link_target": f.link_target}
                    for f in type_schema.fields
                ]
                for type_name, type_schema in self.types.items()
            },
        }
        Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def from_json(cls, path: str | Path) -> "Schema":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        schema = cls(
            data_folder=payload.get("data_folder", "data"),
            bases_folder=payload.get("bases_folder", "bases"),
        )
        for type_name, fields_data in payload.get("types", {}).items():
            fields = [
                FieldSchema(
                    name=f["name"],
                    type=FieldType(f["type"]),
                    link_target=f.get("link_target"),
                )
                for f in fields_data
            ]
            schema.types[type_name] = TypeSchema(name=type_name, fields=fields)
        return schema
