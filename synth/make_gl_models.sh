#!/usr/bin/env bash
# Only needed with Icarus Verilog 12 (the TT CI uses Icarus 13, which is fine).
# Icarus Verilog 12 does not drive the "delayed_*" nets that the IHP
# cell models create through $setuphold/$recrem. For local functional
# gate-level simulation, emit a copy of the models where delayed_X == X.
# (Timing checks are irrelevant for zero-delay functional GL sim.)
set -euo pipefail
: "${PDK_ROOT:?}"
PDK=${PDK:-ihp-sg13cmos5l}
SCL=${PDK#ihp-}
V=$PDK_ROOT/$PDK/libs.ref/${SCL}_stdcell/verilog
OUT=${1:-gl_models}
mkdir -p "$OUT"
sed -E -e '/^\s*wire\s+delayed_[A-Za-z_]+(\s*,\s*delayed_[A-Za-z_]+)*\s*;/d' \
       -e 's/\bdelayed_([A-Za-z_]+)\b/\1/g' \
       "$V/${SCL}_stdcell.v" > "$OUT/${SCL}_stdcell_functional.v"
cp "$V/${SCL}_udp.v" "$OUT/" 2>/dev/null || true
echo "wrote $OUT/${SCL}_stdcell_functional.v"
