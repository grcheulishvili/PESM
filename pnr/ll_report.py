#!/usr/bin/env python3
# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""
Summarize a LibreLane run directory (local replay or the runs/wokwi folder
from a Tiny Tapeout GDS_logs artifact):

    python3 pnr/ll_report.py <run dir> [--steps] [--check SETUP_SLOW_NS]

Prints the signoff table (setup/hold worst slack and slew/cap/fanout
violation counts per corner, from the post-PnR STA reports), routing and
antenna results, cell/area numbers, and optionally the per-step metric
changes. With --check, exits 1 unless slow-corner setup slack exceeds the
given value, every hold slack is positive and there are no routing DRC or
antenna violations.
"""
import glob
import json
import os
import re
import sys

CORNERS = ["nom_typ_1p20V_25C", "nom_slow_1p08V_125C", "nom_fast_1p32V_m40C"]
STEP_KEYS = [("timing__setup__ws", "setup_ws"), ("timing__hold__ws", "hold_ws"),
             ("design__instance__count", "cells"), ("design__instance__area", "area"),
             ("route__wirelength__estimated", "est_wl"), ("global_route__wirelength", "grt_wl"),
             ("route__wirelength", "drt_wl"),
             ("design__instance__count__setup_buffer", "setup_buf"),
             ("design__instance__count__hold_buffer", "hold_buf"),
             ("route__drc_errors", "route_drc"), ("antenna__violating__nets", "antenna_nets")]


def first_slack(path):
    try:
        for line in open(path):
            if "slack" in line:
                return float(line.split()[0])
    except OSError:
        pass
    return None


def count(path, what):
    try:
        m = re.search(rf"max {what} violation count (\d+)", open(path).read())
        return int(m.group(1)) if m else None
    except OSError:
        return None


def step_dirs(run):
    return sorted(d for d in os.listdir(run) if d[:2].isdigit())


def metrics_of(run, step):
    p = os.path.join(run, step, "state_out.json")
    return json.load(open(p))["metrics"] if os.path.exists(p) else None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    run = args[0]
    check = None
    if "--check" in sys.argv:
        check = float(sys.argv[sys.argv.index("--check") + 1])
        args = [a for a in args if a != sys.argv[sys.argv.index("--check") + 1]]
    steps = step_dirs(run)
    if "--steps" in sys.argv:
        last = {}
        for d in steps:
            m = metrics_of(run, d)
            if m is None:
                print(f"{d:44s} (no result)")
                continue
            chg = {}
            for k, s in STEP_KEYS:
                if k in m and last.get(k) != m[k]:
                    chg[s] = round(m[k], 3) if isinstance(m[k], float) else m[k]
                    last[k] = m[k]
            if chg:
                print(f"{d:44s} {chg}")
        print()

    sta = [d for d in steps if d.endswith("stapostpnr")]
    final = {}
    for d in reversed(steps):
        final = metrics_of(run, d) or {}
        if final:
            break
    ok = True
    if not sta:
        print("no post-PnR STA step in this run")
        return 1
    sd = os.path.join(run, sta[-1])
    print(f"{'corner':22s} {'setup ws':>9s} {'hold ws':>9s} {'slew':>5s} {'cap':>4s} {'fanout':>6s}")
    res = {}
    for c in CORNERS:
        su = first_slack(os.path.join(sd, c, "max.rpt"))
        ho = first_slack(os.path.join(sd, c, "min.rpt"))
        chk = os.path.join(sd, c, "checks.rpt")
        res[c] = (su, ho)
        fmt = lambda v: "      n/a" if v is None else f"{v:9.3f}"   # noqa: E731
        print(f"{c:22s} {fmt(su)} {fmt(ho)} {count(chk, 'slew')!s:>5s} {count(chk, 'cap')!s:>4s} "
              f"{count(chk, 'fanout')!s:>6s}")
    drc = final.get("route__drc_errors")
    ant = final.get("antenna__violating__nets")
    # cell count / area before filler insertion
    pre_fill = {}
    for d in steps:
        if d.endswith("fillinsertion"):
            break
        pre_fill = metrics_of(run, d) or pre_fill
    core = final.get("design__core__area")
    area = pre_fill.get("design__instance__area")
    util = f" ({100 * area / core:.1f} % of the core)" if area and core else ""
    print(f"routing DRC errors: {drc}   antenna violating nets: {ant}")
    print(f"cells (no fill): {pre_fill.get('design__instance__count')}   "
          f"area: {area} um^2{util}   routed wirelength: {final.get('route__wirelength')} um")
    for k in ("magic__drc_error__count", "design__lvs_error__count",
              "klayout__drc_error__count"):
        if k in final:
            print(f"{k}: {final[k]}")
    rt = None
    drt = [d for d in steps if d.endswith("detailedrouting")]
    if drt:
        try:
            rt = open(os.path.join(run, drt[-1], "runtime.txt")).read().strip()
        except OSError:
            pass
    print(f"detailed-routing runtime: {rt}")
    if check is not None:
        slow = res["nom_slow_1p08V_125C"][0]
        ok = (slow is not None and slow > check and all(v[1] is not None and v[1] > 0 for v in res.values())
              and drc == 0 and ant == 0)
        print(f"check (slow setup > {check} ns, hold > 0, route DRC 0, antenna 0): "
              f"{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
