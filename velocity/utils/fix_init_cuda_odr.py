"""Fix __dace_init_cuda_N ODR violation between main .cu and _cuda.cu.

DaCe codegen bug: the forward declaration in the main .cu has the full
__dace_init signature (all pointers + scalars), but the actual implementation
in _cuda.cu only takes __state + scalar shape metadata ints.

This module extracts the correct signature from _cuda.cu, replaces the wrong
forward declaration in the main .cu, moves struct metadata reads before the
call, and rewrites the call to pass correct scalar parameters.
"""

import os
import re


def _extract_init_cuda_fwd_decl(cuda_src: str) -> tuple:
    """Extract the correct __dace_init_cuda_N forward declaration from _cuda.cu content.
    Returns (marker_with_index, full_declaration_text) or ("", "")."""
    m = re.search(r"DACE_EXPORTED int (__dace_init_cuda_\d+)\(", cuda_src)
    if not m:
        return "", ""
    fn_name = m.group(1)
    marker = f"DACE_EXPORTED int {fn_name}("
    start = cuda_src.find(marker)
    if start == -1:
        return "", ""
    # Find matching ");", accounting for nested parens
    depth = 0
    i = start
    while i < len(cuda_src):
        if cuda_src[i] == '(':
            depth += 1
        elif cuda_src[i] == ')':
            depth -= 1
            if depth == 0:
                return fn_name, cuda_src[start : i + 1] + ";"
        i += 1
    return "", ""


def _extract_param_names(fwd_decl: str) -> list:
    """Extract parameter names (in order) from a C function declaration."""
    # Get content between outermost parens
    paren_start = fwd_decl.index('(')
    paren_end = fwd_decl.rindex(')')
    params_str = fwd_decl[paren_start + 1 : paren_end]
    # Split by comma, but respect nested parens/templates
    params = []
    depth = 0
    current = ""
    for ch in params_str:
        if ch in '(<':
            depth += 1
            current += ch
        elif ch in ')>':
            depth -= 1
            current += ch
        elif ch == ',' and depth == 0:
            params.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        params.append(current.strip())
    # Extract the name from each "type name" declaration
    names = []
    for p in params:
        p = p.strip()
        if not p:
            continue
        # Remove trailing array brackets if any
        p = re.sub(r'\[.*\]$', '', p).strip()
        # The name is the last token (after *, &, etc.)
        # Handle "type *__restrict__ name" patterns
        tokens = p.split()
        if tokens:
            name = tokens[-1].lstrip('*&')
            names.append(name)
    return names


