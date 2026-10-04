# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""
PESM v3 host programmer.

Talks to the chip's framed SPI host port (mode 0, MSB first):

    ui_in[0] HOST_SCK   <- adapter SCK
    ui_in[1] HOST_MOSI  <- adapter MOSI
    ui_in[2] HOST_CS_N  <- adapter CS
    ui_in[3] MODE       <- adapter GPIO   (1 = BOOT: core held, imem/cfg writable)
    uo_out[0] HOST_MISO -> adapter MISO
    rst_n (optional)    <- adapter GPIO

Backends ("transports"):
    ftdi      FT232H/FT2232H via pyftdi       (url ftdi://ftdi:232h/1)
              AD0 SCK, AD1 MOSI, AD2 MISO, AD3 CS, AD4 MODE, AD5 RST_N (optional)
    spidev    Linux spidev + libgpiod for MODE/RST_N (e.g. Raspberry Pi)
    emulator  pure-Python chip emulator (no hardware)

SCK must be <= f_clk/8 (6.25 MHz at 50 MHz). Default 1 MHz.

Programming sequence (Programmer.load + run):
    1. MODE high: core held in reset (BOOT)
    2. CONTROL frame: flush FIFOs, clear flags
    3. READ_STATUS: check the chip ID
    4. CS_N low, WRITE_CFG 0 + 20 bytes, CS_N high
    5. CS_N low, WRITE_IMEM 0 + 64 words, CS_N high
    6. READ_IMEM / READ_CFG over MISO and compare (retry, then VerifyError)
    7. MODE low: the core starts at ENTRY

CLI:
    python -m pesm.programmer prog.pasm --backend ftdi --url ftdi://ftdi:232h/1
    python -m pesm.programmer prog.bin  --backend spidev --spi 0.0 --mode-gpio gpiochip0:17
    python -m pesm.programmer --status --backend ftdi
    python -m pesm.programmer prog.pasm --backend ftdi --monitor 2.0
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Union

from . import hostproto as hp
from . import isa


class ProgrammerError(RuntimeError):
    pass


class VerifyError(ProgrammerError):
    pass


# ===========================================================================
# Transports
# ===========================================================================
class Transport:
    """Byte-level access to the host port. Subclasses implement xfer/set_mode."""

    def xfer(self, mosi: Sequence[int]) -> List[int]:
        """One CS-framed full-duplex transfer; returns the MISO bytes."""
        raise NotImplementedError

    def set_mode(self, boot: bool) -> None:
        raise NotImplementedError

    def set_reset(self, active: bool) -> None:
        """Drive rst_n (optional; default: not wired)."""

    def delay(self, seconds: float) -> None:
        time.sleep(seconds)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FtdiTransport(Transport):
    """FT232H / FT2232H MPSSE (pyftdi). MODE (and optionally RST_N) on GPIO pins."""

    def __init__(self, url: str = "ftdi://ftdi:232h/1", freq: float = 1e6,
                 mode_pin: int = 4, reset_pin: Optional[int] = 5):
        try:
            from pyftdi.spi import SpiController
        except ImportError as e:  # pragma: no cover
            raise ProgrammerError("pyftdi is required: pip install pyftdi") from e
        self._ctrl = SpiController(cs_count=1)
        self._ctrl.configure(url)
        self._port = self._ctrl.get_port(cs=0, freq=freq, mode=0)
        self._gpio = self._ctrl.get_gpio()
        self._mode_mask = 1 << mode_pin
        self._rst_mask = (1 << reset_pin) if reset_pin is not None else 0
        mask = self._mode_mask | self._rst_mask
        self._gpio.set_direction(mask, mask)
        self._state = self._mode_mask | self._rst_mask   # BOOT, not in reset
        self._gpio.write(self._state)

    def xfer(self, mosi: Sequence[int]) -> List[int]:
        return list(self._port.exchange(bytes(mosi), duplex=True))

    def _write(self):
        self._gpio.write(self._state)

    def set_mode(self, boot: bool) -> None:
        self._state = (self._state | self._mode_mask) if boot else (self._state & ~self._mode_mask)
        self._write()
        self.delay(1e-4)

    def set_reset(self, active: bool) -> None:
        if not self._rst_mask:
            return
        self._state = (self._state & ~self._rst_mask) if active else (self._state | self._rst_mask)
        self._write()
        self.delay(1e-3)

    def close(self) -> None:
        self._ctrl.terminate()


class SpidevTransport(Transport):
    """Linux spidev for SPI; libgpiod line(s) for MODE and optional RST_N.

    gpio spec: "gpiochip0:17" (chip:line offset)."""

    def __init__(self, bus: int = 0, dev: int = 0, speed_hz: int = 1_000_000,
                 mode_gpio: str = "gpiochip0:17", reset_gpio: Optional[str] = None):
        try:
            import spidev
        except ImportError as e:  # pragma: no cover
            raise ProgrammerError("spidev is required: pip install spidev") from e
        self._spi = spidev.SpiDev()
        self._spi.open(bus, dev)
        self._spi.mode = 0
        self._spi.max_speed_hz = int(speed_hz)
        self._spi.bits_per_word = 8
        self._mode = self._line(mode_gpio, 1)
        self._rst = self._line(reset_gpio, 1) if reset_gpio else None

    @staticmethod
    def _line(spec: str, initial: int):
        import gpiod
        chip, off = spec.split(":")
        off = int(off)
        path = chip if chip.startswith("/dev/") else f"/dev/{chip}"
        if hasattr(gpiod, "request_lines"):                     # libgpiod v2 bindings
            from gpiod.line import Direction, Value
            req = gpiod.request_lines(path, consumer="pesm", config={
                off: gpiod.LineSettings(direction=Direction.OUTPUT,
                                        output_value=Value.ACTIVE if initial else Value.INACTIVE)})
            return ("v2", req, off, Value)
        ch = gpiod.Chip(path)                                   # libgpiod v1 bindings
        ln = ch.get_line(off)
        ln.request(consumer="pesm", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[initial])
        return ("v1", ln, off, None)

    @staticmethod
    def _set(line, v: int):
        kind, obj, off, Value = line
        if kind == "v2":
            obj.set_value(off, Value.ACTIVE if v else Value.INACTIVE)
        else:
            obj.set_value(v)

    def xfer(self, mosi: Sequence[int]) -> List[int]:
        return list(self._spi.xfer2(list(mosi)))

    def set_mode(self, boot: bool) -> None:
        self._set(self._mode, 1 if boot else 0)
        self.delay(1e-4)

    def set_reset(self, active: bool) -> None:
        if self._rst is not None:
            self._set(self._rst, 0 if active else 1)
            self.delay(1e-3)

    def close(self) -> None:
        self._spi.close()


class EmulatorTransport(Transport):
    """Backend driving pesm.emulator.PESMEmulator (no hardware)."""

    def __init__(self, emulator=None, sck_hz: float = 1e6):
        from .emulator import PESMEmulator
        self.emu = emulator or PESMEmulator(sck_hz=sck_hz)

    def xfer(self, mosi: Sequence[int]) -> List[int]:
        return self.emu.xfer(list(mosi))

    def set_mode(self, boot: bool) -> None:
        self.emu.set_mode(boot)

    def delay(self, seconds: float) -> None:
        self.emu.advance_seconds(seconds)


def open_transport(backend: str, **kw) -> Transport:
    if backend == "ftdi":
        return FtdiTransport(url=kw.get("url") or "ftdi://ftdi:232h/1",
                             freq=kw.get("freq", 1e6),
                             mode_pin=kw.get("mode_pin", 4),
                             reset_pin=kw.get("reset_pin", 5))
    if backend == "spidev":
        bus, dev = (int(x) for x in str(kw.get("spi", "0.0")).split("."))
        return SpidevTransport(bus, dev, int(kw.get("freq", 1e6)),
                               kw.get("mode_gpio") or "gpiochip0:17", kw.get("reset_gpio"))
    if backend == "emulator":
        return EmulatorTransport(sck_hz=kw.get("freq", 1e6))
    raise ProgrammerError(f"unknown backend {backend}")


# ===========================================================================
# Programmer
# ===========================================================================
@dataclass
class MonitorResult:
    """What Programmer.monitor() saw."""
    statuses: List[hp.Status] = field(default_factory=list)   # one per change of state
    rx: List[int] = field(default_factory=list)               # every byte drained
    polls: int = 0
    final: Optional[hp.Status] = None


class Programmer:
    """High-level operations on a PESM chip over a Transport."""

    def __init__(self, transport: Transport, log=None):
        self.t = transport
        self.log = log or (lambda msg: None)

    # ---------------- raw ----------------
    def boot(self) -> None:
        """MODE high: core held in reset, program/config writable."""
        self.t.set_mode(True)

    def run(self) -> None:
        """MODE low: core starts at cfg ENTRY (0 unless changed)."""
        self.t.set_mode(False)

    def reset_chip(self) -> None:
        self.t.set_reset(True)
        self.t.set_reset(False)

    def status(self) -> hp.Status:
        return hp.Status.decode(self.t.xfer(hp.frame_status()))

    def identify(self) -> int:
        """Chip ID byte (0x30 for ISA v3). Raises if no PESM v3 answers."""
        cid = self.status().chip_id
        if cid != isa.CHIP_ID:
            raise ProgrammerError(f"unexpected chip ID {cid:#04x} (expected {isa.CHIP_ID:#04x}): "
                                  "check wiring, clock and SCK <= f_clk/8")
        return cid

    def control(self, bits: int) -> None:
        self.t.xfer(hp.frame_control(bits))

    def clear_flags(self) -> None:
        self.control(hp.CTRL_CLR_FLAGS)

    def flush(self, tx: bool = True, rx: bool = True) -> None:
        self.control((hp.CTRL_FLUSH_TX if tx else 0) | (hp.CTRL_FLUSH_RX if rx else 0))

    def set_hflag(self, v: bool = True) -> None:
        self.control(hp.CTRL_HFLAG_SET if v else hp.CTRL_HFLAG_CLR)

    def write_imem(self, words: Sequence[int], addr: int = 0) -> None:
        self.t.xfer(hp.frame_write_imem(addr, words))

    def read_imem(self, addr: int = 0, n: int = isa.IMEM_DEPTH) -> List[int]:
        return hp.parse_read_imem(self.t.xfer(hp.frame_read_imem(addr, n)), n)

    def write_config(self, cfg: Sequence[int], addr: int = 0) -> None:
        self.t.xfer(hp.frame_write_cfg(addr, cfg))

    def read_config(self, addr: int = 0, n: int = isa.NUM_CFG) -> List[int]:
        return list(self.t.xfer(hp.frame_read_cfg(addr, n))[1:])

    # ---------------- program load ----------------
    def load(self, image: isa.Image, verify: bool = True, retries: int = 2,
             flush_fifos: bool = True) -> None:
        """BOOT, write config + imem, verify by readback over MISO, stay in BOOT."""
        self.boot()
        if verify:
            self.identify()
        if flush_fifos:
            self.control(hp.CTRL_FLUSH_TX | hp.CTRL_FLUSH_RX | hp.CTRL_HFLAG_CLR
                         | hp.CTRL_CLR_FLAGS)
        exp_cfg = [c & m for c, m in zip(image.cfg, isa.CFG_MASKS)]
        for attempt in range(retries + 1):
            self.write_config(image.cfg)
            self.write_imem(image.words)
            if not verify:
                return
            got_w = self.read_imem(0, isa.IMEM_DEPTH)
            got_c = self.read_config(0, isa.NUM_CFG)
            bad_w = [i for i, (a, b) in enumerate(zip(got_w, image.words)) if a != b]
            bad_c = [i for i, (a, b) in enumerate(zip(got_c, exp_cfg)) if a != b]
            if not bad_w and not bad_c:
                self.log(f"loaded {image.name}: {image.length} words, verified")
                return
            self.log(f"verify failed (attempt {attempt + 1}): imem {bad_w} cfg {bad_c}")
        raise VerifyError(f"readback mismatch: imem addrs {bad_w}, cfg addrs {bad_c}")

    def patch(self, addr: int, words: Sequence[int], verify: bool = True) -> None:
        """Random-access imem patch (core must be in BOOT)."""
        self.write_imem(words, addr)
        if verify and self.read_imem(addr, len(words)) != list(words):
            raise VerifyError(f"patch at {addr} did not verify")

    # ---------------- FIFOs ----------------
    def write_tx(self, data: Union[bytes, Sequence[int]], timeout: float = 1.0,
                 poll: float = 1e-4) -> None:
        """Queue bytes into the TX FIFO, never overflowing it."""
        data = list(data)
        t_end = None
        while data:
            st = self.status()
            space = hp.FIFO_DEPTH - st.tx_level
            if space > 0:
                chunk, data = data[:space], data[space:]
                self.t.xfer(hp.frame_write_tx(chunk))
                t_end = None
                continue
            t_end = t_end or (self._now() + timeout)
            if self._now() > t_end:
                raise ProgrammerError("TX FIFO stayed full (is the core running?)")
            self.t.delay(poll)

    def read_rx(self, n: Optional[int] = None, timeout: float = 1.0,
                poll: float = 1e-4) -> List[int]:
        """Read n bytes (waiting up to timeout) or, with n=None, whatever is queued."""
        out: List[int] = []
        t_end = self._now() + timeout
        while True:
            st = self.status()
            want = st.rx_level if n is None else min(st.rx_level, n - len(out))
            if want:
                out += list(self.t.xfer(hp.frame_read_rx(want))[1:])
            if n is None or len(out) >= n:
                return out
            if self._now() > t_end:
                raise ProgrammerError(f"RX timeout: got {len(out)} of {n} bytes")
            self.t.delay(poll)

    def wait_halt(self, timeout: float = 1.0, poll: float = 1e-3) -> hp.Status:
        t_end = self._now() + timeout
        while True:
            st = self.status()
            if st.halted:
                return st
            if self._now() > t_end:
                raise ProgrammerError(f"core did not halt: {st}")
            self.t.delay(poll)

    # ---------------- runtime monitoring ----------------
    def monitor(self, duration: Optional[float] = None, interval: float = 0.01,
                drain_rx: bool = True, until_halt: bool = False,
                on_status: Optional[Callable[[hp.Status], None]] = None,
                on_rx: Optional[Callable[[List[int]], None]] = None,
                max_polls: Optional[int] = None) -> MonitorResult:
        """Poll the running chip: READ_STATUS every `interval` seconds, drain the
        RX FIFO as bytes arrive, report every change of pc/flags/levels.

        Ends after `duration` seconds, when the core halts (until_halt), or after
        max_polls polls, whichever comes first; at least one of them is needed.
        Sticky error flags (ERR, RX_OVF, TX_OVF, RX_UNF) are reported, not cleared."""
        if duration is None and not until_halt and max_polls is None:
            raise ProgrammerError("monitor() needs duration, until_halt or max_polls")
        res = MonitorResult()
        t_end = None if duration is None else self._now() + duration
        last = None
        while True:
            st = self.status()
            res.polls += 1
            res.final = st
            if drain_rx and st.rx_level:
                data = list(self.t.xfer(hp.frame_read_rx(st.rx_level))[1:])
                res.rx += data
                if on_rx:
                    on_rx(data)
            key = (st.pc, st.running, st.halted, st.irq, st.err, st.tx_ovf, st.rx_ovf,
                   st.rx_unf, st.hflag, st.tx_level, st.rx_level)
            if key != last:
                last = key
                res.statuses.append(st)
                if on_status:
                    on_status(st)
            if until_halt and st.halted:
                break
            if max_polls is not None and res.polls >= max_polls:
                break
            if t_end is not None and self._now() >= t_end:
                break
            self.t.delay(interval)
        if drain_rx and res.final.halted:
            # the core stopped: pick up what was pushed after the last drain
            st = self.status()
            if st.rx_level:
                data = list(self.t.xfer(hp.frame_read_rx(st.rx_level))[1:])
                res.rx += data
                if on_rx:
                    on_rx(data)
            res.final = self.status()
        return res

    # time source that also works for simulated transports
    def _now(self) -> float:
        emu = getattr(self.t, "emu", None)
        if emu is not None:
            return emu.cycle / emu.clk_hz
        now = getattr(self.t, "now", None)
        return now() if now else time.monotonic()


# ===========================================================================
# CLI
# ===========================================================================
def load_image(path: str) -> isa.Image:
    if path.endswith(".bin"):
        with open(path, "rb") as f:
            return isa.Image.from_bin(f.read(), name=path)
    if path.endswith(".json"):
        with open(path) as f:
            return isa.Image.from_json(f.read())
    from .assembler import assemble_file
    return assemble_file(path)


def add_transport_args(ap: argparse.ArgumentParser) -> None:
    g = ap.add_argument_group("transport")
    g.add_argument("--backend", default="ftdi", choices=["ftdi", "spidev", "emulator"])
    g.add_argument("--url", help="pyftdi URL (ftdi backend)")
    g.add_argument("--freq", type=float, default=1e6, help="SCK Hz (<= f_clk/8)")
    g.add_argument("--mode-pin", type=int, default=4, help="FTDI GPIO for MODE (ADx)")
    g.add_argument("--reset-pin", type=int, default=5, help="FTDI GPIO for RST_N (-1: none)")
    g.add_argument("--spi", default="0.0", help="spidev bus.dev")
    g.add_argument("--mode-gpio", default="gpiochip0:17", help="spidev backend: MODE line")
    g.add_argument("--reset-gpio", default=None, help="spidev backend: RST_N line")


def transport_from_args(a) -> Transport:
    return open_transport(a.backend, url=a.url, freq=a.freq, mode_pin=a.mode_pin,
                          reset_pin=None if a.reset_pin < 0 else a.reset_pin, spi=a.spi,
                          mode_gpio=a.mode_gpio, reset_gpio=a.reset_gpio)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", nargs="?", help=".pasm/.asm, .bin or .json image")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--no-run", action="store_true", help="leave the core in BOOT")
    ap.add_argument("--status", action="store_true", help="only print status")
    ap.add_argument("--reset", action="store_true", help="pulse rst_n before loading")
    ap.add_argument("--monitor", type=float, metavar="SECONDS",
                    help="after starting, print status changes and RX bytes for this long")
    ap.add_argument("--interval", type=float, default=0.01, help="monitor poll interval (s)")
    add_transport_args(ap)
    a = ap.parse_args(argv)
    with transport_from_args(a) as t:
        p = Programmer(t, log=print)
        if a.reset:
            p.reset_chip()
        if a.image:
            img = load_image(a.image)
            p.load(img, verify=not a.no_verify)
            if not a.no_run:
                p.run()
        if a.monitor:
            p.monitor(a.monitor, a.interval, on_status=lambda st: print(f"status: {st}"),
                      on_rx=lambda d: print("rx: " + " ".join(f"{b:02x}" for b in d)))
        print(p.status())
    return 0


if __name__ == "__main__":
    sys.exit(main())
