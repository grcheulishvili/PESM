# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""
Unified PESM pipeline: source -> image -> flash -> run -> exchange data.

Source may be:
  * a PESMProgram instance (Python DSL) or an Image,
  * a .pasm/.asm text file,
  * a .py file defining `program` (PESMProgram) or `build()` returning one,
  * a .bin (156-byte image) or .json image.

    python -m pesm.run_pipeline firmware/uart_tx.pasm --backend ftdi --tx "hello\\n"
    python -m pesm.run_pipeline sw/examples/crc16_usb.py --backend emulator \\
        --tx-hex "31 32 33" --hflag --wait-halt --rx 2
    python -m pesm.run_pipeline firmware/uart_rx.pasm --backend ftdi --monitor 5
    python -m pesm.run_pipeline prog.py --save build/prog --no-flash      # artifacts only

API:
    from pesm.run_pipeline import run_pipeline
    res = run_pipeline(program, transport, tx=b"\\x55", rx=1)
    print(res.status, res.rx)
    res = run_pipeline(program, transport, monitor=2.0, on_status=print, on_rx=print)
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Union

from . import isa
from .builder import PESMProgram
from .programmer import (Programmer, Transport, add_transport_args, load_image,
                         transport_from_args)
from .hostproto import Status

Source = Union[str, PESMProgram, isa.Image]


@dataclass
class PipelineResult:
    image: isa.Image
    status: Status
    rx: List[int] = field(default_factory=list)
    history: List[Status] = field(default_factory=list)   # status changes seen by the monitor
    files: List[str] = field(default_factory=list)        # artifacts written (save=)


def compile_source(source: Source) -> isa.Image:
    if isinstance(source, isa.Image):
        return source
    if isinstance(source, PESMProgram):
        return source.compile()
    path = str(source)
    if path.endswith(".py"):
        spec = importlib.util.spec_from_file_location(
            os.path.splitext(os.path.basename(path))[0], path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        prog = getattr(mod, "program", None)
        if prog is None and hasattr(mod, "build"):
            prog = mod.build()
        if not isinstance(prog, (PESMProgram, isa.Image)):
            raise SystemExit(f"{path}: define `program` or `build()` returning a PESMProgram")
        return compile_source(prog)
    return load_image(path)


def run_pipeline(source: Source, transport: Optional[Transport], tx: Sequence[int] = b"",
                 rx: Optional[int] = None, hflag: bool = False, wait_halt: bool = False,
                 run_time: float = 0.0, timeout: float = 1.0, verify: bool = True,
                 monitor: Optional[float] = None, interval: float = 0.01,
                 on_status: Optional[Callable[[Status], None]] = None,
                 on_rx: Optional[Callable[[List[int]], None]] = None,
                 save: Optional[str] = None, reset: bool = False, log=None) -> PipelineResult:
    """Compile, optionally save all artifacts, load (verified), start, feed TX,
    optionally set the host flag, then either

      * monitor=seconds: poll status and drain the RX FIFO for that long
        (ends early on HALT when wait_halt is set), reporting through
        on_status / on_rx, or
      * wait (run_time, wait_halt) and collect RX bytes: rx=N waits for N
        bytes, rx=None reads what is queued, rx=0 reads nothing.

    transport=None compiles (and saves) only."""
    img = compile_source(source)
    files: List[str] = []
    if save:
        from .assembler import write_all
        files = write_all(img, save)
    if transport is None:
        return PipelineResult(image=img, status=None, files=files)
    p = Programmer(transport, log=log)
    if reset:
        p.reset_chip()
    p.load(img, verify=verify)
    p.run()
    if tx:
        p.write_tx(list(tx), timeout=timeout)
    if run_time:
        transport.delay(run_time)
    if hflag:
        p.set_hflag(True)
    if monitor is not None:
        m = p.monitor(monitor, interval, until_halt=wait_halt, on_status=on_status, on_rx=on_rx)
        return PipelineResult(image=img, status=m.final, rx=m.rx, history=m.statuses,
                              files=files)
    if wait_halt:
        p.wait_halt(timeout=timeout)
    data = p.read_rx(rx, timeout=timeout) if rx != 0 else []
    return PipelineResult(image=img, status=p.status(), rx=data, files=files)


def _parse_tx(a) -> bytes:
    if a.tx_hex:
        return bytes(int(x, 16) for x in a.tx_hex.replace(",", " ").split())
    if a.tx:
        return a.tx.encode().decode("unicode_escape").encode("latin-1")
    return b""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("--tx", help="text to send to the TX FIFO (escapes allowed)")
    ap.add_argument("--tx-hex", help="hex bytes to send, e.g. '55 aa 01'")
    ap.add_argument("--rx", type=int, default=None,
                    help="number of RX bytes to wait for (default: read what is queued)")
    ap.add_argument("--hflag", action="store_true", help="set the host flag after TX")
    ap.add_argument("--wait-halt", action="store_true")
    ap.add_argument("--run-time", type=float, default=0.0, help="seconds to let it run")
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--listing", action="store_true", help="print the compiled listing")
    ap.add_argument("--monitor", type=float, metavar="SECONDS",
                    help="poll status and drain RX for this long, printing every change")
    ap.add_argument("--interval", type=float, default=0.01, help="monitor poll interval (s)")
    ap.add_argument("--save", metavar="BASE",
                    help="also write BASE.bin/.mem/_cfg.mem/.py/.json/.lst/.asm")
    ap.add_argument("--no-flash", action="store_true", help="compile (and --save) only")
    ap.add_argument("--reset", action="store_true", help="pulse rst_n before loading")
    add_transport_args(ap)
    a = ap.parse_args(argv)
    try:
        img = compile_source(a.source)
    except isa.IsaError as e:
        print(f"{a.source}: {e}", file=sys.stderr)
        return 1
    if a.listing:
        print(img.listing())
    if a.no_flash:
        res = run_pipeline(img, None, save=a.save)
        for f in res.files:
            print(f)
        return 0

    def show_rx(d):
        print("rx: " + " ".join(f"{b:02x}" for b in d))

    with transport_from_args(a) as t:
        res = run_pipeline(img, t, tx=_parse_tx(a), rx=a.rx, hflag=a.hflag,
                           wait_halt=a.wait_halt, run_time=a.run_time, timeout=a.timeout,
                           verify=not a.no_verify, monitor=a.monitor, interval=a.interval,
                           on_status=(lambda st: print(f"status: {st}")) if a.monitor else None,
                           on_rx=show_rx if a.monitor else None, save=a.save, reset=a.reset,
                           log=print)
    for f in res.files:
        print(f)
    print(f"status: {res.status}")
    if res.rx and not a.monitor:
        show_rx(res.rx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
