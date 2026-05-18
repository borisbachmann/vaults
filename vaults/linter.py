"""Relational and cross-record linting for Obsidian vaults.

Division of labor with the Obsidian community Linter plugin
------------------------------------------------------------
The Obsidian Linter plugin handles *file-level* YAML and Markdown hygiene and
should be run alongside this module. Specifically, defer to it for:

- YAML formatting: quoting style, array style, trailing whitespace.
- Within-file key ordering and blank lines around frontmatter.
- Generic tag and alias formatting.

This module covers *relational and cross-record* concerns the plugin cannot see:

- R-1: fields missing from some records of a type (homogeneity).
- R-2: field values with inconsistent Python types across records (type drift).
- R-3: a link field pointing to more than one target folder.
- R-4: links targeting a folder that is not a known type.
- R-5: backlink missing from one side of a declared relationship pair.
- R-6: a link pointing to a known-type file that does not exist on disk.
- S-*: structural issues (missing .base files, subdirectories in type folders, etc.).
- F-*: formula translation and dependency issues.
"""
from __future__ import annotations

import datetime
import re
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .syntax import BasesCompiler
from .schema import FieldSchema, FieldType
from .links import LINK_TYPES, iter_link_names, parse_wikilink, wikilink_target_folder

if TYPE_CHECKING:
    from .vault import Vault

_FORMULA_DEP_RE = re.compile(r"\bformula\.(\w+)")
_NOTE_REF_RE = re.compile(r"\bnote\.(\w+)")
_CROSS_PLATFORM_INVALID = re.compile(r'[<>:"/\\|?*]')

RULES: dict[str, str] = {
    "S-1": "Missing .base file for a type folder",
    "S-2": "Bases folder not found in vault root",
    "S-3": "Base file without matching type folder",
    "S-4": "Non-record file or subdirectory inside a type folder",
    "S-5": "Cross-platform incompatible file or folder name",
    "R-1": "Field not present across all records of a type",
    "R-2": "Field value type inconsistent across records of a type",
    "R-3": "Links in one field point to multiple target folders",
    "R-4": "Link targets a folder outside known types",
    "R-5": "Paired link field has missing backlinks in source files",
    "R-6": "Link target file does not exist on disk",
    "F-1": "Formula not translatable — possibly malformed in source",
    "F-2": "Formula references an unknown field",
    "F-3": "Circular dependency between formula fields",
    "F-4": "Formula output type inconsistent across records",
}


@dataclass
class LintViolation:
    severity: str  # "error" | "warning" | "suggestion"
    message: str
    rule: Optional[str] = None
    type_name: Optional[str] = None
    field_name: Optional[str] = None
    details: Optional[dict] = None

    def __str__(self) -> str:
        header = f"[{self.rule}] {RULES.get(self.rule, '')}" if self.rule else ""
        parts = [header, self.message] if header else [self.message]
        return " — ".join(parts)

    def __repr__(self) -> str:
        return self.__str__()


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


def _is_cross_platform_safe(name: str) -> bool:
    if _CROSS_PLATFORM_INVALID.search(name):
        return False
    if unicodedata.normalize("NFC", name) != name:
        return False
    return True


