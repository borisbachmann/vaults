"""LALR grammar and parser singleton for Obsidian Bases expressions."""

from lark import Lark

GRAMMAR = r"""
?expr: or_expr

?or_expr: and_expr
        | or_expr "||" and_expr   -> or_

?and_expr: not_expr
         | and_expr "&&" not_expr -> and_

?not_expr: "!" not_expr          -> not_
         | comparison

?comparison: addition
           | addition COMP_OP addition -> comparison

?addition: multiplication
         | addition "+" multiplication -> add
         | addition "-" multiplication -> sub

?multiplication: unary
               | multiplication "*" unary -> mul
               | multiplication "/" unary -> div
               | multiplication "%" unary -> mod

?unary: "-" unary -> neg
      | postfix

?postfix: postfix "." IDENT "(" [arg_list] ")" -> method_call
        | postfix "." IDENT                      -> dot_access
        | postfix "[" expr "]"                   -> index_access
        | atom

?atom: NUMBER                         -> number
     | STRING                          -> string
     | "true"                          -> true_
     | "false"                         -> false_
     | REGEX                           -> regex
     | "[" [arg_list] "]"             -> list_literal
     | "{" [pair_list] "}"            -> object_literal
     | IDENT "(" [arg_list] ")"       -> func_call
     | IDENT                           -> name
     | "(" expr ")"

arg_list: expr ("," expr)*
pair_list: pair ("," pair)*
pair: STRING ":" expr
    | IDENT ":" expr

COMP_OP: "==" | "!=" | ">=" | "<=" | ">" | "<"

IDENT: /[a-zA-Z_]\w*/
STRING: /\"[^\"]*\"|'[^']*'/
REGEX: /\/[^\/]+\//

%import common.NUMBER
%import common.WS
%ignore WS
"""

# Single parser instance — Lark compilation is expensive; reuse across calls.
_parser = Lark(GRAMMAR, parser="lalr", start="expr")
