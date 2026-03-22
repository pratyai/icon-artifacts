"""ssa_analysis.py — Non-mutating analysis for Data SSA.

Produces an SSAPlan that describes all required renames for a reaching-definitions
pass over an SDFG.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Set, Dict, List, Optional, Tuple, Any, Union

from tqdm import tqdm
import dace
from dace import nodes as nd, symbolic
from dace.sdfg.state import ControlFlowRegion, SDFGState, LoopRegion, ConditionalBlock, ControlFlowBlock

from ssa.graph_utils import topological_sort, is_write, is_read


class SSAVersionMap:
    """Maps original variable name → current reaching version object."""

    def __init__(self, init: Dict[str, SSAVersion] | None = None):
        self._map: Dict[str, SSAVersion] = dict(init) if init else {}

    def get(self, name: str) -> Optional[SSAVersion]:
        return self._map.get(name)

    def set(self, name: str, version: Optional[SSAVersion]):
        if version is None:
            if name in self._map:
                del self._map[name]
        else:
            self._map[name] = version

    def copy(self) -> "SSAVersionMap":
        return SSAVersionMap(self._map)

    def snapshot(self, targets: Set[str]) -> Dict[str, SSAVersion]:
        """Return a mapping of original names to current versions for all targets."""
        return {name: self.get(name) for name in targets if self.get(name) is not None}

    def __repr__(self):
        return f"SSAVersionMap({self._map})"


class SSAVersion:
    """Represents a single unique version of a variable (reaching definition group)."""
    
    def __init__(self, base_name: str, version_id: int):
        self.base_name = base_name
        self.version_id = version_id
        
        # Sites belonging to this reaching definition
        self.access_nodes: Set[Tuple[SDFGState, nd.AccessNode]] = set()
        self.symbol_usages: Set[Union[nd.Node, dace.InterstateEdge, ControlFlowBlock]] = set()

    def record_access_node(self, state: SDFGState, node: nd.AccessNode):
        self.access_nodes.add((state, node))

    def record_usage(self, obj: Union[nd.Node, dace.InterstateEdge, ControlFlowBlock]):
        self.symbol_usages.add(obj)

    def merge_from(self, other: "SSAVersion"):
        """Merge another version into this one."""
        if other == self:
            return
        self.access_nodes.update(other.access_nodes)
        self.symbol_usages.update(other.symbol_usages)

    def __repr__(self):
        return f"V({self.base_name}, {self.version_id})"


class SSAPlan:
    """A complete plan for SSA renames across an SDFG hierarchy."""

    def __init__(self):
        # List of all unit SSA operations (reaching definition groups)
        self.versions: List[SSAVersion] = []

    def create_version(self, base_name: str) -> SSAVersion:
        ver = SSAVersion(base_name, len(self.versions))
        self.versions.append(ver)
        return ver

    def merge_versions(self, target: SSAVersion, source: SSAVersion):
        """Unify source into target and remove source from the plan."""
        if target == source:
            return
        target.merge_from(source)
        if source in self.versions:
            self.versions.remove(source)


def _var_in_block_metadata(block: ControlFlowBlock, varname: str) -> bool:
    """Check if varname is referenced in a block's metadata (conditions, bounds, etc.)."""
    pattern = re.compile(r'\b' + re.escape(varname) + r'\b')
    if isinstance(block, LoopRegion):
        for attr in (block.loop_condition, block.init_statement, block.update_statement):
            if attr and pattern.search(attr.as_string):
                return True
    elif isinstance(block, ConditionalBlock):
        for cond, _ in block.branches:
            if cond and pattern.search(cond.as_string):
                return True
    return False


def _var_in_edge(edge: dace.InterstateEdge, varname: str) -> bool:
    """Check if varname is referenced in an interstate edge's condition or assignments."""
    pattern = re.compile(r'\b' + re.escape(varname) + r'\b')
    if edge.condition and pattern.search(edge.condition.as_string):
        return True
    for v in edge.assignments.values():
        if pattern.search(str(v)):
            return True
    return False