def fix_init_cuda_0_signature(cpu_src: str, dev_src: str):
    """
    Fix the __dace_init_cuda_N ODR violation between main .cu and _cuda.cu.

    This function:
    1. Extracts the correct signature from _cuda.cu
    2. Replaces the wrong forward declaration in the main .cu
    3. Moves struct metadata reads before the __dace_init_cuda_N call
    4. Rewrites the call to pass correct scalar parameters
    """
    with open(dev_src, "r") as f:
        cuda_code = f.read()
    with open(cpu_src, "r") as f:
        host_code = f.read()

    # 1. Extract correct forward declaration from _cuda.cu
    fn_name, correct_fwd = _extract_init_cuda_fwd_decl(cuda_code)
    if not correct_fwd:
        print(f"[ODR fix] WARNING: __dace_init_cuda_N not found in {os.path.basename(dev_src)} — skipping")
        return

    correct_param_names = _extract_param_names(correct_fwd)
    # First param is __state, rest are the scalar args
    cuda_0_arg_names = correct_param_names[1:]  # skip __state

    # 2. Find the wrong forward declaration in the main .cu
    wrong_fwd_marker = f"DACE_EXPORTED int {fn_name}("
    wrong_fwd_start = host_code.find(wrong_fwd_marker)
    if wrong_fwd_start == -1:
        print(f"[ODR fix] WARNING: {fn_name} fwd decl not found in {os.path.basename(cpu_src)} — skipping")
        return

    # Find end of wrong forward declaration
    depth = 0
    i = wrong_fwd_start
    while i < len(host_code):
        if host_code[i] == '(':
            depth += 1
        elif host_code[i] == ')':
            depth -= 1
            if depth == 0:
                break
        i += 1
    # Skip past the ");"
    wrong_fwd_end = host_code.index(';', i) + 1

    # Check if signatures actually differ
    wrong_fwd = host_code[wrong_fwd_start:wrong_fwd_end]
    wrong_param_names = _extract_param_names(wrong_fwd)
    if wrong_param_names == correct_param_names:
        print(f"[ODR fix] {fn_name} signatures already match — no fix needed")
        return

    print(f"[ODR fix] {fn_name} violation detected in {os.path.basename(cpu_src)}")
    print(f"  main .cu forward decl: {len(wrong_param_names)} params")
    for i, n in enumerate(wrong_param_names):
        print(f"    [{i}] {n}")
    print(f"  _cuda.cu actual impl:  {len(correct_param_names)} params")
    for i, n in enumerate(correct_param_names):
        print(f"    [{i}] {n}")
    # Show which params are being dropped
    wrong_set = set(wrong_param_names)
    correct_set = set(correct_param_names)
    dropped = wrong_set - correct_set
    if dropped:
        print(f"  DROPPING from fwd decl: {sorted(dropped)}")
    added = correct_set - wrong_set
    if added:
        print(f"  ADDED (unexpected):     {sorted(added)}")

    # Replace wrong forward declaration with correct one
    host_code = host_code[:wrong_fwd_start] + correct_fwd + host_code[wrong_fwd_end:]

    # 3. Find the __dace_init function body and fix the call + struct reads
    init_fn_marker = f"__result |= {fn_name}("
    call_start = host_code.find(init_fn_marker)
    if call_start == -1:
        print(f"[ODR fix] WARNING: {fn_name}() call not found in {os.path.basename(cpu_src)} — fwd decl fixed but call not rewritten")
        with open(cpu_src, "w") as f:
            f.write(host_code)
        return

    # Find end of the wrong call: matching ");"
    depth = 0
    i = host_code.index('(', call_start)
    while i < len(host_code):
        if host_code[i] == '(':
            depth += 1
        elif host_code[i] == ')':
            depth -= 1
            if depth == 0:
                break
        i += 1
    call_end = host_code.index(';', i) + 1

    # 4. Find struct metadata reads that follow the call.
    # These are lines like:
    #   tmp_struct_symbol_0 = global_data->nproma;
    #   __f2dace_SOA_xxx = p_patch->edges->__f2dace_SOA_xxx;
    # They end when we hit a line that's NOT an assignment from a struct
    # (i.e., the state init block with __state->... or cudaMalloc)
    after_call = host_code[call_end:]
    struct_read_lines = []
    rest_start = 0
    lines = after_call.split('\n')
    li = 0
    while li < len(lines):
        line = lines[li].strip()
        if not line:
            li += 1
            continue
        # A struct read is: var = something->something; (possibly multi-line)
        # It does NOT start with __state, DACE_GPU_CHECK, cudaMalloc, return, etc.
        if (line.startswith('__f2dace_') or line.startswith('tmp_struct_symbol_') or
                line.startswith('__f2dace_SA_') or line.startswith('__f2dace_SOA_')):
            # Collect possibly multi-line assignment
            collected = lines[li]
            while not collected.rstrip().endswith(';'):
                li += 1
                if li < len(lines):
                    collected += '\n' + lines[li]
                else:
                    break
            struct_read_lines.append(collected)
            li += 1
        else:
            # Not a struct read — this is where state init begins
            break
    # rest_start in the after_call string
    rest_start = 0
    for j in range(li):
        rest_start = after_call.index('\n', rest_start) + 1 if rest_start < len(after_call) else rest_start

    struct_reads_block = '\n'.join(struct_read_lines)
    remaining_after_reads = after_call[rest_start:]

    # 5. Build the correct call
    call_args = ', '.join(cuda_0_arg_names)
    new_call = f"  __result |= {fn_name}(\n      __state, {call_args});"

    # 6. Reassemble: struct reads THEN call THEN rest
    host_code = (host_code[:call_start] +
                 struct_reads_block + '\n' +
                 new_call + '\n' +
                 remaining_after_reads)

    with open(cpu_src, "w") as f:
        f.write(host_code)
    print(f"[ODR fix] Patched {os.path.basename(cpu_src)}:")
    print(f"  Replaced fwd decl with correct {len(correct_param_names)}-param signature")
    if struct_read_lines:
        print(f"  Moved {len(struct_read_lines)} struct-read assignments before {fn_name}() call:")
        for sr in struct_read_lines:
            first_line = sr.strip().split('\n')[0].strip()
            if len(first_line) > 100:
                first_line = first_line[:97] + "..."
            print(f"    {first_line}")
    print(f"  Rewrote call: {fn_name}(__state, {call_args})")
