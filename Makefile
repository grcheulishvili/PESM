# Top-level convenience targets.
#   make lint     Verilator -Wall
#   make test     cocotb directed + constrained-random + toolchain (RTL)
#   make sw-test  pytest of the Python toolchain (no simulator)
#   make formal   SymbiYosys proofs
#   make synth    pre-layout IHP SG13G2 synthesis + ABC timing (needs PDK_ROOT)
#   make gl       gate-level cocotb on the yosys netlist (needs PDK_ROOT)
#   make pnr      OpenROAD place/route/STA on the TT 6x4 template (needs PDK_ROOT, TT_TOOLS)
#   make asm      assemble all firmware listings

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
	./synth/make_gl_models.sh synth/gl_models/ihp-sg13g2/libs.ref/sg13g2_stdcell/verilog
	mkdir -p synth/gl_models/ihp-sg13g2/libs.ref/sg13g2_io/verilog
	cp $(PDK_ROOT)/ihp-sg13g2/libs.ref/sg13g2_io/verilog/sg13g2_io.v synth/gl_models/ihp-sg13g2/libs.ref/sg13g2_io/verilog/
	mv synth/gl_models/ihp-sg13g2/libs.ref/sg13g2_stdcell/verilog/sg13g2_stdcell_functional.v \
	   synth/gl_models/ihp-sg13g2/libs.ref/sg13g2_stdcell/verilog/sg13g2_stdcell.v
	cp synth/netlist_typ.v test/gate_level_netlist.v
	cd test && $(MAKE) clean && $(MAKE) GATES=yes PDK_ROOT=$(CURDIR)/synth/gl_models
	python3 -m cocotb_tools.check_results test/results.xml

pnr:
	./pnr/run_pnr.sh

asm:
	@for f in firmware/*.pasm; do echo "== $$f"; python3 tools/pesm_asm.py $$f || exit 1; done

.PHONY: lint test sw-test formal synth gl pnr asm
