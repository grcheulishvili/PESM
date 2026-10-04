#!/usr/bin/env bash
# Local replay of the Tiny Tapeout IHP CMOS5L hardening flow (LibreLane
# Classic flow up to the post-route multi-corner STA), using the project's
# src/config.json exactly as CI does.
#
#   PDK_ROOT=/path/IHP-Open-PDK TT_TOOLS=/path/tt-support-tools pnr/run_local.sh [tag] [librelane args]
#
#   PDK_ROOT  IHP-Open-PDK checkout containing ihp-sg13cmos5l (and ihp-sg13g2:
#             the CMOS5L OpenRCX rules file is a symlink into it)
#   TT_TOOLS  tt-support-tools, branch ihp-sg13cmos5l (for the 6x4 DEF template)
#   tag       run name, default "local"; results in pnr/out/<tag>/runs/<tag>
#
# Not run here: Magic/KLayout DRC, LVS, TT precheck (the TT gds workflow is
# the signoff for those). Run pnr/setup.sh first.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
: "${PDK_ROOT:?}"; : "${TT_TOOLS:?path to tt-support-tools (branch ihp-sg13cmos5l)}"
VENV=${VENV:-$HERE/.venv}
[ -x "$VENV/shim-bin/openroad" ] || { echo "run pnr/setup.sh first"; exit 1; }
TAG=${1:-local}; shift || true
PDK=ihp-sg13cmos5l
P=$HERE/out/$TAG
rm -rf "$P"; mkdir -p "$P/src" "$P/runs/$TAG"
cp "$REPO"/src/*.v "$REPO/src/config.json" "$P/src/"
cp "$REPO/info.yaml" "$P/"
ln -sfn "$TT_TOOLS" "$P/tt"
# config_merged.json exactly as tt_tool.py --create-user-config builds it:
# project config.json, overridden by the Tiny Tapeout user_config keys
"$VENV/bin/python" - "$P" <<'PY'
import json, re, sys
p = sys.argv[1]
cfg = json.load(open(f"{p}/src/config.json"))
cfg.pop("//", None)
info = open(f"{p}/info.yaml").read()
top = re.search(r'top_module:\s*"([^"]+)"', info).group(1)
tiles = re.search(r'tiles:\s*"([^"]+)"', info).group(1)
srcs = re.findall(r'^\s*-\s*"([^"]+\.v)"', info, re.M)
defp = f"{p}/tt/tech/ihp-sg13cmos5l/def/tt_block_{tiles}_pgvdd.def"
die = re.search(r"DIEAREA \( (\d+) (\d+) \) \( (\d+) (\d+) \)", open(defp).read()).groups()
cfg.update({
    "DESIGN_NAME": top,
    "VERILOG_FILES": [f"dir::{s}" for s in srcs],
    "DIE_AREA": " ".join(str(int(v) / 1000).rstrip("0").rstrip(".") for v in die),
    "FP_DEF_TEMPLATE": f"dir::../tt/tech/ihp-sg13cmos5l/def/tt_block_{tiles}_pgvdd.def",
    "VDD_PIN": "VPWR", "GND_PIN": "VGND", "RT_MAX_LAYER": "Metal4",
})
json.dump(cfg, open(f"{p}/src/config_merged.json", "w"), indent=2)
PY
export PATH="$VENV/shim-bin:$VENV/bin:$PATH"
cd "$P"
set +e
python -m librelane --pdk-root "$PDK_ROOT" --pdk $PDK --manual-pdk --run-tag "$TAG" \
    --force-run-dir "runs/$TAG" src/config_merged.json --to OpenROAD.STAPostPNR "$@" \
    > "runs/$TAG.log" 2>&1
rc=$?
set -e
echo "librelane exit code $rc (log: $P/runs/$TAG.log)"
"$VENV/bin/python" "$HERE/ll_report.py" "$P/runs/$TAG" || true
exit $rc
