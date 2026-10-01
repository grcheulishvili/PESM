#!/usr/bin/env bash
# Local physical-implementation check (NOT the TT signoff flow).
#   PDK_ROOT=/path/IHP-Open-PDK TT_TOOLS=/path/tt-support-tools ./run_pnr.sh
# Needs: yosys, python with the 'openroad' pip package (OpenROAD bindings).
set -euo pipefail
cd "$(dirname "$0")"
: "${PDK_ROOT:?}"; : "${TT_TOOLS:?path to tt-support-tools checkout (for the 6x4 DEF template)}"
PY=${PYTHON:-python3}
OUT=${OUT:-$(pwd)/out}
mkdir -p "$OUT"
LIB=$PDK_ROOT/ihp-sg13g2/libs.ref/sg13g2_stdcell/lib
# Liberty without excluded / dont_use cells for synthesis
"$PY" - "$LIB/sg13g2_stdcell_typ_1p20V_25C.lib" "$OUT/synth.lib" <<'PYEOF'
import re, sys
src, dst = sys.argv[1:3]
excl = {"sg13g2_lgcp_1","sg13g2_sighold","sg13g2_slgcp_1","sg13g2_sdfbbp_1","sg13g2_dfrbp_2",
        "sg13g2_buf_16","sg13g2_inv_16"}
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
SRC="../src/tt_um_protocol_engine.v ../src/pesm_core.v ../src/pesm_host.v ../src/pesm_fifo.v ../src/pesm_clkdiv.v ../src/pesm_sync.v"
yosys -q -l "$OUT/synth.log" -p "
  read_liberty -lib $OUT/synth.lib
  read_verilog $SRC
  synth -top tt_um_protocol_engine -flatten
  dfflibmap -liberty $OUT/synth.lib
  abc -liberty $OUT/synth.lib -D 20000
  hilomap -hicell sg13g2_tiehi L_HI -locell sg13g2_tielo L_LO
  setundef -zero
  splitnets
  opt_clean -purge
  insbuf -buf sg13g2_buf_1 A X
  check -assert
  write_verilog -noattr -noexpr -nohex -nodec $OUT/synth.v
"
NETLIST=$OUT/synth.v DEF_TEMPLATE=$TT_TOOLS/tech/ihp-sg13g2/def/tt_block_6x4_pgvdd.def OUT=$OUT \
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
