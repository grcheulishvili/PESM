#!/usr/bin/env bash
# Mutation smoke test: inject known bugs into pesm_core.v and check that the
# directed suite or the CRV suite fails for each one.
#   ./mutate.sh            (takes ~10 min)
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cp -r "$HERE/.." "$WORK/pesm"
cd "$WORK/pesm"
ORIG=src/pesm_core.v
cp $ORIG /tmp/pesm_core.orig.v

declare -a NAME=(
  "jmp !osre uses <= instead of <"
  "wrap ignored on fall-through"
  "toggle reads side-set-modified value"
  "dlyt ignores tick on first cycle"
  "autopull threshold > instead of >="
  "RX full ignores the pending registered write"
)
declare -a SED=(
  "s/3'd6: cond = (osr_cnt < pull_th);/3'd6: cond = (osr_cnt <= pull_th);/"
  "s/wire \[4:0\] seq_pc = (pc == cfg_wrap_top) ? cfg_wrap_bot : (pc + 5'd1);/wire [4:0] seq_pc = pc + 5'd1;/"
  "s/2'd2: if (pin != 4'd15) out_n\[pin\] = ~out_reg\[pin\];/2'd2: if (pin != 4'd15) out_n[pin] = ~out_n[pin];/"
  "s/if (!(tick \&\& (n16 == 16'd1))) begin/if (!(n16 == 16'd1)) begin/"
  "s/if (cfg_autopull \&\& (osr_cnt >= pull_th)) begin/if (cfg_autopull \&\& (osr_cnt > pull_th)) begin/"
  "s/wire        rx_full_c = rx_full | (rx_push \& (rx_level == 4'd7));/wire        rx_full_c = rx_full;/"
)
escaped=0
for i in "${!NAME[@]}"; do
  cp /tmp/pesm_core.orig.v $ORIG
  sed -i "${SED[$i]}" $ORIG
  if cmp -s $ORIG /tmp/pesm_core.orig.v; then echo "mutation $i did not apply"; exit 2; fi
  (cd test && make clean >/dev/null 2>&1; make COCOTB_TEST_MODULES=test,test_crv >/dev/null 2>&1)
  n=$(grep -c "<failure" test/results.xml || true)
  if [ "$n" -gt 0 ]; then echo "KILLED  ($n failing tests): ${NAME[$i]}";
  else echo "ESCAPED: ${NAME[$i]}"; escaped=$((escaped+1)); fi
done
exit $escaped
