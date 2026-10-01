#!/usr/bin/env bash
# Icarus Verilog 12 does not drive the "delayed_*" nets that the IHP SG13G2
# cell models create through $setuphold/$recrem. For local functional
# gate-level simulation, emit a copy of the models where delayed_X == X.
# (Timing checks are irrelevant for zero-delay functional GL sim.)
set -euo pipefail
: "${PDK_ROOT:?}"
V=$PDK_ROOT/ihp-sg13g2/libs.ref/sg13g2_stdcell/verilog
OUT=${1:-gl_models}
mkdir -p "$OUT"
sed -E -e '/^\s*wire\s+delayed_[A-Za-z_]+(\s*,\s*delayed_[A-Za-z_]+)*\s*;/d' \
       -e 's/\bdelayed_([A-Za-z_]+)\b/\1/g' \
       "$V/sg13g2_stdcell.v" > "$OUT/sg13g2_stdcell_functional.v"
cp "$V/sg13g2_udp.v" "$OUT/" 2>/dev/null || true
echo "wrote $OUT/sg13g2_stdcell_functional.v"
