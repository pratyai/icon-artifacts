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
):
    """Update memlets for a BFP-packed array, inserting decode shims at scalar reads.

    After changing an array from 3D to 1D uint8, existing memlets still have
    3D subsets. This function:
    - Scalar memlets into Tasklets: insert a BFP decode shim tasklet
    - Range/other memlets: flatten to 1D [0:packed_size]
    """
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
                    orig_dtype,
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
) -> dace.SDFG:
    """Transform selected arrays to BFP uint8 packed format on GPU.

    For each array:
    1. Changes gpu_ array to uint8[packed_size]
    2. Adds CPU-side packed transient
    3. Inserts BFP pack Map (CPU float → CPU uint8)
    4. Replaces H2D AN→AN edge with packed version
    5. Removes D2H edge (read-only data)

    mantissa_bits: 16 for bfp16/bfp32, 8 for bfp8.
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

        _inject_bfp_for_array(sdfg, cpu_name, gpu_name, mantissa_bits=mantissa_bits)

    sdfg.validate()
    return sdfg


def _inject_bfp_for_array(
    sdfg: dace.SDFG, cpu_name: str, gpu_name: str, mantissa_bits: int = 16
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

    # Step 2: Change GPU array to uint8[packed_size]
    gpu_arr.dtype = dace.uint8
    gpu_arr.shape = (packed_size,)
    gpu_arr.strides = (1,)
    gpu_arr.offset = [0]
    gpu_arr.total_size = packed_size

    # Step 2b: Update existing memlets referencing the GPU array.
    # Scalar reads into Tasklets get a BFP decode shim; range memlets flatten to 1D.
    _update_memlets_and_insert_decode(
        sdfg,
        gpu_name,
        packed_size,
        orig_strides,
        orig_dtype,
        mantissa_bits=mantissa_bits,
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
