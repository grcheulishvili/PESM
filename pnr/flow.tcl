# PESM physical implementation with OpenROAD, following the LibreLane step
# order and the IHP / Tiny Tapeout settings:
#   floorplan from the TT 6x4 DEF template -> PDN -> global placement ->
#   resize -> detailed placement -> CTS -> setup/hold repair -> global route
#   -> detailed route -> antenna -> fill -> OpenRCX -> multi-corner STA.
#
# Env: PDK_ROOT, PDK (ihp-sg13cmos5l | ihp-sg13g2), NETLIST, DEF_TEMPLATE, OUT,
#      [DENSITY=0.60] [STA_ONLY=1: extraction + STA on $OUT/routed.odb]

set pdkname $::env(PDK)
set sclp [string range $pdkname 4 end]         ;# sg13cmos5l | sg13g2
set pdk  $::env(PDK_ROOT)/$pdkname
set scl  $pdk/libs.ref/${sclp}_stdcell
set out  $::env(OUT)
set density [expr {[info exists ::env(DENSITY)] ? $::env(DENSITY) : 0.60}]
file mkdir $out

# Per-PDK settings, mirroring the PDK's librelane/config.tcl and the Tiny
# Tapeout overrides (RT_MAX_LAYER, FP_PDN_VPITCH/VWIDTH, FP_PDN_MULTILAYER=0).
if {$pdkname eq "ihp-sg13cmos5l"} {
    # CMOS5L: Metal1..Metal4 + TopMetal1. TT routes signals up to Metal4 and
    # puts the vertical power straps on Metal4 as well.
    set rt_layers   Metal2-Metal4
    set pdn_layer   Metal4
    set pdn_width   2.1
    set pdn_pitch   50.0
    set pdn_offset  10.0
    set pdn_spacing 2.0
    set wire_sig    Metal3
    set wire_clk    Metal3
    set cts_bufs    [list ${sclp}_buf_8 ${sclp}_buf_4 ${sclp}_buf_2 ${sclp}_buf_1]
    set rcx_rules   $pdk/libs.tech/librelane/IHP_rcx_patterns.rules
    set layer_rc    {}                         ;# LAYERS_RC empty: use the tech LEF values
} else {
    set rt_layers   Metal2-TopMetal1
    set pdn_layer   TopMetal1
    set pdn_width   2.2
    set pdn_pitch   38.87
    set pdn_offset  13.6
    set pdn_spacing 4.0
    set wire_sig    Metal3
    set wire_clk    Metal4
    set cts_bufs    [list ${sclp}_buf_8 ${sclp}_buf_4 ${sclp}_buf_2]
    set rcx_rules   $pdk/libs.tech/librelane/openrcx/IHP_rcx_patterns.rules
    set layer_rc    {Metal1 8.54576E-03 1e-10  Metal2 2.53519E-03 1.69121E-04
                     Metal3 1.54329E-03 1.82832E-04  Metal4 6.31424E-04 1.66454E-04
                     Metal5 6.84051E-04 8.57431E-05}
}

proc banner {s} { puts "\n================ $s ================" }

set sta_only [expr {[info exists ::env(STA_ONLY)] && $::env(STA_ONLY)}]
set ::pesm_scl $sclp

# ------------------------------------------------------------- libraries
if {!$sta_only} {
    read_lef $scl/lef/${sclp}_tech.lef
    read_lef $scl/lef/${sclp}_stdcell.lef
}
define_corners typ slow fast
read_liberty -corner typ  $scl/lib/${sclp}_stdcell_typ_1p20V_25C.lib
read_liberty -corner slow $scl/lib/${sclp}_stdcell_slow_1p08V_125C.lib
read_liberty -corner fast $scl/lib/${sclp}_stdcell_fast_1p32V_m40C.lib

if {$sta_only} {
    # STA_ONLY=1: re-run extraction + STA on the routed checkpoint
    read_db $out/routed.odb
} else {
    read_verilog $::env(NETLIST)
    link_design tt_um_protocol_engine
}
read_sdc [file join [file dirname [info script]] pesm.sdc]

foreach c {lgcp_1 sighold slgcp_1 sdfbbp_1 dfrbp_2 buf_16 inv_16} {
    set_dont_use ${sclp}_$c
}

