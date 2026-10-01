# Timing constraints mirroring the LibreLane/TT defaults for IHP SG13G2.
create_clock -name clk -period 20.0 [get_ports clk]
set_clock_uncertainty 0.25 [get_clocks clk]
set_clock_transition 0.15 [get_clocks clk]
set_timing_derate -early 0.95
set_timing_derate -late 1.05
set in_ports [delete_from_list [all_inputs] [get_ports clk]]
set_input_delay  4.0 -clock clk $in_ports
set_output_delay 4.0 -clock clk [all_outputs]
set_driving_cell -lib_cell sg13g2_buf_4 -pin X $in_ports
set_load 0.006 [all_outputs]
set_max_fanout 10 [current_design]
