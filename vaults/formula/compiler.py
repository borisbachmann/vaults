"""BasesCompiler — walks the Lark AST and produces Python callables."""

from __future__ import annotations

import functools
import re
from typing import Any, Callable

from lark import Token, Tree

from .context import EvalContext
from .grammar import _parser
from .runtime import (
    _FileProxy,
    _NoteProxy,
    _ThisProxy,
    _call_method,
    _dot,
    _ensure_list,
    _index,
    _safe_add,
    _safe_compare,
    _safe_sub,
    _untranslatable,
    _DISPLAY_FUNCS,
    _GLOBALS,
)

# Internal callable type: (EvalContext, scope_dict) -> Any
_Fn = Callable[["EvalContext", dict], Any]


class BasesCompiler:
    """Compile Obsidian Bases formula strings into Python callables.

    Thread-safe and stateless — no instance state is held between calls.
    """

    def parse(self, expression: str) -> Tree:
        """Parse *expression* into a Lark AST."""
        return _parser.parse(expression)

    # ── public API ───────────────────────────────────────────────

    def translate(self, expression: str) -> Callable[[EvalContext], Any]:
        errors: list[str] = []
        try:
            tree = self.parse(expression)
        except Exception as e:
            return _untranslatable(f"parse error: {e}")
        fn = self._compile(tree, errors)
        if errors:
            return _untranslatable("; ".join(errors))

        def evaluate(ctx: EvalContext) -> Any:
            try:
                return fn(ctx, {})
            except Exception:
                return None
        return evaluate

    def translate_filter(self, expression: str) -> Callable[[EvalContext], bool]:
        inner = self.translate(expression)
        if hasattr(inner, "translation_error"):
            return inner  # type: ignore[return-value]
        return lambda ctx: bool(inner(ctx))

    # ── compiler dispatch ────────────────────────────────────────

    def _compile(self, node: Tree | Token, errors: list[str]) -> _Fn:
        if isinstance(node, Token):
            return self._compile_token(node)
        handler = getattr(self, f"_c_{node.data}", None)
        if handler is None:
            errors.append(f"unsupported construct: {node.data}")
            return lambda ctx, scope: None
        return handler(node, errors)

    def _compile_token(self, tok: Token) -> _Fn:
        if tok.type == "NUMBER":
            v: int | float = float(tok) if "." in tok else int(tok)
            return lambda ctx, scope: v
        if tok.type == "STRING":
            s = str(tok)[1:-1]
            return lambda ctx, scope: s
        if tok.type == "IDENT":
            name = str(tok)
            return lambda ctx, scope: scope[name] if name in scope else ctx.record.fields.get(name)
        if tok.type == "REGEX":
            pattern = str(tok)[1:-1]
            compiled = re.compile(pattern)
            return lambda ctx, scope: compiled
        return lambda ctx, scope: None

    # ── literals ─────────────────────────────────────────────────

    def _c_number(self, node: Tree, errors: list[str]) -> _Fn:
        v: int | float = float(node.children[0]) if "." in node.children[0] else int(node.children[0])
        return lambda ctx, scope: v

    def _c_string(self, node: Tree, errors: list[str]) -> _Fn:
        s = str(node.children[0])[1:-1]
        return lambda ctx, scope: s

    def _c_true_(self, _node: Tree, errors: list[str]) -> _Fn:
        return lambda ctx, scope: True

    def _c_false_(self, _node: Tree, errors: list[str]) -> _Fn:
        return lambda ctx, scope: False

    def _c_regex(self, node: Tree, errors: list[str]) -> _Fn:
        pattern = str(node.children[0])[1:-1]
        compiled = re.compile(pattern)
        return lambda ctx, scope: compiled

    def _c_list_literal(self, node: Tree, errors: list[str]) -> _Fn:
        arg_list = node.children[0]
        if arg_list is None:
            return lambda ctx, scope: []
        fns = [self._compile(c, errors) for c in arg_list.children]
        return lambda ctx, scope: [f(ctx, scope) for f in fns]

    def _c_object_literal(self, node: Tree, errors: list[str]) -> _Fn:
        pair_list = node.children[0]
        if pair_list is None:
            return lambda ctx, scope: {}
        pairs: list[tuple[str, _Fn]] = []
        for pair in pair_list.children:
            key = str(pair.children[0])
            if key[0] in ('"', "'"):
                key = key[1:-1]
            val_fn = self._compile(pair.children[1], errors)
            pairs.append((key, val_fn))
        return lambda ctx, scope: {k: v(ctx, scope) for k, v in pairs}

    # ── names & access ───────────────────────────────────────────

    def _c_name(self, node: Tree, errors: list[str]) -> _Fn:
        ident = str(node.children[0])
        if ident == "file":
            return lambda ctx, scope: _FileProxy(ctx.record, ctx.vault)
        if ident == "this":
            return lambda ctx, scope: _ThisProxy(ctx.base_path)
        if ident in ("note", "formula"):
            return lambda ctx, scope: _NoteProxy(ctx.record.fields)
        return lambda ctx, scope: scope[ident] if ident in scope else ctx.record.fields.get(ident)

    def _c_dot_access(self, node: Tree, errors: list[str]) -> _Fn:
        obj_fn = self._compile(node.children[0], errors)
        attr = str(node.children[1])
        return lambda ctx, scope: _dot(obj_fn(ctx, scope), attr)

    def _c_index_access(self, node: Tree, errors: list[str]) -> _Fn:
        obj_fn = self._compile(node.children[0], errors)
        idx_fn = self._compile(node.children[1], errors)
        return lambda ctx, scope: _index(obj_fn(ctx, scope), idx_fn(ctx, scope))

    # ── method calls ─────────────────────────────────────────────

    def _c_method_call(self, node: Tree, errors: list[str]) -> _Fn:
        obj_fn = self._compile(node.children[0], errors)
        method = str(node.children[1])
        arg_tree = node.children[2]
        raw_args = arg_tree.children if arg_tree is not None else []

        if method == "filter":
            pred = self._compile(raw_args[0], errors) if raw_args else (lambda ctx, scope: True)
            return lambda ctx, scope: [
                v for i, v in enumerate(_ensure_list(obj_fn(ctx, scope)))
                if pred(ctx, {**scope, "value": v, "index": i})
            ]

        if method == "map":
            transform = self._compile(raw_args[0], errors) if raw_args else (lambda ctx, scope: scope.get("value"))
            return lambda ctx, scope: [
                transform(ctx, {**scope, "value": v, "index": i})
                for i, v in enumerate(_ensure_list(obj_fn(ctx, scope)))
            ]

        if method == "reduce":
            combine = self._compile(raw_args[0], errors) if raw_args else (lambda ctx, scope: scope.get("value"))
            init_fn = self._compile(raw_args[1], errors) if len(raw_args) > 1 else (lambda ctx, scope: None)
            return lambda ctx, scope: functools.reduce(
                lambda acc, pair: combine(ctx, {**scope, "acc": acc, "value": pair[1], "index": pair[0]}),
                enumerate(_ensure_list(obj_fn(ctx, scope))),
                init_fn(ctx, scope),
            )

        arg_fns = [self._compile(a, errors) for a in raw_args]
        return lambda ctx, scope: _call_method(
            obj_fn(ctx, scope), method, [a(ctx, scope) for a in arg_fns], ctx,
        )

    # ── function calls ───────────────────────────────────────────

    def _c_func_call(self, node: Tree, errors: list[str]) -> _Fn:
        name = str(node.children[0])
        arg_tree = node.children[1]
        raw_args = arg_tree.children if arg_tree is not None else []

        # Short-circuit if()
        if name == "if":
            cond = self._compile(raw_args[0], errors) if raw_args else (lambda ctx, scope: False)
            then = self._compile(raw_args[1], errors) if len(raw_args) > 1 else (lambda ctx, scope: None)
            else_ = self._compile(raw_args[2], errors) if len(raw_args) > 2 else (lambda ctx, scope: None)
            return lambda ctx, scope: then(ctx, scope) if cond(ctx, scope) else else_(ctx, scope)

        arg_fns = [self._compile(a, errors) for a in raw_args]

        if name in _GLOBALS:
            g = _GLOBALS[name]
            return lambda ctx, scope: g(ctx, [a(ctx, scope) for a in arg_fns])

        if name in _DISPLAY_FUNCS:
            return lambda ctx, scope: None

        errors.append(f"unknown function: {name}")
        return lambda ctx, scope: None

    # ── arithmetic ───────────────────────────────────────────────

    def _c_add(self, node: Tree, errors: list[str]) -> _Fn:
        left, right = self._compile(node.children[0], errors), self._compile(node.children[1], errors)
        return lambda ctx, scope: _safe_add(left(ctx, scope), right(ctx, scope))

    def _c_sub(self, node: Tree, errors: list[str]) -> _Fn:
        left, right = self._compile(node.children[0], errors), self._compile(node.children[1], errors)
        return lambda ctx, scope: _safe_sub(left(ctx, scope), right(ctx, scope))

    def _c_mul(self, node: Tree, errors: list[str]) -> _Fn:
        left, right = self._compile(node.children[0], errors), self._compile(node.children[1], errors)
        return lambda ctx, scope: (lambda a, b: None if a is None or b is None else a * b)(left(ctx, scope), right(ctx, scope))

    def _c_div(self, node: Tree, errors: list[str]) -> _Fn:
        left, right = self._compile(node.children[0], errors), self._compile(node.children[1], errors)
        def _div(ctx: EvalContext, scope: dict) -> Any:
            a, b = left(ctx, scope), right(ctx, scope)
            if a is None or b is None or b == 0:
                return None
            return a / b
        return _div

    def _c_mod(self, node: Tree, errors: list[str]) -> _Fn:
        left, right = self._compile(node.children[0], errors), self._compile(node.children[1], errors)
        def _mod(ctx: EvalContext, scope: dict) -> Any:
            a, b = left(ctx, scope), right(ctx, scope)
            if a is None or b is None or b == 0:
                return None
            return a % b
        return _mod

    def _c_neg(self, node: Tree, errors: list[str]) -> _Fn:
        operand = self._compile(node.children[0], errors)
        def _neg(ctx: EvalContext, scope: dict) -> Any:
            v = operand(ctx, scope)
            return -v if v is not None else None
        return _neg

    # ── comparison & logic ───────────────────────────────────────

    def _c_comparison(self, node: Tree, errors: list[str]) -> _Fn:
        left = self._compile(node.children[0], errors)
        op = str(node.children[1])
        right = self._compile(node.children[2], errors)
        return lambda ctx, scope: _safe_compare(op, left(ctx, scope), right(ctx, scope))

    def _c_or_(self, node: Tree, errors: list[str]) -> _Fn:
        left, right = self._compile(node.children[0], errors), self._compile(node.children[1], errors)
        return lambda ctx, scope: left(ctx, scope) or right(ctx, scope)

    def _c_and_(self, node: Tree, errors: list[str]) -> _Fn:
        left, right = self._compile(node.children[0], errors), self._compile(node.children[1], errors)
        return lambda ctx, scope: left(ctx, scope) and right(ctx, scope)

    def _c_not_(self, node: Tree, errors: list[str]) -> _Fn:
        operand = self._compile(node.children[0], errors)
        return lambda ctx, scope: not operand(ctx, scope)
