"""lowprec.py — Precision lowering for CloudSC CPU pipeline.

SDFG-level transforms to lower float64 arrays/scalars to a target dtype.
Ported from velocity/utils/boundary_cast.py and compile_gpu_stage8.py.

Strategy (same as velocity):
  - Lower EVERYTHING we can to the target dtype
  - Non-transient arrays (function parameters / ABI) stay double externally
  - Boundary cast Maps at read/write sites convert double ↔ lowered dtype
  - Transient arrays are lowered directly (no cast needed)
  - Scalars: top-level params stay double, nested transients are lowered
"""

from __future__ import annotations

import re

import dace
from dace import dtypes, nodes
from dace.sdfg.state import ConditionalBlock
from dace.properties import CodeBlock


LOWPREC_MAP = {
    "fp64": dace.float64,
    "fp32": dace.float32,
    "fp16": dace.float16,
    "f32": dace.float32,
    "f64": dace.float64,
    "f16": dace.float16,
    # TODO: BFP modes
    # "bfp8": dace.float16,
    # "bfp16": dace.float16,
    # "bfp32": dace.float32,
}


# --- Core dtype propagation (from velocity/utils/boundary_cast.py) ---


def _propagate_dtype(
    sdfg: dace.SDFG,
    array_name: str,
    new_dtype: dace.typeclass,
    visited: set | None = None,
):
    """Recursively change an array's dtype through all NestedSDFGs.

    When an array's dtype is changed at the top level, NestedSDFGs that
    access it via connectors still have the old dtype in their internal
    array descriptors.  DaCe codegen uses the internal descriptors for
    parameter types, so they must match the allocation dtype.

    This function walks the SDFG hierarchy and updates:
    - The array descriptor dtype at each level
    - NestedSDFG connector-mapped internal arrays (recursively)
    """
    if visited is None:
        visited = set()
    key = (id(sdfg), array_name)
    if key in visited:
        return
    visited.add(key)

    if array_name not in sdfg.arrays:
        return
    sdfg.arrays[array_name].dtype = new_dtype

    for state in sdfg.states():
        for node in state.nodes():
            if not isinstance(node, nodes.NestedSDFG):
                continue
            for edge in state.in_edges(node):
                if edge.data.data == array_name and edge.dst_conn:
                    _propagate_dtype(node.sdfg, edge.dst_conn, new_dtype, visited)
            for edge in state.out_edges(node):
                if edge.data.data == array_name and edge.src_conn:
                    _propagate_dtype(node.sdfg, edge.src_conn, new_dtype, visited)


# --- Boundary cast for CPU non-transient arrays ---
#
# Same strategy as velocity's H2D/D2H boundary cast, treating the lowered
# computation as a "device":
#
#   Entry state ("H2D"): AN(param, double) → [cast Map] → AN(param_lowered, float)
#   ... all computation states use param_lowered ...
#   Exit state ("D2H"):  AN(param_lowered, float) → [cast Map] → AN(param, double)
#
# Non-transient arrays keep double in the function signature (ABI).
# All internal references are renamed to the lowered transient.


def inject_cpu_boundary_cast(
    sdfg: dace.SDFG,
    array_names: list[str],
    external_dtype: dace.typeclass,
    scalar_names: list[str] | None = None,
):
    """Insert boundary cast states for non-transient CPU arrays and scalars.

    For each non-transient array:
    1. Create a lowered transient ``{name}_lowered`` with external_dtype
    2. Rename ALL internal references (AccessNodes + memlets) to the lowered name
    3. Propagate the lowered dtype into NestedSDFGs
    4. After all renames, add entry/exit cast states at the SDFG boundary

    For each non-transient scalar:
    1. Create a lowered transient scalar ``{name}_lowered``
    2. Rename internal references
    3. Add simple cast tasklet (no Map) in entry/exit states

    The original non-transient data stays double (ABI preserved).
    All computation runs in the lowered dtype.
    """
    scalar_names = scalar_names or []

    # Identify modified outputs BEFORE performing any renames.
    # If we rename first, read_and_write_sets will only see the new name,
    # making it impossible to match against the original parameter names.
    _, written = sdfg.read_and_write_sets()

    array_pairs = []  # (orig_name, lowered_name) for arrays
    scalar_pairs = []  # (orig_name, lowered_name) for scalars

    for name in array_names:
        if name not in sdfg.arrays:
            print(f"  cpu_boundary_cast: {name} not in SDFG, skipping.")
            continue

        arr = sdfg.arrays[name]
        orig_dtype = arr.dtype
        lowered_name = f"{name}_lowered"

        # Create lowered transient with same shape
        sdfg.add_array(
            lowered_name,
            shape=arr.shape,
            dtype=external_dtype,
            transient=True,
            storage=dtypes.StorageType.CPU_Heap,
        )

        print(
            f"  cpu_boundary_cast: {name} ({orig_dtype}) → "
            f"{lowered_name} ({external_dtype})"
        )

        _rename_array_references(sdfg, name, lowered_name)
        _propagate_dtype(sdfg, lowered_name, external_dtype)

        array_pairs.append((name, lowered_name))

    for name in scalar_names:
        if name not in sdfg.arrays:
            print(f"  cpu_boundary_cast(scalar): {name} not in SDFG, skipping.")
            continue

        arr = sdfg.arrays[name]
        orig_dtype = arr.dtype
        lowered_name = f"{name}_lowered"

        sdfg.add_scalar(
            lowered_name,
            dtype=external_dtype,
            transient=True,
            storage=dtypes.StorageType.Register,
        )

        print(
            f"  cpu_boundary_cast(scalar): {name} ({orig_dtype}) → "
            f"{lowered_name} ({external_dtype})"
        )

        _rename_array_references(sdfg, name, lowered_name)
        _propagate_dtype(sdfg, lowered_name, external_dtype)

        scalar_pairs.append((name, lowered_name))

    if not array_pairs and not scalar_pairs:
        return

    array_outputs = [p for p in array_pairs if p[0] in written]
    scalar_outputs = [p for p in scalar_pairs if p[0] in written]

    # Debug: Print what we are excluding
    excluded_arrays = [p[0] for p in array_pairs if p[0] not in written]
    excluded_scalars = [p[0] for p in scalar_pairs if p[0] not in written]
    if excluded_arrays:
        print(f"  lowprec: Excluding {len(excluded_arrays)} read-only arrays from exit cast: {excluded_arrays[:10]}...")
    if excluded_scalars:
        print(f"  lowprec: Excluding {len(excluded_scalars)} read-only scalars from exit cast: {excluded_scalars[:10]}...")

    # Add entry state: cast double→float for all inputs ("H2D")
    _add_entry_cast_state(sdfg, array_pairs, external_dtype, scalar_pairs)

    # Add exit state: cast float→double for modified outputs ("D2H")
    if array_outputs or scalar_outputs:
        _add_exit_cast_state(sdfg, array_outputs, scalar_outputs)

    # Re-index internal CFG list after adding new states
    sdfg.reset_cfg_list()


