#!/usr/bin/env python3
"""Run flow.tcl inside the OpenROAD Python bindings (pip package 'openroad')."""
import os, sys
from openroad import Tech, Design
t = Tech()
d = Design(t)
flow = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flow.tcl")
d.evalTclString(
    f"if {{[catch {{source {flow}}} err]}} {{ puts \"TCL ERROR: $::errorInfo\"; set ::pesm_fail 1 }} else {{ set ::pesm_fail 0 }}"
)
d.evalTclString("set fh [open $::env(OUT)/flow.status w]; puts $fh $::pesm_fail; close $fh")
sys.stdout.flush()
os._exit(int(open(os.path.join(os.environ["OUT"], "flow.status")).read().strip() or 1))
