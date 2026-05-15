from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .syntax import BasesCompiler
from .schema import FieldSchema, FieldType

if TYPE_CHECKING:
    from .vault import Vault

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


class Linter:
    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    def lint(self) -> list[LintViolation]:
        violations: list[LintViolation] = []
        compiler = BasesCompiler()

        for type_name, type_schema in self.vault.schema.types.items():
            known_fields = {f.name for f in type_schema.fields if f.type != FieldType.FORMULA}
            formula_fields = [f for f in type_schema.fields if f.type == FieldType.FORMULA]

            if self.vault.path is not None:
                base_file = self.vault.path / self.vault.schema.bases_folder / f"{type_name}.base"
                if not base_file.exists():
                    violations.append(LintViolation(
                        severity="error",
                        message=f"Missing .base file for type '{type_name}'",
                        type_name=type_name,
                    ))

            for f in formula_fields:
                fn = compiler.translate(f.formula or "")
                if hasattr(fn, "translation_error"):
                    violations.append(LintViolation(
                        severity="error",
                        message=f"Untranslatable formula: {fn.translation_error}",
                        type_name=type_name,
                        field_name=f.name,
                    ))

                for ref in _NOTE_REF_RE.findall(f.formula or ""):
                    if ref not in known_fields:
                        violations.append(LintViolation(
                            severity="warning",
                            message=f"Formula references unknown field 'note.{ref}'",
                            type_name=type_name,
                            field_name=f.name,
                        ))

                if f.output_type == FieldType.UNKNOWN:
                    recs = self.vault.records.get(type_name, [])
                    non_null = [r.fields.get(f.name) for r in recs if r.fields.get(f.name) is not None]
                    if non_null:
                        violations.append(LintViolation(
                            severity="warning",
                            message=f"Formula output type is inconsistent across records",
                            type_name=type_name,
                            field_name=f.name,
                        ))

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