foreach {l r c} $layer_rc { set_layer_rc -layer $l -resistance $r -capacitance $c }
set_wire_rc -signal -layer $wire_sig
set_wire_rc -clock  -layer $wire_clk

if {$sta_only} {
    set_propagated_clock [all_clocks]
} else {
# ------------------------------------------------------------- floorplan
banner FLOORPLAN
read_def -floorplan_initialize $::env(DEF_TEMPLATE)
remove_buffers

# ------------------------------------------------------------- PDN
# TT: FP_PDN_MULTILAYER=0 -> Metal1 follow-pin rails + vertical straps only
banner PDN
add_global_connection -net VPWR -pin_pattern {^VDD$} -power
add_global_connection -net VGND -pin_pattern {^VSS$} -ground
global_connect
set_voltage_domain -power VPWR -ground VGND
define_pdn_grid -name core -starts_with POWER
add_pdn_stripe -grid core -layer Metal1 -width 0.44 -followpins
add_pdn_stripe -grid core -layer $pdn_layer -width $pdn_width -pitch $pdn_pitch \
    -offset $pdn_offset -spacing $pdn_spacing
add_pdn_connect -grid core -layers [list Metal1 $pdn_layer]
pdngen

# ------------------------------------------------------------- placement
banner GLOBAL_PLACEMENT
set_routing_layers -signal $rt_layers -clock $rt_layers
global_placement -density $density -timing_driven -routability_driven -pad_left 0 -pad_right 0
estimate_parasitics -placement
banner RESIZE
repair_design
repair_tie_fanout ${sclp}_tiehi/L_HI
repair_tie_fanout ${sclp}_tielo/L_LO
detailed_placement
check_placement -verbose
estimate_parasitics -placement
report_worst_slack -max

# ------------------------------------------------------------- CTS
banner CTS
clock_tree_synthesis -root_buf ${sclp}_buf_8 -buf_list $cts_bufs \
    -sink_clustering_enable -sink_clustering_size 8
set_propagated_clock [all_clocks]
estimate_parasitics -placement
repair_clock_nets
detailed_placement
estimate_parasitics -placement
banner REPAIR_TIMING_CTS
# ask for 3 ns of setup margin at the worst corner, not just >= 0
repair_timing -setup -setup_margin 3.0 -max_utilization 40
repair_timing -hold -hold_margin 0.15
detailed_placement
check_placement -verbose
estimate_parasitics -placement
report_clock_skew > $out/cts_skew.rpt

# ------------------------------------------------------------- routing
banner GLOBAL_ROUTE
set_global_routing_layer_adjustment $rt_layers 0.0
global_route -allow_congestion -congestion_report_file $out/grt_congestion.rpt
estimate_parasitics -global_routing
repair_design
repair_timing -setup -setup_margin 2.0 -max_utilization 40
repair_timing -hold -hold_margin 0.12
repair_design
# antenna: diodes (<scl>_antennanp), 20 % ratio margin, as LibreLane
repair_antennas ${sclp}_antennanp -iterations 5 -ratio_margin 20
detailed_placement
global_route -allow_congestion
estimate_parasitics -global_routing
report_worst_slack -max
report_worst_slack -min

banner DETAILED_ROUTE
set_thread_count 2
detailed_route -output_drc $out/drt_drc.rpt -droute_end_iter 64 -verbose 0
# post-route antenna repair loop (as ORFS detail_route.tcl): insert diodes on
# nets still violating, then incrementally re-route
for {set i 0} {$i < 5} {incr i} {
    if {![check_antennas]} { break }
    banner "ANTENNA_REPAIR_POST_DRT $i"
    repair_antennas ${sclp}_antennanp -iterations 1 -ratio_margin 10
    detailed_route -output_drc $out/drt_drc.rpt -droute_end_iter 64 -verbose 0
}
check_antennas -report_file $out/antenna.rpt

banner FILL
filler_placement [list ${sclp}_decap_8 ${sclp}_decap_4 ${sclp}_fill_8 ${sclp}_fill_4 ${sclp}_fill_2 ${sclp}_fill_1]
check_placement

}

# checkpoint: `STA_ONLY=1` re-runs extraction + STA from here without re-routing
if {!$sta_only} { write_db $out/routed.odb }

# ------------------------------------------------------------- signoff-ish STA
banner RCX
define_process_corner -ext_model_index 0 X
extract_parasitics -ext_model_file $rcx_rules
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