def _first_access_in_body(cfg: ControlFlowRegion, varname: str) -> str | None:
    """Check whether the first access to `varname` in cfg's body is a read or write."""
    for child in topological_sort(cfg):
        # Check metadata reads on structured control flow blocks
        if isinstance(child, (LoopRegion, ConditionalBlock)):
            if _var_in_block_metadata(child, varname):
                return "R"

        if isinstance(child, SDFGState):
            found_r, found_w = False, False
            for node in child.nodes():
                if isinstance(node, nd.AccessNode) and node.data == varname:
                    if is_read(child, node): found_r = True
                    if is_write(child, node): found_w = True
            if found_r and found_w: return "RW"
            if found_r: return "R"
            if found_w: return "W"
        elif isinstance(child, ConditionalBlock):
            for _, branch in child.branches:
                res = _first_access_in_body(branch, varname)
                if res: return res
        elif isinstance(child, (ControlFlowRegion, dace.SDFG)):
            res = _first_access_in_body(child, varname)
            if res: return res

        # Check outgoing interstate edges for reads
        if isinstance(cfg, (ControlFlowRegion, dace.SDFG)):
            for e in cfg.out_edges(child):
                if _var_in_edge(e.data, varname):
                    return "R"
    return None


def analyze_state(
    state: SDFGState,
    vmap: SSAVersionMap,
    targets: Set[str],
    plan: SSAPlan,
):
    """Analyze a single state and record renames in the plan."""
    # topological_sort ensures we process definitions before usages
    for node in tqdm(topological_sort(state), desc=f"  state {state.label}", leave=False):
        if isinstance(node, nd.AccessNode) and node.data in targets:
            orig_name = node.data
            if is_write(state, node):
                # New definition: start a fresh version
                new_ver = plan.create_version(orig_name)
                new_ver.record_access_node(state, node)
                vmap.set(orig_name, new_ver)
            else:
                # Reference: use current reaching version
                cur_ver = vmap.get(orig_name)
                if cur_ver:
                    cur_ver.record_access_node(state, node)
        
        elif isinstance(node, nd.Node):
            # 1. Definitions: Only computational nodes (Tasklet, NestedSDFG, etc.) can start a new version.
            # They only start a new version if the memlet path does NOT end at an AccessNode for the same data.
            # If it ends at an AccessNode, the AccessNode itself will start the version.
            definitions = set()
            if not isinstance(node, (nd.MapEntry, nd.MapExit, nd.ConsumeEntry, nd.ConsumeExit)):
                for e in state.out_edges(node):
                    if e.data.data in targets:
                        path = state.memlet_path(e)
                        last_node = path[-1].dst
                        if not isinstance(last_node, nd.AccessNode) or last_node.data != e.data.data:
                            definitions.add(e.data.data)
            
            # Record definitions and update version map
            for orig_name in definitions:
                new_ver = plan.create_version(orig_name)
                new_ver.record_usage(node)
                vmap.set(orig_name, new_ver)
            
            # 2. Usages: Record reaching versions for ALL target variables seen by this node.
            # This includes scope nodes (MapEntry/Exit) to ensure their connectors are updated,
            # and computational nodes using variables in properties or mappings.
            node_free_syms = {str(s) for s in node.free_symbols}
            for name in targets:
                if name in definitions:
                    continue
                
                is_user = False
                # Check edges
                for e in state.in_edges(node):
                    if e.data.data == name:
                        is_user = True; break
                if not is_user:
                    for e in state.out_edges(node):
                        if e.data.data == name:
                            is_user = True; break
                # Check free symbols (covers symbol mappings in NestedSDFG, etc.)
                if not is_user and name in node_free_syms:
                    is_user = True
                
                if is_user:
                    cur_ver = vmap.get(name)
                    if cur_ver:
                        cur_ver.record_usage(node)


def _is_conditional_exhaustive(cb: ConditionalBlock) -> bool:
    """Check if a ConditionalBlock covers all paths (no implicit fall-through)."""
    if any(cond is None for cond, _ in cb.branches):
        return True
    # TODO: We need a way to symbolically check if the disjunction of all branch
    # conditions is a tautology (covers all paths). This would let us recognize
    # exhaustive conditionals like "x > 0" / "x <= 0" without an explicit else.
    # Current blockers:
    #   - DaCe's pystr_to_symbolic is for math expressions, not boolean conditions.
    #     "True"/"False" literals and relational operators aren't handled correctly.
    #   - Sympy's Or/simplify would work if we could get proper boolean expressions,
    #     but the CodeBlock → sympy conversion path doesn't exist cleanly.
    # For now, we only rely on cond=None (the canonical DaCe "else" marker).
    # Conditions like ("True", "False") or ("x>0", "x<=0") are NOT detected as
    # exhaustive and will conservatively include the fall-through path.
    try:
        from sympy import Or, true as sym_true
        cond_exprs = []
        for cond, _ in cb.branches:
            if cond is not None:
                expr = symbolic.pystr_to_symbolic(cond.as_string)
                cond_exprs.append(expr)
        if cond_exprs:
            combined = Or(*cond_exprs)
            if combined.simplify() == sym_true or combined == sym_true:
                return True
    except Exception:
        pass
    return False


