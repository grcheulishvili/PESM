# Top-level convenience targets.
#   make lint     Verilator -Wall
#   make test     cocotb directed + constrained-random + toolchain (RTL)
#   make sw-test  pytest of the Python toolchain (no simulator)
#   make formal   SymbiYosys proofs
#   make synth    pre-layout synthesis + ABC timing (needs PDK_ROOT; PDK=ihp-sg13cmos5l default)
#   make gl       gate-level cocotb on the yosys netlist (needs PDK_ROOT, Icarus >= 13)
#   make pnr      OpenROAD place/route/STA on the TT 6x4 template (needs PDK_ROOT, TT_TOOLS)
#   make asm      assemble all firmware listings

# Target process: IHP CMOS5L (ihp-sg13cmos5l). PDK=ihp-sg13g2 is also supported.
PDK ?= ihp-sg13cmos5l
export PDK

SRC = src/tt_um_protocol_engine.v src/pesm_core.v src/pesm_host.v src/pesm_fifo.v src/pesm_clkdiv.v src/pesm_sync.v

lint:
	verilator --lint-only -Wall --top-module tt_um_protocol_engine $(SRC)

test:
	cd test && $(MAKE) clean && $(MAKE)
	python3 -m cocotb_tools.check_results test/results.xml

sw-test:
	cd sw && python3 -m pytest -q tests

formal:
	cd formal && $(MAKE)

synth:
	./synth/run_synth.sh

gl: synth
	cp synth/netlist_typ.v test/gate_level_netlist.v
	cd test && $(MAKE) clean && $(MAKE) GATES=yes PDK=$(PDK) PDK_ROOT=$(PDK_ROOT)
	python3 -m cocotb_tools.check_results test/results.xml

pnr:
	./pnr/run_pnr.sh

asm:
	@for f in firmware/*.pasm; do echo "== $$f"; python3 tools/pesm_asm.py $$f || exit 1; done

.PHONY: lint test sw-test formal synth gl pnr asm
