"""Runtime helpers: namespace proxies, safe operators, method/global dispatch."""

from __future__ import annotations

import datetime
import math
import re
from typing import TYPE_CHECKING, Any, Callable

from .context import EvalContext

if TYPE_CHECKING:
    from ..vault import Record, Vault

# ── Namespace proxies ────────────────────────────────────────────


class _FileProxy:
    """Runtime proxy for the ``file.*`` namespace."""
    __slots__ = ("_record", "_vault", "_path")

    def __init__(self, record: Record, vault: Vault):
        self._record = record
        self._vault = vault
        self._path: Any = record.path if record else None

    @property
    def name(self) -> str | None:
        return self._path.name if self._path else None

    @property
    def basename(self) -> str | None:
        return self._path.stem if self._path else None

    @property
    def path(self) -> str | None:
        if not self._path or not self._vault or not self._vault.path:
            return str(self._path) if self._path else None
        try:
            return str(self._path.relative_to(self._vault.path))
        except ValueError:
            return str(self._path)

    @property
    def folder(self) -> str | None:
        return self._path.parent.name if self._path else None

    @property
    def ext(self) -> str | None:
        return self._path.suffix if self._path else None

    @property
    def size(self) -> int | None:
        if self._path and self._path.exists():
            return self._path.stat().st_size
        return None

    @property
    def ctime(self) -> datetime.datetime | None:
        if self._path and self._path.exists():
            return datetime.datetime.fromtimestamp(self._path.stat().st_ctime)
        return None

    @property
    def mtime(self) -> datetime.datetime | None:
        if self._path and self._path.exists():
            return datetime.datetime.fromtimestamp(self._path.stat().st_mtime)
        return None

    @property
    def file(self) -> _FileProxy:
        return self

    @property
    def properties(self) -> dict[str, Any]:
        return dict(self._record.fields)

    @property
    def tags(self) -> list:
        t = self._record.fields.get("tags", [])
        return t if isinstance(t, list) else [t] if t else []

    @property
    def links(self) -> list:
        from ..schema import FieldType
        out: list = []
        for f in self._record.type_schema.fields:
            if f.type in (FieldType.LINK, FieldType.LIST_LINKS):
                v = self._record.fields.get(f.name)
                if isinstance(v, list):
                    out.extend(v)
                elif v is not None:
                    out.append(v)
        return out

    def hasTag(self, *values: str) -> bool:
        return any(v in self.tags for v in values)

    def inFolder(self, folder: str) -> bool:
        if not self._path:
            return False
        return folder in str(self._path.parent)

    def hasProperty(self, prop: str) -> bool:
        return prop in self._record.fields

    def hasLink(self, other: Any) -> bool:
        return any(str(other) in str(lk) for lk in self.links)

    def asLink(self, display: str | None = None) -> str:
        stem = self._path.stem if self._path else ""
        return f"[[{stem}|{display}]]" if display else f"[[{stem}]]"


class _ThisFileProxy:
    """Runtime proxy for ``this.file.*``."""
    __slots__ = ("_path",)

    def __init__(self, base_path: Any):
        self._path = base_path

    @property
    def name(self) -> str | None:
        return self._path.stem if self._path else None

    @property
    def folder(self) -> str | None:
        return self._path.parent.name if self._path else None


class _ThisProxy:
    """Runtime proxy for the ``this.*`` namespace."""
    __slots__ = ("file",)

    def __init__(self, base_path: Any):
        self.file = _ThisFileProxy(base_path)


class _NoteProxy:
    """Runtime proxy for the ``note.*`` / ``formula.*`` namespace."""
    __slots__ = ("_fields",)

    def __init__(self, fields: dict[str, Any]):
        object.__setattr__(self, "_fields", fields)

    def __getattr__(self, name: str) -> Any:
        return self._fields.get(name)

    def __getitem__(self, key: str) -> Any:
        return self._fields.get(key)


# ── Duration parsing ─────────────────────────────────────────────

# Month = 30 days, year = 365 days — matches Bases' approximate semantics.
_DURATION_RE = re.compile(
    r"(\d+)\s*"
    r"(y(?:ears?)?|M|months?|w(?:eeks?)?|d(?:ays?)?|h(?:ours?)?|m(?:inutes?)?|s(?:econds?)?)"
)


