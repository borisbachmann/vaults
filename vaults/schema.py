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
    """Canonical field types inferred from observed frontmatter values across a type's records."""

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
    """
    Infer the most specific FieldType consistent with all observed values.

    Null-like values (None, empty string, empty list) are ignored; the type is
    determined solely from non-null entries. If no non-null values exist, returns
    UNKNOWN. Type precedence (most to least specific): BOOLEAN > INTEGER > NUMBER >
    DATETIME > DATE > LIST_LINKS > LIST_MIXED > LIST_STRINGS > LINK > STRING > UNKNOWN.

    Parameters
    ----------
    values : list
        Raw field values collected across all records of a type, as returned by
        the frontmatter parser. May contain None, "", [], and mixed Python types.

    Returns
    -------
    FieldType
        The inferred type. Returns UNKNOWN when values are all null-like or contain
        an unrecognised mix of types.
    """
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
    """
    Infer the single target folder for a link or list-of-links field, if unambiguous.

    Collects all distinct target folders from wikilinks in ``values``. Returns the
    folder name only when every non-null link resolves to exactly one folder; returns
    None if links point to multiple folders or no folder can be determined.

    Parameters
    ----------
    values : list
        Raw field values for a link-typed field, as collected across records. Each
        entry may be a wikilink string, a list of wikilink strings, None, or "".

    Returns
    -------
    str or None
        The unique target folder name, or None if the target is ambiguous or absent.
    """
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
    """
    Schema for a single field within a type.

    Parameters
    ----------
    name : str
        Field name as it appears in frontmatter (or the display name for formula fields).
    type : FieldType
        Inferred or declared type of the field.
    link_target : str or None
        For LINK / LIST_LINKS fields, the single unambiguous target folder. None when
        the target is ambiguous or not a link field.
    formula : str or None
        Raw formula expression string for FORMULA fields; None otherwise.
    output_type : FieldType or None
        Inferred output type of a FORMULA field after evaluation; None until determined.
    compiled : callable or None
        Compiled callable produced by BasesCompiler for FORMULA fields. Excluded from
        repr to avoid noise. None for non-formula fields or before compilation.
    """

    name: str
    type: FieldType
    link_target: Optional[str] = None
    formula: Optional[str] = None
    output_type: Optional[FieldType] = None
    compiled: Optional[Callable[..., Any]] = field(default=None, repr=False)

    @property
    def effective_type(self) -> "FieldType":
        """
        The resolved output type, substituting FORMULA with its actual output type.

        Returns
        -------
        FieldType
            ``output_type`` when this is a FORMULA field and output_type is known;
            UNKNOWN when it is a FORMULA field but output_type has not been determined;
            otherwise the field's own ``type``.
        """
        if self.type == FieldType.FORMULA:
            return self.output_type or FieldType.UNKNOWN
        return self.type


@dataclass
class TypeSchema:
    """
    Schema for a single vault type (one subdirectory under the data folder).

    Parameters
    ----------
    name : str
        Type name, matching the directory name on disk.
    fields : list of FieldSchema
        Ordered list of field schemas inferred from the type's records.
    base_filter : str or None
        Serialized filter expression from the corresponding ``.base`` file, if present.
    compiled_filter : callable or None
        Compiled boolean callable for ``base_filter``, produced by BasesCompiler.
        Excluded from repr. None when no filter is defined.
    property_display : dict[str, str]
        Mapping of property path (e.g. ``"formula.revenue"``) to display name, as
        defined in the ``.base`` file's ``properties`` section. Excluded from repr.
    """

    name: str
    fields: list[FieldSchema] = field(default_factory=list)
    base_filter: Optional[str] = None
    compiled_filter: Optional[Callable[..., Any]] = field(default=None, repr=False)
    property_display: dict[str, str] = field(default_factory=dict, repr=False)


def serialize_filter(filters: Any) -> Optional[str]:
    """
    Normalise a ``.base`` filter value to a single filter string.

    ``.base`` files can express filters as a plain string, a list of expressions,
    or a dict with ``and``/``or`` keys. This function collapses all forms to the
    string expected by BasesCompiler, or None when the filter is empty.

    Parameters
    ----------
    filters : str, list, dict, or None
        Raw value of the ``filters`` key from a parsed ``.base`` YAML file.

    Returns
    -------
    str or None
        A single filter expression string, or None if ``filters`` is falsy or
        reduces to an empty expression.
    """
    if not filters:
        return None
    if isinstance(filters, str):
        return filters
    _OP_TOKEN = {"and": "&&", "or": "||"}
    if isinstance(filters, list):
        parts = [str(e) for e in filters if e]
        return " && ".join(parts) if parts else None
    if isinstance(filters, dict):
        for op in ("and", "or"):
            if op in filters:
                items = filters[op]
                if isinstance(items, list):
                    parts = [str(e) for e in items if e]
                    if len(parts) == 1:
                        return parts[0]
                    tok = _OP_TOKEN[op]
                    return f" {tok} ".join(parts)
                return str(items)
    return None


@dataclass
class Schema:
    """
    Full structural description of a vault: all types and their field schemas.

    Produced by ``_from_vault()`` and attached to a ``Vault`` instance. Acts as
    the authoritative type map used by accessors, the linter, and the expander.

    Parameters
    ----------
    types : dict[str, TypeSchema]
        Mapping of type name to its TypeSchema. Keys match subdirectory names under
        the data folder.
    data_folder : str
        Name of the directory under the vault root that holds type subdirectories.
    bases_folder : str
        Name of the directory under the vault root that holds ``.base`` files.
    """

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
        """
        Build a Schema by scanning a vault directory tree.

        Iterates over type subdirectories under ``<path>/<data_folder>/``, reads
        every ``.md`` file's frontmatter to infer field types, then enriches each
        TypeSchema with formula fields, display names, and filter expressions read
        from the corresponding ``.base`` file under ``<path>/<bases_folder>/``.

        Directories whose names begin with ``_`` are skipped. The ``full_text``
        pseudo-field is added to the schema for any type whose records contain
        non-empty Markdown body content.

        Parameters
        ----------
        path : str or Path
            Root directory of the vault (parent of ``data_folder`` and ``bases_folder``).
        data_folder : str
            Subdirectory name that contains one folder per type.
        bases_folder : str
            Subdirectory name that contains ``.base`` YAML files.
        ignore_empty : bool
            When True, types with no frontmatter fields across all their records are
            omitted from the resulting Schema.

        Returns
        -------
        Schema
            Fully populated Schema instance with compiled formula callables and
            compiled filter callables where applicable.
        """
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
                type_schema.base_filter = serialize_filter(raw_filter)
                type_schema.compiled_filter = compiler.translate_filter(type_schema.base_filter)

        return schema

    def diff(self, other: "Schema") -> dict:
        """
        Compare this schema against another and report structural changes.

        Useful for detecting vault drift between two loads (e.g. after files are
        added or edited), or for validating that an expander operation produced
        the expected schema change.

        Parameters
        ----------
        other : Schema
            The schema to compare against. Treated as the "new" state; ``self``
            is the "old" state.

        Returns
        -------
        dict
            A dict with three keys:

            ``added_types`` : list of str
                Type names present in ``other`` but not in ``self``.
            ``removed_types`` : list of str
                Type names present in ``self`` but not in ``other``.
            ``changed_fields`` : dict[str, dict]
                For each type present in both schemas where fields differ, a dict
                with keys ``added_fields``, ``removed_fields``, and ``changed_fields``
                (each a sorted list of field names). A field is considered changed
                when its ``type`` or ``link_target`` differs between schemas.
        """
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
