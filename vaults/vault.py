from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import frontmatter

from .syntax import BasesCompiler, EvalContext
from .schema import FieldSchema, FieldType, Schema, infer_field_type
from .record import Record
from .links import apply_dangling_refs, resolve_pairs

if TYPE_CHECKING:
    from .accessors.db import DbAccessor
    from .accessors.dfs import DfsAccessor
    from .accessors.graph import GraphAccessor

logger = logging.getLogger(__name__)

_VALID_DANGLING_REFS = ("drop", "stub")


def _compute_fingerprint(data_root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(data_root.rglob("*.md")):
        h.update(str(p.relative_to(data_root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


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
class Vault:
    schema: Schema
    records: dict[str, list[Record]] = field(default_factory=dict)
    path: Optional[Path] = None
    relationship_pairs: list[tuple[str, str]] = field(default_factory=list)
    fingerprint: str = field(default="", repr=False)

    @property
    def db(self) -> "DbAccessor":
        from .accessors.db import DbAccessor
        return DbAccessor(self)

    @property
    def dfs(self) -> "DfsAccessor":
        from .accessors.dfs import DfsAccessor
        return DfsAccessor(self)

    @property
    def graph(self) -> "GraphAccessor":
        from .accessors.graph import GraphAccessor
        return GraphAccessor(self)

    def _resolve_pairs(self, expand_to_lists: bool = True) -> None:
        resolve_pairs(self, expand_to_lists=expand_to_lists)

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

        records = apply_dangling_refs(dangling_refs, schema, records)

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

