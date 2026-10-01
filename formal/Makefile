# Formal verification of the PESM RTL (SymbiYosys).
#   unbounded (k-induction / PDR): fifo, clkdiv (full 16.8 width), host, core
#   bounded (BMC, depth 40):       top-level cross-block handshakes
# Needs: yosys >= 0.33, sby, z3 >= 4.12 (older z3 4.8 hangs smtbmc), abc.

JOBS = fifo:prove fifo:cover clkdiv:prove clkdiv:cover host:prove host:cover \
       core:prove core:bmc core:cover top:bmc

all:
	@set -e; for j in $(JOBS); do \
	  f=$${j%%:*}; t=$${j##*:}; \
	  printf '%-14s ' "$$f:$$t"; \
	  sby -f $$f.sby $$t > /dev/null 2>&1 && grep -h "DONE" $${f}_$$t/logfile.txt | sed 's/.*DONE/DONE/' || { echo FAIL; exit 1; }; \
	done

clean:
	rm -rf *_prove *_bmc *_cover

.PHONY: all clean
