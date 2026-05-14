# Bases Integration & FormulaTranslator — Design Instructions

## Implementation Status

### Done
- [x] `vaults/formula/` package split into four submodules:
  - `context.py` — `EvalContext` dataclass (isolated to avoid circular imports with `vault.py`)
  - `grammar.py` — LALR grammar string and `_parser` singleton
  - `runtime.py` — namespace proxies (`_FileProxy`, `_NoteProxy`, `_ThisProxy`), safe operators, method/global dispatch tables
  - `compiler.py` — `BasesCompiler` class with `translate()`, `translate_filter()`, full `_compile` dispatch
- [x] Grammar covers full Bases expression syntax (operators, literals, method chains, regex, implicit lambda vars)
- [x] `BasesCompiler.translate()` — returns `(EvalContext) -> Any` callable; attaches `translation_error` on failure
- [x] `BasesCompiler.translate_filter()` — bool-coercing wrapper, preserves `translation_error`
- [x] Implicit variable scoping (`value`, `index`, `acc`) for `filter()`, `map()`, `reduce()`
- [x] All Bases method and global function dispatch (string, number, list, date, regex, object, link)
- [x] Thread-safe compilation (`errors` list passed as parameter, no instance state)
- [x] `_c_neg` single-evaluation fix

### Next: Schema integration (Section 1)
- [ ] Add `FieldType.FORMULA = "formula"` to `schema.py`
- [ ] Add `formula: Optional[str]` and `output_type: Optional[FieldType]` to `FieldSchema`
- [ ] Add `base_filter: Optional[str]` to `TypeSchema`
- [ ] `Schema.from_vault()` — after frontmatter pass, read each `bases/{type_name}.base` YAML file; extract `formulas:` block (append `FieldSchema(type=FORMULA, formula=...)`) and `filters:` block (set `TypeSchema.base_filter`)

### Then: Evaluation pass (Section 4)
- [ ] In `Vault.from_vault()`, after loading all records: topological sort of formula fields per type (detect `formula.X` dependencies); evaluate in order using `BasesCompiler`; materialize results into `Record.fields`; infer `FieldSchema.output_type` from result values
- [ ] Detect and report cyclic formula dependencies (evaluate to `None`, surface via lint)
- [ ] Optional `apply_base_filters=True` parameter — translate `TypeSchema.base_filter` and drop non-matching records

### Then: Lint checks (Section 5)
- [ ] Untranslatable formula (`translation_error` attribute present) → error
- [ ] Cyclic formula dependency → error
- [ ] `note.X` referencing unknown field → warning
- [ ] Formula output type inconsistent across records → warning
- [ ] Missing `.base` file for a type → error

## Overview

Obsidian Bases define computed fields (formulas) and filters per type. These live in `.base` files under `bases/`, one per type, and are part of the vault's source of truth alongside frontmatter. The goal is to:

1. Parse `.base` files as part of schema derivation
2. Represent formula fields in the schema alongside stored fields
3. Translate formula expressions to Python callables
4. Evaluate formulas at load time, materializing results into `Record.fields`

Formula fields are treated as read-only derived properties. They are never written back to frontmatter.

---

## 1. Schema Changes

### `FieldType` enum

Add one new member:

```python
class FieldType(str, Enum):
    ...existing members...
    FORMULA = "formula"
```

`FORMULA` describes the *origin* of a field (computed from an expression), not its output type. The output type of a formula field is either inferred after evaluation or declared as `UNKNOWN` until then.

### `FieldSchema` dataclass

Add two optional attributes:

```python
@dataclass
class FieldSchema:
    name: str
    type: FieldType
    link_target: Optional[str] = None   # existing
    formula: Optional[str] = None       # expression string, set iff type == FORMULA
    output_type: Optional[FieldType] = None  # inferred after evaluation; None until then
```

- `formula` stores the raw expression string exactly as written in the `.base` file
- `output_type` is populated after the first evaluation pass (inferred from the result values)
- Formula fields with `output_type` of `LINK` or `LIST_LINKS` participate in link traversal during evaluation

### `Schema.from_vault()` changes

After deriving the schema from frontmatter, read each `bases/{type_name}.base` file:

- Parse the YAML
- Extract the `formulas:` block — each key is a formula field name, each value is the expression string
- Extract the `filters:` block — store as a single `_filter` expression on `TypeSchema` (see below)
- For each formula, append a `FieldSchema(name=..., type=FORMULA, formula=...)` to the relevant `TypeSchema`
- Formula fields are appended after stored fields; they do not affect stored field inference

### `TypeSchema` dataclass

Add one optional attribute:

```python
@dataclass
class TypeSchema:
    name: str
    fields: list[FieldSchema] = field(default_factory=list)
    base_filter: Optional[str] = None   # raw filter expression from .base file, if any
```

`base_filter` is the top-level `and`/`or` filter expression from the `.base` file. It can be evaluated by `FormulaTranslator` to pre-filter records at load time (optional, off by default).

---

## 2. `EvalContext` dataclass

Shared context passed to every formula callable:

```python
@dataclass
class EvalContext:
    record: Record          # the record being evaluated
    vault: Vault            # full vault, for link traversal
    base_path: Path         # path to the .base file (resolves `this`)
    now: datetime           # frozen at Vault load time for reproducibility
```

