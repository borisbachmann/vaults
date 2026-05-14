from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import frontmatter

from .formula import BasesCompiler, EvalContext
from .schema import FieldSchema, FieldType, Schema, TypeSchema, infer_field_type

if TYPE_CHECKING:
    from .accessors.dfs import DfsAccessor
    from .accessors.graph import GraphAccessor

logger = logging.getLogger(__name__)

_WIKILINK_RE = re.compile(r"^\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]$")
_VALID_DANGLING_REFS = ("drop", "stub")

# FieldType → DuckDB column type (LINK and LIST_LINKS produce join tables, not columns)
_FIELD_TO_DB_TYPE: dict[FieldType, str] = {
    FieldType.STRING: "VARCHAR",
    FieldType.INTEGER: "BIGINT",
    FieldType.NUMBER: "DOUBLE",
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


def _iter_link_names(value: Any) -> list[str]:
    """Extract record name stems from a raw link field value (string or list)."""
    if value is None:
        return []
    if isinstance(value, str):
        parsed = _parse_wikilink(value)
        return [parsed[1]] if parsed else []
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str):
                parsed = _parse_wikilink(item)
                if parsed:
                    result.append(parsed[1])
        return result
    return []


def _write_link_field(
    records: list,
    field_name: str,
    record_to_targets: dict[str, set[str]],
    link_target_folder: str,
    field_schema: Optional[FieldSchema],
    expand_to_lists: bool,
) -> None:
    """Write reconciled link targets back to a field on each record.

    Preserves existing wikilinks in their original order; appends any new
    targets (sorted for determinism) discovered from the paired side.
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
            parsed = _parse_wikilink(existing_value)
            if parsed:
                existing_wikilinks.append(existing_value)
                existing_names.add(parsed[1])
        elif isinstance(existing_value, list):
            for item in existing_value:
                if isinstance(item, str):
                    parsed = _parse_wikilink(item)
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


def _compute_fingerprint(data_root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(data_root.rglob("*.md")):
        h.update(str(p.relative_to(data_root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()



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

    @property
    def dfs(self) -> "DfsAccessor":
        from .accessors.dfs import DfsAccessor
        return DfsAccessor(self)

    @property
    def graph(self) -> "GraphAccessor":
        from .accessors.graph import GraphAccessor
        return GraphAccessor(self)

    def _resolve_pairs(self, expand_to_lists: bool = True) -> None:
        """Reconcile paired link fields across all records in-place.

        For each declared pair (TypeA.field_x, TypeB.field_y), computes the
        outer union of edges asserted on either side and writes the complete
        set back to both fields on every relevant record. After this runs,
        all downstream consumers (.dfs, .db, .graph) see fully populated
        link fields and need no pair-awareness of their own.

        A pre-loop snapshot of all pair-field values is taken so that writes
        from one pair never corrupt the edge collection of another pair that
        shares the same field name on the secondary side.
        """
        if not self.relationship_pairs:
            return

        field_lookup: dict[tuple[str, str], FieldSchema] = {
            (type_name, f.name): f
            for type_name, type_schema in self.schema.types.items()
            for f in type_schema.fields
        }

        # Snapshot initial link values for all pair fields before any mutation.
        pair_fields: set[tuple[str, str]] = set()
        for first, second in self.relationship_pairs:
            type_a, field_a = first.split(".", 1)
            type_b, field_b = second.split(".", 1)
            pair_fields.add((type_a, field_a))
            pair_fields.add((type_b, field_b))

        snapshot: dict[tuple[str, str, str], Any] = {}
        for type_name, field_name in pair_fields:
            for rec in self.records.get(type_name, []):
                snapshot[(type_name, rec.name, field_name)] = rec.fields.get(field_name)

        for first, second in self.relationship_pairs:
            type_a, field_a = first.split(".", 1)
            type_b, field_b = second.split(".", 1)

            schema_fa = field_lookup.get((type_a, field_a))
            schema_fb = field_lookup.get((type_b, field_b))

            # Outer union of (a_name, b_name) edges declared on either side,
            # read from the snapshot so earlier pair writes don't bleed through.
            edges: set[tuple[str, str]] = set()
            for rec in self.records.get(type_a, []):
                for name in _iter_link_names(snapshot.get((type_a, rec.name, field_a))):
                    edges.add((rec.name, name))
            for rec in self.records.get(type_b, []):
                for name in _iter_link_names(snapshot.get((type_b, rec.name, field_b))):
                    edges.add((name, rec.name))  # inverted: B→A becomes (a, b)

            if not edges:
                continue

            a_to_bs: dict[str, set[str]] = defaultdict(set)
            b_to_as: dict[str, set[str]] = defaultdict(set)
            for a_name, b_name in edges:
                a_to_bs[a_name].add(b_name)
                b_to_as[b_name].add(a_name)

            link_target_b = (schema_fa.link_target if schema_fa else None) or type_b
            _write_link_field(
                records=self.records.get(type_a, []),
                field_name=field_a,
                record_to_targets=a_to_bs,
                link_target_folder=link_target_b,
                field_schema=schema_fa,
                expand_to_lists=expand_to_lists,
            )
            if schema_fa is None and a_to_bs and type_a in self.schema.types:
                self.schema.types[type_a].fields.append(
                    FieldSchema(name=field_a, type=FieldType.LIST_LINKS, link_target=link_target_b)
                )

            link_target_a = (schema_fb.link_target if schema_fb else None) or type_a
            _write_link_field(
                records=self.records.get(type_b, []),
                field_name=field_b,
                record_to_targets=b_to_as,
                link_target_folder=link_target_a,
                field_schema=schema_fb,
                expand_to_lists=expand_to_lists,
            )
            if schema_fb is None and b_to_as and type_b in self.schema.types:
                self.schema.types[type_b].fields.append(
                    FieldSchema(name=field_b, type=FieldType.LIST_LINKS, link_target=link_target_a)
                )

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
        expand_to_lists: bool = True,
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

        vault = cls(
            schema=schema,
            records=records,
            path=path,
            relationship_pairs=relationship_pairs or [],
            fingerprint=_compute_fingerprint(data_root),
        )
        vault._resolve_pairs(expand_to_lists=expand_to_lists)
        return vault

    def to_db(self):
        """Return a read-only DuckDB in-memory connection with the vault loaded as tables and views."""
        import duckdb

        con = duckdb.connect()

        for type_name, type_schema in self.schema.types.items():
            recs = self.records.get(type_name, [])

            scalar_fields = [f for f in type_schema.fields if f.type not in _LINK_TYPES]

            col_defs = ["record VARCHAR PRIMARY KEY"]
            for f in scalar_fields:
                effective = (f.output_type or FieldType.UNKNOWN) if f.type == FieldType.FORMULA else f.type
                db_type = _FIELD_TO_DB_TYPE.get(effective, "VARCHAR")
                col_defs.append(f'"{f.name}" {db_type}')

            con.execute(f'CREATE TABLE "{type_name}" ({", ".join(col_defs)})')

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

            for f in type_schema.fields:
                if f.type not in _LINK_TYPES:
                    continue
                table_name = f"{type_name}__{f.name}"
                rows = []
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
                        rows.append((rec.name, target))
                con.execute(
                    f'CREATE TABLE "{table_name}" '
                    f'(source VARCHAR, target VARCHAR, PRIMARY KEY (source, target))'
                )
                unique = list({(s, t) for s, t in rows})
                if unique:
                    con.executemany(f'INSERT INTO "{table_name}" VALUES (?, ?)', unique)

        return con
