#!/usr/bin/env python3
# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""
openroad / sta command-line shim on top of the pip 'openroad' bindings, so
that LibreLane can run without the Nix/Docker tool bundle.

Accepts the subset of the OpenROAD CLI that LibreLane uses:
  openroad [-exit] [-no_splash] [-threads N] [-metrics FILE] script.tcl
  openroad [-exit] [-no_splash] [-metrics FILE] -python script.py [args...]
  sta      [-no_splash] [-exit] script.tcl          (OR_SHIM_MODE=sta)

Each invocation also drops a _shim_replay_<pid>.sh into the step directory
that re-runs exactly that step by hand.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    argv = sys.argv[1:]
    metrics = None
    threads = None
    python_mode = False
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-exit", "-no_splash", "-no_init", "-gui"):
            pass
        elif a == "-threads":
            i += 1
            threads = argv[i]
        elif a == "-metrics":
            i += 1
            metrics = argv[i]
        elif a == "-python":
            python_mode = True
        elif a in ("-version", "--version"):
            print("pip-openroad-shim")
            return 0
        else:
            rest = argv[i:]
            break
        i += 1
    if not rest:
        print("or_shim: no script", file=sys.stderr)
        return 2

    import faulthandler
    faulthandler.enable()
    sd = os.environ.get("STEP_DIR")
    if sd and os.path.isdir(sd):
        import shlex
        with open(os.path.join(sd, f"_shim_replay_{os.getpid()}.sh"), "w") as f:
            for k, v in os.environ.items():
                if (k.startswith("_") and k != "_") or k in (
                        "SCRIPTS_DIR", "DESIGN_DIR", "STEP_DIR", "PDK_ROOT", "PDK", "OR_SHIM_MODE"):
                    f.write(f"export {k}={shlex.quote(v)}\n")
            tool = "sta" if os.environ.get("OR_SHIM_MODE") == "sta" else "openroad"
            f.write(" ".join(shlex.quote(a) for a in [tool] + sys.argv[1:]) + "\n")

    import openroad
    import openroad.odb as odb
    import openroad.utl as utl

    sys.modules.setdefault("odb", odb)
    sys.modules.setdefault("utl", utl)

    if python_mode:
        import atexit
        import json
        import runpy

        _metrics = {}

        def _dump():
            if metrics:
                with open(metrics, "w") as f:
                    json.dump(_metrics, f, indent=1)

        atexit.register(_dump)
        utl.metric = lambda k, v: _metrics.__setitem__(k, str(v))
        utl.metric_integer = lambda k, v: _metrics.__setitem__(k, int(v))
        utl.metric_float = lambda k, v: _metrics.__setitem__(k, float(v))
        if not hasattr(utl, "error"):
            def _err(tool, num, msg):
                raise RuntimeError(f"[ERROR {num}] {msg}")
            utl.error = _err

        sys.argv = rest
        sys.path.insert(0, os.path.dirname(os.path.abspath(rest[0])))
        runpy.run_path(rest[0], run_name="__main__")
        return 0

    from openroad import Design, Tech

    tech = Tech()
    design = Design(tech)
    status = os.environ.get("OR_SHIM_STATUS", f"/tmp/or_shim_{os.getpid()}.status")
    if threads and threads not in ("None", "max"):
        pre = f"set_thread_count {threads}\n"
    else:
        pre = f"set_thread_count {os.cpu_count()}\n"
    if metrics:
        pre += f"utl::open_metrics {{{metrics}}}\n"
    post = f"catch {{utl::close_metrics {{{metrics}}}}} ::or_shim_cm\n" if metrics else ""
    pre += "source {%s}\n" % os.path.join(HERE, "compat.tcl")
    if os.environ.get("OR_SHIM_MODE") == "sta":
        # LibreLane tells OpenSTA from OpenROAD by [namespace exists ::ord];
        # OpenROAD's link_design also needs the LEF masters.
        pre += """
if {[info exists ::env(_TCL_ENV_IN)]} { source $::env(_TCL_ENV_IN) }
if {[info exists ::env(TECH_LEF)]} {
    read_lef $::env(TECH_LEF)
    foreach l $::env(CELL_LEFS) { read_lef $l }
    if {[info exists ::env(MACRO_LEFS)]} { foreach l $::env(MACRO_LEFS) { read_lef $l } }
    if {[info exists ::env(EXTRA_LEFS)]} { foreach l $::env(EXTRA_LEFS) { read_lef $l } }
}
rename namespace ::or_shim_namespace
proc namespace {args} {
    if {[lindex $args 0] eq "exists" && [lindex $args 1] eq "::ord"} { return 0 }
    return [uplevel 1 [list ::or_shim_namespace {*}$args]]
}
"""
    script = rest[0]
    tcl = f"""
{pre}
set ::or_shim_rc 0
rename exit ::or_shim_real_exit
proc exit {{{{code 0}}}} {{ return -code error -errorcode [list OR_SHIM_EXIT $code] "exit $code" }}
if {{[catch {{source {{{script}}}}} ::or_shim_err ::or_shim_opts]}} {{
    set ec [dict get $::or_shim_opts -errorcode]
    if {{[lindex $ec 0] eq "OR_SHIM_EXIT"}} {{
        set ::or_shim_rc [lindex $ec 1]
    }} else {{
        puts stderr $::errorInfo
        set ::or_shim_rc 1
    }}
}}
{post}
set fp [open {{{status}}} w]; puts $fp $::or_shim_rc; close $fp
"""
    sys.stdout.flush()
    design.evalTclString(tcl)
    sys.stdout.flush()
    try:
        rc = int(open(status).read().strip())
        os.unlink(status)
    except Exception:
        rc = 1
    # skip interpreter teardown: the bindings can crash in their destructors
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)


if __name__ == "__main__":
    sys.exit(main())