def _parse_duration(s: Any) -> datetime.timedelta | None:
    total = datetime.timedelta()
    found = False
    for m in _DURATION_RE.finditer(str(s)):
        found = True
        n = int(m.group(1))
        u = m.group(2)
        if u.startswith("y"):
            total += datetime.timedelta(days=n * 365)
        elif u == "M" or u.startswith("month"):
            total += datetime.timedelta(days=n * 30)
        elif u.startswith("w"):
            total += datetime.timedelta(weeks=n)
        elif u.startswith("d"):
            total += datetime.timedelta(days=n)
        elif u.startswith("h"):
            total += datetime.timedelta(hours=n)
        elif u.startswith("m"):
            total += datetime.timedelta(minutes=n)
        elif u.startswith("s"):
            total += datetime.timedelta(seconds=n)
    return total if found else None


# ── Safe operators ───────────────────────────────────────────────


def _safe_add(a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    if isinstance(a, (datetime.datetime, datetime.date)):
        dur = _parse_duration(b) if isinstance(b, str) else b
        if isinstance(dur, datetime.timedelta):
            return a + dur
    if isinstance(b, (datetime.datetime, datetime.date)):
        dur = _parse_duration(a) if isinstance(a, str) else a
        if isinstance(dur, datetime.timedelta):
            return b + dur
    if isinstance(a, datetime.timedelta) and isinstance(b, datetime.timedelta):
        return a + b
    try:
        return a + b
    except TypeError:
        return None


def _safe_sub(a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    if isinstance(a, (datetime.datetime, datetime.date)):
        if isinstance(b, (datetime.datetime, datetime.date)):
            if isinstance(a, datetime.date) and not isinstance(a, datetime.datetime):
                a = datetime.datetime.combine(a, datetime.time())
            if isinstance(b, datetime.date) and not isinstance(b, datetime.datetime):
                b = datetime.datetime.combine(b, datetime.time())
            return (a - b).total_seconds() * 1000
        dur = _parse_duration(b) if isinstance(b, str) else b
        if isinstance(dur, datetime.timedelta):
            return a - dur
    try:
        return a - b
    except TypeError:
        return None


_COMP_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">":  lambda a, b: a > b,
    "<":  lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
}


def _safe_compare(op: str, a: Any, b: Any) -> bool:
    if a is None or b is None:
        if op == "==":
            return a is b
        if op == "!=":
            return a is not b
        return False
    try:
        return _COMP_OPS[op](a, b)
    except TypeError:
        return False


# ── Property / method access helpers ────────────────────────────

_PROP_MAP: dict[str, Callable[[Any], Any]] = {
    "length": lambda obj: len(obj) if hasattr(obj, "__len__") else None,
    "millisecond": lambda obj: obj.microsecond // 1000 if hasattr(obj, "microsecond") else None,
}


def _dot(obj: Any, attr: str) -> Any:
    if obj is None:
        return None
    prop = _PROP_MAP.get(attr)
    if prop is not None:
        return prop(obj)
    if isinstance(obj, dict):
        return obj.get(attr)
    try:
        return getattr(obj, attr)
    except AttributeError:
        return None


def _index(obj: Any, idx: Any) -> Any:
    if obj is None:
        return None
    if isinstance(idx, (int, float)):
        try:
            return obj[int(idx)]
        except (IndexError, KeyError, TypeError):
            return None
    if isinstance(idx, str):
        if isinstance(obj, dict):
            return obj.get(idx)
        if hasattr(obj, "__getitem__"):
            try:
                return obj[idx]
            except (KeyError, TypeError, IndexError):
                return None
    return None


def _flatten(lst: Any) -> list:
    out: list = []
    for item in (lst if isinstance(lst, list) else [lst]):
        if isinstance(item, list):
            out.extend(_flatten(item))
        else:
            out.append(item)
    return out


def _ensure_list(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


# ── Date helpers ─────────────────────────────────────────────────

_MOMENT_TO_STRFTIME = [
    ("YYYY", "%Y"), ("YY", "%y"),
    ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"),
    ("DD", "%d"),
    ("dddd", "%A"), ("ddd", "%a"),
    ("HH", "%H"), ("hh", "%I"),
    ("mm", "%M"), ("ss", "%S"),
    ("A", "%p"), ("a", "%p"),
]


def _format_date(obj: Any, fmt: str) -> str:
    if not hasattr(obj, "strftime"):
        return str(obj)
    result = fmt
    for moment, strftime in _MOMENT_TO_STRFTIME:
        result = result.replace(moment, strftime)
    return obj.strftime(result)


def _relative_date(obj: Any, now: datetime.datetime) -> str:
    if not isinstance(obj, (datetime.date, datetime.datetime)):
        return str(obj)
    if isinstance(obj, datetime.date) and not isinstance(obj, datetime.datetime):
        obj = datetime.datetime.combine(obj, datetime.time())
    if isinstance(now, datetime.date) and not isinstance(now, datetime.datetime):
        now = datetime.datetime.combine(now, datetime.time())
    secs = (now - obj).total_seconds()
    abss = abs(secs)
    if abss < 60:
        return "just now"
    if abss < 3600:
        n = int(abss // 60)
        label = f"{n} minute{'s' if n != 1 else ''}"
    elif abss < 86400:
        n = int(abss // 3600)
        label = f"{n} hour{'s' if n != 1 else ''}"
    else:
        n = int(abss // 86400)
        label = f"{n} day{'s' if n != 1 else ''}"
    return f"{label} ago" if secs > 0 else f"in {label}"


def _parse_date(s: Any) -> datetime.datetime | datetime.date | None:
    if s is None:
        return None
    if isinstance(s, (datetime.datetime, datetime.date)):
        return s
    s = str(s).strip()
    for fmt in (datetime.date.fromisoformat, datetime.datetime.fromisoformat):
        try:
            return fmt(s)
        except ValueError:
            continue
    return None


def _to_number(val: Any) -> int | float | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return 1 if val else 0
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, datetime.datetime):
        return val.timestamp() * 1000
    if isinstance(val, datetime.date):
        return datetime.datetime.combine(val, datetime.time()).timestamp() * 1000
    try:
        f = float(str(val))
        return int(f) if f == int(f) else f
    except (ValueError, TypeError):
        return None


# ── Link resolution ──────────────────────────────────────────────

_WIKILINK_RESOLVE_RE = re.compile(r"^\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]$")


def _resolve_link(link_str: Any, ctx: EvalContext) -> _FileProxy | None:
    if not link_str or not ctx.vault:
        return None
    m = _WIKILINK_RESOLVE_RE.match(str(link_str).strip())
    if not m:
        return None
    target = m.group(1)
    if "/" in target:
        folder, name = target.split("/", 1)
    else:
        folder, name = None, target
    for type_name, records in ctx.vault.records.items():
        if folder and type_name != folder:
            continue
        for rec in records:
            if rec.name == name:
                return _FileProxy(rec, ctx.vault)
    return None


def _str_replace(obj: Any, args: list) -> Any:
    if not isinstance(obj, str) or not args:
        return obj
    pattern, repl = args[0], args[1] if len(args) > 1 else ""
    if hasattr(pattern, "pattern"):
        return re.sub(pattern.pattern, repl, obj)
    return obj.replace(str(pattern), str(repl))


# ── Method dispatch ──────────────────────────────────────────────
# Each handler: (obj, args: list, ctx: EvalContext) -> Any
# filter / map / reduce are special-cased in the compiler.

_METHODS: dict[str, Callable[..., Any]] = {
    # String
    "contains":   lambda obj, args, ctx: args[0] in obj if isinstance(obj, (str, list)) and args else False,
    "replace":    lambda obj, args, ctx: _str_replace(obj, args),
    "split":      lambda obj, args, ctx: obj.split(*args) if isinstance(obj, str) else [obj],
    "slice":      lambda obj, args, ctx: obj[int(args[0]): int(args[1]) if len(args) > 1 else None],
    "lower":      lambda obj, args, ctx: obj.lower() if isinstance(obj, str) else obj,
    "title":      lambda obj, args, ctx: obj.title() if isinstance(obj, str) else obj,
    "trim":       lambda obj, args, ctx: obj.strip() if isinstance(obj, str) else obj,
    "startsWith": lambda obj, args, ctx: obj.startswith(args[0]) if isinstance(obj, str) and args else False,
    "endsWith":   lambda obj, args, ctx: obj.endswith(args[0]) if isinstance(obj, str) and args else False,
    # Number
    "abs":        lambda obj, args, ctx: abs(obj),
    "round":      lambda obj, args, ctx: round(obj, int(args[0])) if args else round(obj),
    "ceil":       lambda obj, args, ctx: math.ceil(obj),
    "floor":      lambda obj, args, ctx: math.floor(obj),
    "toFixed":    lambda obj, args, ctx: f"{obj:.{int(args[0])}f}" if args else str(obj),
    # List  (filter / map / reduce handled in compiler)
    "sort":       lambda obj, args, ctx: sorted(obj) if isinstance(obj, list) else obj,
    "unique":     lambda obj, args, ctx: list(dict.fromkeys(obj)) if isinstance(obj, list) else obj,
    "join":       lambda obj, args, ctx: (args[0] if args else "").join(str(v) for v in obj) if isinstance(obj, list) else str(obj),
    "flat":       lambda obj, args, ctx: _flatten(obj),
    # Any
    "isTruthy":   lambda obj, args, ctx: bool(obj),
    "isType":     lambda obj, args, ctx: type(obj).__name__.lower() == str(args[0]).lower() if args else False,
    "toString":   lambda obj, args, ctx: str(obj) if obj is not None else "",
    # Object / dict
    "isEmpty":    lambda obj, args, ctx: not obj,
    "keys":       lambda obj, args, ctx: list(obj.keys()) if isinstance(obj, dict) else [],
    "values":     lambda obj, args, ctx: list(obj.values()) if isinstance(obj, dict) else [],
    # Regex
    "matches":    lambda obj, args, ctx: bool(re.search(obj.pattern if hasattr(obj, "pattern") else str(obj), str(args[0]))) if args else False,
    # Date
    "format":     lambda obj, args, ctx: _format_date(obj, args[0]) if args else str(obj),
    "relative":   lambda obj, args, ctx: _relative_date(obj, ctx.now),
    "date":       lambda obj, args, ctx: obj.date() if hasattr(obj, "date") and callable(getattr(obj, "date")) else obj,
    "time":       lambda obj, args, ctx: obj.strftime("%H:%M:%S") if hasattr(obj, "strftime") else str(obj),
    # Link
    "asFile":     lambda obj, args, ctx: _resolve_link(obj, ctx),
    "linksTo":    lambda obj, args, ctx: _resolve_link(obj, ctx) is not None and bool(args) and _resolve_link(obj, ctx).hasLink(args[0]),
}


def _call_method(obj: Any, method: str, args: list, ctx: EvalContext) -> Any:
    if obj is None:
        return None
    handler = _METHODS.get(method)
    if handler is not None:
        return handler(obj, args, ctx)
    fn = getattr(obj, method, None)
    if fn is not None and callable(fn):
        return fn(*args)
    return None


# ── Global function dispatch ─────────────────────────────────────
# if() is compiled with short-circuit in the compiler; everything else here.
# Each handler: (ctx: EvalContext, args: list[evaluated]) -> Any

_GLOBALS: dict[str, Callable[..., Any]] = {
    "now":      lambda ctx, args: ctx.now,
    "today":    lambda ctx, args: ctx.now.date() if isinstance(ctx.now, datetime.datetime) else ctx.now,
    "date":     lambda ctx, args: _parse_date(args[0]) if args else None,
    "number":   lambda ctx, args: _to_number(args[0]) if args else None,
    "list":     lambda ctx, args: args[0] if args and isinstance(args[0], list) else list(args),
    "file":     lambda ctx, args: _resolve_link(args[0], ctx) if args else _FileProxy(ctx.record, ctx.vault),
    "link":     lambda ctx, args: f"[[{args[0]}|{args[1]}]]" if len(args) > 1 else f"[[{args[0]}]]" if args else None,
    "duration": lambda ctx, args: _parse_duration(args[0]) if args else None,
    "max":      lambda ctx, args: max(a for a in args if a is not None) if args else None,
    "min":      lambda ctx, args: min(a for a in args if a is not None) if args else None,
}

_DISPLAY_FUNCS = frozenset({"image", "icon", "html", "escapeHTML"})


# ── Untranslatable sentinel ──────────────────────────────────────


def _untranslatable(reason: str) -> Callable[[EvalContext], None]:
    def _fail(_ctx: EvalContext) -> None:
        return None
    _fail.translation_error = reason  # type: ignore[attr-defined]
    return _fail