def analyze_cfg(
    cfg: ControlFlowRegion,
    vmap: SSAVersionMap,
    targets: Set[str],
    plan: SSAPlan,
):
    """Recursively analyze a control-flow region and populate the plan."""
    for child in tqdm(topological_sort(cfg), desc=f"Analyzing {cfg.label}", leave=False):
        if isinstance(child, SDFGState):
            analyze_state(child, vmap, targets, plan)

        elif isinstance(child, LoopRegion):
            # 1. Use snapshot for loop metadata (bound, init, update)
            for _, ver in vmap.snapshot(targets).items():
                ver.record_usage(child)
            
            # 2. Body analysis
            first_access = {orig: _first_access_in_body(child, orig) for orig in targets}
            entry_vmap = vmap.copy()
            plan_before = set(plan.versions)

            body_vmap = vmap.copy()
            analyze_cfg(child, body_vmap, targets, plan)

            # 3. Carry detection
            for orig in targets:
                entry_ver = entry_vmap.get(orig)
                exit_ver = body_vmap.get(orig)
                if exit_ver and exit_ver != entry_ver:
                    if first_access.get(orig) in ("R", "RW"):
                        # Collect ALL versions created in body for this variable
                        body_versions = [v for v in plan.versions
                                         if v not in plan_before and v.base_name == orig]
                        if entry_ver:
                            # Merge all body versions into entry_ver
                            for bv in body_versions:
                                plan.merge_versions(entry_ver, bv)
                            vmap.set(orig, entry_ver)
                        else:
                            # Revert ALL body versions (no pre-loop def)
                            for bv in body_versions:
                                if bv in plan.versions:
                                    plan.versions.remove(bv)
                            vmap.set(orig, None)
                    else:
                        # Non-carried write: the last iteration's version persists
                        vmap.set(orig, exit_ver)

        elif isinstance(child, ConditionalBlock):
            # 1. Use snapshot for conditions
            for _, ver in vmap.snapshot(targets).items():
                ver.record_usage(child)
            
            # 2. Branch analysis
            branch_vmaps = []
            for _, branch_reg in child.branches:
                bvmap = vmap.copy()
                analyze_cfg(branch_reg, bvmap, targets, plan)
                branch_vmaps.append(bvmap)

            # 3. Merge branches
            # A conditional is exhaustive if it has a default/else branch (cond is None),
            # or if the disjunction of all branch conditions simplifies to True.
            is_exhaustive = _is_conditional_exhaustive(child)
            pre_ver_map = vmap.copy()
            for orig in targets:
                versions = {bvmap.get(orig) for bvmap in branch_vmaps if bvmap.get(orig) is not None}
                # If not exhaustive, the implicit fall-through keeps pre_ver
                if not is_exhaustive:
                    pre_ver_ft = pre_ver_map.get(orig)
                    if pre_ver_ft is not None:
                        versions.add(pre_ver_ft)
                if len(versions) == 1:
                    vmap.set(orig, list(versions)[0])
                elif len(versions) > 1:
                    pre_ver = pre_ver_map.get(orig)
                    if pre_ver and pre_ver not in versions:
                        # All branches wrote (pre_ver is dead after conditional).
                        # Merge branch versions together, keep pre_ver separate.
                        survivor = next(iter(versions))
                        for bver in versions:
                            if bver is not survivor:
                                plan.merge_versions(survivor, bver)
                        vmap.set(orig, survivor)
                    elif pre_ver:
                        # Some branches didn't write — merge everything to pre_ver
                        for bver in list(versions):
                            plan.merge_versions(pre_ver, bver)
                        vmap.set(orig, pre_ver)
                    else:
                        # No pre-conditional definition: revert all branches back to original
                        for bver in list(versions):
                            if bver in plan.versions:
                                plan.versions.remove(bver)
                        vmap.set(orig, None)

        elif isinstance(child, ControlFlowRegion):
            analyze_cfg(child, vmap, targets, plan)

        # 4. Outgoing edges (InterstateEdge usage)
        snapshot = vmap.snapshot(targets)
        for _, ver in snapshot.items():
            for e in cfg.out_edges(child):
                ver.record_usage(e.data)
