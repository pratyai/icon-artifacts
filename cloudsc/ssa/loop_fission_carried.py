"""loop_fission_carried.py — Loop fission by loop-carried dependency.

DETECTION + ANALYSIS ONLY (v1-tight, pass A).

Pattern targeted (the cloudsc shape):

    state X
    └── Map[jl]                                (outer parallel)
        └── NSDFG (e.g. nested_single_state_body)
            └── LoopRegion L [jk = lo : hi]    (sequential per thread)
                └── single body state B
                    └── single inner NSDFG N (e.g. loop_body)

Inside N, an array X is "loop-carried" if it appears in BOTH N's
in_connectors and N's out_connectors. Each such X conceptually does
``X[jk] = X[jk-1] + delta(jk, level-local inputs)`` — a prefix sum.

This pass finds the pattern, classifies every Tasklet/NSDFG/LibraryNode
inside N as either ``carried`` (in the dataflow chain that ends in a write
to a carried array) or ``pure`` (level-local), and prints a fission plan.
It does NOT modify the SDFG yet — that surgery comes next once the
analysis is verified.

Run after gpu_offload (so gpu_<X> arrays exist).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import dace
from dace import nodes
from dace.sdfg import SDFG
from dace.sdfg.state import (
    SDFGState, LoopRegion,
    ControlFlowRegion, AbstractControlFlowRegion, ConditionalBlock,
)


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------

@dataclass
class FissionCandidate:
    """One loop region that matches the carried-fission pattern."""
    parent_sdfg: SDFG
    loop: LoopRegion
    body_state: SDFGState
    inner_nsdfg: nodes.NestedSDFG
    carried_arrays: set[str] = field(default_factory=set)
    pure_nodes: set[nodes.Node] = field(default_factory=set)
    carried_nodes: set[nodes.Node] = field(default_factory=set)
    bail_reason: Optional[str] = None


def _all_blocks(graph):
    for blk in graph.nodes():
        yield blk
        if isinstance(blk, (ControlFlowRegion, AbstractControlFlowRegion,
                            LoopRegion, ConditionalBlock)):
            yield from _all_blocks(blk)


def _all_loop_regions(sdfg: SDFG):
    for cur in sdfg.all_sdfgs_recursive():
        for blk in _all_blocks(cur):
            if isinstance(blk, LoopRegion):
                yield cur, blk


def _detect_pattern(parent_sdfg: SDFG, loop: LoopRegion) -> Optional[FissionCandidate]:
    """Return a FissionCandidate if `loop` matches the cloudsc shape, else None."""
    body_states = [b for b in loop.nodes() if isinstance(b, SDFGState)]
    if len(body_states) != 1:
        return None
    body = body_states[0]

    # Body must contain exactly one top-level NSDFG (no other top-level compute).
    sdict = body.scope_dict()
    nsdfgs = [n for n in body.nodes()
              if isinstance(n, nodes.NestedSDFG) and sdict.get(n) is None]
    other_compute = [n for n in body.nodes()
                     if isinstance(n, (nodes.Tasklet, nodes.LibraryNode))
                     and sdict.get(n) is None]
    if len(nsdfgs) != 1 or other_compute:
        return None
    inner = nsdfgs[0]

    carried = set(inner.in_connectors) & set(inner.out_connectors)
    if not carried:
        return None  # nothing to fission

    return FissionCandidate(
        parent_sdfg=parent_sdfg,
        loop=loop,
        body_state=body,
        inner_nsdfg=inner,
        carried_arrays=carried,
    )


# ---------------------------------------------------------------------------
# Body classification: carried vs pure
# ---------------------------------------------------------------------------

def _classify_body(cand: FissionCandidate) -> None:
    """Walk the inner NSDFG body and split every Tasklet / NSDFG / LibraryNode
    into carried (forward-reachable from a READ of a carried array, including
    via cross-state transient AccessNodes) vs pure (everything else).

    Algorithm — fixed-point taint propagation over ARRAYS (not nodes), so
    cross-state dataflow through intermediate transients is captured:

      1. Seed: every carried-array name is "tainted" (its values depend on
         the previous iteration).
      2. Propagate: any tasklet reading a tainted array taints ALL its output
         arrays.  Repeat to fixed point.
      3. Mark: a tasklet is carried iff it reads any tainted array OR
         directly writes a carried array (the boundary RMW).
      4. Pure = everything else.
    """
    inner = cand.inner_nsdfg.sdfg

    # Tainted arrays: start with the carried-array set itself.
    tainted: set[str] = set(cand.carried_arrays)

    # Index: for each tasklet/NSDFG/LibraryNode (across states), its input
    # array names and output array names (via AccessNode neighbours).
    inputs_of: dict[nodes.Node, set[str]] = {}
    outputs_of: dict[nodes.Node, set[str]] = {}
    # Plus direct AccessNode→AccessNode propagation pairs (src_array, dst_array).
    an_to_an_edges: list[tuple[str, str]] = []
    for st in inner.states():
        for nd in st.nodes():
            if not isinstance(nd, (nodes.Tasklet, nodes.NestedSDFG, nodes.LibraryNode)):
                continue
            ins = set()
            outs = set()
            for e in st.in_edges(nd):
                if isinstance(e.src, nodes.AccessNode):
                    ins.add(e.src.data)
            for e in st.out_edges(nd):
                if isinstance(e.dst, nodes.AccessNode):
                    outs.add(e.dst.data)
            inputs_of[nd] = ins
            outputs_of[nd] = outs
        for e in st.edges():
            if isinstance(e.src, nodes.AccessNode) and isinstance(e.dst, nodes.AccessNode):
                an_to_an_edges.append((e.src.data, e.dst.data))

    # Fixed-point: keep expanding `tainted` until no more growth.
    # Two propagation rules:
    #   (a) tasklet with any tainted input → all its outputs tainted
    #   (b) direct AN→AN edge with tainted source → dst tainted
    changed = True
    while changed:
        changed = False
        for nd, ins in inputs_of.items():
            if ins & tainted:
                new_outs = outputs_of[nd] - tainted
                if new_outs:
                    tainted |= new_outs
                    changed = True
        for src_arr, dst_arr in an_to_an_edges:
            if src_arr in tainted and dst_arr not in tainted:
                tainted.add(dst_arr)
                changed = True

    # Classify: carried iff (reads any tainted array) OR (directly writes
    # a carried array).
    for nd, ins in inputs_of.items():
        if ins & tainted:
            cand.carried_nodes.add(nd)
            continue
        if outputs_of[nd] & cand.carried_arrays:
            cand.carried_nodes.add(nd)
            continue
        cand.pure_nodes.add(nd)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def report_candidate(cand: FissionCandidate) -> None:
    """Print a fission plan for one candidate."""
    inner = cand.inner_nsdfg.sdfg
    print(f"--- Fission candidate ---")
    print(f"  parent SDFG : {cand.parent_sdfg.name}")
    print(f"  loop region : {cand.loop.label}  (var={cand.loop.loop_variable})")
    print(f"  body state  : {cand.body_state.label}")
    print(f"  inner NSDFG : {cand.inner_nsdfg.label}  (sd={inner.name})")
    print(f"  carried arrays ({len(cand.carried_arrays)}):")
    for a in sorted(cand.carried_arrays):
        d = inner.arrays.get(a)
        shape = tuple(d.shape) if d else "?"
        print(f"      {a}  shape={shape}")
    print(f"  carried nodes : {len(cand.carried_nodes)}")
    print(f"  pure nodes    : {len(cand.pure_nodes)}")
    if cand.pure_nodes:
        # Sample
        sample = list(cand.pure_nodes)[:5]
        for s in sample:
            kind = type(s).__name__
            lbl = getattr(s, "label", "?")
            print(f"      pure: {kind}({lbl})")
        if len(cand.pure_nodes) > 5:
            print(f"      ... +{len(cand.pure_nodes)-5} more")
    if cand.bail_reason:
        print(f"  BAIL: {cand.bail_reason}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def find_candidates(sdfg: SDFG) -> list[FissionCandidate]:
    """Return all loop-fission candidates in `sdfg` with classification done."""
    candidates: list[FissionCandidate] = []
    for parent, loop in _all_loop_regions(sdfg):
        cand = _detect_pattern(parent, loop)
        if cand is None:
            continue
        _classify_body(cand)
        candidates.append(cand)
    return candidates


def report(sdfg: SDFG) -> int:
    """Print fission plans for every candidate in `sdfg`. Returns count."""
    cands = find_candidates(sdfg)
    if not cands:
        print("[loop_fission_carried] no fission candidates found")
        return 0
    print(f"[loop_fission_carried] found {len(cands)} candidate loop(s)")
    for c in cands:
        report_candidate(c)
    return len(cands)


# ---------------------------------------------------------------------------
# Surgery
# ---------------------------------------------------------------------------

def _find_boundaries(cand: FissionCandidate) -> dict[str, str]:
    """Return {boundary_array_name (in inner NSDFG): owning_state_label}.
    A boundary is a transient AccessNode produced by a pure tasklet AND
    consumed by a carried tasklet (the level-local delta values that need
    to be staged across the fission)."""
    inner = cand.inner_nsdfg.sdfg
    bnds: dict[str, str] = {}
    for st in inner.states():
        for nd in st.nodes():
            if not isinstance(nd, nodes.AccessNode):
                continue
            if st.in_degree(nd) == 0:
                continue
            srcs = [e.src for e in st.in_edges(nd)
                    if isinstance(e.src, (nodes.Tasklet, nodes.NestedSDFG, nodes.LibraryNode))]
            if not srcs or not all(s in cand.pure_nodes for s in srcs):
                continue
            dsts = [e.dst for e in st.out_edges(nd)
                    if isinstance(e.dst, (nodes.Tasklet, nodes.NestedSDFG, nodes.LibraryNode))]
            if any(d in cand.carried_nodes for d in dsts):
                bnds.setdefault(nd.data, st.label)
    return bnds


def _parse_loop_range(loop: LoopRegion):
    """Return (start_sym, end_sym) parsed from "v = start" + "v < end"."""
    from dace import symbolic
    if loop.init_statement is None or loop.loop_condition is None:
        return None, None
    init = loop.init_statement.as_string
    cond = loop.loop_condition.as_string
    if "=" not in init or "<" not in cond:
        return None, None
    try:
        _, rhs = init.split("=", 1)
        start = symbolic.pystr_to_symbolic(rhs.strip())
        cond_clean = cond.replace("(", "").replace(")", "").strip()
        _, end_expr = cond_clean.split("<", 1)
        end = symbolic.pystr_to_symbolic(end_expr.strip())
        return start, end
    except Exception:
        return None, None


def apply_fission(cand: FissionCandidate) -> bool:
    """Perform the fission surgery for one candidate. Returns True on success.

    Strategy: the lifted parallel phase is added as a NEW STATE at the top-
    level outer SDFG, BEFORE the state that contains the outer Map+seq-loop
    structure. The lifted state has a 2-D Map[jl_lift, jk_lift] containing a
    stripped copy of the inner-NSDFG body, writing 10 delta arrays of shape
    [klev+1, klon]. The original seq loop is then reduced to: for jk read
    delta[jk, jl] and do the carried RMW on pf*[jk, jl].
    """
    import copy as _copy
    from dace import data as dt, dtypes, subsets
    from dace import symbolic as _symbolic
    from dace import Memlet

    # 1. Boundaries.
    boundaries = _find_boundaries(cand)
    if not boundaries:
        return False

    # 2. Loop range.
    start, end = _parse_loop_range(cand.loop)
    if start is None:
        return False
    loop_size = end - start  # number of iterations

    # 3. Locate outer state and outer Map.
    inner_ns_node = cand.parent_sdfg.parent_nsdfg_node
    outer_state = cand.parent_sdfg.parent
    outer_sdfg = outer_state.parent if outer_state is not None else None
    if inner_ns_node is None or outer_state is None or outer_sdfg is None:
        return False
    sdict_outer = outer_state.scope_dict()
    outer_map_entry = sdict_outer.get(inner_ns_node)
    if not isinstance(outer_map_entry, nodes.MapEntry):
        return False  # need an enclosing parallel Map

    # 4. Staging arrays at outer_sdfg level. Shape = first carried-array shape.
    sample_carried = next(iter(cand.carried_arrays))
    sample_shape = tuple(cand.inner_nsdfg.sdfg.arrays[sample_carried].shape)
    sample_dtype = cand.inner_nsdfg.sdfg.arrays[sample_carried].dtype

    staging_names: dict[str, str] = {}  # boundary in inner → staging in outer
    for bnd in sorted(boundaries):
        nm = f"stage_{bnd}"
        i = 0
        while nm in outer_sdfg.arrays:
            i += 1
            nm = f"stage_{bnd}_{i}"
        outer_sdfg.add_datadesc(nm, dt.Array(
            dtype=sample_dtype,
            shape=sample_shape,
            transient=True,
            storage=dtypes.StorageType.GPU_Global,
        ))
        staging_names[bnd] = nm

    # 5. Build the lifted phase. Strategy: deepcopy cand.parent_sdfg (the
    # entire NSDFG containing the loop). Inside the deepcopy, REPLACE the
    # loop body's inner NSDFG with a stripped copy that only has pure nodes,
    # writing to NEW out-connectors named after the staging arrays.
    # Wrap the resulting (modified) NSDFG inside a Map[jl_lift, jk_lift]
    # in a new state before outer_state.
    if not _build_and_install_lifted_phase(
        cand, outer_state, outer_sdfg, outer_map_entry,
        boundaries, staging_names, start, end,
    ):
        return False

    # 6. Modify the original (seq) inner NSDFG: strip pure nodes, replace the
    # boundary AccessNodes with reads from staging arrays (new in_conns on
    # every NSDFG layer between outer state and the inner body).
    if not _rewire_seq_phase(
        cand, outer_state, outer_sdfg, inner_ns_node, outer_map_entry,
        boundaries, staging_names,
    ):
        return False

    return True


def _build_and_install_lifted_phase(cand, outer_state, outer_sdfg,
                                    outer_map_entry, boundaries, staging_names,
                                    start, end) -> bool:
    """Build the parallel lifted phase. We construct a fresh NSDFG that
    matches the inner-NSDFG body but with carried nodes removed and boundary
    AccessNodes redirected to NEW out-connectors named after staging arrays.
    The resulting NSDFG is wrapped in a Map[jl_lift, jk_lift] inside a new
    state before outer_state.
    """
    import copy as _copy
    from dace import data as dt, dtypes, subsets
    from dace import Memlet

    inner_sd = cand.inner_nsdfg.sdfg
    # Deepcopy to avoid mutating the original.
    lifted_body = _copy.deepcopy(inner_sd)
    lifted_body.name = f"lifted_{inner_sd.name}"

    # Re-classify the deepcopy with the same logic to find which nodes
    # correspond to carried/pure (identities differ after deepcopy, so we
    # can't reuse cand.{pure,carried}_nodes directly).
    fake_cand = FissionCandidate(
        parent_sdfg=cand.parent_sdfg,  # not used by classification
        loop=cand.loop,
        body_state=cand.body_state,
        inner_nsdfg=cand.inner_nsdfg,
        carried_arrays=cand.carried_arrays,
    )
    # Re-do classification on lifted_body (mimics _classify_body but on a
    # different sdfg).
    fake_cand_lifted = FissionCandidate(
        parent_sdfg=cand.parent_sdfg,
        loop=cand.loop,
        body_state=cand.body_state,
        inner_nsdfg=cand.inner_nsdfg,
        carried_arrays=cand.carried_arrays,
    )

    # Manual taint over lifted_body's states.
    inputs_of: dict[nodes.Node, set[str]] = {}
    outputs_of: dict[nodes.Node, set[str]] = {}
    an_to_an: list[tuple[str, str]] = []
    for st in lifted_body.states():
        for nd in st.nodes():
            if isinstance(nd, (nodes.Tasklet, nodes.NestedSDFG, nodes.LibraryNode)):
                ins = {e.src.data for e in st.in_edges(nd) if isinstance(e.src, nodes.AccessNode)}
                outs = {e.dst.data for e in st.out_edges(nd) if isinstance(e.dst, nodes.AccessNode)}
                inputs_of[nd] = ins
                outputs_of[nd] = outs
        for e in st.edges():
            if isinstance(e.src, nodes.AccessNode) and isinstance(e.dst, nodes.AccessNode):
                an_to_an.append((e.src.data, e.dst.data))

    tainted = set(cand.carried_arrays)
    chg = True
    while chg:
        chg = False
        for nd, ins in inputs_of.items():
            if ins & tainted:
                new = outputs_of[nd] - tainted
                if new:
                    tainted |= new
                    chg = True
        for s, d in an_to_an:
            if s in tainted and d not in tainted:
                tainted.add(d)
                chg = True

    lifted_carried_nodes: set[nodes.Node] = set()
    for nd, ins in inputs_of.items():
        if ins & tainted:
            lifted_carried_nodes.add(nd)
        elif outputs_of[nd] & cand.carried_arrays:
            lifted_carried_nodes.add(nd)

    # Strip carried nodes & their carried-only AccessNodes.
    for st in lifted_body.states():
        for nd in list(st.nodes()):
            if nd in lifted_carried_nodes:
                for e in list(st.in_edges(nd)) + list(st.out_edges(nd)):
                    st.remove_edge(e)
                st.remove_node(nd)
        # Remove carried-array AccessNodes (they're either dangling or only
        # read by removed carried nodes).
        for nd in list(st.nodes()):
            if isinstance(nd, nodes.AccessNode) and nd.data in cand.carried_arrays:
                for e in list(st.in_edges(nd)) + list(st.out_edges(nd)):
                    st.remove_edge(e)
                st.remove_node(nd)
        # Remove ALL isolated AccessNodes (level-local inputs whose only
        # consumers were carried, transient intermediates with no producer,
        # etc.).
        for nd in list(st.nodes()):
            if isinstance(nd, nodes.AccessNode) and st.degree(nd) == 0:
                st.remove_node(nd)

    # Drop carried-array descriptors from lifted_body (no longer accessed).
    for nm in cand.carried_arrays:
        if nm in lifted_body.arrays:
            try:
                lifted_body.remove_data(nm, validate=False)
            except Exception:
                pass

    # Convert each boundary AccessNode into a write to a new out-connector
    # of the lifted_body NSDFG. The simplest way: rename the boundary
    # transient inside lifted_body to a non-transient with the staging name,
    # add it to the inner sdfg's arrays as non-transient.
    for bnd in boundaries:
        if bnd not in lifted_body.arrays:
            continue
        staging = staging_names[bnd]
        # Add a non-transient descriptor for the staging name (full carried shape).
        sample_shape = outer_sdfg.arrays[staging].shape
        sample_dtype = outer_sdfg.arrays[staging].dtype
        if staging not in lifted_body.arrays:
            lifted_body.add_datadesc(staging, dt.Array(
                dtype=sample_dtype, shape=sample_shape,
                transient=False, storage=dtypes.StorageType.GPU_Global,
            ))
        # Rename inner AccessNodes for `bnd` to write into `staging` at index
        # [jk_lift, jl_lift].
        # We rename the AccessNode but rewrite its memlet to index into staging.
        for st in lifted_body.states():
            for nd in list(st.nodes()):
                if isinstance(nd, nodes.AccessNode) and nd.data == bnd:
                    nd.data = staging
                    for e in list(st.in_edges(nd)) + list(st.out_edges(nd)):
                        if e.data is None:
                            continue
                        # Rewrite memlet to staging[jk_lift, jl_lift].
                        # We use placeholders here that the outer Map will bind.
                        e.data.data = staging
                        e.data.subset = subsets.Range(
                            [(_symbolic_(cand.loop.loop_variable),
                              _symbolic_(cand.loop.loop_variable), 1),
                             (_symbolic_(outer_map_entry.map.params[0]),
                              _symbolic_(outer_map_entry.map.params[0]), 1)]
                        )

    # Build lifted state and Map.
    lifted_state = outer_sdfg.add_state_before(
        outer_state, label=f"lifted_{cand.loop.label}",
    )
    jl_var = outer_map_entry.map.params[0]
    jl_range_str = str(outer_map_entry.map.range)  # falls back; better to use range tuple
    jl_lo, jl_hi, _ = outer_map_entry.map.range[0]
    jk_var = cand.loop.loop_variable
    # Iterator order matters for GPU codegen: DaCe maps the LAST iter to
    # block.x (with default block size 256), and earlier iters to grid.y/z
    # (capped at 65535).  Put jk (small, ~klev=138) first so klon lands on
    # the threaded axis where it can scale past 65535.
    me, mx = lifted_state.add_map(
        name=f"lifted_{cand.loop.label}",
        ndrange={jk_var: f"{start}:{end}",
                 jl_var: f"{jl_lo}:{jl_hi + 1}"},
        schedule=dtypes.ScheduleType.GPU_Device,
    )
    me.map.schedule = dtypes.ScheduleType.GPU_Device

    # Determine in-conns for the lifted NSDFG: every non-transient array
    # currently read.
    in_arrays = set()
    out_arrays = set(staging_names.values())
    for st in lifted_body.states():
        for nd in st.nodes():
            if not isinstance(nd, nodes.AccessNode):
                continue
            if nd.data not in lifted_body.arrays:
                continue
            desc = lifted_body.arrays[nd.data]
            if desc.transient:
                continue
            if st.out_degree(nd) > 0 and nd.data not in out_arrays:
                in_arrays.add(nd.data)

    # Add lifted NSDFG node.
    # Build symbol mapping: copy the original inner_nsdfg's symbol_mapping
    # so inner names (e.g. jk__l8) bind to the outer Map's iterators
    # (jk_var, jl_var).  Plus pass through klev/klon/etc.
    symbol_mapping = dict(cand.inner_nsdfg.symbol_mapping)
    for s in lifted_body.free_symbols:
        s_str = str(s)
        if s_str not in symbol_mapping:
            symbol_mapping[s_str] = _symbolic_(s_str)
    nsdfg_node = lifted_state.add_nested_sdfg(
        lifted_body,
        inputs={c: None for c in in_arrays},
        outputs={c: None for c in out_arrays},
        symbol_mapping=symbol_mapping,
    )

    # Wire in-edges.
    for c in sorted(in_arrays):
        if c not in outer_sdfg.arrays:
            return False
        an = lifted_state.add_access(c)
        memlet = Memlet.from_array(c, outer_sdfg.arrays[c])
        lifted_state.add_edge(an, None, me, f"IN_{c}", memlet)
        lifted_state.add_edge(me, f"OUT_{c}", nsdfg_node, c, _copy.deepcopy(memlet))
        me.add_in_connector(f"IN_{c}")
        me.add_out_connector(f"OUT_{c}")
    for c in sorted(out_arrays):
        an = lifted_state.add_access(c)
        memlet = Memlet.from_array(c, outer_sdfg.arrays[c])
        lifted_state.add_edge(nsdfg_node, c, mx, f"IN_{c}", memlet)
        lifted_state.add_edge(mx, f"OUT_{c}", an, None, _copy.deepcopy(memlet))
        mx.add_in_connector(f"IN_{c}")
        mx.add_out_connector(f"OUT_{c}")

    return True


def _rewire_seq_phase(cand, outer_state, outer_sdfg, inner_ns_node,
                      outer_map_entry, boundaries, staging_names) -> bool:
    """Strip pure nodes from the inner NSDFG and replace boundary AccessNodes
    with reads from staging arrays at index [loop_var, jl_var]."""
    import copy as _copy
    from dace import subsets, data as dt, dtypes
    from dace import Memlet

    inner_sd = cand.inner_nsdfg.sdfg
    # Strip pure nodes.
    for st in inner_sd.states():
        for nd in list(st.nodes()):
            if nd in cand.pure_nodes:
                for e in list(st.in_edges(nd)) + list(st.out_edges(nd)):
                    st.remove_edge(e)
                st.remove_node(nd)
        # Clean up isolated AccessNodes (level-local inputs whose only
        # consumers were the pure tasklets we just removed).
        for nd in list(st.nodes()):
            if isinstance(nd, nodes.AccessNode) and st.degree(nd) == 0:
                st.remove_node(nd)

    # Each boundary AccessNode (still present) now has zero in-edges (the
    # producer was a pure tasklet, which we removed). Convert it to read from
    # the staging array.
    # Need staging to be reachable inside this NSDFG: register descriptor as
    # non-transient and add an in_connector at every NSDFG layer up to the
    # outer state.

    # Ensure staging arrays are accessible inside inner_sd as non-transients.
    for bnd, staging in staging_names.items():
        if staging not in inner_sd.arrays:
            inner_sd.add_datadesc(staging, dt.Array(
                dtype=outer_sdfg.arrays[staging].dtype,
                shape=outer_sdfg.arrays[staging].shape,
                transient=False, storage=dtypes.StorageType.GPU_Global,
            ))
        cand.inner_nsdfg.add_in_connector(staging, force=True)

    # Inside inner_sd: replace each boundary AccessNode with a same-named
    # staging access reading at [loop_var, jl_var].
    loop_var = cand.loop.loop_variable
    jl_var = outer_map_entry.map.params[0]
    for st in inner_sd.states():
        for nd in list(st.nodes()):
            if not isinstance(nd, nodes.AccessNode):
                continue
            if nd.data not in boundaries:
                continue
            staging = staging_names[nd.data]
            nd.data = staging
            # Rewrite each outgoing edge's memlet to read staging[loop_var, jl_var].
            for e in list(st.out_edges(nd)):
                if e.data is None:
                    continue
                e.data.data = staging
                e.data.subset = subsets.Range([
                    (_symbolic_(loop_var), _symbolic_(loop_var), 1),
                    (_symbolic_(jl_var), _symbolic_(jl_var), 1),
                ])

    # Now add the staging in_connector to nested NSDFG layers so the data
    # actually reaches inner_sd.
    # cand.parent_sdfg is the NSDFG that contains cand.loop. We need to add
    # in_conns at this level too. Then to inner_ns_node (the NSDFG node in
    # outer_state) so the outer Map can pass staging in.
    parent_ns_node = cand.parent_sdfg.parent_nsdfg_node
    for staging in staging_names.values():
        if staging not in cand.parent_sdfg.arrays:
            cand.parent_sdfg.add_datadesc(staging, dt.Array(
                dtype=outer_sdfg.arrays[staging].dtype,
                shape=outer_sdfg.arrays[staging].shape,
                transient=False, storage=dtypes.StorageType.GPU_Global,
            ))
        if parent_ns_node is not None:
            parent_ns_node.add_in_connector(staging, force=True)

    # Connect parent_ns_node's new staging in_conns through the outer Map.
    # The outer Map's MapEntry needs IN_<staging>/OUT_<staging> connectors.
    for staging in staging_names.values():
        # Parent state (outer_state) needs an AccessNode for staging feeding
        # the outer Map.
        an = outer_state.add_access(staging)
        memlet = Memlet.from_array(staging, outer_sdfg.arrays[staging])
        outer_state.add_edge(an, None, outer_map_entry, f"IN_{staging}", memlet)
        outer_map_entry.add_in_connector(f"IN_{staging}")
        outer_map_entry.add_out_connector(f"OUT_{staging}")
        # And from outer_map_entry to parent_ns_node.
        outer_state.add_edge(outer_map_entry, f"OUT_{staging}",
                             inner_ns_node, staging, _copy.deepcopy(memlet))

    # Connect inside cand.parent_sdfg's body state from the staging in_conn
    # to the inner NSDFG's body. cand.parent_sdfg has a state that contains
    # `cand.loop`. Inside `cand.loop`, the body state contains the inner
    # NSDFG (cand.inner_nsdfg). We need to thread staging through:
    # parent_ns_node -> (parent_sdfg arrays as non-transient) -> inside loop
    # -> cand.inner_nsdfg's in_conn.
    # For simplicity in this v1, add an AccessNode for staging directly in
    # cand.body_state, feeding cand.inner_nsdfg.
    for staging in staging_names.values():
        an = cand.body_state.add_access(staging)
        memlet = Memlet.from_array(staging, cand.parent_sdfg.arrays[staging])
        cand.body_state.add_edge(an, None, cand.inner_nsdfg, staging, memlet)

    # Symbol-mapping plumbing.  The staging-array memlets we wrote inside
    # cand.inner_nsdfg use loop_var (jk) and outer-map var (jl).  Both must
    # be bound in the inner NSDFG's symbol_mapping (and propagated up).
    loop_var = cand.loop.loop_variable
    jl_var = outer_map_entry.map.params[0]
    for var in (loop_var, jl_var):
        if var not in cand.inner_nsdfg.symbol_mapping:
            cand.inner_nsdfg.symbol_mapping[var] = _symbolic_(var)
        # Ensure the inner sdfg knows about the symbol.
        if var not in cand.inner_nsdfg.sdfg.symbols:
            cand.inner_nsdfg.sdfg.add_symbol(var, dace.int64)
    # And propagate jl through cand.parent_sdfg -> parent_ns_node.
    parent_ns_node = cand.parent_sdfg.parent_nsdfg_node
    if parent_ns_node is not None:
        if jl_var not in parent_ns_node.symbol_mapping:
            parent_ns_node.symbol_mapping[jl_var] = _symbolic_(jl_var)
        if jl_var not in cand.parent_sdfg.symbols:
            cand.parent_sdfg.add_symbol(jl_var, dace.int64)

    return True


def _symbolic_(name):
    """Tiny symbol-from-string helper."""
    from dace import symbolic
    return symbolic.pystr_to_symbolic(name)


# ---------------------------------------------------------------------------
# Top-level entry point: apply to all candidates
# ---------------------------------------------------------------------------

def apply_all(sdfg: SDFG) -> int:
    """Find every candidate and attempt the surgery. Returns count applied."""
    n = 0
    for cand in find_candidates(sdfg):
        if apply_fission(cand):
            n += 1
    if n:
        print(f"[loop_fission_carried] applied surgery to {n} loops")
    return n
