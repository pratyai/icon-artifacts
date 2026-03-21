import dace
import re

def replace_kfdia_symbols(input_file, output_file):
    sdfg = dace.SDFG.from_file(input_file)

    pattern = re.compile(r'kfdia_plus_1.*')
    matches = [s for s in sdfg.symbols if pattern.match(s)]
    print(f"Found {len(matches)} matching symbols.")

    for e, _ in sdfg.all_edges_recursive():
        if not isinstance(e.data, dace.InterstateEdge):
            continue
        del_k = [k for k in e.data.assignments if k.startswith('kfdia_plus_1')]
        for k in del_k:
            del e.data.assignments[k]

    # Loop over all SDFGs (including nested ones) as suggested
    for s in sdfg.all_sdfgs_recursive():
        # 1. REPLACE kfdia_plus_1_* WITH (kfdia + 1)
        for m in matches:
            # s.replace handles replacements in all nodes and edges within this SDFG
            s.replace(m, '(kfdia + 1)')
        
        # Clean up the symbols dictionary for this SDFG
        for m in matches:
            if m in s.symbols:
                del s.symbols[m]

    sdfg.save(output_file, compress=True)
    print(f"Saved modified SDFG to {output_file}")

if __name__ == "__main__":
    replace_kfdia_symbols('cloudsc_pydace_simplified_symbolic.sdfgz', 'cloudsc_pydace_replaced.sdfgz')
