import h5py
import numpy as np
import polars as pl
import argparse
import os
import sys

def calculate_snr(ref, test):
    """Calculates Signal-to-Noise Ratio in dB."""
    signal_power = np.mean(np.square(ref))
    noise_power = np.mean(np.square(ref - test))
    if noise_power == 0:
        return float('inf')
    if signal_power == 0:
        return 0.0
    return 10 * np.log10(signal_power / noise_power)

def compare_files(file_ref, file_test, tol=1e-12):
    if not os.path.exists(file_ref):
        print(f"Error: Reference file {file_ref} not found.")
        return
    if not os.path.exists(file_test):
        print(f"Error: Test file {file_test} not found.")
        return

    print(f"Comparing:\n  Ref:  {file_ref}\n  Test: {file_test}\n")

    results = []
    with h5py.File(file_ref, 'r') as f_ref, h5py.File(file_test, 'r') as f_test:
        common_keys = sorted(set(f_ref.keys()) & set(f_test.keys()))
        
        if not common_keys:
            print("No common datasets found between files.")
            return

        for key in common_keys:
            ref_data = f_ref[key][()]
            test_data = f_test[key][()]

            if ref_data.shape != test_data.shape:
                results.append({
                    "Field": key,
                    "Status": "SHAPE MISMATCH",
                    "Max Abs Err": None,
                    "Max Rel Err": None,
                    "RMS": None,
                    "SNR (dB)": None
                })
                continue

            abs_err = np.abs(ref_data - test_data)
            max_abs = np.max(abs_err)
            
            # Relative error with small epsilon to avoid div by zero
            rel_err = abs_err / (np.abs(ref_data) + 1e-15)
            max_rel = np.max(rel_err)
            
            rms = np.sqrt(np.mean(np.square(abs_err)))
            snr = calculate_snr(ref_data, test_data)
            
            status = "PASS" if max_rel <= tol else "FAIL"
            
            results.append({
                "Field": key,
                "Status": status,
                "Max Abs Err": max_abs,
                "Max Rel Err": max_rel,
                "RMS": rms,
                "SNR (dB)": snr
            })

    df = pl.DataFrame(results)
    
    # Configure polars to show more rows
    with pl.Config(tbl_rows=len(results), tbl_width_chars=120):
        print(df)

    failed = df.filter(pl.col("Status") != "PASS")
    if failed.height > 0:
        print(f"\n❌ Validation FAILED for {failed.height} fields (tol={tol})")
        sys.exit(1)
    else:
        print(f"\n✅ Validation PASSED (tol={tol})")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare CloudSC HDF5 outputs.")
    parser.add_argument("--step", type=int, help="Comparison step index (compares ref vs cpp)")
    parser.add_argument("--ref", type=str, help="Path to reference HDF5 file")
    parser.add_argument("--test", type=str, help="Path to test HDF5 file")
    parser.add_argument("--tol", type=float, default=1e-12, help="Relative error tolerance")
    args = parser.parse_args()

    if args.step is not None:
        file_ref = f"outputs_ref/ref_output_step_{args.step}.h5"
        file_test = f"outputs_cpp/cpp_output_step_{args.step}.h5"
    else:
        if not args.ref or not args.test:
            print("Error: Provide either --step or both --ref and --test")
            sys.exit(1)
        file_ref = args.ref
        file_test = args.test

    compare_files(file_ref, file_test, args.tol)
