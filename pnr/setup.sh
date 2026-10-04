#!/usr/bin/env bash
# One-time setup of the local LibreLane replay (no Docker / Nix needed).
#   pnr/setup.sh [venv dir]           default: pnr/.venv
# Installs LibreLane 3.1.0.dev3 (the version the Tiny Tapeout IHP CMOS5L
# action uses), Yosys 0.66 as the pip 'pyosys' module (the CI's Yosys
# version) and the pip 'openroad' bindings, and creates openroad / sta /
# yosys wrappers in <venv>/shim-bin.
# Needs: python3 with tkinter and venv (apt: python3-tk python3-venv).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
VENV=${1:-$HERE/.venv}
PY=${PYTHON:-python3}
"$PY" -c "import tkinter" 2>/dev/null || { echo "python tkinter is required (apt install python3-tk)"; exit 1; }
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q "librelane==3.1.0.dev3" "pyosys==0.66" "openroad==0.0.1"
BIN=$VENV/shim-bin
mkdir -p "$BIN"
STDBUF=$(command -v stdbuf || true)
cat > "$BIN/openroad" <<EOF
#!/bin/sh
exec ${STDBUF:+$STDBUF -oL} "$VENV/bin/python" -u "$HERE/shim/or_shim.py" "\$@"
EOF
cat > "$BIN/sta" <<EOF
#!/bin/sh
OR_SHIM_MODE=sta exec "$VENV/bin/python" -u "$HERE/shim/or_shim.py" "\$@"
EOF
cat > "$BIN/yosys" <<EOF
#!/bin/sh
OR_SHIM_BIN="$BIN" exec "$VENV/bin/python" -u "$HERE/shim/yosys_shim.py" "\$@"
EOF
chmod +x "$BIN"/*
echo "ok: $VENV (wrappers in $BIN)"