def _rename_array_references(sdfg: dace.SDFG, old_name: str, new_name: str):
    """Rename all AccessNodes, memlets, and ConditionalBlock conditions
    from old_name to new_name across the full CFG tree."""
    _rename_in_cfg(sdfg, old_name, new_name)


def _rename_in_cfg(cfg, old_name: str, new_name: str):
    """Recursively walk CFG nodes (states, loops, conditionals) and rename.

    Covers: AccessNodes, memlets, ConditionalBlock branch conditions,
    and InterstateEdge conditions/assignments.
    """
    # Rename in interstate edges (conditions + assignments)
    for _src, _dst, edge in cfg.edges():
        if hasattr(edge, 'condition') and old_name in edge.condition.get_free_symbols():
            new_code = re.sub(
                r'\b' + re.escape(old_name) + r'\b', new_name,
                edge.condition.as_string,
            )
            edge.condition = CodeBlock(new_code)
        if hasattr(edge, 'assignments'):
            new_assigns = {}
            for k, v in edge.assignments.items():
                nk = new_name if k == old_name else k
                nv = re.sub(r'\b' + re.escape(old_name) + r'\b', new_name, str(v))
                new_assigns[nk] = nv
            edge.assignments = new_assigns

    for node in cfg.nodes():
        if isinstance(node, ConditionalBlock):
            # Rename in branch conditions
            for i, (cond, region) in enumerate(node.branches):
                if cond is not None and old_name in cond.get_free_symbols():
                    new_code = re.sub(
                        r'\b' + re.escape(old_name) + r'\b',
                        new_name,
                        cond.as_string,
                    )
                    node.branches[i] = (CodeBlock(new_code), region)
                _rename_in_cfg(region, old_name, new_name)
        elif isinstance(node, dace.SDFGState):
            # Rename AccessNodes and memlets in this state
            for n in node.nodes():
                if isinstance(n, nodes.AccessNode) and n.data == old_name:
                    n.data = new_name
            for edge in node.edges():
                if edge.data.data == old_name:
                    edge.data.data = new_name
        elif hasattr(node, 'nodes'):
            # LoopRegion or other control flow region
            _rename_in_cfg(node, old_name, new_name)


def _add_entry_cast_state(
    sdfg: dace.SDFG,
    array_pairs: list[tuple[str, str]],
    external_dtype: dace.typeclass,
    scalar_pairs: list[tuple[str, str]] | None = None,
):
    """Add a new entry state with cast Maps/tasklets: orig(double) → lowered(float).

    Inserts before the current start state, like an H2D copy state.
    """
    scalar_pairs = scalar_pairs or []
    source_states = sdfg.source_nodes()

    cast_state = sdfg.add_state("boundary_cast_entry", is_start_state=True)

    for orig_name, lowered_name in array_pairs:
        orig_arr = sdfg.arrays[orig_name]

        src_an = cast_state.add_access(orig_name)
        dst_an = cast_state.add_access(lowered_name)

        _add_cast_map(
            sdfg, cast_state,
            src_an, orig_name,
            dst_an, lowered_name,
            orig_arr.shape,
            external_dtype,
            f"h2d_{orig_name}",
        )

    for orig_name, lowered_name in scalar_pairs:
        _add_scalar_cast_tasklet(
            cast_state,
            orig_name, lowered_name,
            external_dtype,
            f"h2d_{orig_name}",
        )

    # Connect to previous source states
    for src_state in source_states:
        sdfg.add_edge(cast_state, src_state, dace.InterstateEdge())


def _add_exit_cast_state(
    sdfg: dace.SDFG,
    array_pairs: list[tuple[str, str]],
    scalar_pairs: list[tuple[str, str]] | None = None,
):
    """Add a new exit state with cast Maps/tasklets: lowered(float) → orig(double).

    Appends after the current sink states, like a D2H copy state.
    """
    scalar_pairs = scalar_pairs or []
    sink_states = sdfg.sink_nodes()

    cast_state = sdfg.add_state("boundary_cast_exit")

    for orig_name, lowered_name in array_pairs:
        orig_arr = sdfg.arrays[orig_name]

        src_an = cast_state.add_access(lowered_name)
        dst_an = cast_state.add_access(orig_name)

        _add_cast_map(
            sdfg, cast_state,
            src_an, lowered_name,
            dst_an, orig_name,
            orig_arr.shape,
            orig_arr.dtype,  # cast back to double
            f"d2h_{orig_name}",
        )

    for orig_name, lowered_name in scalar_pairs:
        _add_scalar_cast_tasklet(
            cast_state,
            lowered_name, orig_name,
            sdfg.arrays[orig_name].dtype,  # cast back to double
            f"d2h_{orig_name}",
        )

    # Connect from previous sink states
    for sink_state in sink_states:
        sdfg.add_edge(sink_state, cast_state, dace.InterstateEdge())


