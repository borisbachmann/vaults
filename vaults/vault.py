from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import frontmatter

from .formula import BasesCompiler, EvalContext
from .schema import FieldSchema, FieldType, Schema, TypeSchema, infer_field_type

logger = logging.getLogger(__name__)

_WIKILINK_RE = re.compile(r"^\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]$")
_VALID_DANGLING_REFS = ("drop", "stub")

# FieldType → DuckDB column type (LINK and LIST_LINKS produce join tables, not columns)
_FIELD_TO_DB_TYPE: dict[FieldType, str] = {
    FieldType.STRING: "VARCHAR",
    FieldType.NUMBER: "DOUBLE",      # refined per-field to BIGINT if all-int
    FieldType.BOOLEAN: "BOOLEAN",
    FieldType.DATE: "DATE",
    FieldType.DATETIME: "TIMESTAMP",
    FieldType.LIST_STRINGS: "VARCHAR[]",
    FieldType.LIST_MIXED: "VARCHAR[]",
    FieldType.UNKNOWN: "VARCHAR",
}

_LINK_TYPES = (FieldType.LINK, FieldType.LIST_LINKS)


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


def _numeric_db_type(values: list[Any]) -> str:
    non_null = [v for v in values if v is not None]
    if not non_null or all(isinstance(v, int) and not isinstance(v, bool) for v in non_null):
        return "BIGINT"
    return "DOUBLE"


def _coerce_for_db(value: Any, ft: FieldType) -> Any:
    if value is None:
        return None
    if ft == FieldType.LIST_MIXED:
        return [str(item) if item is not None else "" for item in value] if isinstance(value, list) else [str(value)]
    if ft == FieldType.UNKNOWN:
        return str(value)
    return value


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


_FORMULA_DEP_RE = re.compile(r"\bformula\.(\w+)")
_NOTE_REF_RE = re.compile(r"\bnote\.(\w+)")


@dataclass
class LintViolation:
    severity: str  # "error" | "warning"
    message: str
    type_name: Optional[str] = None
    field_name: Optional[str] = None


