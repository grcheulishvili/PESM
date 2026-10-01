#!/usr/bin/env bash
# Reproduce the v1 findings in AUDIT.md against the ORIGINAL RTL.
#   ./run_probes.sh path/to/original/src/tt_um_protocol_engine.v
set -euo pipefail
V1=${1:?path to v1 tt_um_protocol_engine.v}
cd "$(dirname "$0")"
for p in probe_v1_a probe_v1_b; do
  iverilog -g2012 -o /tmp/$p.vvp "$V1" $p.v
  vvp -n /tmp/$p.vvp | grep -v VCD
done