class Linter:
    def __init__(self, vault: Vault, *, homogeneity_threshold: float = 0.0) -> None:
        self.vault = vault
        self.homogeneity_threshold = homogeneity_threshold

    def lint(self) -> list[LintViolation]:
        violations: list[LintViolation] = []
        violations.extend(self._check_structure())
        violations.extend(self._check_records())
        violations.extend(self._check_formulas())
        return violations

    # -- Structure checks (S-*) -----------------------------------------------

    def _check_structure(self) -> list[LintViolation]:
        violations: list[LintViolation] = []
        if self.vault.path is None:
            return violations

        bases_dir = self.vault.path / self.vault.schema.bases_folder
        data_dir = self.vault.path / self.vault.schema.data_folder

        # S-2: bases folder missing
        if not bases_dir.exists():
            violations.append(LintViolation(
                severity="warning",
                message="Bases folder not found",
                rule="S-2",
            ))
        else:
            # S-1: type folder without matching .base file
            for type_name in self.vault.schema.types:
                if not (bases_dir / f"{type_name}.base").exists():
                    violations.append(LintViolation(
                        severity="error",
                        message=f"Missing .base file for type '{type_name}'",
                        rule="S-1",
                        type_name=type_name,
                    ))

            # S-3: .base file without matching type folder
            for base_file in sorted(bases_dir.glob("*.base")):
                if base_file.stem not in self.vault.schema.types:
                    violations.append(LintViolation(
                        severity="warning",
                        message=f"Base file '{base_file.name}' has no matching type folder",
                        rule="S-3",
                    ))

        # S-4: non-.md files or subdirectories in type folders
        for type_name in self.vault.schema.types:
            type_dir = data_dir / type_name
            if not type_dir.exists():
                continue
            for child in sorted(type_dir.iterdir()):
                if child.is_dir():
                    violations.append(LintViolation(
                        severity="warning",
                        message=f"Subdirectory '{child.name}' inside type folder",
                        rule="S-4",
                        type_name=type_name,
                    ))
                elif child.suffix != ".md":
                    violations.append(LintViolation(
                        severity="warning",
                        message=f"Non-record file '{child.name}' inside type folder",
                        rule="S-4",
                        type_name=type_name,
                    ))

        # S-5: cross-platform filename compatibility
        for type_name in self.vault.schema.types:
            if not _is_cross_platform_safe(type_name):
                violations.append(LintViolation(
                    severity="warning",
                    message=f"Type name '{type_name}' contains cross-platform incompatible characters",
                    rule="S-5",
                    type_name=type_name,
                ))
            for rec in self.vault.records.get(type_name, []):
                if not _is_cross_platform_safe(rec.name):
                    violations.append(LintViolation(
                        severity="warning",
                        message=f"Record name '{rec.name}' contains cross-platform incompatible characters",
                        rule="S-5",
                        type_name=type_name,
                    ))

        return violations

    # -- Record checks (R-*) --------------------------------------------------

    def _check_records(self) -> list[LintViolation]:
        violations: list[LintViolation] = []

        for type_name, type_schema in self.vault.schema.types.items():
            recs = self.vault.records.get(type_name, [])
            total = len(recs)

            # R-1: field homogeneity
            if total > 0:
                non_formula_fields = [f for f in type_schema.fields if f.type != FieldType.FORMULA]
                for f in non_formula_fields:
                    has = [r.name for r in recs if f.name in r.fields]
                    missing = [r.name for r in recs if f.name not in r.fields]
                    proportion = len(has) / total
                    minority = min(proportion, 1 - proportion)
                    if minority > self.homogeneity_threshold:
                        pct = round(proportion * 100, 1)
                        details = {"present": sorted(has), "missing": sorted(missing)}
                        violations.append(LintViolation(
                            severity="warning",
                            message=f"Field '{f.name}' present in {pct}% of records ({len(has)}/{total})",
                            rule="R-1",
                            type_name=type_name,
                            field_name=f.name,
                            details=details,
                        ))

            # R-2: type drift — value Python type varies across records for scalar fields
            scalar_fields = [
                f for f in type_schema.fields
                if f.type not in LINK_TYPES and f.type != FieldType.FORMULA
            ]
            for f in scalar_fields:
                by_type: dict[str, list[str]] = {}
                for rec in recs:
                    val = rec.fields.get(f.name)
                    if val is not None:
                        # YAML coerces ISO date strings to datetime.date while
                        # non-standard strings like "2026-05-MM" stay as str.
                        # Treat both as the same category to avoid spurious drift.
                        type_key = "str" if isinstance(val, datetime.date) else type(val).__name__
                        by_type.setdefault(type_key, []).append(rec.name)
                if len(by_type) > 1:
                    violations.append(LintViolation(
                        severity="warning",
                        message=(
                            f"Field '{f.name}' has inconsistent value types across records: "
                            f"{sorted(by_type)}"
                        ),
                        rule="R-2",
                        type_name=type_name,
                        field_name=f.name,
                        details=by_type,
                    ))

            # R-3 / R-4 / R-6: collect link violations per field
            known_types = set(self.vault.schema.types.keys())
            data_root = (
                self.vault.path / self.vault.schema.data_folder
                if self.vault.path else None
            )
            folder_violations: dict[str, dict[str, list[str]]] = {}
            orphaned_violations: dict[str, dict[str, list[str]]] = {}

            for f in type_schema.fields:
                if f.type not in LINK_TYPES:
                    continue

                # Collect links by target folder: {folder: [record_names]}
                by_folder: dict[str, list[str]] = {}
                for rec in recs:
                    value = rec.fields.get(f.name)
                    if value is None:
                        continue
                    items = value if isinstance(value, list) else [value]
                    for item in items:
                        if not isinstance(item, str):
                            continue
                        parsed = parse_wikilink(item)
                        if parsed is None:
                            continue
                        folder, target_name = parsed
                        by_folder.setdefault(folder, []).append(rec.name)
                        if folder not in known_types:
                            # R-4: link targets a folder outside known types
                            folder_violations.setdefault(rec.name, {}).setdefault(f.name, []).append(item)
                        elif data_root is not None:
                            # R-6: known type, but the target file is missing on disk
                            if not (data_root / folder / f"{target_name}.md").exists():
                                orphaned_violations.setdefault(rec.name, {}).setdefault(f.name, []).append(item)

                # R-3: field has links pointing to more than one folder
                if len(by_folder) > 1:
                    violations.append(LintViolation(
                        severity="warning",
                        message=f"Field '{f.name}' has links to multiple folders: {sorted(by_folder.keys())}",
                        rule="R-3",
                        type_name=type_name,
                        field_name=f.name,
                        details=by_folder,
                    ))

            if folder_violations:
                violations.append(LintViolation(
                    severity="warning",
                    message="Links target folders outside known types",
                    rule="R-4",
                    type_name=type_name,
                    details={type_name: folder_violations},
                ))

            if orphaned_violations:
                violations.append(LintViolation(
                    severity="warning",
                    message="Links target files that do not exist on disk",
                    rule="R-6",
                    type_name=type_name,
                    details={type_name: orphaned_violations},
                ))

            # F-4: formula output type inconsistent
            formula_fields = [f for f in type_schema.fields if f.type == FieldType.FORMULA]
            for f in formula_fields:
                if f.output_type == FieldType.UNKNOWN:
                    non_null = [r.fields.get(f.name) for r in recs if r.fields.get(f.name) is not None]
                    if non_null:
                        violations.append(LintViolation(
                            severity="warning",
                            message="Formula output type is inconsistent across records",
                            rule="F-4",
                            type_name=type_name,
                            field_name=f.name,
                        ))

        # R-5: incomplete pairs — backlinks missing in source files
        for first, second in self.vault.relationship_pairs:
            type_a, field_a = first.split(".", 1)
            type_b, field_b = second.split(".", 1)

            # Edges from side A: a → b
            edges_a: set[tuple[str, str]] = set()
            for rec in self.vault.records.get(type_a, []):
                for name in iter_link_names(rec.fields.get(field_a)):
                    edges_a.add((rec.name, name))

            # Edges from side B: a ← b (inverted to match A's direction)
            edges_b: set[tuple[str, str]] = set()
            for rec in self.vault.records.get(type_b, []):
                for name in iter_link_names(rec.fields.get(field_b)):
                    edges_b.add((name, rec.name))

            # Missing on side A: edges only asserted by B
            missing_a: dict[str, list[str]] = {}
            for a_name, b_name in sorted(edges_b - edges_a):
                missing_a.setdefault(a_name, []).append(b_name)
            if missing_a:
                total = sum(len(v) for v in missing_a.values())
                violations.append(LintViolation(
                    severity="suggestion",
                    message=f"{first}: {total} backlinks missing across {len(missing_a)} records",
                    rule="R-5",
                    type_name=type_a,
                    field_name=field_a,
                    details=missing_a,
                ))

            # Missing on side B: edges only asserted by A
            missing_b: dict[str, list[str]] = {}
            for a_name, b_name in sorted(edges_a - edges_b):
                missing_b.setdefault(b_name, []).append(a_name)
            if missing_b:
                total = sum(len(v) for v in missing_b.values())
                violations.append(LintViolation(
                    severity="suggestion",
                    message=f"{second}: {total} backlinks missing across {len(missing_b)} records",
                    rule="R-5",
                    type_name=type_b,
                    field_name=field_b,
                    details=missing_b,
                ))

        return violations

    # -- Formula checks (F-*) -------------------------------------------------

    def _check_formulas(self) -> list[LintViolation]:
        violations: list[LintViolation] = []
        compiler = BasesCompiler()
        for type_name, type_schema in self.vault.schema.types.items():
            known_fields = {f.name for f in type_schema.fields if f.type != FieldType.FORMULA}
            formula_fields = [f for f in type_schema.fields if f.type == FieldType.FORMULA]

            for f in formula_fields:
                fn = compiler.translate(f.formula or "")
                if hasattr(fn, "translation_error"):
                    violations.append(LintViolation(
                        severity="warning",
                        message=f"Untranslatable formula: {fn.translation_error}",
                        rule="F-1",
                        type_name=type_name,
                        field_name=f.name,
                    ))

                for ref in _NOTE_REF_RE.findall(f.formula or ""):
                    if ref not in known_fields:
                        violations.append(LintViolation(
                            severity="warning",
                            message=f"Formula references unknown field 'note.{ref}'",
                            rule="F-2",
                            type_name=type_name,
                            field_name=f.name,
                        ))

            if formula_fields:
                _, cyclic = _topo_sort_formulas(formula_fields)
                for f in cyclic:
                    violations.append(LintViolation(
                        severity="error",
                        message="Cyclic formula dependency",
                        rule="F-3",
                        type_name=type_name,
                        field_name=f.name,
                    ))

        return violations
