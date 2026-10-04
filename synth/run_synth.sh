#!/usr/bin/env bash
# Pre-layout synthesis of PESM to IHP standard cells + ABC static timing
# estimate. This is NOT signoff: no placement, no wires, no CTS. It exists to
# catch area/timing problems before the LibreLane flow runs in CI.
#
#   PDK_ROOT=/path/to/IHP-Open-PDK [PDK=ihp-sg13cmos5l|ihp-sg13g2] ./run_synth.sh
set -euo pipefail
cd "$(dirname "$0")"
: "${PDK_ROOT:?set PDK_ROOT to the IHP-Open-PDK checkout}"
PDK=${PDK:-ihp-sg13cmos5l}          # competition target: IHP CMOS5L
SCL=${PDK#ihp-}                     # sg13cmos5l | sg13g2
LIBDIR=$PDK_ROOT/$PDK/libs.ref/${SCL}_stdcell/lib
TYP=$LIBDIR/${SCL}_stdcell_typ_1p20V_25C.lib
SLOW=$LIBDIR/${SCL}_stdcell_slow_1p08V_125C.lib
SRC="../src/tt_um_protocol_engine.v ../src/pesm_core.v ../src/pesm_host.v ../src/pesm_fifo.v ../src/pesm_clkdiv.v ../src/pesm_sync.v ../src/pesm_mux4.v"

run() {  # $1 = liberty, $2 = tag
  yosys -q -l "synth_$2.log" -p "
    read_liberty -lib $1
    read_verilog -DSCL_${SCL}_stdcell $SRC
    synth -top tt_um_protocol_engine -flatten
    dfflibmap -liberty $1
    abc -liberty $1 -D 20000 -script +strash;&get,-n;&fraig,-x;&put;scorr;dc2;dretime;strash;&get,-n;&dch,-f;&nf,-D,20000;&put;buffer;upsize,-D,20000;dnsize,-D,20000;stime,-p
    hilomap -singleton -hicell ${SCL}_tiehi L_HI -locell ${SCL}_tielo L_LO
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
flops=$(grep -E "${SCL}_(s?dfrbp|dfrbpq|sdfbbp)" stat_typ.txt | awk '{s+=$2} END {print s}')
d_typ=$(grep -E "ABC: (WireLoad|Path|.*Delay =)" synth_typ.log | grep -oE "Delay = *[0-9.]+ ps" | tail -1 | grep -oE "[0-9.]+")
d_slow=$(grep -oE "Delay = *[0-9.]+ ps" synth_slow.log | tail -1 | grep -oE "[0-9.]+")
tile_area=916214   # TT 6x4 die (CMOS5L and SG13G2): 1289.28 x 710.64 um (tt-support-tools tile_sizes.yaml)
echo "PDK                    : $PDK"
echo "cells (typ map)        : $cells"
echo "flip-flops             : $flops"
echo "std-cell area (typ)    : $area um^2"
echo "6x4 die area           : $tile_area um^2  -> utilisation $(python3 -c "print(round(100*$area/$tile_area,1))") %"
echo "ABC comb. delay typ    : ${d_typ} ps"
echo "ABC comb. delay slow   : ${d_slow} ps   (budget 20000 ps at 50 MHz, minus clk->q/setup/skew)"
