#!/bin/bash
# Spatial diagnostic wrapper with CLI args.
# Usage:
#   ./run_spatial_diag.sh --field vn --phys 21 --ss 5
#   ./run_spatial_diag.sh --field w --phys 21 --scan
#   ./run_spatial_diag.sh --field vn --raw-edges
#   ./run_spatial_diag.sh --coarse-res R02B03 --fine-res R02B04 --coarse-grid-id 0050 --fine-grid-id 0010

# Defaults
EXPBASE=/capstor/scratch/cscs/pmazumde/gitspace/icon-dace/build/verification
COARSE_RES=R02B04
FINE_RES=R02B05
COARSE_GRID_ID=0010
FINE_GRID_ID=0008
SERDE=p_prog
PHYS=1
VT=1
SS=5
FIELD=vn
NLEV=90
SCAN=false
RAW_EDGES=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --expbase)       EXPBASE="$2";        shift 2 ;;
        --coarse-res)    COARSE_RES="$2";     shift 2 ;;
        --fine-res)      FINE_RES="$2";       shift 2 ;;
        --coarse-grid-id) COARSE_GRID_ID="$2"; shift 2 ;;
        --fine-grid-id)  FINE_GRID_ID="$2";   shift 2 ;;
        --serde)         SERDE="$2";          shift 2 ;;
        --phys)          PHYS="$2";           shift 2 ;;
        --vt)            VT="$2";             shift 2 ;;
        --ss)            SS="$2";             shift 2 ;;
        --field)         FIELD="$2";          shift 2 ;;
        --nlev)          NLEV="$2";           shift 2 ;;
        --scan)          SCAN=true;           shift ;;
        --raw-edges)     RAW_EDGES=true;      shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

COARSE_GRID=${EXPBASE}/icon_grid_${COARSE_GRID_ID}_${COARSE_RES}_G.nc
FINE_GRID=${EXPBASE}/icon_grid_${FINE_GRID_ID}_${FINE_RES}_G.nc
COARSE_EXP=${EXPBASE}/experiments/exclaim_ape_R02B04_dt8_g${COARSE_GRID_ID}_${COARSE_RES}-SER-OG
FINE_EXP=${EXPBASE}/experiments/exclaim_ape_R02B04_dt8_g${FINE_GRID_ID}_${FINE_RES}-SER-OG
COARSE_FILE=${COARSE_EXP}/${SERDE}.t0.p${PHYS}.d1.vt${VT}.ss${SS}.data
FINE_FILE=${FINE_EXP}/${SERDE}.t0.p${PHYS}.d1.vt${VT}.ss${SS}.data

echo "=== spatial_diag: ${FIELD} from ${SERDE} p${PHYS} ss${SS} | ${COARSE_RES} vs ${FINE_RES} ==="

EXTRA_ARGS=()
if $SCAN; then
    EXTRA_ARGS+=(--scan --scan-dir "$COARSE_EXP")
fi
if $RAW_EDGES; then
    EXTRA_ARGS+=(--raw-edges)
fi

if $SCAN; then
    python utils/spatial_diag.py \
        --field "$FIELD" \
        --nlev "$NLEV" \
        "${EXTRA_ARGS[@]}"
else
    python utils/spatial_diag.py \
        --coarse-grid "$COARSE_GRID" \
        --fine-grid "$FINE_GRID" \
        --coarse-file "$COARSE_FILE" \
        --fine-file "$FINE_FILE" \
        --field "$FIELD" \
        --nlev "$NLEV" \
        "${EXTRA_ARGS[@]}"
fi
