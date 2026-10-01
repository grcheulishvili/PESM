#!/usr/bin/env bash
# Pre-layout synthesis of PESM to IHP SG13G2 standard cells + ABC static timing
# estimate. This is NOT signoff: no placement, no wires, no CTS. It exists to
# catch area/timing problems before the LibreLane flow runs in CI.
#
#   PDK_ROOT=/path/to/IHP-Open-PDK ./run_synth.sh
set -euo pipefail
cd "$(dirname "$0")"
: "${PDK_ROOT:?set PDK_ROOT to the IHP-Open-PDK checkout}"
LIBDIR=$PDK_ROOT/ihp-sg13g2/libs.ref/sg13g2_stdcell/lib
TYP=$LIBDIR/sg13g2_stdcell_typ_1p20V_25C.lib
SLOW=$LIBDIR/sg13g2_stdcell_slow_1p08V_125C.lib
SRC="../src/tt_um_protocol_engine.v ../src/pesm_core.v ../src/pesm_host.v ../src/pesm_fifo.v ../src/pesm_clkdiv.v ../src/pesm_sync.v"

run() {  # $1 = liberty, $2 = tag
  yosys -q -l "synth_$2.log" -p "
    read_liberty -lib $1
    read_verilog $SRC
    synth -top tt_um_protocol_engine -flatten
    dfflibmap -liberty $1
    abc -liberty $1 -D 20000 -script +strash;&get,-n;&fraig,-x;&put;scorr;dc2;dretime;strash;&get,-n;&dch,-f;&nf,-D,20000;&put;buffer;upsize,-D,20000;dnsize,-D,20000;stime,-p
    hilomap -singleton -hicell sg13g2_tiehi L_HI -locell sg13g2_tielo L_LO
    opt_clean -purge
    check -assert
    tee -o stat_$2.txt stat -liberty $1
    write_verilog -noattr -noexpr netlist_$2.v
  "
}
run "$TYP"  typ
run "$SLOW" slow

area=$(awk '/Chip area for module/ {print $NF}' stat_typ.txt | tail -1)
cells=$(awk '/Number of cells/ {print $NF}' stat_typ.txt | tail -1)
flops=$(grep -E "sg13g2_(s?dfrbp|dfrbpq|sdfbbp)" stat_typ.txt | awk '{s+=$2} END {print s}')
d_typ=$(grep -E "ABC: (WireLoad|Path|.*Delay =)" synth_typ.log | grep -oE "Delay = *[0-9.]+ ps" | tail -1 | grep -oE "[0-9.]+")
d_slow=$(grep -oE "Delay = *[0-9.]+ ps" synth_slow.log | tail -1 | grep -oE "[0-9.]+")
tile_area=720000   # 24 tiles x ~200 um x 150 um (competition rules, approximate)
echo "cells (typ map)        : $cells"
echo "flip-flops             : $flops"
echo "std-cell area (typ)    : $area um^2"
echo "6x4 tile area (~)      : $tile_area um^2  -> utilisation $(python3 -c "print(round(100*$area/$tile_area,1))") %"
echo "ABC comb. delay typ    : ${d_typ} ps"
echo "ABC comb. delay slow   : ${d_slow} ps   (budget 20000 ps at 50 MHz, minus clk->q/setup/skew)"