def _add_cast_map(
    sdfg: dace.SDFG,
    state: dace.SDFGState,
    src_an: nodes.AccessNode,
    src_name: str,
    dst_an: nodes.AccessNode,
    dst_name: str,
    shape: tuple,
    target_dtype: dace.typeclass,
    label: str,
):
    """Add a Map that casts every element from src to dst.

    Ported from velocity/utils/boundary_cast.py _add_cast_map.
    """
    src_desc = sdfg.arrays[src_name]
    dst_desc = sdfg.arrays[dst_name]

    map_ranges = {f"__i{i}": f"0:{s}" for i, s in enumerate(shape)}
    indices = ", ".join(f"__i{i}" for i in range(len(shape)))

    map_entry, map_exit = state.add_map(label, map_ranges)

    tasklet = state.add_tasklet(
        name=label,
        inputs={"_in"},
        outputs={"_out"},
        code=f"_out = static_cast<{target_dtype.ctype}>(_in);",
        language=dtypes.Language.CPP,
    )

    # Map entry connectors
    in_conn = f"IN_{src_name}"
    out_conn = f"OUT_{src_name}"
    map_entry.add_in_connector(in_conn)
    map_entry.add_out_connector(out_conn)

    # Map exit connectors
    exit_in_conn = f"IN_{dst_name}"
    exit_out_conn = f"OUT_{dst_name}"
    map_exit.add_in_connector(exit_in_conn)
    map_exit.add_out_connector(exit_out_conn)

    # Wire: src_an → map_entry
    state.add_edge(
        src_an, None, map_entry, in_conn,
        dace.Memlet.from_array(src_name, src_desc),
    )

    # Wire: map_entry → tasklet
    state.add_edge(
        map_entry, out_conn, tasklet, "_in",
        dace.Memlet(f"{src_name}[{indices}]"),
    )

    # Wire: tasklet → map_exit
    state.add_edge(
        tasklet, "_out", map_exit, exit_in_conn,
        dace.Memlet(f"{dst_name}[{indices}]"),
    )

    # Wire: map_exit → dst_an
    state.add_edge(
        map_exit, exit_out_conn, dst_an, None,
        dace.Memlet.from_array(dst_name, dst_desc),
    )


def _add_scalar_cast_tasklet(
    state: dace.SDFGState,
    src_name: str,
    dst_name: str,
    target_dtype: dace.typeclass,
    label: str,
):
    """Add a tasklet that casts a scalar: src → dst (no Map needed)."""
    src_an = state.add_access(src_name)
    dst_an = state.add_access(dst_name)

    tasklet = state.add_tasklet(
        name=label,
        inputs={"_in"},
        outputs={"_out"},
        code=f"_out = static_cast<{target_dtype.ctype}>(_in);",
        language=dtypes.Language.CPP,
    )

    state.add_edge(
        src_an, None, tasklet, "_in",
        dace.Memlet(f"{src_name}[0]"),
    )
    state.add_edge(
        tasklet, "_out", dst_an, None,
        dace.Memlet(f"{dst_name}[0]"),
    )


# --- Direct lowering for transient arrays ---


def lower_transient_arrays(
    sdfg: dace.SDFG,
    external_dtype: dace.typeclass,
    exclude: set[str] | None = None,
):
    """Lower transient float64 arrays directly (no cast needed — internal only)."""
    exclude = exclude or set()

    candidates = [
        name
        for name, arr in sdfg.arrays.items()
        if isinstance(arr, dace.data.Array)
        and arr.dtype == dace.float64
        and arr.transient
        and arr.total_size != 1
        and name not in exclude
    ]

    if candidates:
        print(f"Lowering {len(candidates)} transient arrays to {external_dtype}")
        for name in candidates:
            _propagate_dtype(sdfg, name, external_dtype)

    return candidates


# --- Top-level transient scalar lowering ---


def lower_top_level_scalars(
    sdfg: dace.SDFG,
    external_dtype: dace.typeclass,
    exclude: set[str] | None = None,
):
    """Lower transient double scalars at the root SDFG level.

    These are intermediates like zvqx_index, zvqx_slice that DaCe creates
    for array element indexing.  They must match the dtype of the arrays
    they feed into, otherwise CopyND emits mismatched types.

    Non-transient scalars are function parameters — they stay double (ABI).
    """
    exclude = exclude or set()
    count = 0

    for name, arr in list(sdfg.arrays.items()):
        if (
            isinstance(arr, dace.data.Scalar)
            and arr.dtype == dace.float64
            and arr.transient
            and name not in exclude
            and not any(name.startswith(ex + "_") for ex in exclude)
        ):
            arr.dtype = external_dtype
            count += 1

    if count:
        print(f"Lowered {count} top-level transient scalars to {external_dtype}")


# --- Scalar lowering (from compile_gpu_stage8.py) ---


