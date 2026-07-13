"""bfp_compression.py — SDFG-level BFP packing for read-only GPU arrays.

Transforms selected arrays from their original dtype/shape to
uint8[packed_size] using Block Floating Point encoding. Inserts a
CPU-side packing Map between the flattened source and the GPU copy.

Called from compile_gpu_stage8.py alongside inject_pointwise_decompression.
"""

from __future__ import annotations

from functools import reduce
from operator import mul
from typing import Iterable, Union

import dace
from dace import dtypes, nodes, subsets, symbolic

BFP_BLOCK_SIZE = 32
BFP_MANTISSA_BITS = 16  # default; overridden by inject_bfp_packing(mantissa_bits=)


def _bfp_block_bytes(mantissa_bits):
    return 1 + BFP_BLOCK_SIZE * (mantissa_bits // 8)


def _packed_size_expr(total_elems, mantissa_bits=16):
    """Symbolic packed buffer size: ceil(N/block_size) * block_bytes."""
    block_bytes = _bfp_block_bytes(mantissa_bits)
    return ((total_elems + BFP_BLOCK_SIZE - 1) // BFP_BLOCK_SIZE) * block_bytes


def _total_elems(arr):
    """Product of all shape dimensions (symbolic)."""
    return reduce(mul, arr.shape, 1)


def _update_memlets_and_insert_decode(
    sdfg: dace.SDFG,
    gpu_name: str,
    packed_size,
    orig_strides,
    orig_dtype,
    mantissa_bits: int = 16,
    decode_dtype=None,
):
    """Update memlets for a BFP-packed array, inserting decode shims at scalar reads.

    After changing an array from 3D to 1D uint8, existing memlets still have
    3D subsets. This function:
    - Scalar memlets into Tasklets: insert a BFP decode shim tasklet
    - Range/other memlets: flatten to 1D [0:packed_size]

    `decode_dtype` (optional) overrides the cast type at the decode shim's
    output. Defaults to `orig_dtype` (preserves the original scalar type).
    Pass `external_dtype` (e.g. fp32) to keep consumer locals in the lowered
    precision and avoid silent fp64 promotion.
    """
    if decode_dtype is None:
        decode_dtype = orig_dtype
    flatten_count = 0
    decode_count = 0
    for state in sdfg.all_states():
        for edge in list(state.edges()):  # copy — we add edges
            if edge.data is None or edge.data.data != gpu_name:
                continue
            subset = edge.data.subset
            if subset is None or len(subset) <= 1:
                continue  # already 1D

            is_scalar = all(r[0] == r[1] for r in subset)

            if is_scalar and isinstance(edge.dst, nodes.Tasklet):
                # Scalar read into Tasklet — insert decode shim
                indices = [r[0] for r in subset]
                flat_idx = sum(
                    idx * stride for idx, stride in zip(indices, orig_strides)
                )
                _insert_bfp_decode_shim(
                    state,
                    edge,
                    gpu_name,
                    packed_size,
                    flat_idx,
                    decode_dtype,
                    mantissa_bits=mantissa_bits,
                )
                decode_count += 1
            else:
                # Range or non-Tasklet — just flatten to 1D
                edge.data.subset = subsets.Range([(0, packed_size - 1, 1)])
                flatten_count += 1
    if flatten_count > 0:
        print(f"    Flattened {flatten_count} range memlets for {gpu_name} to 1D")
    if decode_count > 0:
        print(f"    Inserted {decode_count} BFP decode shims for {gpu_name}")


def _insert_bfp_decode_shim(
    state,
    edge,
    gpu_name,
    packed_size,
    flat_idx_expr,
    orig_dtype,
    mantissa_bits: int = 16,
):
    """Insert a BFP decode shim tasklet between a packed array and a consuming Tasklet.

    Before:
      MapEntry ──[arr[i0, i1, i2] (scalar)]──> Tasklet(_in)

    After:
      MapEntry ──[arr[0:packed_size] (full)]──> BFP_Decode_Tasklet(_packed)
      BFP_Decode_Tasklet(_val) ──> tmp_scalar_AN ──> Tasklet(_in)
    """
    dst_node = edge.dst  # original Tasklet
    dst_conn = edge.dst_conn  # e.g. "_in" or "IN_1"
    src_node = edge.src  # MapEntry
    src_conn = edge.src_conn  # e.g. "OUT_gpu___CG_..."

    # 1. Create scalar transient for decoded value
    tmp_name, _ = state.sdfg.add_scalar(
        name=f"t_{dst_conn}_bfp_decoded",
        dtype=orig_dtype,
        transient=True,
        find_new_name=True,
    )
    tmp_an = state.add_access(tmp_name)

    # 2. Create BFP decode tasklet
    flat_idx_c = symbolic.symstr(flat_idx_expr)
    decode_tasklet = state.add_tasklet(
        name=f"bfp_decode_{dst_conn}",
        inputs={"_packed"},
        outputs={"_val"},
        code=f"_val = ({orig_dtype.ctype})bfp_decode<{BFP_BLOCK_SIZE}, {mantissa_bits}>(_packed, (int)({flat_idx_c}));",
        language=dtypes.Language.CPP,
    )

    # 3. Rewire
    state.remove_edge(edge)

    # src → decode_tasklet (full packed array)
    state.add_edge(
        src_node,
        src_conn,
        decode_tasklet,
        "_packed",
        dace.Memlet(data=gpu_name, subset=f"0:{packed_size - 1}"),
    )

    # decode_tasklet → tmp scalar
    state.add_edge(
        decode_tasklet,
        "_val",
        tmp_an,
        None,
        dace.Memlet(f"{tmp_name}[0]"),
    )

    # tmp scalar → original dest tasklet
    state.add_edge(
        tmp_an,
        None,
        dst_node,
        dst_conn,
        dace.Memlet(f"{tmp_name}[0]"),
    )


# ---------------------------------------------------------------------------
# BFP pack tasklet — CPP code for one block of 32 elements
# ---------------------------------------------------------------------------


def _bfp_pack_warp_code(mantissa_bits: int = 16) -> str:
    """GPU warp-cooperative BFP encode: one warp = one BFP block, 32 lanes
    cooperate via __shfl_xor_sync. The 2-D Map iter (__bfp_block, __lane)
    must be set up with gpu_block_size=(32,1,1) so:
      - outer iter __bfp_block ↔ blockIdx.x
      - inner iter __lane      ↔ threadIdx.x (0..31)

    Bit format identical to _bfp_pack_block_code (the CPU encoder). Reads
    from full source array _src and writes to full packed buffer _dst.
    Both __bfp_N (total source elements) and __bfp_BB (block_bytes) are
    placeholder tokens substituted by the caller.
    """
    bias = mantissa_bits - 1
    mant_min = -(1 << (mantissa_bits - 1))
    mant_max = (1 << (mantissa_bits - 1)) - 1
    mbytes = mantissa_bits // 8

    # _dst is the per-block pointer (memlet subset = [block*BB : (block+1)*BB]),
    # so we use offsets relative to the block start (just like the CPU encoder).
    if mantissa_bits == 16:
        store_mant = (
            f"    int16_t mant = (int16_t)q;\n"
            f"    memcpy(_dst + 1 + (int64_t)__lane * 2, &mant, sizeof(int16_t));"
        )
    else:
        store_mant = (
            f"    _dst[1 + (int64_t)__lane] = (uint8_t)(int8_t)q;"
        )

    # CUB WarpReduce: leaves the result only in lane 0, so broadcast back
    # to all lanes with a single __shfl_sync. 8 warps per CUDA block → 8
    # TempStorage slots indexed by threadIdx.y.
    return f"""
// Warp-cooperative BFP encode ({mantissa_bits}-bit mantissa) using CUB.
// One warp per BFP block; 32 lanes cooperate.  Each lane handles its own
// element (or contributes 0 if past the end of the array).  _dst is the
// per-block pointer (memlet pre-offsets by __bfp_block * BLOCK_BYTES).
int64_t elem_idx = (int64_t)__bfp_block * 32 + (int64_t)__lane;
bool active = elem_idx < __bfp_N;

double v = active ? _src[elem_idx] : 0.0;
float vabs = fabsf((float)v);

// CUB warp reduce-max.  Result only in lane 0, then broadcast to all lanes.
typedef cub::WarpReduce<float, 32> WarpReduceF;
__shared__ typename WarpReduceF::TempStorage __bfp_temp[8];
float max_abs = WarpReduceF(__bfp_temp[threadIdx.y]).Reduce(vabs, cub::Max());
max_abs = __shfl_sync(0xffffffff, max_abs, 0);

int shared_exp;
if (max_abs == 0.0f) {{
    shared_exp = 0;
}} else {{
    int fexp;
    frexpf(max_abs, &fexp);
    shared_exp = fexp + {bias};
}}
if (shared_exp < -128) shared_exp = -128;
if (shared_exp >  127) shared_exp =  127;

// Lane 0 writes the shared exponent byte at offset 0 of this block.
if (__lane == 0) {{
    _dst[0] = (uint8_t)(int8_t)shared_exp;
}}

// Per-lane quantization.  Padding lanes hold v=0, quantize to 0, write 0 —
// matches the CPU encoder's explicit zero-padding loop.
float scale = ldexpf(1.0f, -(shared_exp - {bias}));
float scaled = (float)v * scale;
int32_t q = (int32_t)roundf(scaled);
if (q < {mant_min}) q = {mant_min};
if (q >  {mant_max}) q =  {mant_max};

{store_mant}
"""


def _bfp_pack_block_code(mantissa_bits: int = 16) -> str:
    """Generate C code for packing one BFP block."""
    bias = mantissa_bits - 1
    mant_min = -(1 << (mantissa_bits - 1))
    mant_max = (1 << (mantissa_bits - 1)) - 1
    mbytes = mantissa_bits // 8

    if mantissa_bits == 16:
        store = (
            f"    int16_t mant = (int16_t)q;\n"
            f"    memcpy(_dst + 1 + (int)(i - start) * 2, &mant, sizeof(int16_t));"
        )
        pad = (
            f"    int16_t zero = 0;\n"
            f"    memcpy(_dst + 1 + (int)(i - start) * 2, &zero, sizeof(int16_t));"
        )
    else:
        store = f"    _dst[1 + (int)(i - start)] = (uint8_t)(int8_t)q;"
        pad = f"    _dst[1 + (int)(i - start)] = 0;"

    return f"""
// Pack one BFP block ({mantissa_bits}-bit mantissa).
int64_t start = (int64_t)__bfp_block * 32;
int64_t end   = start + 32;
if (end > __bfp_N) end = __bfp_N;

float max_abs = 0.0f;
for (int64_t i = start; i < end; i++) {{
    float v = fabsf((float)_src[i]);
    if (v > max_abs) max_abs = v;
}}

int shared_exp;
if (max_abs == 0.0f) {{
    shared_exp = 0;
}} else {{
    int fexp;
    frexpf(max_abs, &fexp);
    shared_exp = fexp + {bias};
}}
if (shared_exp < -128) shared_exp = -128;
if (shared_exp >  127) shared_exp =  127;

_dst[0] = (uint8_t)(int8_t)shared_exp;

float scale = ldexpf(1.0f, -(shared_exp - {bias}));
for (int64_t i = start; i < end; i++) {{
    float scaled = (float)_src[i] * scale;
    int32_t q = (int32_t)roundf(scaled);
    if (q < {mant_min}) q = {mant_min};
    if (q >  {mant_max}) q =  {mant_max};
{store}
}}

for (int64_t i = end; i < start + 32; i++) {{
{pad}
}}
"""


def inject_bfp_packing(
    sdfg: dace.SDFG,
    array_names: Union[str, Iterable[str]],
    mantissa_bits: int = 16,
    decode_dtype=None,
) -> dace.SDFG:
    """Transform selected arrays to BFP uint8 packed format on GPU.

    For each array:
    1. Changes gpu_ array to uint8[packed_size]
    2. Adds CPU-side packed transient
    3. Inserts BFP pack Map (CPU float → CPU uint8)
    4. Replaces H2D AN→AN edge with packed version
    5. Removes D2H edge (read-only data)

    mantissa_bits: 16 for bfp16/bfp32, 8 for bfp8.
    decode_dtype:  optional cast type for the decode shim's output (defaults
                   to each array's original dtype). Pass the lowered dtype
                   (e.g. fp32) so consumer locals don't auto-promote to fp64.
    """
    if isinstance(array_names, str):
        array_names = [array_names]

    for name in array_names:
        cpu_name = name
        gpu_name = f"gpu_{name}"

        if cpu_name not in sdfg.arrays:
            print(f"  BFP: {cpu_name} not found in SDFG {sdfg.name}, skipping.")
            continue
        if gpu_name not in sdfg.arrays:
            print(f"  BFP: {gpu_name} not found in SDFG {sdfg.name}, skipping.")
            continue

        _inject_bfp_for_array(
            sdfg, cpu_name, gpu_name, mantissa_bits=mantissa_bits,
            decode_dtype=decode_dtype,
        )

    # Skip validation — nested SDFGs still have the original double[3D]
    # descriptors. The type/shape mismatch is fixed at text level by
    # patch_bfp_reads() in compile_if_propagated_sdfgs.py after codegen.
    return sdfg


def _inject_bfp_for_array(
    sdfg: dace.SDFG, cpu_name: str, gpu_name: str, mantissa_bits: int = 16,
    decode_dtype=None,
):
    """Apply BFP packing for a single array pair."""
    cpu_arr = sdfg.arrays[cpu_name]
    gpu_arr = sdfg.arrays[gpu_name]

    orig_dtype = cpu_arr.dtype
    orig_shape = tuple(cpu_arr.shape)
    orig_strides = tuple(cpu_arr.strides)
    total_elems = _total_elems(cpu_arr)
    packed_size = _packed_size_expr(total_elems, mantissa_bits)
    num_blocks = (total_elems + BFP_BLOCK_SIZE - 1) // BFP_BLOCK_SIZE
    block_bytes = _bfp_block_bytes(mantissa_bits)

    print(
        f"  BFP{mantissa_bits}: packing {gpu_name} from {orig_dtype}[{orig_shape}] "
        f"to uint8[{packed_size}] ({block_bytes} bytes/block)"
    )

    # Step 1: Add CPU-side packed transient
    packed_cpu_name = f"{cpu_name}_bfp"
    sdfg.add_array(
        packed_cpu_name,
        shape=(packed_size,),
        dtype=dace.uint8,
        strides=(1,),
        transient=True,
        storage=dtypes.StorageType.CPU_Heap,
    )

    # Step 2a: Propagate the consumer-facing dtype (decode_dtype, e.g. fp32)
    # through nested-SDFG inner descriptors. This makes DaCe emit local vars
    # of the lowered type ("float c = ..." instead of "double c = ...") in
    # the consumer kernels. Done BEFORE the top-level uint8 retype so it
    # doesn't propagate uint8 (which would be wrong for consumer locals).
    # The text patcher then replaces the parameter type (float* → uint8_t*)
    # to match the actual allocation.
    if decode_dtype is not None and decode_dtype != orig_dtype:
        from utils.boundary_cast import _propagate_dtype
        _propagate_dtype(sdfg, gpu_name, decode_dtype)

    # Step 2b: Change GPU array to uint8[packed_size] in top-level SDFG
    gpu_arr.dtype = dace.uint8
    gpu_arr.shape = (packed_size,)
    gpu_arr.strides = (1,)
    gpu_arr.offset = [0]
    gpu_arr.total_size = packed_size

    # Step 2b: Update existing memlets referencing the GPU array.
    # Scalar reads into Tasklets get a BFP decode shim; range memlets flatten to 1D.
    # NOTE: nested SDFG array descriptors are NOT changed here because
    # interstate edges may reference the array with 3D subscripts (AST-level),
    # and changing the descriptor shape would cause validation errors.
    # Instead, the type mismatch (double* param receiving uint8* data) is
    # fixed at text level in compile_if_propagated_sdfgs.py.
    _update_memlets_and_insert_decode(
        sdfg,
        gpu_name,
        packed_size,
        orig_strides,
        orig_dtype,
        mantissa_bits=mantissa_bits,
        decode_dtype=decode_dtype,
    )

    # Step 3: Find and replace H2D edge, remove D2H edge
    for state in sdfg.states():
        edges_to_remove = []
        for edge in state.edges():
            src = edge.src
            dst = edge.dst
            if not (
                isinstance(src, nodes.AccessNode) and isinstance(dst, nodes.AccessNode)
            ):
                continue

            # H2D: __CG_... -> gpu___CG_...
            if src.data == cpu_name and dst.data == gpu_name:
                _replace_h2d_with_bfp_pack(
                    sdfg,
                    state,
                    edge,
                    cpu_name,
                    gpu_name,
                    packed_cpu_name,
                    orig_dtype,
                    orig_shape,
                    orig_strides,
                    total_elems,
                    packed_size,
                    num_blocks,
                    block_bytes,
                    mantissa_bits,
                )
                edges_to_remove.append(edge)

            # D2H: gpu___CG_... -> __CG_... (remove for read-only BFP)
            if src.data == gpu_name and dst.data == cpu_name:
                print(
                    f"    Removing D2H edge: {gpu_name} -> {cpu_name} "
                    f"(read-only BFP array)"
                )
                edges_to_remove.append(edge)

        for edge in edges_to_remove:
            state.remove_edge(edge)

        # Clean up access nodes that became isolated after edge removal
        for edge in edges_to_remove:
            for node in [edge.src, edge.dst]:
                if isinstance(node, nodes.AccessNode):
                    if node in state.nodes() and state.degree(node) == 0:
                        state.remove_node(node)


def _replace_h2d_with_bfp_pack(
    sdfg,
    state,
    old_edge,
    cpu_name,
    gpu_name,
    packed_cpu_name,
    orig_dtype,
    orig_shape,
    orig_strides,
    total_elems,
    packed_size,
    num_blocks,
    block_bytes,
    mantissa_bits,
):
    """Replace AN(__CG_...) -> AN(gpu___CG_...) with pack Map + new H2D."""
    print(f"    Replacing H2D edge with BFP pack Map")

    cpu_an = old_edge.src  # AccessNode for __CG_... (float, 3D)
    # gpu_an = old_edge.dst  # AccessNode for gpu___CG_... (now uint8, 1D)

    # Create AccessNode for the packed CPU array
    packed_cpu_an = state.add_access(packed_cpu_name)

    # Create AccessNode for GPU (reuse existing or find it)
    gpu_an = old_edge.dst

    # ---- Build the BFP Pack Map ----
    # Map iterates over blocks: __bfp_block = 0 .. num_blocks-1
    map_entry, map_exit = state.add_map(
        f"bfp_pack_{cpu_name}",
        {"__bfp_block": f"0:{num_blocks}"},
        schedule=dtypes.ScheduleType.CPU_Multicore,
    )

    # Tasklet: pack one block
    # The total element count is inlined as a C expression using existing SDFG symbols.
    n_expr_c = symbolic.symstr(total_elems)
    tasklet = state.add_tasklet(
        name=f"bfp_pack_block_{cpu_name}",
        inputs={"_src"},
        outputs={"_dst"},
        code=_bfp_pack_block_code(mantissa_bits).replace(
            "__bfp_N", f"((int64_t)({n_expr_c}))"
        ),
        language=dtypes.Language.CPP,
    )

    # Connectors on map entry/exit
    src_conn_in = f"IN_{cpu_name}"
    src_conn_out = f"OUT_{cpu_name}"
    dst_conn_in = f"IN_{packed_cpu_name}"
    dst_conn_out = f"OUT_{packed_cpu_name}"

    map_entry.add_in_connector(src_conn_in)
    map_entry.add_out_connector(src_conn_out)
    map_exit.add_in_connector(dst_conn_in)
    map_exit.add_out_connector(dst_conn_out)

    # Wire: cpu_an -> map_entry (full source array)
    state.add_edge(
        cpu_an,
        None,
        map_entry,
        src_conn_in,
        dace.Memlet.from_array(cpu_name, sdfg.arrays[cpu_name]),
    )

    # Wire: map_entry -> tasklet (full source array — tasklet indexes internally)
    state.add_edge(
        map_entry,
        src_conn_out,
        tasklet,
        "_src",
        dace.Memlet.from_array(cpu_name, sdfg.arrays[cpu_name]),
    )

    # Wire: tasklet -> map_exit (block_bytes per block)
    block_sym = symbolic.symbol("__bfp_block")
    state.add_edge(
        tasklet,
        "_dst",
        map_exit,
        dst_conn_in,
        dace.Memlet(
            data=packed_cpu_name,
            subset=f"{block_sym * block_bytes}:{block_sym * block_bytes + block_bytes}",
        ),
    )

    # Wire: map_exit -> packed_cpu_an (full packed array)
    state.add_edge(
        map_exit,
        dst_conn_out,
        packed_cpu_an,
        None,
        dace.Memlet.from_array(packed_cpu_name, sdfg.arrays[packed_cpu_name]),
    )

    # Wire: packed_cpu_an -> gpu_an (H2D memcpy — both uint8, 1D)
    state.add_edge(
        packed_cpu_an,
        None,
        gpu_an,
        None,
        dace.Memlet.from_array(packed_cpu_name, sdfg.arrays[packed_cpu_name]),
    )

    print(f"    Added BFP pack Map ({num_blocks} blocks) + H2D edge")


# ---------------------------------------------------------------------------
# GPU-side BFP encode for transients with covering full-array writes.
# Phase 1: per-thread one-block encode (simple; warp-cooperative is a Phase 2
# optimization). Bit-format identical to CPU pack — existing decode shims
# work unchanged.
# ---------------------------------------------------------------------------


def _find_covering_write_states(sdfg: dace.SDFG, gpu_name: str):
    """Return list of (containing_sdfg, state, access_node) tuples for every
    AccessNode that has a full-array covering in-edge for gpu_name.

    Restricted to the top-level SDFG for Phase 1 — nested-SDFG producers
    require descriptor pass-through plumbing not handled here.
    """
    if gpu_name not in sdfg.arrays:
        return []
    arr = sdfg.arrays[gpu_name]
    full_subset = ", ".join(f"0:{s}" for s in arr.shape)
    out = []
    for state in sdfg.states():
        for node in state.nodes():
            if not isinstance(node, nodes.AccessNode) or node.data != gpu_name:
                continue
            in_e = state.in_edges(node)
            if not in_e:
                continue
            for e in in_e:
                if e.data is None or e.data.subset is None:
                    continue
                if str(e.data.subset) == full_subset:
                    out.append((sdfg, state, node))
                    break
    return out


def _add_gpu_bfp_encode_state(
    sdfg: dace.SDFG,
    after_state,
    gpu_name: str,
    packed_name: str,
    orig_dtype,
    total_elems,
    packed_size,
    num_blocks,
    block_bytes,
    mantissa_bits: int,
):
    """Insert a new state right after `after_state` containing a GPU encode
    Map: reads `gpu_name` (full-array), writes `packed_name` (one block per
    Map iteration). Bit-format identical to the CPU pack tasklet."""
    # Pick a unique label.
    base = f"bfp_encode_{gpu_name}"
    existing = {s.label for s in sdfg.states()}
    label = base
    suffix = 0
    while label in existing:
        suffix += 1
        label = f"{base}_{suffix}"
    encode_state = sdfg.add_state_after(after_state, label=label)

    src_an = encode_state.add_access(gpu_name)
    dst_an = encode_state.add_access(packed_name)

    n_blocks_str = symbolic.symstr(num_blocks)
    map_entry, map_exit = encode_state.add_map(
        f"bfp_encode_map_{gpu_name}",
        {"__bfp_block": f"0:{n_blocks_str}"},
        schedule=dtypes.ScheduleType.GPU_Device,
    )

    n_expr_c = symbolic.symstr(total_elems)
    tasklet = encode_state.add_tasklet(
        name=f"bfp_encode_tasklet_{gpu_name}",
        inputs={"_src"},
        outputs={"_dst"},
        code=_bfp_pack_block_code(mantissa_bits).replace(
            "__bfp_N", f"((int64_t)({n_expr_c}))"
        ),
        language=dtypes.Language.CPP,
    )

    src_in = f"IN_{gpu_name}"
    src_out = f"OUT_{gpu_name}"
    dst_in = f"IN_{packed_name}"
    dst_out = f"OUT_{packed_name}"
    map_entry.add_in_connector(src_in)
    map_entry.add_out_connector(src_out)
    map_exit.add_in_connector(dst_in)
    map_exit.add_out_connector(dst_out)

    encode_state.add_edge(
        src_an, None, map_entry, src_in,
        dace.Memlet.from_array(gpu_name, sdfg.arrays[gpu_name]),
    )
    encode_state.add_edge(
        map_entry, src_out, tasklet, "_src",
        dace.Memlet.from_array(gpu_name, sdfg.arrays[gpu_name]),
    )
    block_sym = symbolic.symbol("__bfp_block")
    encode_state.add_edge(
        tasklet, "_dst", map_exit, dst_in,
        dace.Memlet(
            data=packed_name,
            subset=f"{block_sym * block_bytes}:{block_sym * block_bytes + block_bytes}",
        ),
    )
    encode_state.add_edge(
        map_exit, dst_out, dst_an, None,
        dace.Memlet.from_array(packed_name, sdfg.arrays[packed_name]),
    )
    return encode_state


def _repoint_consumer_reads_to_packed(
    sdfg: dace.SDFG,
    gpu_name: str,
    packed_name: str,
    skip_states: set,
    packed_size,
    orig_strides,
    orig_dtype,
    mantissa_bits: int,
    decode_dtype=None,
):
    """For every memlet in any state outside `skip_states` whose data is
    `gpu_name`, repoint to `packed_name`:
    - Scalar reads into Tasklets: replace with BFP decode shim.
    - Range memlets: rewrite data field + flatten subset to 1D [0:packed_size].
    Producer in-edges (writes to gpu_name) are left untouched.
    """
    flatten_count = 0
    decode_count = 0
    rename_count = 0
    for state in sdfg.all_states():
        if state in skip_states:
            continue
        # Step 1: rename consumer-side AccessNodes (no in-edges → pure read,
        # AND no AN→AN out-edges — those feed D2H/transient copies that need
        # the fp64 form).
        for node in list(state.nodes()):
            if not isinstance(node, nodes.AccessNode) or node.data != gpu_name:
                continue
            if state.in_edges(node):
                # Has writes → this is a producer or RMW; not handled here.
                continue
            if any(isinstance(e.dst, nodes.AccessNode) for e in state.out_edges(node)):
                # Feeds a D2H or transient copy — must stay fp64.
                continue
            node.data = packed_name
            rename_count += 1
        # Step 2: repoint memlets / install decode shims.
        for edge in list(state.edges()):
            if edge.data is None or edge.data.data != gpu_name:
                continue
            # Producer-side write (memlet flows INTO an AN named gpu_name).
            if isinstance(edge.dst, nodes.AccessNode) and edge.dst.data == gpu_name:
                continue
            # AN-to-AN copy (D2H or transient-to-transient): the destination
            # needs the fp64 form, leave the source on gpu_name.
            if isinstance(edge.dst, nodes.AccessNode) and edge.dst.data != gpu_name:
                continue
            subset = edge.data.subset
            if subset is None:
                continue
            is_scalar = len(subset) > 0 and all(r[0] == r[1] for r in subset)
            if is_scalar and isinstance(edge.dst, nodes.Tasklet):
                indices = [r[0] for r in subset]
                flat_idx = sum(idx * stride for idx, stride in zip(indices, orig_strides))
                _insert_bfp_decode_shim(
                    state, edge, packed_name, packed_size, flat_idx,
                    decode_dtype if decode_dtype is not None else orig_dtype,
                    mantissa_bits=mantissa_bits,
                )
                decode_count += 1
            else:
                edge.data.data = packed_name
                edge.data.subset = subsets.Range([(0, packed_size - 1, 1)])
                flatten_count += 1
    if rename_count:
        print(f"    Renamed {rename_count} consumer AccessNode(s) to {packed_name}")
    if flatten_count:
        print(f"    Repointed {flatten_count} range memlets to {packed_name}")
    if decode_count:
        print(f"    Inserted {decode_count} decode shims reading {packed_name}")


def _add_gpu_bfp_encode_inplace(
    sdfg: dace.SDFG,
    state,
    src_an,
    gpu_name: str,
    packed_name: str,
    total_elems,
    packed_size,
    num_blocks,
    block_bytes,
    mantissa_bits: int,
):
    """Insert encode subgraph into the SAME state, reading from `src_an`
    (an existing AccessNode for gpu_name) and writing to a new AccessNode
    for `packed_name`. Returns the packed AccessNode.
    """
    dst_an = state.add_access(packed_name)
    n_blocks_str = symbolic.symstr(num_blocks)
    # Warp-cooperative encode: 32 threads share one BFP block (coalesced reads
    # + warp shuffle reduce). 8 warps per CUDA block = 256 threads = good SM
    # occupancy.
    # Iter mapping (DaCe assigns last iter → X):
    #   __lane (last) → X, blockDim.x=32 → threadIdx.x = lane, gridDim.x=1
    #   __bfp_block (first) → Y, blockDim.y=8 → threadIdx.y = warp_in_block,
    #                            gridDim.y = ceil(num_blocks/8)
    # Each warp = 32 consecutive linear threads (y=fixed, x=0..31), so all
    # 32 lanes of a warp share the same __bfp_block → warp shuffles work.
    map_entry, map_exit = state.add_map(
        f"bfp_encode_map_{gpu_name}",
        {"__bfp_block": f"0:{n_blocks_str}", "__lane": "0:32"},
        schedule=dtypes.ScheduleType.GPU_Device,
    )
    map_entry.map.gpu_block_size = (32, 8, 1)
    n_expr_c = symbolic.symstr(total_elems)
    tasklet_code = _bfp_pack_warp_code(mantissa_bits).replace(
        "__bfp_N", f"((int64_t)({n_expr_c}))"
    )
    tasklet = state.add_tasklet(
        name=f"bfp_encode_tasklet_{gpu_name}",
        inputs={"_src"},
        outputs={"_dst"},
        code=tasklet_code,
        language=dtypes.Language.CPP,
        code_global="#include <cub/cub.cuh>",
    )
    src_in = f"IN_{gpu_name}"
    src_out = f"OUT_{gpu_name}"
    dst_in = f"IN_{packed_name}"
    dst_out = f"OUT_{packed_name}"
    map_entry.add_in_connector(src_in)
    map_entry.add_out_connector(src_out)
    map_exit.add_in_connector(dst_in)
    map_exit.add_out_connector(dst_out)
    state.add_edge(
        src_an, None, map_entry, src_in,
        dace.Memlet.from_array(gpu_name, sdfg.arrays[gpu_name]),
    )
    state.add_edge(
        map_entry, src_out, tasklet, "_src",
        dace.Memlet.from_array(gpu_name, sdfg.arrays[gpu_name]),
    )
    # Tasklet → map_exit memlet: each thread writes the per-block range of
    # `block_bytes` bytes (1 exponent + 32 mantissas).
    block_sym = symbolic.symbol("__bfp_block")
    state.add_edge(
        tasklet, "_dst", map_exit, dst_in,
        dace.Memlet(
            data=packed_name,
            subset=f"{block_sym * block_bytes}:{block_sym * block_bytes + block_bytes}",
        ),
    )
    state.add_edge(
        map_exit, dst_out, dst_an, None,
        dace.Memlet.from_array(packed_name, sdfg.arrays[packed_name]),
    )
    return dst_an


def _retarget_sdfg_recursive(
    sdfg: dace.SDFG,
    gpu_name: str,
    packed_name: str,
    packed_size,
    orig_strides,
    orig_dtype,
    mantissa_bits: int,
    decode_dtype=None,
):
    """Apply gpu_name → packed_name rename + uint8 retype + memlet flatten +
    decode shims throughout this SDFG and all nested SDFGs recursively.
    Each NSDFG node carrying gpu_name as a connector also has its connector
    renamed.
    """
    if gpu_name in sdfg.arrays:
        sdfg.replace(gpu_name, packed_name)
        desc = sdfg.arrays[packed_name]
        desc.dtype = dace.uint8
        desc.shape = (packed_size,)
        desc.strides = (1,)
        desc.offset = [0]
        desc.total_size = packed_size
        desc.debuginfo = dace.dtypes.DebugInfo(
            start_line=0, end_line=0, filename="BFP_PACKED"
        )
        _update_memlets_and_insert_decode(
            sdfg, packed_name, packed_size, orig_strides, orig_dtype,
            mantissa_bits=mantissa_bits, decode_dtype=decode_dtype,
        )

    for state in sdfg.states():
        for n in state.nodes():
            if not isinstance(n, nodes.NestedSDFG):
                continue
            # If this NSDFG node has gpu_name as a connector, rename it.
            if gpu_name in n.in_connectors:
                n.remove_in_connector(gpu_name)
                n.add_in_connector(packed_name)
                # Also update edges that targeted dst_conn=gpu_name.
                for edge in list(state.in_edges(n)):
                    if edge.dst_conn == gpu_name:
                        edge.dst_conn = packed_name
            if gpu_name in n.out_connectors:
                n.remove_out_connector(gpu_name)
                n.add_out_connector(packed_name)
                for edge in list(state.out_edges(n)):
                    if edge.src_conn == gpu_name:
                        edge.src_conn = packed_name
            # Recurse into the inner SDFG.
            if packed_name in n.sdfg.arrays or gpu_name in n.sdfg.arrays:
                _retarget_sdfg_recursive(
                    n.sdfg, gpu_name, packed_name, packed_size,
                    orig_strides, orig_dtype, mantissa_bits,
                )
        # Also rename MapEntry/MapExit connectors (IN_X / OUT_X) where X is
        # the array name. DaCe sometimes uses the array name as the connector
        # suffix; the symbolic IN_7 cases don't need this.
        for n in state.nodes():
            if isinstance(n, (nodes.MapEntry, nodes.MapExit)):
                in_old = f"IN_{gpu_name}"
                out_old = f"OUT_{gpu_name}"
                in_new = f"IN_{packed_name}"
                out_new = f"OUT_{packed_name}"
                if in_old in n.in_connectors:
                    n.remove_in_connector(in_old)
                    n.add_in_connector(in_new)
                    for e in list(state.in_edges(n)):
                        if e.dst_conn == in_old:
                            e.dst_conn = in_new
                if out_old in n.out_connectors:
                    n.remove_out_connector(out_old)
                    n.add_out_connector(out_new)
                    for e in list(state.out_edges(n)):
                        if e.src_conn == out_old:
                            e.src_conn = out_new


def _retarget_consumer_nsdfg(
    parent_state, nsdfg_node, gpu_name: str, packed_name: str,
    packed_size, orig_strides, orig_dtype, mantissa_bits: int,
    decode_dtype=None,
):
    """For a NestedSDFG that consumes gpu_name (input connector only):
    1. Inside the inner SDFG, replace gpu_name → packed_name everywhere.
    2. Update the inner array descriptor: dtype=uint8, shape=(packed_size,).
    3. Tag for text patcher (BFP_PACKED debuginfo).
    4. Rename in_connector at the NSDFG node from gpu_name to packed_name.
    5. Update the parent edge feeding this connector: change dst_conn.
    The parent-side AN/MapEntry rewiring is left to the caller — they need
    to know what the new source AN should be (the packed AN).
    """
    inner = nsdfg_node.sdfg
    if gpu_name not in inner.arrays:
        # Inner SDFG doesn't actually have this array — skip rename.
        return False
    if gpu_name not in nsdfg_node.in_connectors:
        return False
    if gpu_name in nsdfg_node.out_connectors:
        # Producer too — out of scope for pure-consumer redirect.
        return False

    _retarget_sdfg_recursive(
        inner, gpu_name, packed_name, packed_size, orig_strides, orig_dtype,
        mantissa_bits, decode_dtype=decode_dtype,
    )

    # Rename connector on the NestedSDFG node.
    nsdfg_node.remove_in_connector(gpu_name)
    nsdfg_node.add_in_connector(packed_name)

    # Update the parent edge that feeds this NSDFG's connector.
    for edge in list(parent_state.in_edges(nsdfg_node)):
        if edge.dst_conn == gpu_name:
            edge.dst_conn = packed_name
    return True


def discover_gpu_bfp_transient_candidates(
    sdfg: dace.SDFG,
    excluded_names: Iterable[str] = (),
    excluded_prefixes: Iterable[str] = (),
) -> list[str]:
    """Return top-level fp64 GPU transient array names that are safe to BFP
    encode. A candidate must:
      - be a top-level fp64 GPU transient,
      - have at least one covering full-array write,
      - have no kernel-level RMW in any top-level state (in-edge + out-edge
        to a non-AccessNode dst),
      - not appear as both input AND output connector on any NestedSDFG
        anywhere in the SDFG (NSDFG-level RMW),
      - not be in `excluded_names`,
      - not match any `excluded_prefixes` (e.g. output-struct prefixes).
    """
    excluded = set(excluded_names)
    out = []
    for name, arr in sdfg.arrays.items():
        if not isinstance(arr, dace.data.Array):
            continue
        if arr.dtype != dace.float64:
            continue
        if not arr.transient:
            continue
        if arr.storage != dtypes.StorageType.GPU_Global:
            continue
        if name in excluded:
            continue
        if any(name.startswith(p) for p in excluded_prefixes):
            continue

        full_subset = ", ".join(f"0:{s}" for s in arr.shape)
        has_covering = False
        rmw = False
        for state in sdfg.states():
            ans = [n for n in state.nodes()
                   if isinstance(n, nodes.AccessNode) and n.data == name]
            if not ans:
                continue
            for an in ans:
                in_e = state.in_edges(an)
                out_e = state.out_edges(an)
                if in_e:
                    for e in in_e:
                        if e.data and e.data.subset is not None and str(e.data.subset) == full_subset:
                            has_covering = True
                if in_e and out_e:
                    for e in out_e:
                        if not isinstance(e.dst, nodes.AccessNode):
                            rmw = True
                            break
            if rmw:
                break

        if rmw or not has_covering:
            continue
        # NOTE: D2H out-edges (AN(gpu_name) → AN(host_name)) are tolerated.
        # The original fp64 buffer stays alive for both D2H copies and the
        # encoder source; only consumer NSDFGs that take this array as input
        # get retargeted to read the packed buffer.

        # NSDFG-level RMW: any nested SDFG node with this array as both in
        # and out connector means the consumer also writes back — encode
        # placement gets ambiguous, exclude.
        for nsdfg in sdfg.all_sdfgs_recursive():
            for state in nsdfg.states():
                for n in state.nodes():
                    if not isinstance(n, nodes.NestedSDFG):
                        continue
                    if name in n.in_connectors and name in n.out_connectors:
                        rmw = True
                        break
                if rmw:
                    break
            if rmw:
                break
        if rmw:
            continue

        out.append(name)
    return sorted(out)


def inject_gpu_bfp_encode(
    sdfg: dace.SDFG,
    gpu_names: Union[str, Iterable[str]],
    mantissa_bits: int = 16,
    decode_dtype=None,
) -> dace.SDFG:
    """Add GPU-side BFP encode for transient arrays with covering full-array
    writes. For each `gpu_<name>`:
    1. Add packed transient `gpu_<name>_bfp: uint8[packed_size]` (GPU_Global).
    2. In each state with a covering full-array write, insert encode subgraph
       in-place after the producer AccessNode → produces a packed AccessNode.
    3. Retarget every consumer NestedSDFG (in_conn only, not out) to read the
       packed array: rename inner array via sdfg.replace, change descriptor,
       rename connector, redirect parent edge.
    4. For top-level (non-NSDFG) consumers, repoint memlets / install decode
       shims. Producer edges remain on gpu_<name> so encode can read them.
    """
    if isinstance(gpu_names, str):
        gpu_names = [gpu_names]

    for gpu_name in gpu_names:
        if gpu_name not in sdfg.arrays:
            print(f"  GPU-BFP: {gpu_name} not in SDFG, skipping")
            continue
        arr = sdfg.arrays[gpu_name]
        if not isinstance(arr, dace.data.Array):
            print(f"  GPU-BFP: {gpu_name} is not an Array, skipping")
            continue

        producers = _find_covering_write_states(sdfg, gpu_name)
        if not producers:
            print(f"  GPU-BFP: no covering-write producer found for {gpu_name}, skipping")
            continue

        orig_dtype = arr.dtype
        orig_strides = tuple(arr.strides)
        total_elems = _total_elems(arr)
        packed_size = _packed_size_expr(total_elems, mantissa_bits)
        num_blocks = (total_elems + BFP_BLOCK_SIZE - 1) // BFP_BLOCK_SIZE
        block_bytes = _bfp_block_bytes(mantissa_bits)

        packed_name = f"{gpu_name}_bfp"
        if packed_name in sdfg.arrays:
            print(f"  GPU-BFP: {packed_name} already exists, skipping {gpu_name}")
            continue

        print(
            f"  GPU-BFP{mantissa_bits}: encoding {gpu_name} from {orig_dtype}{tuple(arr.shape)} "
            f"-> uint8[{packed_size}] ({len(producers)} producer state(s))"
        )

        sdfg.add_array(
            packed_name,
            shape=(packed_size,),
            dtype=dace.uint8,
            strides=(1,),
            transient=True,
            storage=dtypes.StorageType.GPU_Global,
        )

        # 1. Insert encode subgraph in EACH producer state — produces a
        # packed AccessNode in the same state. Returns the packed AN.
        packed_ans_by_state = {}
        for owning_sdfg, prod_state, prod_an in producers:
            packed_an = _add_gpu_bfp_encode_inplace(
                owning_sdfg, prod_state, prod_an,
                gpu_name, packed_name,
                total_elems, packed_size, num_blocks, block_bytes,
                mantissa_bits,
            )
            packed_ans_by_state[prod_state] = packed_an
            print(f"    Encode subgraph added in {prod_state.label} (packed AN created)")

        # 2. In each producer state, retarget consumer NSDFGs that take
        # gpu_name as input (and not output). They should now consume the
        # packed AN via a new edge through the existing MapEntry.
        retargeted_nsdfgs = 0
        for prod_state, packed_an in packed_ans_by_state.items():
            for n in list(prod_state.nodes()):
                if not isinstance(n, nodes.NestedSDFG):
                    continue
                if gpu_name not in n.in_connectors or gpu_name in n.out_connectors:
                    continue
                # Find the chain: AN(gpu_name) → MapEntry(IN_X→OUT_X) → NSDFG.in[gpu_name]
                me_in_edges = [e for e in prod_state.in_edges(n) if e.dst_conn == gpu_name]
                if not me_in_edges:
                    continue
                # The edge feeding the NSDFG is from a MapEntry's OUT_X.
                me_out_edge = me_in_edges[0]
                if not isinstance(me_out_edge.src, nodes.MapEntry):
                    # Unusual layout — direct AN-to-NSDFG. Just retarget the
                    # NSDFG and redirect the edge.
                    if _retarget_consumer_nsdfg(prod_state, n, gpu_name, packed_name, packed_size, orig_strides, orig_dtype, mantissa_bits, decode_dtype=decode_dtype):
                        # Redirect the edge to come from packed_an
                        me_out_edge.data = dace.Memlet.from_array(packed_name, sdfg.arrays[packed_name])
                        # Reroute via the graph machinery
                        prod_state.remove_edge(me_out_edge)
                        prod_state.add_edge(packed_an, None, n, packed_name,
                                            dace.Memlet.from_array(packed_name, sdfg.arrays[packed_name]))
                        retargeted_nsdfgs += 1
                    continue

                map_entry = me_out_edge.src
                map_out_conn = me_out_edge.src_conn  # e.g. OUT_7
                # Find the matching IN_X feeding this MapEntry.
                map_in_conn = "IN_" + map_out_conn[len("OUT_"):]
                an_to_me_edges = [e for e in prod_state.in_edges(map_entry)
                                  if e.dst_conn == map_in_conn]
                if not an_to_me_edges:
                    continue
                an_to_me_edge = an_to_me_edges[0]

                # Step a: retarget the NSDFG inner / connector.
                if not _retarget_consumer_nsdfg(prod_state, n, gpu_name, packed_name, packed_size, orig_strides, orig_dtype, mantissa_bits):
                    continue
                # Step b: change the memlet on me_out_edge to packed.
                me_out_edge.data = dace.Memlet.from_array(packed_name, sdfg.arrays[packed_name])
                # Step c: rewire AN→ME edge to come from packed_an.
                prod_state.remove_edge(an_to_me_edge)
                prod_state.add_edge(
                    packed_an, None, map_entry, map_in_conn,
                    dace.Memlet.from_array(packed_name, sdfg.arrays[packed_name]),
                )
                retargeted_nsdfgs += 1
        if retargeted_nsdfgs:
            print(f"    Retargeted {retargeted_nsdfgs} consumer NestedSDFG(s) to {packed_name}")

        # 3. For non-NSDFG consumers (top-level reads outside producer states):
        # use the existing repoint-and-decode-shim machinery, skipping
        # producer states.
        skip_states = set(packed_ans_by_state.keys())
        _repoint_consumer_reads_to_packed(
            sdfg, gpu_name, packed_name, skip_states,
            packed_size, orig_strides, orig_dtype, mantissa_bits,
            decode_dtype=decode_dtype,
        )

    return sdfg
