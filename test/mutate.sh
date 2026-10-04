#!/usr/bin/env bash
# Mutation smoke test: inject known bugs into the RTL and check that the
# directed, toolchain or CRV suite fails for each one.
#   ./mutate.sh              run all mutants, JOBS at a time (default 2)
#   ./mutate.sh <n>          worker: run mutant n in its own copy (internal)
# Takes about 25 min with JOBS=2. Exit status = number of escaped mutants.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)

# file | description | sed expression
MUT=(
  "pesm_core.v|jmp !osre uses <= instead of <|s/3'd6:    jcond = (osr_cnt < pull_th);/3'd6:    jcond = (osr_cnt <= pull_th);/"
  "pesm_core.v|wrap ignored on fall-through|s/wire \[5:0\] seq_pc = (pc == cfg_wrap_top) ? cfg_wrap_bot : (pc + 6'd1);/wire [5:0] seq_pc = pc + 6'd1;/"
  "pesm_core.v|toggle reads side-set-modified value|s/2'd2: if (pin != 4'd15) out_n\[pin\] = ~out_reg\[pin\];/2'd2: if (pin != 4'd15) out_n[pin] = ~out_n[pin];/"
  "pesm_core.v|dlyt ignores tick on first cycle|s/if (!(tick \&\& (n16 == 16'd1))) begin/if (!(n16 == 16'd1)) begin/"
  "pesm_core.v|autopull threshold > instead of >=|s/if (cfg_autopull \&\& (osr_cnt >= pull_th)) begin/if (cfg_autopull \&\& (osr_cnt > pull_th)) begin/"
  "pesm_core.v|RX full ignores the pending registered write|s/wire        rx_full_c = rx_full | (rx_push \& (rx_level == 4'd7));/wire        rx_full_c = rx_full;/"
  "pesm_core.v|JMP target bit 5 dropped|s/OP_JMP, OP_JMPH: tgt = {op\[3\], instr\[8:4\]};/OP_JMP, OP_JMPH: tgt = {1'b0, instr[8:4]};/"
  "pesm_core.v|JPIN target truncated to 5 bits|s/default:         tgt = instr\[5:0\];          \/\/ JPIN, JPAT/default:         tgt = {1'b0, instr[4:0]};/"
  "pesm_core.v|MOV PC truncated to 5 bits|s/OP_MOV:          tgt = mv_res\[5:0\];/OP_MOV:          tgt = {1'b0, mv_res[4:0]};/"
  "pesm_core.v|JPAT ignores the mask|s/wire       pat_match = (((pat_win ^ pat_exp) \& cfg_pat_mask) == 8'd0);/wire       pat_match = ((pat_win ^ pat_exp) == 8'd0);/"
  "pesm_core.v|JPAT always compares against PAT_VAL|s/wire \[7:0\] pat_exp   = instr\[11\] ? x : cfg_pat_val;/wire [7:0] pat_exp   = cfg_pat_val;/"
  "pesm_core.v|JPAT window base ignored|s/wire \[7:0\] pat_win   = rotr16_lo8(in_vec, instr\[9:6\]);/wire [7:0] pat_win   = in_vec[7:0];/"
  "pesm_core.v|bgclk divider off by one|s/if (bg_cnt == 8'd0) begin/if (bg_cnt == 8'd1) begin/"
  "pesm_core.v|bgclk reset flag ignored|s/if (instr\[5\]) begin/if (1'b0) begin/"
  "pesm_core.v|bgclk free-runs when stopped|s/if (bg_run \& tick) begin/if (tick) begin/"
  "pesm_core.v|input pin 15 is not the bgclk level|s/wire \[15:0\] in_vec = {bg_q, hflag,/wire [15:0] in_vec = {1'b0, hflag,/"
  "pesm_core.v|stalled instruction still advances the prefetch|s/wire        adv        = boot | (exec \& ~stall);/wire        adv        = boot | exec;/"
  "pesm_core.v|x-- branch decision uses the decremented value|s/3'd2:    jcond = (x != 8'd0);/3'd2:    jcond = (x != 8'd1);/"
  "pesm_host.v|imem\/cfg write gate ignores boot_pre|s/wire wr_ok = boot \& boot_pre;/wire wr_ok = boot;/"
  "pesm_host.v|read port not handed to the core in the last BOOT cycle|s/assign rd_addr = (boot \& boot_pre) ? addr : rd_pc;/assign rd_addr = boot ? addr : rd_pc;/"
  "pesm_host.v|read mux level 2 uses the wrong address bits|s/.s(rd_addr\[3:2\]), .y(rd_l2\[gw\]\[gb\])/.s(rd_addr[2:1]), .y(rd_l2[gw][gb])/"
  "pesm_host.v|READ_IMEM decoded with a 5-bit address|s/3'b01?:  dec_op = 3'b001;/3'b01?:  begin dec_op = 3'b001; dec_addr = {1'b0, rx_byte[4:0]}; end/"
  "pesm_host.v|cfg 19 (BGCLK_DIV) not writable|s/5'd19: r_bg_div    <= rx_byte;/5'd19: ;/"
  "pesm_host.v|host readback register samples only in RUN|s/else        imem_rd <= rd_instr;/else if (!boot) imem_rd <= rd_instr;/"
  "tt_um_protocol_engine.v|bgclk drives its pin even when not enabled|s/if (cfg_bg_en \&\& cfg_bg_pin != 4'd15) out_pad\[cfg_bg_pin\] = bg_q;/if (cfg_bg_pin != 4'd15) out_pad[cfg_bg_pin] = bg_q;/"
)

