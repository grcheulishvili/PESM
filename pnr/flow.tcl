# PESM physical implementation with OpenROAD, following the LibreLane step
# order and the IHP SG13G2 / Tiny Tapeout settings:
#   floorplan from the TT 6x4 DEF template -> PDN -> global placement ->
#   resize -> detailed placement -> CTS -> setup/hold repair -> global route
#   -> detailed route -> antenna -> fill -> OpenRCX -> multi-corner STA.
#
# Env: PDK_ROOT, NETLIST, DEF_TEMPLATE, OUT, [DENSITY=0.60]

set pdk  $::env(PDK_ROOT)/ihp-sg13g2
set scl  $pdk/libs.ref/sg13g2_stdcell
set out  $::env(OUT)
set density [expr {[info exists ::env(DENSITY)] ? $::env(DENSITY) : 0.60}]
file mkdir $out

proc banner {s} { puts "\n================ $s ================" }
proc ws {c m} { return [sta::worst_slack_corner [sta::find_corner $c] $m] }

# ------------------------------------------------------------- libraries
read_lef $scl/lef/sg13g2_tech.lef
read_lef $scl/lef/sg13g2_stdcell.lef
define_corners typ slow fast
read_liberty -corner typ  $scl/lib/sg13g2_stdcell_typ_1p20V_25C.lib
read_liberty -corner slow $scl/lib/sg13g2_stdcell_slow_1p08V_125C.lib
read_liberty -corner fast $scl/lib/sg13g2_stdcell_fast_1p32V_m40C.lib

read_verilog $::env(NETLIST)
link_design tt_um_protocol_engine
read_sdc [file join [file dirname [info script]] pesm.sdc]

foreach c {sg13g2_lgcp_1 sg13g2_sighold sg13g2_slgcp_1 sg13g2_sdfbbp_1 sg13g2_dfrbp_2
           sg13g2_buf_16 sg13g2_inv_16} {
    set_dont_use $c
}

# layer / via RC (IHP librelane config.tcl LAYERS_RC, VIAS_R; per um)
set_layer_rc -layer Metal1 -resistance 8.54576E-03 -capacitance 1e-10
set_layer_rc -layer Metal2 -resistance 2.53519E-03 -capacitance 1.69121E-04
set_layer_rc -layer Metal3 -resistance 1.54329E-03 -capacitance 1.82832E-04
set_layer_rc -layer Metal4 -resistance 6.31424E-04 -capacitance 1.66454E-04
set_layer_rc -layer Metal5 -resistance 6.84051E-04 -capacitance 8.57431E-05
set_layer_rc -via Via1 -resistance 2.0E-3
set_layer_rc -via Via2 -resistance 2.0E-3
set_layer_rc -via Via3 -resistance 2.0E-3
set_layer_rc -via Via4 -resistance 2.0E-3
set_wire_rc -signal -layer Metal3
set_wire_rc -clock  -layer Metal4

# ------------------------------------------------------------- floorplan
banner FLOORPLAN
read_def -floorplan_initialize $::env(DEF_TEMPLATE)
remove_buffers

# ------------------------------------------------------------- PDN
# TT: FP_PDN_MULTILAYER=0 -> Metal1 follow-pin rails + vertical TopMetal1 stripes
banner PDN
add_global_connection -net VPWR -pin_pattern {^VDD$} -power
add_global_connection -net VGND -pin_pattern {^VSS$} -ground
global_connect
set_voltage_domain -power VPWR -ground VGND
define_pdn_grid -name core -starts_with POWER
add_pdn_stripe -grid core -layer Metal1 -width 0.44 -followpins
add_pdn_stripe -grid core -layer TopMetal1 -width 2.2 -pitch 38.87 -offset 13.6 -spacing 4.0
add_pdn_connect -grid core -layers {Metal1 TopMetal1}
pdngen

# ------------------------------------------------------------- placement
banner GLOBAL_PLACEMENT
set_routing_layers -signal Metal2-TopMetal1 -clock Metal2-TopMetal1
global_placement -density $density -timing_driven -routability_driven -pad_left 0 -pad_right 0
estimate_parasitics -placement
banner RESIZE
repair_design
repair_tie_fanout sg13g2_tiehi/L_HI
repair_tie_fanout sg13g2_tielo/L_LO
detailed_placement
check_placement -verbose
estimate_parasitics -placement
report_worst_slack -max

# ------------------------------------------------------------- CTS
banner CTS
clock_tree_synthesis -root_buf sg13g2_buf_8 -buf_list {sg13g2_buf_8 sg13g2_buf_4 sg13g2_buf_2} \
    -sink_clustering_enable -sink_clustering_size 8
set_propagated_clock [all_clocks]
estimate_parasitics -placement
repair_clock_nets
detailed_placement
estimate_parasitics -placement
banner REPAIR_TIMING_CTS
# ask for 3 ns of setup margin at the worst corner, not just >= 0
repair_timing -setup -setup_margin 3.0 -max_utilization 40
repair_timing -hold -hold_margin 0.1
detailed_placement
check_placement -verbose
estimate_parasitics -placement
report_clock_skew > $out/cts_skew.rpt

# ------------------------------------------------------------- routing
banner GLOBAL_ROUTE
set_global_routing_layer_adjustment Metal2-TopMetal1 0.0
global_route -allow_congestion -congestion_report_file $out/grt_congestion.rpt
estimate_parasitics -global_routing
repair_timing -setup -setup_margin 2.0 -max_utilization 40
repair_timing -hold -hold_margin 0.05
repair_antennas
detailed_placement
global_route -allow_congestion
estimate_parasitics -global_routing
report_worst_slack -max
report_worst_slack -min

banner DETAILED_ROUTE
set_thread_count 2
detailed_route -output_drc $out/drt_drc.rpt -droute_end_iter 64 -verbose 0
check_antennas -report_file $out/antenna.rpt

banner FILL
filler_placement {sg13g2_decap_8 sg13g2_decap_4 sg13g2_fill_8 sg13g2_fill_4 sg13g2_fill_2 sg13g2_fill_1}
check_placement

# ------------------------------------------------------------- signoff-ish STA
banner RCX
define_process_corner -ext_model_index 0 X
extract_parasitics -ext_model_file $pdk/libs.tech/librelane/openrcx/IHP_rcx_patterns.rules
write_spef $out/pesm.spef
read_spef $out/pesm.spef

banner STA
foreach c {typ slow fast} {
    report_checks -corner $c -path_delay max -fields {slew cap fanout} -digits 3 > $out/sta_${c}_max.rpt
    report_checks -corner $c -path_delay min -fields {slew cap fanout} -digits 3 > $out/sta_${c}_min.rpt
}
report_check_types -max_slew -max_capacitance -max_fanout -violators > $out/drv.rpt
report_clock_skew > $out/final_skew.rpt
report_design_area > $out/area.rpt
report_power -corner typ > $out/power_typ.rpt

write_def $out/pesm_final.def
write_db  $out/pesm_final.odb
write_verilog $out/pesm_final.v
banner DONE
