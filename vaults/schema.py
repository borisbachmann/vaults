from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import frontmatter
import yaml


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
    FORMULA = "formula"
    INTEGER = "integer"


def infer_field_type(values: list) -> FieldType:
    from .links import is_wikilink

    non_null = [v for v in values if v is not None and v != "" and v != []]
    if not non_null:
        return FieldType.UNKNOWN
    if all(isinstance(v, bool) for v in non_null):
        return FieldType.BOOLEAN
    if all(isinstance(v, int) and not isinstance(v, bool) for v in non_null):
        return FieldType.INTEGER
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null):
        return FieldType.NUMBER
    if all(isinstance(v, datetime.datetime) for v in non_null):
        return FieldType.DATETIME
    if all(isinstance(v, datetime.date) and not isinstance(v, datetime.datetime) for v in non_null):
        return FieldType.DATE
    if all(isinstance(v, list) for v in non_null):
        flat = [item for sub in non_null for item in sub if item is not None and item != ""]
        if not flat:
            return FieldType.LIST_STRINGS
        if all(is_wikilink(str(item)) for item in flat):
            return FieldType.LIST_LINKS
        if any(is_wikilink(str(item)) for item in flat):
            return FieldType.LIST_MIXED
        return FieldType.LIST_STRINGS
    if all(isinstance(v, str) for v in non_null):
        if all(is_wikilink(v) for v in non_null):
            return FieldType.LINK
        return FieldType.STRING
    return FieldType.UNKNOWN


def infer_link_target(values: list) -> Optional[str]:
    from .links import wikilink_target_folder

    folders: set[str] = set()
    for v in values:
        if v is None or v == "" or v == []:
            continue
        items = v if isinstance(v, list) else [v]
        for item in items:
            folder = wikilink_target_folder(str(item))
            if folder:
                folders.add(folder)
    return folders.pop() if len(folders) == 1 else None


@dataclass
class FieldSchema:
    name: str
    type: FieldType
    link_target: Optional[str] = None
    formula: Optional[str] = None
    output_type: Optional[FieldType] = None
    compiled: Optional[Callable[..., Any]] = field(default=None, repr=False)

    @property
    def effective_type(self) -> "FieldType":
        if self.type == FieldType.FORMULA:
            return self.output_type or FieldType.UNKNOWN
        return self.type


@dataclass
class TypeSchema:
    name: str
    fields: list[FieldSchema] = field(default_factory=list)
    base_filter: Optional[str] = None
    compiled_filter: Optional[Callable[..., Any]] = field(default=None, repr=False)
    property_display: dict[str, str] = field(default_factory=dict, repr=False)


def _serialize_filter(filters) -> Optional[str]:
    if not filters:
        return None
    if isinstance(filters, str):
        return filters
    if isinstance(filters, list):
        parts = [str(e) for e in filters if e]
        return " and ".join(parts) if parts else None
    if isinstance(filters, dict):
        for op in ("and", "or"):
            if op in filters:
                items = filters[op]
                if isinstance(items, list):
                    parts = [str(e) for e in items if e]
                    if len(parts) == 1:
                        return parts[0]
                    return f" {op} ".join(parts)
                return str(items)
    return None


@dataclass
class Schema:
    types: dict[str, TypeSchema] = field(default_factory=dict)
    data_folder: str = "data"
    bases_folder: str = "bases"

    @classmethod
    def _from_vault(
        cls,
        path: str | Path,
        data_folder: str = "data",
        bases_folder: str = "bases",
        ignore_empty: bool = False,
    ) -> "Schema":
        from .syntax import BasesCompiler

        root = Path(path) / data_folder
        compiler = BasesCompiler()

        schema = cls(data_folder=data_folder, bases_folder=bases_folder)

        for type_dir in sorted(root.iterdir()):
            if not type_dir.is_dir() or type_dir.name.startswith("_"):
                continue

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

        bases_root = Path(path) / bases_folder
        for type_name, type_schema in schema.types.items():
            base_file = bases_root / f"{type_name}.base"
            if not base_file.exists():
                continue
            data = yaml.safe_load(base_file.read_text(encoding="utf-8")) or {}
            properties = data.get("properties") or {}
            prop_display: dict[str, str] = {}
            for prop_path, config in properties.items():
                if isinstance(config, dict) and "displayName" in config:
                    prop_display[prop_path] = config["displayName"]
            type_schema.property_display = prop_display
            for formula_name, expr in (data.get("formulas") or {}).items():
                display_name = prop_display.get(f"formula.{formula_name}", formula_name)
                compiled = compiler.translate(str(expr))
                type_schema.fields.append(
                    FieldSchema(
                        name=display_name,
                        type=FieldType.FORMULA,
                        formula=str(expr),
                        compiled=compiled,
                    )
                )
            raw_filter = data.get("filters")
            if raw_filter:
                type_schema.base_filter = _serialize_filter(raw_filter)
                type_schema.compiled_filter = compiler.translate_filter(type_schema.base_filter)

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