worker() {
  local i=$1 file name expr work n
  IFS='|' read -r file name expr <<< "${MUT[$i]}"
  work=$(mktemp -d)
  mkdir -p "$work/pesm"
  cp -r "$HERE/../src" "$HERE/../sw" "$HERE/../test" "$HERE/../firmware" "$work/pesm/"
  cd "$work/pesm" || exit 3
  sed -i "$expr" "src/$file"
  if cmp -s "src/$file" "$HERE/../src/$file"; then
    echo "INVALID (did not apply): $name"; rm -rf "$work"; exit 2
  fi
  (cd test && rm -rf sim_build results.xml &&
   timeout 1500 make COCOTB_TEST_MODULES=test,test_toolchain,test_crv >/dev/null 2>&1)
  if [ ! -f test/results.xml ]; then
    # no result file: build error (invalid mutant) or the suite hung past the timeout
    if [ -f test/sim_build/rtl/sim.vvp ]; then echo "KILLED  (suite timed out): $name"; rm -rf "$work"; exit 0; fi
    echo "INVALID (mutant does not build): $name"; rm -rf "$work"; exit 2
  fi
  n=$(grep -c "<failure" test/results.xml || true)
  rm -rf "$work"
  if [ "${n:-0}" -gt 0 ]; then echo "KILLED  ($n failing tests): $name"; exit 0; fi
  echo "ESCAPED: $name"; exit 1
}

if [ $# -ge 1 ]; then worker "$1"; fi

JOBS=${JOBS:-2}
seq 0 $((${#MUT[@]} - 1)) | xargs -P "$JOBS" -I{} "$0" {} | tee /tmp/pesm_mutate.$$.log
killed=$(grep -c "^KILLED" /tmp/pesm_mutate.$$.log || true)
escaped=$(grep -c "^ESCAPED" /tmp/pesm_mutate.$$.log || true)
invalid=$(grep -c "^INVALID" /tmp/pesm_mutate.$$.log || true)
rm -f /tmp/pesm_mutate.$$.log
echo "mutants: ${#MUT[@]}  killed: $killed  escaped: $escaped  invalid: $invalid"
[ "$invalid" -eq 0 ] || exit 2
exit "$escaped"
