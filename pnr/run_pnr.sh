#!/usr/bin/env bash
# Local physical-implementation check (NOT the TT signoff flow).
#   PDK_ROOT=/path/IHP-Open-PDK TT_TOOLS=/path/tt-support-tools [PDK=ihp-sg13cmos5l] ./run_pnr.sh
# TT_TOOLS must be the tt-support-tools branch for the PDK (ihp-sg13cmos5l branch for CMOS5L).
# Needs: yosys, python with the 'openroad' pip package (OpenROAD bindings).
set -euo pipefail
cd "$(dirname "$0")"
: "${PDK_ROOT:?}"; : "${TT_TOOLS:?path to tt-support-tools checkout (for the 6x4 DEF template)}"
PY=${PYTHON:-python3}
OUT=${OUT:-$(pwd)/out}
mkdir -p "$OUT"
export PDK=${PDK:-ihp-sg13cmos5l}
SCL=${PDK#ihp-}
LIB=$PDK_ROOT/$PDK/libs.ref/${SCL}_stdcell/lib
# Liberty without excluded / dont_use cells for synthesis
"$PY" - "$LIB/${SCL}_stdcell_typ_1p20V_25C.lib" "$OUT/synth.lib" "$SCL" <<'PYEOF'
import re, sys
src, dst, scl = sys.argv[1:4]
excl = {f"{scl}_{c}" for c in ("lgcp_1", "sighold", "slgcp_1", "sdfbbp_1", "dfrbp_2",
                                "buf_16", "inv_16")}
txt = open(src).read()
out, i = [], 0
for m in re.finditer(r'\n\s*cell\s*\(\s*"?(\w+)"?\s*\)\s*\{', txt):
    pass
# simple brace-matching filter
pos = 0; res = []
pat = re.compile(r'\n(\s*)cell\s*\(\s*"?(\w+)"?\s*\)\s*\{')
while True:
    m = pat.search(txt, pos)
    if not m:
        res.append(txt[pos:]); break
    res.append(txt[pos:m.start()])
    depth, j = 1, m.end()
    while depth:
        c = txt[j]
        depth += (c == '{') - (c == '}')
        j += 1
    if m.group(2) not in excl:
        res.append(txt[m.start():j])
    pos = j
open(dst, "w").write("".join(res))
PYEOF
if [ "${STA_ONLY:-0}" != 1 ]; then
SRC="../src/tt_um_protocol_engine.v ../src/pesm_core.v ../src/pesm_host.v ../src/pesm_fifo.v ../src/pesm_clkdiv.v ../src/pesm_sync.v"
yosys -q -l "$OUT/synth.log" -p "
  read_liberty -lib $OUT/synth.lib
  read_verilog $SRC
  synth -top tt_um_protocol_engine -flatten
  dfflibmap -liberty $OUT/synth.lib
  abc -liberty $OUT/synth.lib -D 20000
  hilomap -hicell ${SCL}_tiehi L_HI -locell ${SCL}_tielo L_LO
  setundef -zero
  splitnets
  opt_clean -purge
  insbuf -buf ${SCL}_buf_1 A X
  check -assert
  write_verilog -noattr -noexpr -nohex -nodec $OUT/synth.v
"
fi
NETLIST=$OUT/synth.v DEF_TEMPLATE=$TT_TOOLS/tech/$PDK/def/tt_block_6x4_pgvdd.def OUT=$OUT \
  "$PY" run_pnr.py 2>&1 | tee "$OUT/flow.log"
{
  echo "corner  path   group         worst slack (ns)"
  for c in typ slow fast; do for m in max min; do
    awk -v c=$c -v m=$m '/^Path Group:/ {g=$3} /slack \(/ {printf "%-6s  %-5s  %-12s  %s\n", c, (m=="max"?"setup":"hold"), g, $1}' "$OUT/sta_${c}_${m}.rpt"
  done; done
  echo "detailed-route DRC markers : $(grep -c . "$OUT/drt_drc.rpt" || true)"
  echo "max slew/cap/fanout viol.  : $(grep -c VIOLATED "$OUT/drv.rpt" || true)"
  grep -h "ANT-0002" "$OUT/flow.log" | tail -1
  grep -h "Design area" "$OUT/flow.log" | tail -1
} | tee "$OUT/summary.txt"