`this` is always resolved statically: `this.file.name` → `base_path.stem`, `this.file.folder` → `base_path.parent.name`. No runtime ambiguity.

`now` is frozen at vault load time so that `now()` and `today()` return consistent values across all formula evaluations within a single `Vault` instance.

---

## 3. `FormulaTranslator` class

### Responsibility

Takes a formula string. Returns a Python callable with signature `(ctx: EvalContext) -> Any`. Does not evaluate — only translates.

### Parser

Use `lark` with a defined grammar covering the full Bases expression syntax:
- Method chains: `a.b().c(args)`
- Operators: arithmetic, comparison, boolean
- Literals: strings, numbers, booleans, regex (`/pattern/`)
- List literals: `[1, 2, 3]`
- Object literals: `{"a": 1}`
- Index access: `property[0]`
- Global functions: `if()`, `date()`, `list()`, `now()`, `today()`, etc.
- Implicit lambda variables: `value`, `index`, `acc` (used inside `filter()`, `map()`, `reduce()`)
- Property namespaces: `note.x`, `file.x`, `formula.x`, `this.x`

The grammar is the single most load-bearing implementation decision. Define it fully before writing any evaluation logic.

### Translation strategy

The translator walks the AST and produces a Python callable composed of nested lambdas. Key mappings:

| Bases | Python |
|---|---|
| `property` / `note.property` | `ctx.record.fields.get("property")` |
| `file.name` | `ctx.record.path.name` |
| `file.folder` | `ctx.record.path.parent.name` |
| `file.mtime` | `ctx.record.path.stat().st_mtime` |
| `file.links` | links extracted from record fields |
| `formula.X` | result of evaluating formula `X` for this record (see dependency ordering) |
| `this.file.name` | `ctx.base_path.stem` (static) |
| `this.file.folder` | `ctx.base_path.parent.name` (static) |
| `now()` | `ctx.now` |
| `today()` | `ctx.now.date()` |
| `list.filter(value > 2)` | `[v for v in lst if v > 2]` (implicit `value` bound) |
| `list.map(value + 1)` | `[v + 1 for v in lst]` |
| `list.reduce(acc + value, 0)` | `functools.reduce(lambda acc, v: acc + v, lst, 0)` |
| `/pattern/.matches(s)` | `re.search(pattern, s) is not None` |
| `link.asFile()` | resolve wikilink to `Record` via vault |

Display functions (`image()`, `icon()`, `html()`) return `None` — they are view-only and meaningless in Python context.

### Untranslatable expressions

If any AST node cannot be translated (unknown function, unsupported construct), the translator:
- Returns a callable that always returns `None`
- Attaches a `translation_error: str` attribute to the callable describing what failed
- This attribute is read by `.lint()` to surface the violation

### Interface

```python
class FormulaTranslator:
    def translate(self, expression: str) -> Callable[[EvalContext], Any]:
        ...

    def translate_filter(self, expression: str) -> Callable[[EvalContext], bool]:
        """Same as translate() but asserts boolean output."""
        ...
```

No state is held between translations. The translator is stateless and reusable across vault instances.

---

## 4. Formula Evaluation at Load Time

### Dependency ordering

Before evaluation, build a dependency graph across all formula fields in a type:
- A formula referencing `formula.X` depends on `X`
- Topological sort determines evaluation order
- Cycles are detected and reported as lint violations; cyclic formulas evaluate to `None`

### Evaluation pass

After `from_vault()` loads all records and before returning the `Vault` instance:

```
for each type:
    translator = FormulaTranslator()
    sort formula fields topologically
    for each formula field (in order):
        callable = translator.translate(field.formula)
        for each record:
            ctx = EvalContext(record, vault, base_path, frozen_now)
            result = callable(ctx)
            record.fields[field.name] = result
        infer field.output_type from result values
```

Results are materialized directly into `Record.fields`. Formula fields are indistinguishable from stored fields by the time `to_db()` or `to_graph()` sees them — they are just additional keys in the dict.

### Filter application (optional)

If `apply_base_filters=True` is passed to `from_vault()` (default `False`):
- Translate `type_schema.base_filter` via `translate_filter()`
- Remove records that evaluate to `False`
- Removed records are logged at DEBUG level

Default is `False` to preserve full fidelity by default; filtering is opt-in.

---

## 5. `.lint()` additions

New checks related to formulas:

- **Untranslatable formula**: a formula field whose callable has a `translation_error` attribute → error
- **Cyclic formula dependency**: two or more formula fields in a type that mutually depend on each other → error
- **Formula referencing unknown field**: `note.X` where `X` is not a known field on that type → warning
- **Formula output type mismatch**: `output_type` inferred from results is inconsistent across records (e.g. sometimes number, sometimes string) → warning
- **Missing `.base` file**: a type folder in `data/` with no corresponding `.base` in `bases/` → error (contract violation, already checked structurally)

---

## 6. What does not change

- `Record`, `Vault`, `to_db()`, `to_graph()` require no structural changes
- Formula fields are invisible to those methods — they just see additional entries in `record.fields`
- `output_type` on `FieldSchema` allows `to_db()` to assign the correct DuckDB column type for formula fields, using the same `_FIELD_TO_DB_TYPE` mapping already in place
