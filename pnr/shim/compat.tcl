# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
#
# Compatibility layer for the pip 'openroad' bindings: the ifp:: Tcl commands
# crash there (they use the process-wide OpenRoad singleton, which the Python
# Design/Tech objects do not populate). Re-implement the three that LibreLane's
# floorplan step uses on top of odb / read_def.
proc initialize_floorplan {args} {
    sta::parse_key_args "initialize_floorplan" args \
        keys {-utilization -aspect_ratio -core_space -die_area -core_area -site \
              -additional_sites -row_parity -flip_sites} flags {}
    if {![info exists keys(-die_area)] || ![info exists keys(-core_area)]} {
        utl::error IFP 9001 "shim: only -die_area/-core_area floorplans are supported"
    }
    set db [ord::get_db]
    set dbu [[$db getTech] getDbUnitsPerMicron]
    set site ""
    foreach lib [$db getLibs] {
        set s [$lib findSite $keys(-site)]
        if {$s ne "NULL"} { set site $s }
    }
    if {$site eq ""} { utl::error IFP 9002 "shim: site $keys(-site) not found" }
    set sw [$site getWidth]
    set sh [$site getHeight]
    lassign $keys(-die_area) dlx dly dux duy
    lassign $keys(-core_area) clx cly cux cuy
    foreach v {dlx dly dux duy clx cly cux cuy} { set $v [expr {round([set $v] * $dbu)}] }
    set nrows [expr {($cuy - $cly) / $sh}]
    set nsites [expr {($cux - $clx) / $sw}]
    set block [ord::get_db_block]
    set tmp [file join $::env(STEP_DIR) _shim_floorplan.def]
    set fp [open $tmp w]
    puts $fp "VERSION 5.8 ;\nDIVIDERCHAR \"/\" ;\nBUSBITCHARS \"\[\]\" ;"
    puts $fp "DESIGN [$block getName] ;\nUNITS DISTANCE MICRONS $dbu ;"
    puts $fp "DIEAREA ( $dlx $dly ) ( $dux $duy ) ;"
    for {set r 0} {$r < $nrows} {incr r} {
        set y [expr {$cly + $r * $sh}]
        set o [expr {$r % 2 ? "FS" : "N"}]
        puts $fp "ROW ROW_$r $keys(-site) $clx $y $o DO $nsites BY 1 STEP $sw 0 ;"
    }
    puts $fp "END DESIGN"
    close $fp
    read_def -floorplan_initialize $tmp
    utl::info IFP 1 "Added $nrows rows of $nsites site $keys(-site)."
}

proc make_tracks {args} {
    sta::parse_key_args "make_tracks" args \
        keys {-x_pitch -y_pitch -x_offset -y_offset} flags {}
    set lname [lindex $args 0]
    set db [ord::get_db]
    set tech [$db getTech]
    set dbu [$tech getDbUnitsPerMicron]
    set layer [$tech findLayer $lname]
    set block [ord::get_db_block]
    set die [$block getDieArea]
    set hw [expr {[$layer getWidth] / 2}]
    set grid [$block findTrackGrid $layer]
    if {$grid eq "NULL"} { set grid [odb::dbTrackGrid_create $block $layer] }
    foreach axis {x y} lo [list [$die xMin] [$die yMin]] hi [list [$die xMax] [$die yMax]] {
        set pitch [expr {round($keys(-${axis}_pitch) * $dbu)}]
        set org [expr {$lo + round($keys(-${axis}_offset) * $dbu)}]
        if {$org - $hw < $lo} { set org [expr {$org + $pitch}] }
        set cnt [expr {($hi - $hw - $org) / $pitch + 1}]
        if {$axis eq "x"} { $grid addGridPatternX $org $cnt $pitch } else { $grid addGridPatternY $org $cnt $pitch }
    }
}

proc insert_tiecells {args} {
    # the synthesized netlist is already hilomap'd; CI reports 0 inserted
    utl::info IFP 30 "shim: insert_tiecells skipped ([lindex $args 0])."
}

# global_connect in this build takes no flags
if {[info commands ::or_shim_global_connect] eq ""} {
    rename global_connect ::or_shim_global_connect
    proc global_connect {args} { ::or_shim_global_connect }
}

# older repair_antennas: no -allow_congestion / -jumper_only / -diode_only
if {[info commands ::or_shim_repair_antennas] eq ""} {
    rename repair_antennas ::or_shim_repair_antennas
    proc repair_antennas {args} {
        set a {}
        foreach x $args { if {$x ni {-allow_congestion -jumper_only -diode_only}} { lappend a $x } }
        return [::or_shim_repair_antennas {*}$a]
    }
}
