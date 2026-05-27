from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date as _date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import frontmatter

from .syntax import EvalContext
from .schema import FieldSchema, FieldType, Schema, infer_field_type, infer_link_target
from .record import Record
from .links import apply_dangling_refs, resolve_pairs
from .linter import Linter, LintViolation, topo_sort_formulas

if TYPE_CHECKING:
    from .accessors.db import DbAccessor
    from .accessors.dfs import DfsAccessor
    from .accessors.graph import GraphAccessor
    from .expander import Expander

logger = logging.getLogger(__name__)

_VALID_DANGLING_REFS = ("drop", "stub")
_VALID_UNTRANSLATABLE = ("drop", "error")


def _compute_fingerprint(data_root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(data_root.rglob("*.md")):
        h.update(str(p.relative_to(data_root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def coerce_target(observed: set[type]) -> type | None:
    """
    Return the least-lossy common Python type for a set of observed value types.

    Used when a field contains mixed numeric or date types across records and a
    single Arrow/pandas dtype must be chosen. Precedence (most to least specific):
    str > date-mixed-with-other > float > int. bool is a subtype of int in Python,
    so ``{bool, int}`` resolves to int rather than str.

    Parameters
    ----------
    observed : set of type
        Set of Python types seen across a field's non-null values.

    Returns
    -------
    type or None
        The target type to coerce all values to, or None when all values share
        a single type (no coercion needed).

    Notes
    -----
    Coercion rules:
      ``{int, float}``   → float
      ``{bool, int}``    → int  (bool ⊂ int in Python)
      ``{bool, float}``  → float
      any set with str   → str
      date + non-date    → str  (via isoformat to avoid silent truncation)
      single type        → None
    """
    if len(observed) <= 1:
        return None
    if str in observed:
        return str
    if any(issubclass(t, _date) for t in observed):
        return str  # date mixed with non-date → str
    if float in observed:
        return float
    if int in observed:
        return int
    return str


def coerce_value(val: Any, target: type) -> Any:
    """
    Convert a value to the target Python type, returning it unchanged on failure.

    Intentionally avoids an isinstance short-circuit so that bool values are
    correctly coerced to int (since ``isinstance(True, int)`` is True in Python,
    a short-circuit would leave booleans as-is when the target is int).

    Parameters
    ----------
    val : Any
        The value to coerce. None is passed through unchanged.
    target : type
        The Python type to coerce to. Supported: str, float, int.
        For str, ``datetime.date`` and ``datetime.datetime`` instances are
        converted via ``.isoformat()`` rather than ``str()``.

    Returns
    -------
    Any
        The coerced value, or ``val`` unchanged if coercion raises ValueError
        or TypeError.
    """
    if val is None:
        return val
    try:
        if target is str:
            return val.isoformat() if isinstance(val, _date) else str(val)
        if target is float:
            return float(val)
        if target is int:
            return int(val)
    except (ValueError, TypeError):
        pass
    return val


def _serialize_formula_result(v: Any) -> Any:
    from .syntax.runtime import _FileProxy
    if isinstance(v, _FileProxy):
        return f"[[{v._record.type_schema.name}/{v._record.name}]]"
    if isinstance(v, list):
        return [_serialize_formula_result(item) for item in v]
    return v


def _evaluate_formulas(
    schema: Schema,
    records: dict[str, list[Record]],
    proto: "Vault",
    path: Path,
    bases_folder: str,
    untranslatable_formulas: str,
    frozen_now: datetime,
) -> None:
    """
    Evaluate all formula fields for every type and write results back to records.

    Formula fields are executed in dependency order (topological sort); cyclic
    formulas are set to None. Untranslatable formulas (those that failed
    compilation) are handled per ``untranslatable_formulas``. After evaluation,
    infers and sets ``output_type`` and ``link_target`` on each FieldSchema.

    Each formula is called with an EvalContext giving it access to the record,
    the proto-vault, the base file path, and a frozen timestamp so all formulas
    in a load share the same ``now``.

    Parameters
    ----------
    schema : Schema
        The vault schema whose FORMULA fields are iterated.
    records : dict[str, list[Record]]
        Records mutated in-place: formula field values are written to
        ``rec.fields[formula_field.name]``.
    proto : Vault
        An incomplete Vault used as the context for cross-record formula
        lookups (e.g. ``file.backlinks``).
    path : Path
        Vault root, used to construct the base file path per type.
    bases_folder : str
        Subdirectory name for ``.base`` files, relative to ``path``.
    untranslatable_formulas : str
        ``"drop"`` sets untranslatable formula fields to None for all records;
        ``"error"`` raises ValueError immediately.
    frozen_now : datetime
        Timestamp passed to every EvalContext so that time-sensitive formulas
        are consistent within a single load.
    """
    for type_name, type_schema in schema.types.items():
        formula_fields = [f for f in type_schema.fields if f.type == FieldType.FORMULA]
        if not formula_fields:
            continue
        base_path = path / bases_folder / f"{type_name}.base"
        recs = records.get(type_name, [])
        ordered, cyclic = topo_sort_formulas(formula_fields)

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
                ctx = EvalContext(record=rec, vault=proto, base_path=base_path, now=frozen_now)
                try:
                    result = _serialize_formula_result(f.compiled(ctx))
                except Exception:
                    logger.debug("Formula '%s' failed on %s/%s", f.name, type_name, rec.name, exc_info=True)
                    result = None
                rec.fields[f.name] = result
                results.append(result)
            f.output_type = infer_field_type(results)
            if f.output_type in (FieldType.LINK, FieldType.LIST_LINKS, FieldType.LIST_MIXED):
                f.link_target = infer_link_target(results)

        for f in cyclic:
            for rec in recs:
                rec.fields[f.name] = None


def _apply_base_filters(
    schema: Schema,
    records: dict[str, list[Record]],
    proto: "Vault",
    path: Path,
    bases_folder: str,
    frozen_now: datetime,
) -> None:
    """
    Filter each type's records using the compiled filter from its ``.base`` file.

    Types without a ``compiled_filter`` are skipped. Records for which the filter
    raises are kept (fail-open) and a debug log entry is emitted. Dropped records
    are also logged at debug level.

    Parameters
    ----------
    schema : Schema
        Schema whose types are iterated; only types with ``compiled_filter`` set
        are processed.
    records : dict[str, list[Record]]
        Mutated in-place: each type's list is replaced with the filtered subset.
    proto : Vault
        Used as the vault context inside each EvalContext passed to the filter.
    path : Path
        Vault root, used to construct the base file path per type.
    bases_folder : str
        Subdirectory name for ``.base`` files, relative to ``path``.
    frozen_now : datetime
        Timestamp passed to every EvalContext for consistent time comparisons.
    """
    for type_name, type_schema in schema.types.items():
        if not type_schema.compiled_filter:
            continue
        base_path = path / bases_folder / f"{type_name}.base"
        before = records.get(type_name, [])
        after = []
        for rec in before:
            ctx = EvalContext(record=rec, vault=proto, base_path=base_path, now=frozen_now)
            try:
                keep = type_schema.compiled_filter(ctx)
            except Exception:
                logger.debug("Base filter failed on %s/%s", type_name, rec.name, exc_info=True)
                keep = True
            if keep:
                after.append(rec)
            else:
                logger.debug("Base filter dropped record: %s/%s", type_name, rec.name)
        records[type_name] = after


@dataclass
class Vault:
    """
    Central container for a loaded Obsidian vault.

    Holds all parsed records and their schema, and provides lazy-loaded access
    to DataFrame, graph, database, and expander interfaces. Normally constructed
    via ``Vault.from_vault()`` rather than directly.

    Parameters
    ----------
    schema : Schema
        Structural description of all types and their field schemas.
    records : dict[str, list[Record]]
        Mapping of type name to the list of parsed Record objects for that type.
    path : Path or None
        Absolute path to the vault root directory. None for in-memory vaults.
    relationship_pairs : list of tuple[str, str]
        Declared bidirectional link pairs, each as ``("TypeA.field", "TypeB.field")``.
        Used by ``resolve_pairs`` to reconcile both sides of a relationship.
    violations : list of LintViolation
        Lint violations detected at load time. Populated automatically by
        ``from_vault()``; empty for vaults created directly.
    fingerprint : str
        SHA-256 hash of all ``.md`` file paths and contents at load time.
        Used by ``is_stale`` to detect on-disk changes without re-reading files.
    """

    schema: Schema
    records: dict[str, list[Record]] = field(default_factory=dict)
    path: Optional[Path] = None
    relationship_pairs: list[tuple[str, str]] = field(default_factory=list)
    violations: list[LintViolation] = field(default_factory=list, repr=False)
    fingerprint: str = field(default="", repr=False)
    _load_kwargs: dict[str, Any] = field(default_factory=dict, repr=False)
    _backlinks_index: Optional[dict[str, list[str]]] = field(default=None, repr=False, compare=False, init=False)
    _db_cache: Optional["DbAccessor"] = field(default=None, repr=False, compare=False, init=False)
    _dfs_cache: Optional["DfsAccessor"] = field(default=None, repr=False, compare=False, init=False)
    _graph_cache: Optional["GraphAccessor"] = field(default=None, repr=False, compare=False, init=False)
    _expander_cache: Optional["Expander"] = field(default=None, repr=False, compare=False, init=False)

    @property
    def db(self) -> "DbAccessor":
        if self._db_cache is None:
            from .accessors.db import DbAccessor
            self._db_cache = DbAccessor(self)
        return self._db_cache

    @property
    def dfs(self) -> "DfsAccessor":
        if self._dfs_cache is None:
            from .accessors.dfs import DfsAccessor
            self._dfs_cache = DfsAccessor(self)
        return self._dfs_cache

    @property
    def graph(self) -> "GraphAccessor":
        if self._graph_cache is None:
            from .accessors.graph import GraphAccessor
            self._graph_cache = GraphAccessor(self)
        return self._graph_cache

    @property
    def expand(self) -> "Expander":
        if self._expander_cache is None:
            from .expander import Expander
            self._expander_cache = Expander(self)
        return self._expander_cache

    def _resolve_pairs(self, expand_to_lists: bool = True) -> None:
        resolve_pairs(self, expand_to_lists=expand_to_lists)

    @property
    def is_stale(self) -> bool:
        """
        Return True if any ``.md`` file in the vault has changed since load.

        Recomputes the SHA-256 fingerprint of the data folder on each call and
        compares it against the fingerprint captured at load time. Always returns
        False when ``path`` is None (in-memory vault).

        Returns
        -------
        bool
        """
        if self.path is None:
            return False
        data_root = self.path / self.schema.data_folder
        return _compute_fingerprint(data_root) != self.fingerprint

    def reload(self) -> "Vault":
        """
        Reload the vault from disk using the same parameters as the original load.

        Replaces ``schema``, ``records``, ``violations``, and ``fingerprint`` in-place
        and invalidates all accessor caches. Returns ``self`` so the call can be chained.

        Returns
        -------
        Vault
            The same Vault instance, updated to reflect current on-disk state.
        """
        new = Vault.from_vault(self.path, **self._load_kwargs)
        self.schema = new.schema
        self.records = new.records
        self.violations = new.violations
        self.fingerprint = new.fingerprint
        self._backlinks_index = None
        self._db_cache = None
        self._dfs_cache = None
        self._graph_cache = None
        self._expander_cache = None
        return self

    def lint(self, *, homogeneity_threshold: float = 0.0) -> list[LintViolation]:
        """
        Run all linting rules against the vault and return the violations found.

        Parameters
        ----------
        homogeneity_threshold : float
            Passed to the Linter. Fields present in fewer than this fraction of
            a type's records trigger an R-1 homogeneity violation. Default 0.0
            disables homogeneity checking.

        Returns
        -------
        list of LintViolation
            All violations detected, across all rule categories.
        """
        return Linter(self, homogeneity_threshold=homogeneity_threshold).lint()

    def _get_backlinks_index(self) -> "dict[str, list[str]]":
        if self._backlinks_index is None:
            self._backlinks_index = self._build_backlinks_index()
        return self._backlinks_index

    def _build_backlinks_index(self) -> "dict[str, list[str]]":
        """Build record_name → [wikilink, ...] index for all cross-record links."""
        from .links import parse_wikilink_name
        index: dict[str, list[str]] = {}
        for type_name, recs in self.records.items():
            for rec in recs:
                back_wikilink = f"[[{type_name}/{rec.name}]]"
                seen_targets: set[str] = set()
                for v in rec.fields.values():
                    items = v if isinstance(v, list) else [v]
                    for item in items:
                        if isinstance(item, str):
                            name = parse_wikilink_name(item)
                            if name and name not in seen_targets:
                                seen_targets.add(name)
                                index.setdefault(name, [])
                                if back_wikilink not in index[name]:
                                    index[name].append(back_wikilink)
        return index

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
        """
        Parse a vault directory tree into a fully loaded Vault instance.

        Performs the full load pipeline in order: build schema, parse record
        frontmatter, evaluate formula fields, optionally apply base filters,
        run the linter, resolve dangling wikilinks, and reconcile relationship
        pairs. All load parameters are stored in ``_load_kwargs`` so that
        ``reload()`` can reproduce the same Vault.

        Parameters
        ----------
        path : str or Path
            Root directory of the vault. Must contain ``data_folder`` and
            optionally ``bases_folder`` as subdirectories.
        data_folder : str
            Name of the subdirectory holding one folder per record type.
        bases_folder : str
            Name of the subdirectory holding ``.base`` YAML files.
        relationship_pairs : list of tuple[str, str] or None
            Bidirectional link pairs to reconcile, each as
            ``("TypeA.field", "TypeB.field")``. Pass None or omit to skip
            pair reconciliation.
        ignore_empty : bool
            When True, types with no frontmatter fields are omitted from the
            schema and records. Useful to skip placeholder folders.
        dangling_refs : str
            How to handle wikilinks whose target record does not exist.
            ``"drop"`` removes them from the field; ``"stub"`` creates a
            placeholder Record so graph edges are preserved.
        untranslatable_formulas : str
            How to handle formula fields that could not be compiled.
            ``"drop"`` sets them to None for all records; ``"error"`` raises
            immediately.
        apply_base_filters : bool
            When True, records not matching the filter expression from their
            type's ``.base`` file are excluded after formula evaluation.
        expand_to_lists : bool
            When True, a singular LINK field that pair reconciliation would
            expand to multiple targets is promoted to a list with a warning.
            When False, that situation raises instead.

        Returns
        -------
        Vault
            A fully loaded Vault with schema, records, violations, and fingerprint
            populated. Accessor caches (db, dfs, graph, expand) are empty and
            populated lazily on first access.

        Raises
        ------
        ValueError
            When ``dangling_refs`` or ``untranslatable_formulas`` is not one of
            the accepted values, or when ``expand_to_lists`` is False and pair
            reconciliation would produce multiple targets for a singular LINK field.
        """
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

        # Proto-vault gives formula evaluation access to the full record graph
        # (needed for file.backlinks and other cross-record lookups) before the
        # real Vault object is constructed.
        _proto = cls(schema=schema, records=records, path=path)

        _evaluate_formulas(schema, records, _proto, path, bases_folder, untranslatable_formulas, frozen_now)

        if apply_base_filters:
            _apply_base_filters(schema, records, _proto, path, bases_folder, frozen_now)

        vault = cls(
            schema=schema,
            records=records,
            path=path,
            relationship_pairs=relationship_pairs or [],
            fingerprint=_compute_fingerprint(data_root),
            _load_kwargs={
                "data_folder": data_folder,
                "bases_folder": bases_folder,
                "relationship_pairs": relationship_pairs,
                "ignore_empty": ignore_empty,
                "dangling_refs": dangling_refs,
                "untranslatable_formulas": untranslatable_formulas,
                "apply_base_filters": apply_base_filters,
                "expand_to_lists": expand_to_lists,
            },
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

