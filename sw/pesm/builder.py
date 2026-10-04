# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""
Python DSL for PESM v3 microcode.

    from pesm.builder import PESMProgram

    p = PESMProgram("uart_tx")
    p.clock(tick_hz=115200).shift(out="right").init_pins(tout=0x01)
    p.macro_uart_tx("tout0")            # TX FIFO -> 8N1 frames, forever
    image = p.compile()                 # pesm.isa.Image (64 words + 20 cfg bytes)
    open("uart_tx.bin", "wb").write(p.to_bin())

Every method returns the program, so calls chain. Class-A instructions
(ctl, jmp, wait, in/out, set/dir/toggle/drive, mov) take `side=` (side-set
value) and `delay=` (pre-delay in ticks: the instruction executes on the
delay-th tick). Labels are strings; forward references are resolved by
compile(). See docs/DSL_GUIDE.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Union

from . import isa
from . import usb as _usb
from .isa import IsaError, PinLike

Target = str
Reg = str


class DslError(IsaError):
    pass


@dataclass
class _Ins:
    enc: Callable[[int, Dict[str, int]], int]   # (side_count, labels) -> word
    text: Callable[[], str]                     # assembler source
    class_a: bool


def _pin_txt(p: PinLike) -> str:
    return p.lower() if isinstance(p, str) else str(p)


def _sfx(side, delay) -> str:
    return (f" side {side}" if side is not None else "") + (f" [{delay}]" if delay else "")


