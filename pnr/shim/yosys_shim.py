#!/usr/bin/env python3
# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""yosys shim: 'yosys -y script.py -- args' runs the script with the pip
'pyosys' module (the Yosys version LibreLane's CI uses), everything else is
passed to the system yosys."""
import os
import runpy
import shutil
import sys

argv = sys.argv[1:]
if argv and argv[0] in ("-V", "--version"):
    print("Yosys (pyosys shim)")
    sys.exit(0)
if "-y" not in argv:
    here = os.path.dirname(os.path.abspath(sys.argv[0]))
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                           if os.path.abspath(p) != os.environ.get("OR_SHIM_BIN", here))
    real = shutil.which("yosys", path=path)
    if not real:
        sys.exit("yosys_shim: no system yosys on PATH")
    os.execv(real, [real] + argv)
i = argv.index("-y")
script = argv[i + 1]
rest = argv[i + 2:]
if rest and rest[0] == "--":
    rest = rest[1:]
sys.argv = [script] + rest
sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
runpy.run_path(script, run_name="__main__")