def _topo_sort_formulas(
    formula_fields: list[FieldSchema],
) -> tuple[list[FieldSchema], list[FieldSchema]]:
    """Kahn's algorithm topological sort. Returns (ordered, cyclic)."""
    by_name = {f.name: f for f in formula_fields}
    deps: dict[str, set[str]] = {}
    for f in formula_fields:
        refs = set(_FORMULA_DEP_RE.findall(f.formula or "")) & by_name.keys()
        deps[f.name] = refs

    in_degree: dict[str, int] = {name: 0 for name in by_name}
    dependents: dict[str, list[str]] = defaultdict(list)
    for name, referenced in deps.items():
        for dep in referenced:
            in_degree[name] += 1
            dependents[dep].append(name)

    queue: deque[str] = deque(name for name, deg in in_degree.items() if deg == 0)
    ordered: list[FieldSchema] = []
    while queue:
        name = queue.popleft()
        ordered.append(by_name[name])
        for dependent in dependents[name]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    cyclic = [by_name[name] for name in by_name if in_degree[name] > 0]
    return ordered, cyclic


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

    def lint(self) -> list[LintViolation]:
        violations: list[LintViolation] = []
        compiler = BasesCompiler()

        for type_name, type_schema in self.schema.types.items():
            known_fields = {f.name for f in type_schema.fields if f.type != FieldType.FORMULA}
            formula_fields = [f for f in type_schema.fields if f.type == FieldType.FORMULA]

            # Missing .base file
            if self.path is not None:
                base_file = self.path / self.schema.bases_folder / f"{type_name}.base"
                if not base_file.exists():
                    violations.append(LintViolation(
                        severity="error",
                        message=f"Missing .base file for type '{type_name}'",
                        type_name=type_name,
                    ))

            for f in formula_fields:
                # Untranslatable formula
                fn = compiler.translate(f.formula or "")
                if hasattr(fn, "translation_error"):
                    violations.append(LintViolation(
                        severity="error",
                        message=f"Untranslatable formula: {fn.translation_error}",
                        type_name=type_name,
                        field_name=f.name,
                    ))

                # note.X referencing unknown field
                for ref in _NOTE_REF_RE.findall(f.formula or ""):
                    if ref not in known_fields:
                        violations.append(LintViolation(
                            severity="warning",
                            message=f"Formula references unknown field 'note.{ref}'",
                            type_name=type_name,
                            field_name=f.name,
                        ))

                # Output type mismatch (UNKNOWN output with non-null results)
                if f.output_type == FieldType.UNKNOWN:
                    recs = self.records.get(type_name, [])
                    non_null = [r.fields.get(f.name) for r in recs if r.fields.get(f.name) is not None]
                    if non_null:
                        violations.append(LintViolation(
                            severity="warning",
                            message=f"Formula output type is inconsistent across records",
                            type_name=type_name,
                            field_name=f.name,
                        ))

            # Cyclic formula dependencies
            if formula_fields:
                _, cyclic = _topo_sort_formulas(formula_fields)
                for f in cyclic:
                    violations.append(LintViolation(
                        severity="error",
                        message=f"Cyclic formula dependency",
                        type_name=type_name,
                        field_name=f.name,
                    ))

        return violations

    @classmethod
    def from_vault(
        cls,
        path: str | Path,
        data_folder: str = "data",
        bases_folder: str = "bases",
        relationship_pairs: Optional[list[tuple[str, str]]] = None,
        ignore_empty: bool = False,
        dangling_refs: str = "drop",
        apply_base_filters: bool = False,
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

        frozen_now = datetime.now()
        compiler = BasesCompiler()

        for type_name, type_schema in schema.types.items():
            formula_fields = [f for f in type_schema.fields if f.type == FieldType.FORMULA]
            if not formula_fields:
                continue
            base_path = path / bases_folder / f"{type_name}.base"
            recs = records.get(type_name, [])
            ordered, cyclic = _topo_sort_formulas(formula_fields)

            for f in ordered:
                fn = compiler.translate(f.formula)
                results = []
                for rec in recs:
                    ctx = EvalContext(record=rec, vault=None, base_path=base_path, now=frozen_now)
                    try:
                        result = fn(ctx)
                    except Exception:
                        result = None
                    rec.fields[f.name] = result
                    results.append(result)
                f.output_type = infer_field_type(results)

            for f in cyclic:
                for rec in recs:
                    rec.fields[f.name] = None

        if apply_base_filters:
            for type_name, type_schema in schema.types.items():
                if not type_schema.base_filter:
                    continue
                base_path = path / bases_folder / f"{type_name}.base"
                fn = compiler.translate_filter(type_schema.base_filter)
                before = records.get(type_name, [])
                after = []
                for rec in before:
                    ctx = EvalContext(record=rec, vault=None, base_path=base_path, now=frozen_now)
                    try:
                        keep = fn(ctx)
                    except Exception:
                        keep = True
                    if keep:
                        after.append(rec)
                    else:
                        logger.debug("Base filter dropped record: %s/%s", type_name, rec.name)
                records[type_name] = after

        return cls(
            schema=schema,
            records=records,
            path=path,
            relationship_pairs=relationship_pairs or [],
            fingerprint=_compute_fingerprint(data_root),
        )

    def to_db(self):
        """Return a read-only DuckDB in-memory connection with the vault loaded as tables and views."""
        import duckdb

        con = duckdb.connect()

        # Build pair lookup: (type_name, field_name) -> (base_table_name, is_primary)
        pair_lookup: dict[tuple[str, str], tuple[str, bool]] = {}
        for first, second in self.relationship_pairs:
            type_a, field_a = first.split(".", 1)
            type_b, field_b = second.split(".", 1)
            base = f"{type_a}__{field_a}"
            pair_lookup[(type_a, field_a)] = (base, True)
            pair_lookup[(type_b, field_b)] = (base, False)

        # Accumulate join table rows before creating tables (pairs merge two fields)
        join_rows: dict[str, list[tuple[str, str]]] = {}

        for type_name, type_schema in self.schema.types.items():
            recs = self.records.get(type_name, [])

            scalar_fields = [f for f in type_schema.fields if f.type not in _LINK_TYPES]

            # Pre-collect values per field for numeric refinement
            field_values: dict[str, list[Any]] = {f.name: [] for f in scalar_fields}
            for rec in recs:
                for f in scalar_fields:
                    field_values[f.name].append(rec.fields.get(f.name))

            # Build DDL for main type table
            col_defs = ["record VARCHAR PRIMARY KEY"]
            for f in scalar_fields:
                if f.type == FieldType.FORMULA:
                    effective = f.output_type or FieldType.UNKNOWN
                    if effective == FieldType.NUMBER:
                        db_type = _numeric_db_type(field_values[f.name])
                    else:
                        db_type = _FIELD_TO_DB_TYPE.get(effective, "VARCHAR")
                elif f.type == FieldType.NUMBER:
                    db_type = _numeric_db_type(field_values[f.name])
                else:
                    db_type = _FIELD_TO_DB_TYPE.get(f.type, "VARCHAR")
                col_defs.append(f'"{f.name}" {db_type}')

            con.execute(f'CREATE TABLE "{type_name}" ({", ".join(col_defs)})')

            # Populate main table
            if recs:
                placeholders = ", ".join(["?"] * (1 + len(scalar_fields)))
                insert_sql = f'INSERT INTO "{type_name}" VALUES ({placeholders})'
                rows = []
                for rec in recs:
                    row: list[Any] = [rec.name]
                    for f in scalar_fields:
                        row.append(_coerce_for_db(rec.fields.get(f.name), f.type))
                    rows.append(row)
                con.executemany(insert_sql, rows)

            # Collect join table rows for link fields
            for f in type_schema.fields:
                if f.type not in _LINK_TYPES:
                    continue
                pair_info = pair_lookup.get((type_name, f.name))
                table_name = pair_info[0] if pair_info else f"{type_name}__{f.name}"
                is_primary = pair_info[1] if pair_info else True

                if table_name not in join_rows:
                    join_rows[table_name] = []

                for rec in recs:
                    value = rec.fields.get(f.name)
                    if value is None:
                        continue
                    targets: list[str] = []
                    if isinstance(value, str):
                        parsed = _parse_wikilink(value)
                        if parsed:
                            targets.append(parsed[1])
                    elif isinstance(value, list):
                        for item in value:
                            if isinstance(item, str):
                                parsed = _parse_wikilink(item)
                                if parsed:
                                    targets.append(parsed[1])
                    for target in targets:
                        row_pair = (rec.name, target) if is_primary else (target, rec.name)
                        join_rows[table_name].append(row_pair)

        # Create join tables (dedup rows via set — composite PK collapses duplicates from both pair sides)
        for table_name, rows in join_rows.items():
            con.execute(
                f'CREATE TABLE "{table_name}" '
                f'(source VARCHAR, target VARCHAR, PRIMARY KEY (source, target))'
            )
            unique = list({(s, t) for s, t in rows})
            if unique:
                con.executemany(f'INSERT INTO "{table_name}" VALUES (?, ?)', unique)

        # Create reverse views for secondary fields in relationship pairs
        for first, second in self.relationship_pairs:
            type_a, field_a = first.split(".", 1)
            type_b, field_b = second.split(".", 1)
            base = f"{type_a}__{field_a}"
            view = f"{type_b}__{field_b}"
            con.execute(
                f'CREATE VIEW "{view}" AS '
                f'SELECT target AS source, source AS target FROM "{base}"'
            )

        return con