class PESMProgram:
    """Chainable builder for one PESM program (up to 64 words)."""

    def __init__(self, name: str = "pesm", clk_hz: float = isa.CLK_HZ_DEFAULT):
        self.name = name
        self.clk_hz = clk_hz
        self.cfg = isa.Config()
        self._ins: List[_Ins] = []
        self._labels: Dict[str, int] = {}
        self._uid = 0
        self._wrap_target: Optional[int] = None
        self._wrap_top: Optional[int] = None
        self._entry: Optional[str] = None
        self._owner: Dict[str, str] = {}   # config field -> who set it (conflict detection)

    # ==================================================================
    # Configuration
    # ==================================================================
    def _set(self, field: str, value, who: str = "user"):
        cur = getattr(self.cfg, field)
        prev = self._owner.get(field)
        if prev is not None and cur != value:
            raise DslError(f"config conflict on {field}: {prev} set {cur!r}, {who} needs {value!r}")
        setattr(self.cfg, field, value)
        self._owner[field] = who
        return self

    def clock(self, tick_hz: Optional[float] = None, divider: Optional[float] = None,
              div_int: Optional[int] = None, div_frac: int = 0) -> "PESMProgram":
        """Tick grid: tick_hz (from clk_hz), divider (clk per tick, fractional) or raw."""
        if tick_hz is not None:
            i, f = isa.divider_from_ratio(self.clk_hz / float(tick_hz))
        elif divider is not None:
            i, f = isa.divider_from_ratio(float(divider))
        elif div_int is not None:
            i, f = int(div_int), int(div_frac)
        else:
            raise DslError("clock() needs tick_hz, divider or div_int")
        self._set("div_int", i)
        return self._set("div_frac", f)

    def shift(self, out: Optional[str] = None, in_: Optional[str] = None) -> "PESMProgram":
        """Shift directions: 'right' = LSB first, 'left' = MSB first."""
        for v in (out, in_):
            if v not in (None, "left", "right"):
                raise DslError("shift direction must be 'left' or 'right'")
        if out is not None:
            self._set("out_right", out == "right")
        if in_ is not None:
            self._set("in_right", in_ == "right")
        return self

    def autopull(self, threshold: int = 8, enable: bool = True) -> "PESMProgram":
        self._set("autopull", enable)
        return self._set("pull_thresh", threshold & 31)

    def autopush(self, threshold: int = 8, enable: bool = True) -> "PESMProgram":
        self._set("autopush", enable)
        return self._set("push_thresh", threshold & 31)

    def thresholds(self, pull: Optional[int] = None, push: Optional[int] = None):
        if pull is not None:
            self._set("pull_thresh", pull & 31)
        if push is not None:
            self._set("push_thresh", push & 31)
        return self

    def sideset(self, count: int, base: PinLike = 0, pindir: bool = False) -> "PESMProgram":
        if not 0 <= count <= 4:
            raise DslError("side-set count 0..4")
        self._set("side_count", count)
        self._set("side_base", isa.pin_index(base))
        return self._set("side_pindir", pindir)

    def open_drain(self, *pins: PinLike) -> "PESMProgram":
        """Make BIO pins open drain (writing 1 releases, 0 pulls low)."""
        mask = self.cfg.od_mask
        for p in pins:
            i = isa.pin_index(p)
            if i > 7:
                raise DslError("open drain is only available on BIO0..7")
            mask |= 1 << i
        self.cfg.od_mask = mask
        return self

    def crc_poly(self, poly: int) -> "PESMProgram":
        return self._set("crc_poly", poly & 0xFFFF)

    def pattern(self, mask: int, value: int = 0) -> "PESMProgram":
        """Pattern-compare registers used by jmp_if_pattern()/wait_pattern():
        match = ((window ^ expected) & mask) == 0. `value` is the expected
        window when the compare does not use X."""
        if not (0 <= mask <= 0xFF and 0 <= value <= 0xFF):
            raise DslError("pattern mask/value are 8-bit")
        self._set("pat_mask", mask)
        return self._set("pat_val", value)

    def pattern_pins(self, levels: Dict[PinLike, int], base: PinLike = 0) -> "PESMProgram":
        """pattern() from {pin: level}: the pins must lie in the 8-input
        window starting at `base` (use the same base in jmp_if_pattern)."""
        b = isa.pin_index(base)
        mask = val = 0
        for pin, lvl in levels.items():
            k = (isa.pin_index(pin) - b) & 15
            if k > 7:
                raise DslError(f"{pin} is outside the 8-pin window at {base}")
            mask |= 1 << k
            val |= (int(lvl) & 1) << k
        return self.pattern(mask, val)

    def background_clock(self, pin: Optional[PinLike] = None, hz: Optional[float] = None,
                         div: Optional[int] = None, auto: bool = True,
                         idle: int = 0) -> "PESMProgram":
        """Free-running clock tied to the tick grid: toggles every (div + 1)
        ticks, f = f_tick / (2 * (div + 1)). `pin` (an output pin, or None for
        an internal timebase) is taken over by the generator; its level is
        also readable as input pin 'bgclk'. auto=False: stopped (at `idle`)
        until bgclk_start(). Give `hz` after clock()."""
        if (hz is None) == (div is None):
            raise DslError("background_clock() needs exactly one of hz or div")
        if hz is not None:
            f_tick = self.clk_hz / self.cfg.tick_period()
            div = int(round(f_tick / (2.0 * float(hz)))) - 1
        if not 0 <= div <= 255:
            raise DslError(f"background clock divider {div} out of range 0..255 "
                           "(lower the tick rate or raise the frequency)")
        if pin is None:
            self._set("bg_en", False)
        else:
            i = isa.pin_index(pin)
            if i > 14:
                raise DslError("background clock pin must be an output pin (0..14)")
            self._set("bg_en", True)
            self._set("bg_pin", i)
        self._set("bg_div", div)
        self._set("bg_auto", bool(auto))
        return self._set("bg_idle", int(idle) & 1)

    def init_pins(self, bio_out: Optional[int] = None, bio_oe: Optional[int] = None,
                  tout: Optional[int] = None) -> "PESMProgram":
        """Pin state while in BOOT and at program start."""
        if bio_out is not None:
            self.cfg.init_bio_out = bio_out & 0xFF
        if bio_oe is not None:
            self.cfg.init_bio_oe = bio_oe & 0xFF
        if tout is not None:
            self.cfg.init_tout = tout & 0x7F
        return self

    def _init_bits(self, field: str, pin: PinLike, value: int):
        i = isa.pin_index(pin)
        if field == "tout":
            if i < 8:
                raise DslError(f"{pin} is not a TOUT pin")
            i -= 8
            self.cfg.init_tout = (self.cfg.init_tout & ~(1 << i)) | (value << i)
        else:
            if i > 7:
                raise DslError(f"{pin} is not a BIO pin")
            v = getattr(self.cfg, field)
            setattr(self.cfg, field, (v & ~(1 << i)) | (value << i))

    def wrap_target(self) -> "PESMProgram":
        self._wrap_target = len(self._ins)
        return self

    def wrap(self) -> "PESMProgram":
        if not self._ins:
            raise DslError("wrap() before any instruction")
        self._wrap_top = len(self._ins) - 1
        return self

    def entry(self, label: str) -> "PESMProgram":
        self._entry = label
        return self

    # ==================================================================
    # Labels
    # ==================================================================
    def label(self, name: str) -> "PESMProgram":
        key = name.lower()
        if key in self._labels:
            raise DslError(f"duplicate label {name}")
        self._labels[key] = len(self._ins)
        return self

    def new_label(self, hint: str = "L") -> str:
        self._uid += 1
        return f"_{hint}{self._uid}"

    @property
    def here(self) -> int:
        return len(self._ins)

    # ==================================================================
    # Emission helpers
    # ==================================================================
    def _a(self, fn, txt: str, side=None, delay=None) -> "PESMProgram":
        self._ins.append(_Ins(lambda sc, lab: fn(isa.tail(side, delay, sc), lab),
                              lambda: txt + _sfx(side, delay), True))
        return self

    def _b(self, fn, txt: str) -> "PESMProgram":
        self._ins.append(_Ins(lambda sc, lab: fn(lab), lambda: txt, False))
        return self

    @staticmethod
    def _tgt(lab: Dict[str, int], target: Union[str, int]) -> int:
        if isinstance(target, int):
            return target
        try:
            return lab[target.lower()]
        except KeyError:
            raise DslError(f"undefined label '{target}'") from None

    # ==================================================================
    # Control
    # ==================================================================
    def nop(self, side=None, delay=None):
        return self._a(lambda t, l: isa.enc_nop(t), "nop", side, delay)

    def halt(self, side=None, delay=None):
        return self._a(lambda t, l: isa.enc_halt(t), "halt", side, delay)

    def irq(self, side=None, delay=None):
        """Set the sticky IRQ flag (visible in host STATUS0)."""
        return self._a(lambda t, l: isa.enc_irq(t), "irq", side, delay)

    def hclr(self, side=None, delay=None):
        """Clear the host flag (input pin 'hflag')."""
        return self._a(lambda t, l: isa.enc_hclr(t), "hclr", side, delay)

    def push(self, iffull: bool = False, block: bool = True, side=None, delay=None):
        txt = "push" + (" iffull" if iffull else "") + ("" if block else " noblock")
        return self._a(lambda t, l: isa.enc_push(iffull, block, t), txt, side, delay)

    def pull(self, ifempty: bool = False, block: bool = True, side=None, delay=None):
        txt = "pull" + (" ifempty" if ifempty else "") + ("" if block else " noblock")
        return self._a(lambda t, l: isa.enc_pull(ifempty, block, t), txt, side, delay)

    def sync_grid(self, half: bool = False, side=None, delay=None):
        """Re-phase the tick grid: next tick in DIV_INT (half: DIV_INT/2) cycles."""
        return self._a(lambda t, l: isa.enc_sync(half, t), "sync" + (" half" if half else ""),
                       side, delay)

    def clear(self, isr: bool = True, osr: bool = False, side=None, delay=None):
        which = "all" if (isr and osr) else ("isr" if isr else "osr")
        return self._a(lambda t, l: isa.enc_clr(isr, osr, t), f"clr {which}", side, delay)

    def bgclk_start(self, reset: bool = False, side=None, delay=None):
        """Start the background clock. reset=True: from the idle level with a
        full first half-period (first edge (div + 1) ticks after this one)."""
        return self._a(lambda t, l: isa.enc_bgclk(True, reset, t),
                       "bgclk on" + (" reset" if reset else ""), side, delay)

    def bgclk_stop(self, reset: bool = False, side=None, delay=None):
        """Stop the background clock: freeze the level, or reset=True: back to idle."""
        return self._a(lambda t, l: isa.enc_bgclk(False, reset, t),
                       "bgclk off" + (" reset" if reset else ""), side, delay)

    # ==================================================================
    # Branches
    # ==================================================================
    def jmp(self, target: Union[str, int], cond: str = "always", side=None, delay=None):
        if cond not in isa.JMP_CONDS:
            raise DslError(f"jmp condition must be one of {list(isa.JMP_CONDS)}")
        txt = f"jmp {target}" if cond == "always" else f"jmp {cond}, {target}"
        return self._a(lambda t, l: isa.enc_jmp(cond, self._tgt(l, target), t), txt, side, delay)

    def jmp_if_zero(self, reg: Reg, target, side=None, delay=None):
        return self.jmp(target, f"!{reg}", side, delay)

    def jmp_dec(self, reg: Reg, target, side=None, delay=None):
        """if reg != 0: jump; reg is decremented either way (loop reg+1 times)."""
        return self.jmp(target, f"{reg}--", side, delay)

    def jmp_if_ne(self, target, side=None, delay=None):
        """Compare-and-branch: jump if X != Y."""
        return self.jmp(target, "x!=y", side, delay)

    def jmp_if_osr_not_empty(self, target, side=None, delay=None):
        return self.jmp(target, "!osre", side, delay)

    def jmp_if_lastbit(self, target, side=None, delay=None):
        return self.jmp(target, "lb", side, delay)

    def jmp_if_pin(self, pin: PinLike, level: int, target):
        return self._b(lambda l: isa.enc_jpin(pin, level, self._tgt(l, target)),
                       f"jpin {_pin_txt(pin)}, {level}, {target}")

    def jmp_if_pattern(self, target, base: PinLike = 0, expect: str = "cfg",
                       match: bool = True):
        """Single-cycle multi-pin branch. Window = inputs base..base+7;
        jump if ((window ^ expected) & PAT_MASK) == 0 (match=True) or != 0
        (match=False). expected = pattern() value ('cfg') or register X ('x')."""
        if expect not in ("cfg", "x"):
            raise DslError("expect must be 'cfg' or 'x'")
        mn = "j" + ("" if match else "n") + "pat" + ("x" if expect == "x" else "")
        return self._b(lambda l: isa.enc_jpat(self._tgt(l, target), base, expect == "x",
                                              not match),
                       f"{mn} {_pin_txt(base)}, {target}")

    def wait_pattern(self, base: PinLike = 0, expect: str = "cfg", match: bool = True):
        """Stall until the pattern matches (match=False: until it stops matching).
        One word; the pins are polled every clk."""
        lab = self.new_label("wp")
        return self.label(lab).jmp_if_pattern(lab, base, expect, not match)

    def jmp_reg(self, reg: Reg = "x", side=None, delay=None):
        """Computed jump: pc <- reg[5:0] (jump tables)."""
        return self.mov("pc", reg, side=side, delay=delay)

    # ==================================================================
    # Timing
    # ==================================================================
    def wait_pin(self, pin: PinLike, level: Union[int, str], sync: Optional[str] = None,
                 side=None, delay=None):
        """level: 0/1/'low'/'high' (level) or 'rise'/'fall' (edge seen while waiting).
        sync: None, 'full' or 'half' re-phases the tick grid on release."""
        kind = {0: "low", 1: "high"}.get(level, level)
        opt = {None: "", "full": " sync", "half": " synchalf"}[sync]
        return self._a(lambda t, l: isa.enc_wait(pin, kind, sync, t),
                       f"wait {kind} {_pin_txt(pin)}{opt}", side, delay)

    def wait_cycles(self, n: int, use_xy: bool = False):
        """Occupy exactly n clk cycles (n >= 1). Above 1024 needs use_xy=True
        (clobbers X and Y, up to 65538 cycles)."""
        n = int(n)
        if n < 1:
            raise DslError("wait_cycles needs n >= 1")
        if n == 1:
            return self.nop()
        if n <= 1024:
            return self._b(lambda l: isa.enc_dly(n - 1), f"dly {n - 1}")
        if not use_xy:
            raise DslError("wait_cycles > 1024 needs use_xy=True (clobbers X, Y)")
        k = n - 3
        if k > 0xFFFF:
            raise DslError("wait_cycles max is 65538")
        return self.ldi("x", k >> 8).ldi("y", k & 0xFF)._b(
            lambda l: isa.enc_dly(None, False, True), "dly xy")

    def wait_ticks(self, n: int, use_xy: bool = False):
        """Stall until the n-th tick pulse (counted from this instruction, inclusive)."""
        if 0 <= n <= 1023:
            return self._b(lambda l: isa.enc_dly(n, True), f"dlyt {n}")
        if not use_xy:
            raise DslError("wait_ticks > 1023 needs use_xy=True (clobbers X, Y)")
        return self.ldi("x", (n >> 8) & 0xFF).ldi("y", n & 0xFF)._b(
            lambda l: isa.enc_dly(None, True, True), "dlyt xy")

    # ==================================================================
    # Pins
    # ==================================================================
    def set_pin(self, pin: PinLike, value: int, side=None, delay=None):
        return self._a(lambda t, l: isa.enc_setp(pin, "set", value, t),
                       f"set {_pin_txt(pin)}, {value}", side, delay)

    def dir_pin(self, pin: PinLike, output: bool = True, side=None, delay=None):
        v = int(bool(output))
        return self._a(lambda t, l: isa.enc_setp(pin, "dir", v, t),
                       f"dir {_pin_txt(pin)}, {v}", side, delay)

    def release_pin(self, pin: PinLike, side=None, delay=None):
        return self.dir_pin(pin, False, side, delay)

    def drive_pin(self, pin: PinLike, value: int, side=None, delay=None):
        """OUT = value and output enable on, in one cycle."""
        return self._a(lambda t, l: isa.enc_setp(pin, "drive", value, t),
                       f"drive {_pin_txt(pin)}, {value}", side, delay)

    def toggle_pin(self, pin: PinLike, side=None, delay=None):
        return self._a(lambda t, l: isa.enc_setp(pin, "toggle", 0, t),
                       f"toggle {_pin_txt(pin)}", side, delay)

    def set_pins(self, src: str = "x", op: str = "", side=None, delay=None):
        """All BIO outputs <- src (atomic 8-pin write)."""
        return self.mov("pins", src, op, side, delay)

    def set_pindirs(self, src: str = "x", op: str = "", side=None, delay=None):
        return self.mov("pindirs", src, op, side, delay)

    def set_tout(self, src: str = "x", op: str = "", side=None, delay=None):
        return self.mov("tout", src, op, side, delay)

    def sample_pins(self, side=None, delay=None):
        """ISR <- inputs 0..7 in one cycle (byte placed for the IN direction)."""
        return self.mov("isr", "pins", "", side, delay)

    # ==================================================================
    # Shifting
    # ==================================================================
    def shift_out(self, dst: Union[PinLike, str], n: int = 1, side=None, delay=None):
        """OSR -> pin(s) (pins dst..dst+n-1, n<=8) or register x|y|null|pindirs."""
        if isinstance(dst, str) and dst.lower() in isa.OUT_DST:
            d = dst.lower()
            return self._a(lambda t, l: isa.enc_out_reg(d, n, t), f"out {d}, {n}", side, delay)
        return self._a(lambda t, l: isa.enc_out_pins(dst, n, t),
                       f"out pins, {_pin_txt(dst)}, {n}", side, delay)

    def shift_in(self, src: Union[PinLike, str], n: int = 1, side=None, delay=None):
        """pin(s) (src..src+n-1, n<=8) or register x|y|null|osr -> ISR."""
        if isinstance(src, str) and src.lower() in isa.IN_SRC:
            s = src.lower()
            return self._a(lambda t, l: isa.enc_in_reg(s, n, t), f"in {s}, {n}", side, delay)
        return self._a(lambda t, l: isa.enc_in_pins(src, n, t),
                       f"in pins, {_pin_txt(src)}, {n}", side, delay)

    def get_pin(self, pin: PinLike, side=None, delay=None):
        return self.shift_in(pin, 1, side, delay)

    def mov(self, dst: str, src: str, op: str = "", side=None, delay=None):
        """op: '' copy, '~' invert, 'rev' bit-reverse, 'par' parity (XOR-reduce)."""
        o = {"!": "~", "::": "rev"}.get(op, op)
        txt = f"mov {dst}, {o + ' ' if o in ('rev', 'par') else o}{src}"
        return self._a(lambda t, l: isa.enc_mov(dst, src, o, t), txt, side, delay)

    # ==================================================================
    # ALU
    # ==================================================================
    def ldi(self, reg: Reg, value: int):
        return self._b(lambda l: isa.enc_alu(reg, "ldi", value), f"ldi {reg}, {value & 0xFF:#04x}")

    def and_(self, reg: Reg, mask: int):
        return self._b(lambda l: isa.enc_alu(reg, "and", mask), f"and {reg}, {mask & 0xFF:#04x}")

    def or_(self, reg: Reg, mask: int):
        return self._b(lambda l: isa.enc_alu(reg, "or", mask), f"or {reg}, {mask & 0xFF:#04x}")

    def xor(self, reg: Reg, mask: int):
        return self._b(lambda l: isa.enc_alu(reg, "xor", mask), f"xor {reg}, {mask & 0xFF:#04x}")

    def inc(self, reg: Reg):
        return self._b(lambda l: isa.enc_alu(reg, "inc"), f"inc {reg}")

    def dec(self, reg: Reg):
        return self._b(lambda l: isa.enc_alu(reg, "dec"), f"dec {reg}")

    def crc_step(self, msb: bool = False):
        """{Y,X} <- one Galois CRC step with CRC_POLY, input bit = last shifted bit."""
        return self._b(lambda l: isa.enc_crc(msb), "crc " + ("msb" if msb else "lsb"))

    def raw(self, word: int):
        return self._b(lambda l: word & 0xFFFF, f".word {word & 0xFFFF:#06x}")

    # ==================================================================
    # Protocol macros. Each states its tick-grid assumption, the config it
    # applies, and the registers it clobbers.
    # ==================================================================
    def macro_uart_tx_byte(self, pin: PinLike, byte: Optional[int] = None,
                           stop_bits: int = 1) -> "PESMProgram":
        """One 8N1 frame on `pin`. tick = baud. Data: `byte` (immediate) or the
        next TX FIFO byte. Starts on the next grid point. Clobbers X, OSR.
        Applies: OUT shift right."""
        self._set("out_right", True, "macro_uart_tx_byte")
        if byte is None:
            self.pull()
        else:
            self.ldi("x", byte).mov("osr", "x")
        self.set_pin(pin, 0, delay=1)
        self.ldi("x", 7)
        lab = self.new_label("utx")
        self.label(lab).shift_out(pin, 1, delay=1).jmp_dec("x", lab)
        self.set_pin(pin, 1, delay=1)
        for _ in range(stop_bits - 1):
            self.nop(delay=1)
        return self

    def macro_uart_tx(self, pin: PinLike, label: str = "uart_tx") -> "PESMProgram":
        """Endless TX FIFO -> 8N1 transmitter (8 words). tick = baud.
        Applies: OUT shift right; line idles high (also in BOOT)."""
        if isa.pin_index(pin) >= 8:
            self._init_bits("tout", pin, 1)
        else:
            self._init_bits("init_bio_out", pin, 1)
            self._init_bits("init_bio_oe", pin, 1)
        self.label(label).set_pin(pin, 1)
        self.macro_uart_tx_byte(pin)
        return self.jmp(label)

    def macro_uart_rx(self, pin: PinLike, label: str = "uart_rx",
                      on_framing_error: str = "irq") -> "PESMProgram":
        """Endless 8N1 receiver -> RX FIFO (12 words). tick = baud.
        Start edge re-phases the grid to mid-bit. Framing error: IRQ flag and
        wait for idle. Clobbers X, ISR. Applies: IN shift right, autopush 8."""
        who = "macro_uart_rx"
        self._set("in_right", True, who)
        self._set("autopush", True, who)
        self._set("push_thresh", 8, who)
        bit, ferr = self.new_label("urxb"), self.new_label("urxe")
        self.label(label).wait_pin(pin, "fall", sync="half")
        self.nop(delay=1).jmp_if_pin(pin, 1, label).ldi("x", 7)
        self.label(bit).shift_in(pin, 1, delay=1).jmp_dec("x", bit)
        self.nop(delay=1).jmp_if_pin(pin, 0, ferr).jmp(label)
        self.label(ferr)
        if on_framing_error == "irq":
            self.irq()
        return self.wait_pin(pin, "high").jmp(label)

    def macro_spi_transfer(self, sclk: PinLike, mosi: PinLike, miso: PinLike,
                           bits: int = 8, source: str = "fifo", sink: str = "fifo"
                           ) -> "PESMProgram":
        """Mode-0, MSB-first transfer of `bits` (1..8) bits without side-set.
        tick = 2 x f_SCLK. source: 'fifo' (pull first) or 'osr'; sink: 'fifo'
        (push after) or 'isr'. Leaves SCLK low. Clobbers X, OSR, ISR.
        Applies: OUT/IN shift left, no autopull/autopush."""
        who = "macro_spi_transfer"
        if not 1 <= bits <= 8:
            raise DslError("bits 1..8")
        self._set("out_right", False, who)
        self._set("in_right", False, who)
        self._set("autopull", False, who)
        self._set("autopush", False, who)
        if source == "fifo":
            self.pull()
        elif source != "osr":
            raise DslError("source must be 'fifo' or 'osr'")
        lab = self.new_label("spi")
        self.ldi("x", bits - 1)
        self.label(lab).set_pin(sclk, 0, delay=1).shift_out(mosi, 1)
        self.set_pin(sclk, 1, delay=1).shift_in(miso, 1).jmp_dec("x", lab)
        self.set_pin(sclk, 0, delay=1)
        if sink == "fifo":
            self.push()
        elif sink != "isr":
            raise DslError("sink must be 'fifo' or 'isr'")
        return self

    def _i2c_setup(self, sda: PinLike, scl: PinLike, who: str):
        for p in (sda, scl):
            if isa.pin_index(p) > 7:
                raise DslError("I2C pins must be BIO0..7")
            self.open_drain(p)
            self._init_bits("init_bio_oe", p, 1)
            self._init_bits("init_bio_out", p, 1)

    def macro_i2c_start(self, sda: PinLike, scl: PinLike) -> "PESMProgram":
        """START (bus must be idle). tick = 4 x f_SCL. Applies: open drain on SDA/SCL."""
        self._i2c_setup(sda, scl, "macro_i2c_start")
        return self.set_pin(sda, 0, delay=1).set_pin(scl, 0, delay=1)

    def macro_i2c_stop(self, sda: PinLike, scl: PinLike) -> "PESMProgram":
        """STOP, honouring clock stretching. tick = 4 x f_SCL."""
        self._i2c_setup(sda, scl, "macro_i2c_stop")
        return (self.set_pin(sda, 0, delay=1).set_pin(scl, 1, delay=1)
                .wait_pin(scl, "high").set_pin(sda, 1, delay=1))

    def macro_i2c_write_byte(self, sda: PinLike, scl: PinLike, nack_label: Optional[str] = None,
                             source: str = "fifo") -> "PESMProgram":
        """8 data bits MSB first + ACK clock, with clock stretching. On NACK jump
        to nack_label (SCL still high) if given. tick = 4 x f_SCL.
        Clobbers X, OSR. Applies: OUT shift left, open drain."""
        self._i2c_setup(sda, scl, "macro_i2c_write_byte")
        self._set("out_right", False, "macro_i2c_write_byte")
        if source == "fifo":
            self.pull()
        elif source != "osr":
            raise DslError("source must be 'fifo' or 'osr'")
        lab = self.new_label("i2cw")
        self.ldi("x", 7)
        self.label(lab).shift_out(sda, 1, delay=1).set_pin(scl, 1, delay=1)
        self.wait_pin(scl, "high").set_pin(scl, 0, delay=2).jmp_dec("x", lab)
        self.set_pin(sda, 1, delay=1).set_pin(scl, 1, delay=1).wait_pin(scl, "high")
        if nack_label:
            self.jmp_if_pin(sda, 1, nack_label)
        return self.set_pin(scl, 0, delay=2)

    def macro_i2c_read_byte(self, sda: PinLike, scl: PinLike, ack: bool = True,
                            sink: str = "fifo") -> "PESMProgram":
        """8 bits MSB first, then ACK (ack=True) or NACK. tick = 4 x f_SCL.
        Clobbers X, ISR. Applies: IN shift left, no autopush, open drain."""
        self._i2c_setup(sda, scl, "macro_i2c_read_byte")
        self._set("in_right", False, "macro_i2c_read_byte")
        self._set("autopush", False, "macro_i2c_read_byte")
        lab = self.new_label("i2cr")
        self.set_pin(sda, 1).ldi("x", 7)
        self.label(lab).set_pin(scl, 1, delay=2).wait_pin(scl, "high").shift_in(sda, 1)
        self.set_pin(scl, 0, delay=2).jmp_dec("x", lab)
        self.set_pin(sda, 0 if ack else 1, delay=1).set_pin(scl, 1, delay=1)
        self.wait_pin(scl, "high").set_pin(scl, 0, delay=2).set_pin(sda, 1)
        if sink == "fifo":
            self.push()
        return self

    def macro_crc_osr(self, msb: bool = False) -> "PESMProgram":
        """Feed the bits left in the OSR through the CRC unit ({Y,X}, CRC_POLY)
        until the OSR is empty (PULL_THRESH bits). 3 words, 3 clk per bit."""
        lab = self.new_label("crc")
        return self.label(lab).shift_out("null", 1).crc_step(msb).jmp_if_osr_not_empty(lab)

    def macro_usb_ls_tx(self, dp: PinLike = "bio0", dm: PinLike = "bio1",
                        label: str = "usb_tx") -> "PESMProgram":
        """Endless USB low-speed packet transmitter (23 words): TX FIFO bytes ->
        NRZI + bit stuffing; FIFO empty at a byte boundary -> SE0 SE0 J EOP.
        Host queues SYNC, PID, payload and CRC. tick = 1.5 MHz.
        Clobbers X, Y, OSR. Other BIO outputs are driven to 0 while sending."""
        pdp, pdm = isa.pin_index(dp), isa.pin_index(dm)
        if pdp > 7 or pdm > 7 or pdp == pdm:
            raise DslError("D+/D- must be two different BIO pins")
        j, both = 1 << pdm, (1 << pdp) | (1 << pdm)
        self._set("out_right", True, "macro_usb_ls_tx")
        self.cfg.init_bio_out = (self.cfg.init_bio_out & ~both) | j
        self.cfg.init_bio_oe |= both
        bit, have, one, eop = (self.new_label(h) for h in ("ub", "uh", "u1", "ue"))
        self.label(label).wait_pin("txne", "high").ldi("x", j).ldi("y", 5)
        self.label(bit).jmp_if_osr_not_empty(have).jmp_if_pin("txne", 0, eop).pull()
        self.label(have).shift_out("null", 1).jmp_if_lastbit(one)
        self.xor("x", both).ldi("y", 5).set_pins("x", delay=1).jmp(bit)
        self.label(one).set_pins("x", delay=1).jmp_dec("y", bit)
        self.xor("x", both).set_pins("x", delay=1).ldi("y", 5).jmp(bit)
        self.label(eop).set_pins("null", delay=1).ldi("x", j).set_pins("x", delay=2)
        return self.nop(delay=1).jmp(label)

    def macro_usb_ls_token(self, pid: Union[str, int], addr: int = 0, endp: int = 0,
                           dp: PinLike = "bio0", dm: PinLike = "bio1",
                           idle_ticks: int = 1) -> "PESMProgram":
        """One complete USB low-speed token packet with constant fields:
        SYNC, PID ('setup' | 'out' | 'in' or a 4-bit number), ADDR, ENDP, CRC5
        and the SE0-SE0-J end of packet. CRC5, bit stuffing and NRZI are
        resolved at compile time; the microcode is one atomic D+/D- write per
        line transition (2 + 16..28 + 1 words), each exactly on the tick grid.
        tick = 1.5 MHz. The line is left driving J for `idle_ticks` bit times.
        Clobbers X, Y; the other BIO outputs are driven 0 while sending.
        Applies: D+/D- outputs idle at J (also in BOOT)."""
        who = "macro_usb_ls_token"
        pdp, pdm = isa.pin_index(dp), isa.pin_index(dm)
        if pdp > 7 or pdm > 7 or pdp == pdm:
            raise DslError("D+/D- must be two different BIO pins")
        if idle_ticks < 1:
            raise DslError("idle_ticks >= 1")
        try:
            pkt = _usb.token_packet(pid, addr, endp)
        except (ValueError, KeyError) as e:
            raise DslError(f"{who}: {e}") from None
        # low speed: J = D- high, K = D+ high
        j, k, both = 1 << pdm, 1 << pdp, (1 << pdp) | (1 << pdm)
        self.cfg.init_bio_out = (self.cfg.init_bio_out & ~both) | j
        self.cfg.init_bio_oe |= both
        self._set("side_count", self.cfg.side_count, who)   # delay field width is now fixed
        maxd = (1 << (4 - min(self.cfg.side_count, 4))) - 1
        if maxd < 1:
            raise DslError(f"{who} needs a pre-delay field (side-set count <= 3)")
        src = {_usb.J: "x", _usb.K: "y", _usb.SE0: "null"}
        self.ldi("x", j).ldi("y", k)
        wait = 1                       # ticks from the previous write to this one
        for state, n in _usb.runs(_usb.line_states(pkt)):
            while wait > maxd:
                self.nop(delay=maxd)
                wait -= maxd
            self.set_pins(src[state], delay=wait)
            wait = n
        wait += idle_ticks - 1         # the final J run already lasts one bit time
        while wait > maxd:
            self.nop(delay=maxd)
            wait -= maxd
        return self.nop(delay=wait)

    # ==================================================================
    # Output
    # ==================================================================
    def to_bin(self) -> bytes:
        """Compile straight to the flashable .bin image (see Image.to_bin)."""
        return self.compile().to_bin()

    def to_mem(self) -> str:
        """Compile to a $readmemh image of the instruction memory."""
        return self.compile().to_mem()

    def to_lists(self):
        """Compile to (imem, cfg) Python lists."""
        return self.compile().to_lists()

    def save(self, base: str) -> List[str]:
        """Compile and write <base>.bin/.mem/_cfg.mem/.py/.json/.lst/.asm."""
        from .assembler import write_all
        return write_all(self.compile(), base)

    def compile(self) -> isa.Image:
        if len(self._ins) > isa.IMEM_DEPTH:
            raise DslError(f"program has {len(self._ins)} instructions (max {isa.IMEM_DEPTH})")
        cfg = isa.Config(**vars(self.cfg))
        if self._wrap_target is not None:
            cfg.wrap_bot = self._wrap_target
        if self._wrap_top is not None:
            cfg.wrap_top = self._wrap_top
        if self._entry is not None:
            cfg.entry = self._tgt(self._labels, self._entry)
        sc = min(cfg.side_count, 4)
        words = [isa.INSTR_HALT] * isa.IMEM_DEPTH
        source = {}
        for a, ins in enumerate(self._ins):
            try:
                words[a] = ins.enc(sc, self._labels)
            except IsaError as e:
                raise DslError(f"{self.name}[{a}] {ins.text()}: {e}") from None
            source[a] = ins.text()
        return isa.Image(words=words, cfg=cfg.to_bytes(), labels=dict(self._labels),
                         length=len(self._ins), source=source, name=self.name)

    def to_asm(self) -> str:
        """Equivalent assembler source (assemble(p.to_asm()) == p.compile())."""
        img = self.compile()
        c = img.config
        lines = [f"; {self.name} (generated by pesm.builder)",
                 f".div_raw {c.div_int} {c.div_frac}",
                 f".shift out={'right' if c.out_right else 'left'} in={'right' if c.in_right else 'left'}",
                 f".pull_thresh {c.pull_thresh}", f".push_thresh {c.push_thresh}"]
        if c.autopull:
            lines.append(f".autopull {c.pull_thresh}")
        if c.autopush:
            lines.append(f".autopush {c.push_thresh}")
        lines += [f".cfg 6 {img.cfg[6]:#04x}", f".cfg 7 {c.wrap_top}", f".cfg 8 {c.wrap_bot}",
                  f".cfg 9 {c.entry}", f".od {c.od_mask:#04x}", f".crc_poly {c.crc_poly:#06x}",
                  f".init_out {c.init_bio_out:#04x}", f".init_oe {c.init_bio_oe:#04x}",
                  f".init_tout {c.init_tout:#04x}",
                  f".pattern {c.pat_mask:#04x} {c.pat_val:#04x}",
                  f".cfg 18 {img.cfg[18]:#04x}", f".cfg 19 {img.cfg[19]:#04x}"]
        inv: Dict[int, List[str]] = {}
        for k, v in self._labels.items():
            inv.setdefault(v, []).append(k)
        for a, ins in enumerate(self._ins):
            for lab in inv.get(a, []):
                lines.append(f"{lab}:")
            lines.append("    " + ins.text())
        for lab in inv.get(len(self._ins), []):
            lines.append(f"{lab}:")
        return "\n".join(lines) + "\n"

    def listing(self) -> str:
        return self.compile().listing()

    def __len__(self) -> int:
        return len(self._ins)