def lower_nested_scalars(
    sdfg: dace.SDFG,
    external_dtype: dace.typeclass,
    exclude: set[str] | None = None,
):
    """Lower transient double scalars inside nested SDFGs.

    Top-level non-transient scalars are function parameters — they stay double
    (ABI). Only transient scalars in nested SDFGs (kernel intermediates) are
    lowered. Comparison results get int32 instead (logically boolean).

    Ported from compile_gpu_stage8.py.
    """
    exclude = exclude or set()

    # First pass: find scalars that are comparison outputs
    comparison_scalars: set[str] = set()
    for nsdfg in sdfg.all_sdfgs_recursive():
        if nsdfg is sdfg:
            continue
        for state in nsdfg.states():
            for node in state.nodes():
                if not isinstance(node, nodes.Tasklet):
                    continue
                code = node.code.as_string
                if not any(
                    op in code
                    for op in (" > ", " < ", " >= ", " <= ", " == ", " != ")
                ):
                    continue
                for e in state.out_edges(node):
                    if (
                        isinstance(e.dst, nodes.AccessNode)
                        and e.dst.data in nsdfg.arrays
                        and isinstance(nsdfg.arrays[e.dst.data], dace.data.Scalar)
                    ):
                        comparison_scalars.add(e.dst.data)

    count = 0
    for nsdfg in sdfg.all_sdfgs_recursive():
        if nsdfg is sdfg:
            continue
        for name, arr in list(nsdfg.arrays.items()):
            if (
                isinstance(arr, dace.data.Scalar)
                and arr.dtype == dace.float64
                and arr.transient
                and name not in exclude
            ):
                if name in comparison_scalars:
                    arr.dtype = dace.int32
                else:
                    arr.dtype = external_dtype
                count += 1

    if count:
        print(f"Lowered {count} nested transient scalars")


# --- Floatify tasklet code (from compile_gpu_stage8.py) ---


def floatify_tasklets(sdfg: dace.SDFG):
    """Replace double literals and math functions in tasklets with
    float equivalents to prevent FP64 promotion.

    Ported from compile_gpu_stage8.py — handles both CPP and Python tasklets.
    """
    constants = ["0.5", "0.85", "1.0", "0.0", "0.05", "0.65", "1.15"]

    for node, _state in sdfg.all_nodes_recursive():
        if not isinstance(node, nodes.Tasklet):
            continue

        code = node.code.as_string
        new_code = code

        if node.language == dace.Language.CPP:
            for val in constants:
                new_code = re.sub(
                    rf"(?<![0-9fF]){re.escape(val)}(?![0-9fF])",
                    f"{val}f",
                    new_code,
                )
                new_code = re.sub(
                    rf"(?<![0-9fF])\-{re.escape(val)}(?![0-9fF])",
                    f"-{val}f",
                    new_code,
                )
            # abs() -> fabsf()
            new_code = re.sub(r"\babs\(", "fabsf(", new_code)
            # ipow(x, 2) -> (x * x)
            new_code = re.sub(
                r"dace::math::ipow\(([^,]+),\s*2\)",
                r"((\1) * (\1))",
                new_code,
            )
        else:
            # Python tasklets: float() wrapper
            for val in constants:
                new_code = re.sub(
                    rf"(?<![0-9fF]){re.escape(val)}(?![0-9fF])",
                    f"float({val})",
                    new_code,
                )
                new_code = re.sub(
                    rf"(?<![0-9fF])\-{re.escape(val)}(?![0-9fF])",
                    f"float(-{val})",
                    new_code,
                )
            # x ** 2 or x ** 2.0 -> (x * x)
            new_code = re.sub(
                r"(\w+)\s*\*\*\s*2(?:\.0)?\b",
                r"((\1) * (\1))",
                new_code,
            )

        if new_code != code:
            node.code = dace.properties.CodeBlock(new_code, node.language)


# --- Top-level entry point ---


