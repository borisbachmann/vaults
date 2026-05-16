from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import frontmatter

from .syntax import EvalContext
from .schema import FieldSchema, FieldType, Schema, infer_field_type
from .record import Record
from .links import apply_dangling_refs, resolve_pairs
from .linter import Linter, LintViolation, _topo_sort_formulas

if TYPE_CHECKING:
    from .accessors.db import DbAccessor
    from .accessors.dfs import DfsAccessor
    from .accessors.graph import GraphAccessor

logger = logging.getLogger(__name__)

_VALID_DANGLING_REFS = ("drop", "stub")
_VALID_UNTRANSLATABLE = ("drop", "error")


def _compute_fingerprint(data_root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(data_root.rglob("*.md")):
        h.update(str(p.relative_to(data_root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


@dataclass
class Vault:
    schema: Schema
    records: dict[str, list[Record]] = field(default_factory=dict)
    path: Optional[Path] = None
    relationship_pairs: list[tuple[str, str]] = field(default_factory=list)
    violations: list = field(default_factory=list, repr=False)
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

    def lint(self, *, homogeneity_threshold: float = 0.0) -> list[LintViolation]:
        return Linter(self, homogeneity_threshold=homogeneity_threshold).lint()

    @classmethod
    def from_vault(
        cls,
        path: str | Path,
        data_folder: str = "data",
        bases_folder: str = "bases",
        relationship_pairs: Optional[list[tuple[str, str]]] = None,
        ignore_empty: bool = False,
        dangling_refs: str = "drop",
        untranslatable_formulas: str = "drop",
        apply_base_filters: bool = False,
        expand_to_lists: bool = True,
    ) -> "Vault":
        if dangling_refs not in _VALID_DANGLING_REFS:
            raise ValueError(f"dangling_refs must be one of {_VALID_DANGLING_REFS}; got {dangling_refs!r}")
        if untranslatable_formulas not in _VALID_UNTRANSLATABLE:
            raise ValueError(f"untranslatable_formulas must be one of {_VALID_UNTRANSLATABLE}; got {untranslatable_formulas!r}")

        path = Path(path).resolve()
        data_root = path / data_folder
        schema = Schema._from_vault(path, data_folder=data_folder, bases_folder=bases_folder, ignore_empty=ignore_empty)

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

        frozen_now = datetime.now()

        for type_name, type_schema in schema.types.items():
            formula_fields = [f for f in type_schema.fields if f.type == FieldType.FORMULA]
            if not formula_fields:
                continue
            base_path = path / bases_folder / f"{type_name}.base"
            recs = records.get(type_name, [])
            ordered, cyclic = _topo_sort_formulas(formula_fields)

            for f in ordered:
                if hasattr(f.compiled, "translation_error"):
                    if untranslatable_formulas == "error":
                        raise ValueError(
                            f"Untranslatable formula '{f.name}' on type '{type_name}': {f.compiled.translation_error}"
                        )
                    for rec in recs:
                        rec.fields[f.name] = None
                    continue
                results = []
                for rec in recs:
                    ctx = EvalContext(record=rec, vault=None, base_path=base_path, now=frozen_now)
                    try:
                        result = f.compiled(ctx)
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
                if not type_schema.compiled_filter:
                    continue
                base_path = path / bases_folder / f"{type_name}.base"
                before = records.get(type_name, [])
                after = []
                for rec in before:
                    ctx = EvalContext(record=rec, vault=None, base_path=base_path, now=frozen_now)
                    try:
                        keep = type_schema.compiled_filter(ctx)
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

        vault.violations = vault.lint()

        if vault.violations:
            from collections import Counter
            from .linter import RULES
            counts = Counter(v.rule for v in vault.violations)
            parts = [f"{n}x {RULES.get(rule, rule)}" for rule, n in counts.items()]
            logger.warning("Vault loaded with %d violation(s): %s", len(vault.violations), "; ".join(parts))

        records = apply_dangling_refs(dangling_refs, schema, records)
        vault.records = records
        vault._resolve_pairs(expand_to_lists=expand_to_lists)
        return vault

