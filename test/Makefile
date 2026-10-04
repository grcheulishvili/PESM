# Makefile
# See https://docs.cocotb.org/en/stable/quickstart.html for more info

# defaults
SIM ?= icarus
FST ?= -fst
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../src
PROJECT_SOURCES = tt_um_protocol_engine.v pesm_core.v pesm_host.v pesm_fifo.v pesm_clkdiv.v pesm_sync.v pesm_mux4.v

ifneq ($(GATES),yes)

# RTL simulation:
SIM_BUILD				= sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))

else

# Gate level simulation:
SIM_BUILD				= sim_build/gl
COMPILE_ARGS    += -DGL_TEST
COMPILE_ARGS    += -DFUNCTIONAL
COMPILE_ARGS    += -DSIM
# Target PDK: IHP CMOS5L (competition process). PDK=ihp-sg13g2 also works.
PDK ?= ihp-sg13cmos5l
SCL  = $(patsubst ihp-%,%,$(PDK))
VERILOG_SOURCES += $(PDK_ROOT)/$(PDK)/libs.ref/$(SCL)_io/verilog/$(SCL)_io.v
VERILOG_SOURCES += $(PDK_ROOT)/$(PDK)/libs.ref/$(SCL)_stdcell/verilog/$(SCL)_stdcell.v
# IHP keeps the UDPs (ihp_mux2, ...) in a separate file
VERILOG_SOURCES += $(wildcard $(PDK_ROOT)/$(PDK)/libs.ref/$(SCL)_stdcell/verilog/$(SCL)_udp.v)

# this gets copied in by the GDS action workflow
VERILOG_SOURCES += $(PWD)/gate_level_netlist.v

endif

# Allow sharing configuration between design and testbench via `include`:
COMPILE_ARGS 		+= -I$(SRC_DIR)

# Include the testbench sources:
VERILOG_SOURCES += $(PWD)/tb.v
TOPLEVEL = tb

# List test modules to run, separated by commas and without the .py suffix:
#   test      - directed, black-box (runs on RTL and on the gate-level netlist)
#   test_crv  - constrained-random lockstep against sw/pesm/model.py (black-box)
#   test_toolchain - programmer.py over SPI + DSL macros vs device models (black-box)
COCOTB_TEST_MODULES ?= test,test_crv,test_toolchain

# include cocotb's make rules to take care of the simulator setup
include $(shell cocotb-config --makefiles)/Makefile.sim