def apply_lowprec(sdfg: dace.SDFG, lowprec: str):
    """Apply all precision lowering transforms to the SDFG.

    Strategy (mirrors velocity):
      1. Non-transient arrays: boundary cast (keep double at ABI, cast to lowered)
      2. Transient arrays: lower dtype directly
      3. Nested transient scalars: lower dtype directly
      4. Floatify tasklet literals

    Args:
        sdfg: The SDFG to transform.
        lowprec: Precision mode string (fp64, fp32, fp16, etc.)
    """
    if lowprec not in LOWPREC_MAP:
        raise ValueError(
            f"Unknown lowprec value: {lowprec!r}. "
            f"Valid options: {', '.join(LOWPREC_MAP)}"
        )

    external_dtype = LOWPREC_MAP[lowprec]

    # TODO: BFP mode — separate path like velocity's inject_bfp_packing
    # if lowprec.startswith("bfp"):
    #     ...

    print(f"Applying precision lowering: fp64 → {external_dtype}")

    # # 0. Remove the return block — boundary_cast_exit will be the new sink
    # from dace.sdfg.state import ReturnBlock
    # return_blocks = [s for s in sdfg.nodes() if isinstance(s, ReturnBlock)]
    # assert len(return_blocks) == 1, (
    #     f"Expected exactly 1 ReturnBlock, found {len(return_blocks)}"
    # )
    # rb = return_blocks[0]
    # for edge in list(sdfg.in_edges(rb)):
    #     sdfg.remove_edge(edge)
    # sdfg.remove_node(rb)
    # print(f"  Removed ReturnBlock: {rb.label}")

    # Exclusion list: arrays/scalars that must stay fp64.
    # Everything NOT in this set gets lowered.
    # TODO: scan SDFG for scalars used in ConditionalBlock branch conditions —
    #   these can cause large errors even when per-variable sensitivity is high,
    #   because correlated fp32 rounding on BOTH sides of a comparison flips
    #   branches that one-at-a-time perturbation can't detect (e.g. yrecldp_ramin).
    _LOWERING_EXCLUDE = {
        # === CRITICAL LOCAL (SNR < 20 dB) ===
        "za",  # -65.3 dB
        "zlcust",  # -26.3 dB
        "zlfinal",  # -26.3 dB
        "zqxfg",  # 15.2 dB
        "zdepos",  # 16.3 dB

        # === HIGH-SENSITIVITY LOCAL (SNR 20-100 dB) ===
        "zsolqa",  # 51.5 dB
        "psum_solqa",  # 52.0 dB
        "zlcond1",  # 52.5 dB
        "zlcond2",  # 73.5 dB
        "ztp1",  # 73.9 dB
        "zsupsat",  # 81.3 dB
        # "zqlhs",  # 88.2 dB
        # "zqxn",  # 89.2 dB
        # "zexplicit",  # 93.8 dB
        # "zfallsink",  # 98.3 dB
        # "zfallsrce",  # 98.8 dB
        # "zqx",  # 99.4 dB

        # === MODERATE LOCAL (SNR 100-135 dB) ===
        # "zfoeew",  # 102.5 dB
        # "zqsmix",  # 105.0 dB
        # "zconvsrce",  # 105.7 dB
        # "zqsice",  # 105.9 dB
        # "zfokoop",  # 107.9 dB
        # "zqe",  # 108.1 dB
        # "zevap",  # 109.5 dB
        # "zqold",  # 110.2 dB
        # "zqsat",  # 110.8 dB
        # "zfac",  # 111.6 dB
        # "zqe_0",  # 112.0 dB
        # "zfluxq",  # 113.3 dB
        # "zfoeewmt",  # 113.3 dB
        # "zqsat_0",  # 115.8 dB
        # "zqp",  # 116.0 dB
        # "zcor_3",  # 116.2 dB
        # "zcor",  # 116.6 dB
        # "zsolqb",  # 118.5 dB
        # "zconvsink",  # 123.2 dB
        # "zcovptot",  # 127.6 dB
        # "zqe_5",  # 127.8 dB
        # "zgdp",  # 128.8 dB
        # "zaorig",  # 129.0 dB
        # "zanew",  # 129.1 dB
        # "zdtgdp",  # 129.4 dB
        # "zfac_0",  # 129.5 dB
        # "zdp",  # 130.1 dB
        # "zqp1env",  # 131.5 dB

        # === SAFE LOCAL (SNR >= 135 dB) ===
        # "zinfactor",  # 135.0 dB
        # "zfoealfa",  # 137.5 dB
        # "zrdtgdp",  # 138.7 dB
        # "zlneg",  # 138.8 dB
        # "zalfaw",  # 139.3 dB
        # "zpfplsx",  # 139.3 dB
        # "zqxn2d",  # 141.5 dB
        # "zda",  # 141.9 dB
        # "zratio",  # 142.2 dB
        # "zgdph_r",  # 143.8 dB
        # "zsolac",  # 143.8 dB
        # "zcorqsice",  # 144.2 dB
        # "zcovpclr",  # 144.3 dB
        # "zrr",  # 145.3 dB
        # "zdpevap",  # 146.8 dB
        # "zdenom_1",  # 146.9 dB
        # "zdpr_0",  # 146.9 dB
        # "zqtmst",  # 146.9 dB
        # "zdenom",  # 147.0 dB
        # "zdpr",  # 147.0 dB
        # "zevap_2",  # 147.0 dB
        # "zdpevap_1",  # 147.1 dB
        # "zleros",  # 148.1 dB
        # "zanewm1",  # 148.8 dB
        # "zmf",  # 149.2 dB
        # "zdqs",  # 149.3 dB
        # "zlicld",  # 149.6 dB
        # "zsnowrime",  # 149.8 dB
        # "zldifdt",  # 149.9 dB
        # "zvqx",  # 150.5 dB
        # "zicecld",  # 150.6 dB
        # "zrho",  # 150.6 dB
        # "zqxnm1",  # 151.2 dB
        # "zcor_2",  # 151.7 dB
        # "zacust",  # 152.0 dB
        # "zrg_r",  # 152.1 dB
        # "zdtforc",  # 152.3 dB
        # "ztmpa",  # 152.4 dB
        # "zaeros",  # 152.6 dB
        # "ze",  # 153.0 dB
        # "zmfdn",  # 153.0 dB
        # "zdtforc_0",  # 154.3 dB
        # "zfall",  # 154.4 dB
        # "zwtot",  # 154.8 dB
        # "zevaplimmix",  # 155.1 dB
        # "zacond",  # 155.2 dB
        # "zdtdp",  # 155.2 dB
        # "zsolab",  # 155.2 dB
        # "zdqsicedt",  # 155.4 dB
        # "zfaci",  # 155.4 dB
        # "ztold",  # 155.4 dB
        # "zcorqsmix",  # 155.5 dB
        # "zzzdt",  # 156.0 dB
        # "zdtdiab",  # 156.2 dB
        # "zdqsmixdt",  # 156.8 dB
        # "zfaci_0",  # 157.4 dB
        # "zcor_1",  # 157.8 dB
        # "zdtdp_0",  # 157.8 dB
        # "zcor_0",  # 158.4 dB
        # "zli",  # 160.2 dB
        # "zrdcp",  # 162.3 dB
        # "zliqcld",  # 162.7 dB
        # "zlevap",  # 164.2 dB
        # "zcdmax_0",  # 165.4 dB
        # "zcdmax",  # 166.0 dB
        # "zicefrac",  # 166.1 dB
        # "zbeta",  # 170.4 dB
        # "zbeta_1",  # 170.6 dB
        # "zfacw",  # 171.4 dB
        # "zmfdn_0",  # 173.7 dB
        # "zvpliq",  # 175.0 dB
        # "zpreclr",  # 175.1 dB
        # "zpreclr_1",  # 175.1 dB
        # "zbeta1",  # 175.2 dB
        # "zbeta1_0",  # 175.2 dB
        # "zsnowaut",  # 177.5 dB
        # "zliqfrac",  # 178.3 dB
        # "zqadj",  # 178.9 dB
        # "zdpmxdt",  # 179.5 dB
        # "zqadj_0",  # 179.5 dB
        # "zsinksum",  # 180.3 dB
        # "zzco",  # 180.7 dB
        # "zmm",  # 183.7 dB
        # "zlcrit",  # 184.5 dB
        # "ztmpa_0",  # 186.1 dB
        # "zcond1",  # 190.9 dB
        # "zcldtopdist",  # 191.3 dB
        # "zcond",  # 191.5 dB
        # "zinew",  # 201.0 dB
        # "zicenuclei",  # 202.7 dB
        # "zcvds",  # 203.7 dB
        # "zvpice",  # 207.3 dB
        # "zbdd",  # 207.6 dB
        # "zrainaut",  # 208.9 dB
        # "zadd",  # 211.0 dB
        # "zice0",  # 218.3 dB
        # "zconst",  # 224.2 dB
        # "zqadj_1",  # 229.5 dB
        # "zcor_4",  # 230.5 dB
        # "zsnowcld",  # 231.9 dB
        # "zfallcorr",  # 237.6 dB
        # "zpsupsatsrce",  # 814.4 dB
        # "zqx0",  # 820.0 dB

        # === FAIL LOCAL (crash on perturbation) ===
        # "zalfa",  # FAIL
        # "zalfa2",  # FAIL
        # "zbeta_0",  # FAIL
        # "zcons1",  # FAIL
        # "zcons1_0",  # FAIL
        # "zcorr2",  # FAIL
        # "zcovpmax",  # FAIL
        # "zdenom_0",  # FAIL
        # "zdpevap_0",  # FAIL
        # "zepsec",  # FAIL
        # "zepsilon",  # FAIL
        # "zesatliq",  # FAIL
        # "zevap_1",  # FAIL
        # "zevap_denom",  # FAIL
        # "zfallcorr_0",  # FAIL
        # "zfoeeliqt",  # FAIL
        # "zfrz",  # FAIL
        # "zfrz_0",  # FAIL
        # "zfrz_1",  # FAIL
        # "zfrzmax",  # FAIL
        # "zicetot",  # FAIL
        # "zlambda",  # FAIL
        # "zlambda_0",  # FAIL
        # "zlcondlim",  # FAIL
        # "zldefr",  # FAIL
        # "zlfinal_0",  # FAIL
        # "zlfinalsum",  # FAIL
        # "zmax",  # FAIL
        # "zmelt",  # FAIL
        # "zmeltmax",  # FAIL
        # "zpreclr_0",  # FAIL
        # "zqe_2",  # FAIL
        # "zqe_4",  # FAIL
        # "zqpretot",  # FAIL
        # "zqsliq",  # FAIL
        # "zrainacc",  # FAIL
        # "zraincld",  # FAIL
        # "zrat",  # FAIL
        # "zrhc",  # FAIL
        # "zrldcp",  # FAIL
        # "zsigk",  # FAIL
        # "zsubsat",  # FAIL
        # "zsubsat_0",  # FAIL
        # "ztdmtw0",  # FAIL
        # "ztemp",  # FAIL
        # "ztw1",  # FAIL
        # "ztw2",  # FAIL
        # "ztw3",  # FAIL
        # "ztw4",  # FAIL
        # "ztw5",  # FAIL
        # "zzdl",  # FAIL
        # "zzrh",  # FAIL
        # "zzrh_0",  # FAIL
        # "zzrh_1",  # FAIL

        # === UNUSED LOCAL (0 injection sites) ===
        # "zalfaw_0",  # unused
        # "zalfaw_1",  # unused
        # "zcorqsliq",  # FAIL
        # "zdqsliqdt",  # FAIL
        # "zevaplimice",  # FAIL
        # "zfac_1",  # unused
        # "zqe_1",  # unused
        # "zre_ice",  # unused
        # "zzratio",  # unused

        # === HIGH-SENSITIVITY PARAMS (SNR < 100 dB) ===
        "pa",  # 7.7 dB
        "psupsat",  # 61.8 dB
        "paph",  # 77.7 dB
        "pt",  # 81.4 dB
        "ptsphy",  # 67.7 dB

        # === SAFE PARAMS (SNR >= 100 dB) ===
        "pap",  # 104.8 dB
        "pq",  # 107.4 dB
        # "pclv",  # 126.5 dB
        # "plude",  # 126.9 dB
        # "pfsqitur",  # 128.0 dB
        # "pfcqlng",  # 128.5 dB
        # "pfsqltur",  # 129.2 dB
        # "pfcqnng",  # 130.4 dB
        # "pvfl",  # 130.9 dB
        # "pvfi",  # 131.5 dB
        # "pmfu",  # 131.6 dB
        # "pvfa",  # 131.6 dB
        # "pdynl",  # 131.7 dB
        # "pdyna",  # 131.9 dB
        # "phrsw",  # 131.9 dB
        # "plu",  # 131.9 dB
        # "pvervel",  # 131.9 dB
        # "pdyni",  # 132.0 dB
        # "phrlw",  # 132.0 dB
        # "pfsqlf",  # 134.5 dB
        # "pfsqif",  # 137.0 dB
        # "pfcqsng",  # 139.8 dB
        # "pfcqrng",  # 139.9 dB
        # "pfsqrf",  # 140.5 dB
        # "pfsqsf",  # 140.8 dB
        # "pfplsn",  # 142.6 dB
        # "pfhpsn",  # 144.4 dB
        # "pcovptot",  # 144.8 dB

        # === FAIL PARAMS (crash on perturbation) ===
        # "pccn",  # FAIL
        # "pfhpsl",  # FAIL
        # "pfplsl",  # FAIL
        # "picrit_aer",  # FAIL
        # "plcrit_aer",  # FAIL
        # "plsm",  # FAIL
        # "pmfd",  # FAIL
        # "pnice",  # FAIL
        # "prainfrac_toprfz",  # FAIL
        # "pre_ice",  # FAIL
        # "psnde",  # FAIL

        # === TENDENCIES ===
        "tendency_tmp_a",  # 6.4 dB
        # "tendency_loc_t",  # 118.1 dB
        # "tendency_tmp_q",  # 126.9 dB
        # "tendency_tmp_cld",  # 127.6 dB
        # "tendency_tmp_t",  # 130.1 dB
        # "tendency_loc_a",  # 144.7 dB
        # "tendency_loc_q",  # 144.7 dB
        # "tendency_loc_cld",  # 144.8 dB

        # === PARAM SCALARS (OK, sorted by SNR) ===
        "ydcst_rtt",  # 84.5 dB
        # "ydthf_r2es",  # 110.5 dB
        # "ydthf_r3ies",  # 114.1 dB
        # "ydthf_rtice",  # 116.0 dB
        # "ydthf_r3les",  # 116.6 dB
        # "ydthf_r4les",  # 132.7 dB
        # "ydthf_ralsdcp",  # 133.6 dB
        # "ydcst_rg",  # 134.4 dB
        # "ydthf_ralvdcp",  # 138.6 dB
        # "ydcst_rlstt",  # 145.2 dB
        # "ydthf_rtwat_rtice_r",  # 145.2 dB
        # "ydcst_rd",  # 155.7 dB
        # "yrecldp_rvice",  # 158.4 dB
        # "ydthf_r5ies",  # 160.0 dB
        # "yrecldp_rcldiff",  # 161.4 dB
        # "ydcst_rcpd",  # 161.5 dB
        # "yrecldp_rkooptau",  # 162.4 dB
        # "ydthf_r4ies",  # 168.6 dB
        # "yrecldp_rpecons",  # 169.0 dB
        # "yrecldp_rcovpmin",  # 169.8 dB
        # "ydcst_retv",  # 170.1 dB
        # "ydthf_r5les",  # 173.5 dB
        # "yrecldp_rvrfactor",  # 175.5 dB
        # "yrecldp_rlcritsnow",  # 182.8 dB
        # "yrecldp_rsnowlin1",  # 183.8 dB
        # "yrecldp_rcldiff_convi",  # 185.1 dB
        # "yrecldp_rvsnow",  # 186.3 dB
        # "yrecldp_rsnowlin2",  # 186.9 dB
        # "ydthf_r5alscp",  # 200.5 dB
        # "yrecldp_rdepliqrefdepth",  # 205.2 dB
        # "yrecldp_rcl_kkbauq",  # 205.6 dB
        # "yrecldp_rcl_const8s",  # 210.6 dB
        # "ydthf_r5alvcp",  # 211.0 dB
        # "ydcst_rv",  # 212.0 dB
        # "yrecldp_rcl_kkbaun",  # 213.1 dB
        # "yrecldp_rdepliqrefrate",  # 213.2 dB
        # "yrecldp_rcl_kk_cloud_num_sea",  # 221.3 dB
        # "yrecldp_rcl_kkaau",  # 227.7 dB
        # "yrecldp_rcl_const1s",  # 230.2 dB
        # "yrecldp_rcl_const7s",  # 237.3 dB
        # "yrecldp_rdensref",  # 238.8 dB
        # "yrecldp_riceinit",  # 789.7 dB

        # === PARAM SCALARS (FAIL) ===
        # "ydcst_rlmlt",  # FAIL
        # "ydcst_rlvtt",  # FAIL
        # "ydthf_ralfdcp",  # FAIL
        # "ydthf_rkoop1",  # FAIL
        # "ydthf_rkoop2",  # FAIL
        # "ydthf_rticecu",  # FAIL
        # "ydthf_rtwat",  # FAIL
        # "ydthf_rtwat_rticecu_r",  # FAIL
        # "yrecldp_ramid",  # FAIL
        "yrecldp_ramin",  # FAIL — lowering drops SNR by ~70 dB on tendencies
        # "yrecldp_rccn",  # FAIL
        # "yrecldp_rcl_apb1",  # FAIL
        # "yrecldp_rcl_apb2",  # FAIL
        # "yrecldp_rcl_apb3",  # FAIL
        # "yrecldp_rcl_cdenom1",  # FAIL
        # "yrecldp_rcl_cdenom2",  # FAIL
        # "yrecldp_rcl_cdenom3",  # FAIL
        # "yrecldp_rcl_const1i",  # FAIL
        # "yrecldp_rcl_const1r",  # FAIL
        # "yrecldp_rcl_const2i",  # FAIL
        # "yrecldp_rcl_const2r",  # FAIL
        # "yrecldp_rcl_const2s",  # FAIL
        # "yrecldp_rcl_const3i",  # FAIL
        # "yrecldp_rcl_const3r",  # FAIL
        # "yrecldp_rcl_const3s",  # FAIL
        # "yrecldp_rcl_const4i",  # FAIL
        # "yrecldp_rcl_const4r",  # FAIL
        # "yrecldp_rcl_const4s",  # FAIL
        # "yrecldp_rcl_const5i",  # FAIL
        # "yrecldp_rcl_const5r",  # FAIL
        # "yrecldp_rcl_const5s",  # FAIL
        # "yrecldp_rcl_const6i",  # FAIL
        # "yrecldp_rcl_const6r",  # FAIL
        # "yrecldp_rcl_const6s",  # FAIL
        # "yrecldp_rcl_fac1",  # FAIL
        # "yrecldp_rcl_fac2",  # FAIL
        # "yrecldp_rcl_fzrab",  # FAIL
        # "yrecldp_rcl_ka273",  # FAIL
        # "yrecldp_rcl_kk_cloud_num_land",  # FAIL
        # "yrecldp_rcl_kkaac",  # FAIL
        # "yrecldp_rcl_kkbac",  # FAIL
        # "yrecldp_rclcrit_land",  # FAIL
        # "yrecldp_rclcrit_sea",  # FAIL
        # "yrecldp_rcldtopcf",  # FAIL
        # "yrecldp_rkconv",  # FAIL
        # "yrecldp_rlmin",  # FAIL
        # "yrecldp_rnice",  # FAIL
        # "yrecldp_rprc1",  # FAIL
        # "yrecldp_rprecrhmax",  # FAIL
        # "yrecldp_rtaumel",  # FAIL
        # "yrecldp_rthomo",  # FAIL
        # "yrecldp_rvrain",  # FAIL
    }

    # --- Scan: find everything that CAN be lowered ---

    # All non-transient fp64 arrays (candidates for boundary cast)
    all_non_transient = [
        name
        for name, arr in sdfg.arrays.items()
        if isinstance(arr, dace.data.Array)
        and arr.dtype == dace.float64
        and not arr.transient
        and arr.total_size != 1
    ]

    # All transient fp64 arrays
    all_transient_arrays = [
        name
        for name, arr in sdfg.arrays.items()
        if isinstance(arr, dace.data.Array)
        and arr.dtype == dace.float64
        and arr.transient
        and arr.total_size != 1
    ]

    # All non-transient fp64 scalars (function params: ptsphy, ydcst_*, ydthf_*, yrecldp_*)
    all_param_scalars = [
        name
        for name, arr in sdfg.arrays.items()
        if isinstance(arr, dace.data.Scalar)
        and arr.dtype == dace.float64
        and not arr.transient
    ]

    # All top-level transient fp64 scalars
    all_top_scalars = [
        name
        for name, arr in sdfg.arrays.items()
        if isinstance(arr, dace.data.Scalar)
        and arr.dtype == dace.float64
        and arr.transient
    ]

    # All nested transient fp64 scalars
    all_nested_scalars = []
    for nsdfg in sdfg.all_sdfgs_recursive():
        if nsdfg is sdfg:
            continue
        for name, arr in nsdfg.arrays.items():
            if (isinstance(arr, dace.data.Scalar)
                and arr.dtype == dace.float64
                and arr.transient):
                all_nested_scalars.append((nsdfg, name, arr))

    def is_excluded(name):
        return (name in _LOWERING_EXCLUDE
                or any(name.startswith(ex + "_") for ex in _LOWERING_EXCLUDE))

    # --- Apply: lower everything not excluded ---

    # 1. Non-transient arrays + scalar params: boundary cast
    # Only boundary cast things that ARE NOT already being offloaded to GPU.
    # Offloaded things (with gpu_ siblings) are handled by gpu_offload.py logic.
    bc_targets = [n for n in all_non_transient if not is_excluded(n) and f"gpu_{n}" not in sdfg.arrays]
    ps_targets = [n for n in all_param_scalars if not is_excluded(n) and f"gpu_{n}" not in sdfg.arrays]
    if bc_targets or ps_targets:
        print(f"Boundary cast: {len(bc_targets)} non-transient arrays, {len(ps_targets)} scalar params")
        inject_cpu_boundary_cast(sdfg, bc_targets, external_dtype, scalar_names=ps_targets)

    # 2. Transient arrays: lower directly
    ta_targets = [n for n in all_transient_arrays if not is_excluded(n)]
    if ta_targets:
        print(f"Lowering {len(ta_targets)} transient arrays to {external_dtype}")
        for name in ta_targets:
            _propagate_dtype(sdfg, name, external_dtype)

    # Build set of all lowered array names (for scalar matching)
    _lowered_arrays = set(bc_targets) | set(ta_targets)

    def is_derived_from_lowered(name):
        """A scalar should only be lowered if its parent array was lowered."""
        for arr_name in _lowered_arrays:
            if name == arr_name or name.startswith(arr_name + "_"):
                return True
        return False

    # 3. Top-level transient scalars
    # WARN: using is_derived_from_lowered, not is_excluded — standalone scalars
    # (ztw1, zqadj, etc.) stay double. Lowering them causes severe errors.
    ts_count = 0
    for name in all_top_scalars:
        if is_derived_from_lowered(name):
            sdfg.arrays[name].dtype = external_dtype
            ts_count += 1
    if ts_count:
        print(f"Lowered {ts_count} top-level transient scalars to {external_dtype}")

    # 4. Nested transient scalars
    # Find comparison outputs first (should become int32, not float)
    comparison_scalars: set[str] = set()
    for nsdfg in sdfg.all_sdfgs_recursive():
        if nsdfg is sdfg:
            continue
        for state in nsdfg.states():
            for node in state.nodes():
                if not isinstance(node, nodes.Tasklet):
                    continue
                code = node.code.as_string
                if any(op in code for op in (" > ", " < ", " >= ", " <= ", " == ", " != ")):
                    for e in state.out_edges(node):
                        if (isinstance(e.dst, nodes.AccessNode)
                            and e.dst.data in nsdfg.arrays
                            and isinstance(nsdfg.arrays[e.dst.data], dace.data.Scalar)):
                            comparison_scalars.add(e.dst.data)

    # WARN: same as above — standalone nested scalars stay double.
    ns_count = 0
    for nsdfg, name, arr in all_nested_scalars:
        if is_derived_from_lowered(name):
            if name in comparison_scalars:
                arr.dtype = dace.int32
            else:
                arr.dtype = external_dtype
            ns_count += 1
    if ns_count:
        print(f"Lowered {ns_count} nested transient scalars to {external_dtype}")

    # 5. Floatify tasklet literals
    floatify_tasklets(sdfg)

    sdfg.validate()
