"""simplify_conditions.py — Constant-fold boolean conditions and constant
arithmetic in interstate edges and ConditionalBlocks.

After unroll + constant propagation, expressions like ``(1 == 2)`` and
``(2 - 1)`` survive in the SDFG as literal int operations.  They reach
codegen unchanged and produce patterns like ``if ((cond || (1 == 2)))`` and
``arr[(2-1)]`` that bloat the binary and create dead branches.

This pass walks:

* every interstate-edge condition (``edge.data.condition``);
* every interstate-edge assignment value (``edge.data.assignments``);
* every ConditionalBlock branch condition (``branch.condition``).

Each expression is parsed as a Python AST and simplified by:

* folding ``Compare`` whose operands are all literal constants (``1==2`` →
  ``False``, ``2==2`` → ``True``);
* folding ``BinOp`` whose operands are both literal constants (``2-1`` → ``1``,
  ``klev+1`` stays since ``klev`` is symbolic);
* applying boolean algebra to ``BoolOp`` and ``UnaryOp`` (``x and True`` → ``x``,
  ``x or False`` → ``x``, ``not False`` → ``True``, ``and`` short-circuits on
  literal ``False``, etc.).

After this pass DaCe's standard ``simplify()`` should be able to drop the
now-dead branches.
"""

from __future__ import annotations

import ast
from typing import Optional

import dace
from dace.properties import CodeBlock
from dace.sdfg.state import ConditionalBlock


# ---------------------------------------------------------------------------
# AST-level simplification
# ---------------------------------------------------------------------------

def _is_const_numeric(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float, bool))
        and not isinstance(node.value, str)
    )


def _try_eval(node: ast.AST):
    """Compile + eval a constant AST subtree.  Returns (ok, value)."""
    try:
        v = eval(compile(ast.Expression(body=node), "<simplify>", "eval"))
        return True, v
    except Exception:
        return False, None


class _Simplifier(ast.NodeTransformer):
    """Recursive constant folder for boolean + arithmetic expressions."""

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        # Fold if all operands are numeric constants.
        if _is_const_numeric(node.left) and all(
            _is_const_numeric(c) for c in node.comparators
        ):
            ok, v = _try_eval(node)
            if ok:
                return ast.Constant(value=bool(v))
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if _is_const_numeric(node.left) and _is_const_numeric(node.right):
            ok, v = _try_eval(node)
            if ok:
                return ast.Constant(value=v)
        return node

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Not):
            if isinstance(node.operand, ast.Constant):
                return ast.Constant(value=not bool(node.operand.value))
        elif _is_const_numeric(node.operand):
            ok, v = _try_eval(node)
            if ok:
                return ast.Constant(value=v)
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.And):
            filtered = []
            for v in node.values:
                if isinstance(v, ast.Constant) and v.value is True:
                    continue
                if isinstance(v, ast.Constant) and v.value is False:
                    return ast.Constant(value=False)
                filtered.append(v)
            if not filtered:
                return ast.Constant(value=True)
            if len(filtered) == 1:
                return filtered[0]
            node.values = filtered
            return node
        if isinstance(node.op, ast.Or):
            filtered = []
            for v in node.values:
                if isinstance(v, ast.Constant) and v.value is False:
                    continue
                if isinstance(v, ast.Constant) and v.value is True:
                    return ast.Constant(value=True)
                filtered.append(v)
            if not filtered:
                return ast.Constant(value=False)
            if len(filtered) == 1:
                return filtered[0]
            node.values = filtered
            return node
        return node


def _simplify_expr_str(s: str) -> str:
    """Parse `s` as a Python expression, simplify, return the new source.
    Returns the original string on parse failure."""
    try:
        tree = ast.parse(s, mode="eval")
    except SyntaxError:
        return s
    new_body = _Simplifier().visit(tree.body)
    ast.fix_missing_locations(new_body)
    return ast.unparse(new_body)


# ---------------------------------------------------------------------------
# SDFG walker
# ---------------------------------------------------------------------------

def simplify_conditions(sdfg: dace.SDFG, verbose: bool = False) -> int:
    """Walk every interstate edge condition + assignments and every
    ConditionalBlock branch condition; fold literal-int comparisons +
    constant arithmetic + apply boolean algebra. Returns the count of
    expressions that changed."""
    n_changed = 0
    for cur_sd in sdfg.all_sdfgs_recursive():
        for cfg in cur_sd.all_control_flow_regions():
            # 1. Interstate edges.
            for edge in cfg.edges():
                if edge.data is None:
                    continue
                # 1a. Condition.
                if edge.data.condition is not None:
                    old = edge.data.condition.as_string
                    new = _simplify_expr_str(old)
                    if new != old:
                        edge.data.condition = CodeBlock(new)
                        n_changed += 1
                        if verbose:
                            print(f"  edge cond: {old!r} -> {new!r}")
                # 1b. Assignments.
                if edge.data.assignments:
                    new_asgn = {}
                    asgn_changed = False
                    for k, v in edge.data.assignments.items():
                        new_v = _simplify_expr_str(str(v))
                        if new_v != str(v):
                            asgn_changed = True
                            n_changed += 1
                            if verbose:
                                print(f"  edge asgn {k}: {v!r} -> {new_v!r}")
                        new_asgn[k] = new_v
                    if asgn_changed:
                        edge.data.assignments = new_asgn
            # 2. ConditionalBlocks.
            for blk in cfg.nodes():
                if not isinstance(blk, ConditionalBlock):
                    continue
                new_branches = []
                cb_changed = False
                for cond, branch in blk.branches:
                    if cond is None:
                        new_branches.append((cond, branch))
                        continue
                    old = cond.as_string
                    new = _simplify_expr_str(old)
                    if new != old:
                        new_branches.append((CodeBlock(new), branch))
                        cb_changed = True
                        n_changed += 1
                        if verbose:
                            print(f"  CB {blk.label} branch: {old!r} -> {new!r}")
                    else:
                        new_branches.append((cond, branch))
                if cb_changed:
                    blk._branches = new_branches  # type: ignore[attr-defined]
    if verbose or n_changed:
        print(f"[simplify_conditions] folded {n_changed} expressions")
    return n_changed
