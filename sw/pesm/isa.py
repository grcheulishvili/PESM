# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""
PESM v2 instruction set: the single source of truth for encoding,
decoding and the configuration register map. Used by the text assembler,
the Python DSL builder, the host programmer and the test benches.

Normative description: docs/ISA.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Union

IMEM_DEPTH = 32
INSTR_HALT = 0x0100
CLK_HZ_DEFAULT = 50_000_000


class IsaError(ValueError):
    """Raised for any operand that cannot be encoded."""


# ---------------------------------------------------------------------------
# Opcodes and sub-fields
# ---------------------------------------------------------------------------
OP_CTL, OP_JMP, OP_JPIN, OP_WAIT, OP_IN, OP_OUT, OP_SETP, OP_MOV, OP_ALU, OP_DLY = range(10)
CLASS_A = (OP_CTL, OP_JMP, OP_WAIT, OP_IN, OP_OUT, OP_SETP, OP_MOV)

CTL_NOP, CTL_HALT, CTL_IRQ, CTL_PUSH, CTL_PULL, CTL_SYNC, CTL_HCLR, CTL_CLR = range(8)

JMP_CONDS: Dict[str, int] = {"always": 0, "!x": 1, "x--": 2, "!y": 3, "y--": 4,
                             "x!=y": 5, "!osre": 6, "lb": 7}
JMP_COND_NAMES = {v: k for k, v in JMP_CONDS.items()}

IN_SRC: Dict[str, int] = {"x": 0, "y": 1, "null": 2, "osr": 3}
OUT_DST: Dict[str, int] = {"x": 0, "y": 1, "null": 2, "pindirs": 3}
SETP_FUNCS: Dict[str, int] = {"set": 0, "dir": 1, "toggle": 2, "drive": 3}
MOV_DST: Dict[str, int] = {"x": 0, "y": 1, "isr": 2, "osr": 3, "pins": 4, "pindirs": 5,
                           "tout": 6, "pc": 7}
MOV_SRC: Dict[str, int] = {"x": 0, "y": 1, "isr": 2, "osr": 3, "pins": 4, "pinshi": 5,
                           "null": 6, "lb": 7}
MOV_OPS: Dict[str, int] = {"": 0, "~": 1, "rev": 2, "par": 3}
ALU_FUNCS: Dict[str, int] = {"ldi": 0, "and": 1, "or": 2, "xor": 3, "dec": 4, "inc": 5,
                             "crc": 6}

# Pin names. Output and input spaces share numbers 8..11 (TOUT0-3 / TIN0-3).
PIN_ALIASES: Dict[str, int] = {}
for _i in range(8):
    PIN_ALIASES[f"bio{_i}"] = _i
for _i in range(7):
    PIN_ALIASES[f"tout{_i}"] = 8 + _i
for _i in range(4):
    PIN_ALIASES[f"tin{_i}"] = 8 + _i
PIN_ALIASES.update({"txne": 12, "rxnf": 13, "hflag": 14})

PinLike = Union[int, str]


def pin_index(pin: PinLike) -> int:
    """Resolve a pin name (bio0..7, tout0..6, tin0..3, txne, rxnf, hflag) or number."""
    if isinstance(pin, str):
        p = pin.strip().lower()
        if p in PIN_ALIASES:
            return PIN_ALIASES[p]
        try:
            v = int(p, 0)
        except ValueError:
            raise IsaError(f"unknown pin '{pin}'") from None
    else:
        v = int(pin)
    if not 0 <= v <= 15:
        raise IsaError(f"pin out of range: {pin}")
    return v


def _chk(name: str, v: int, lo: int, hi: int) -> int:
    v = int(v)
    if not lo <= v <= hi:
        raise IsaError(f"{name} {v} out of range {lo}..{hi}")
    return v


# ---------------------------------------------------------------------------
# Tail (side-set / pre-delay) of class-A instructions
# ---------------------------------------------------------------------------
def tail(side: Optional[int] = None, delay: Optional[int] = None, side_count: int = 0) -> int:
    sc = _chk("side count", side_count, 0, 4)
    sv = 0
    if side is not None:
        if sc == 0:
            raise IsaError("side-set used but side count is 0")
        sv = int(side)
        if not 0 <= sv < (1 << sc):
            raise IsaError(f"side value {sv} does not fit in {sc} bit(s)")
    dv = 0
    if delay is not None:
        dv = int(delay)
        if not 0 <= dv < (1 << (4 - sc)):
            raise IsaError(f"delay {dv} does not fit in {4 - sc} bit(s) (side count {sc})")
    return ((sv << (4 - sc)) | dv) & 0xF


def split_tail(t: int, side_count: int):
    sc = min(side_count, 4)
    side = (t >> (4 - sc)) if sc else None
    delay = t & ((1 << (4 - sc)) - 1)
    return side, delay


# ---------------------------------------------------------------------------
# Encoders
# ---------------------------------------------------------------------------
def enc_ctl(func: int, a5: int = 0, a4: int = 0, t: int = 0) -> int:
    _chk("ctl func", func, 0, 7)
    return (OP_CTL << 12) | (func << 8) | ((a5 & 1) << 5) | ((a4 & 1) << 4) | (t & 0xF)


def enc_nop(t: int = 0) -> int:
    return enc_ctl(CTL_NOP, t=t)


def enc_halt(t: int = 0) -> int:
    return enc_ctl(CTL_HALT, t=t)


def enc_irq(t: int = 0) -> int:
    return enc_ctl(CTL_IRQ, t=t)


def enc_push(iffull: bool = False, block: bool = True, t: int = 0) -> int:
    return enc_ctl(CTL_PUSH, int(iffull), int(block), t)


def enc_pull(ifempty: bool = False, block: bool = True, t: int = 0) -> int:
    return enc_ctl(CTL_PULL, int(ifempty), int(block), t)


def enc_sync(half: bool = False, t: int = 0) -> int:
    return enc_ctl(CTL_SYNC, 0, int(half), t)


def enc_hclr(t: int = 0) -> int:
    return enc_ctl(CTL_HCLR, t=t)


def enc_clr(isr: bool = True, osr: bool = False, t: int = 0) -> int:
    if not (isr or osr):
        raise IsaError("clr needs isr and/or osr")
    return enc_ctl(CTL_CLR, int(osr), int(isr), t)


def enc_jmp(cond: Union[str, int], target: int, t: int = 0) -> int:
    c = JMP_CONDS[cond] if isinstance(cond, str) else _chk("jmp cond", cond, 0, 7)
    _chk("jump target", target, 0, IMEM_DEPTH - 1)
    return (OP_JMP << 12) | (c << 9) | (target << 4) | (t & 0xF)


def enc_jpin(pin: PinLike, level: int, target: int) -> int:
    _chk("level", level, 0, 1)
    _chk("jump target", target, 0, IMEM_DEPTH - 1)
    return (OP_JPIN << 12) | (pin_index(pin) << 8) | (level << 7) | target


def enc_wait(pin: PinLike, kind: str, sync: Optional[str] = None, t: int = 0) -> int:
    """kind: high|low|rise|fall; sync: None|'full'|'half'."""
    try:
        pol, edge = {"high": (1, 0), "low": (0, 0), "rise": (1, 1), "fall": (0, 1)}[kind]
    except KeyError:
        raise IsaError("wait kind must be high|low|rise|fall") from None
    if sync not in (None, "full", "half"):
        raise IsaError("wait sync must be None|'full'|'half'")
    s = 0 if sync is None else 1
    hb = 1 if sync == "half" else 0
    return ((OP_WAIT << 12) | (pin_index(pin) << 8) | (pol << 7) | (edge << 6) | (s << 5)
            | (hb << 4) | (t & 0xF))


def enc_in_pins(base: PinLike, n: int, t: int = 0) -> int:
    _chk("pin count", n, 1, 8)
    return (OP_IN << 12) | (pin_index(base) << 7) | ((n - 1) << 4) | (t & 0xF)


def enc_in_reg(src: str, n: int, t: int = 0) -> int:
    if src not in IN_SRC:
        raise IsaError(f"in source must be one of {list(IN_SRC)}")
    _chk("bit count", n, 1, 32)
    return (OP_IN << 12) | (1 << 11) | (IN_SRC[src] << 9) | ((n & 31) << 4) | (t & 0xF)


def enc_out_pins(base: PinLike, n: int, t: int = 0) -> int:
    _chk("pin count", n, 1, 8)
    return (OP_OUT << 12) | (pin_index(base) << 7) | ((n - 1) << 4) | (t & 0xF)


def enc_out_reg(dst: str, n: int, t: int = 0) -> int:
    if dst not in OUT_DST:
        raise IsaError(f"out destination must be one of {list(OUT_DST)}")
    _chk("bit count", n, 1, 32)
    return (OP_OUT << 12) | (1 << 11) | (OUT_DST[dst] << 9) | ((n & 31) << 4) | (t & 0xF)


def enc_setp(pin: PinLike, func: Union[str, int], value: int = 0, t: int = 0) -> int:
    f = SETP_FUNCS[func] if isinstance(func, str) else _chk("setp func", func, 0, 3)
    _chk("value", value, 0, 1)
    if f == 2:
        value = 0
    return (OP_SETP << 12) | (pin_index(pin) << 8) | (f << 6) | (value << 5) | (t & 0xF)


def enc_mov(dst: str, src: str, op: str = "", t: int = 0) -> int:
    if dst not in MOV_DST or src not in MOV_SRC or op not in MOV_OPS:
        raise IsaError(f"bad mov {dst}, {op}{src}")
    return ((OP_MOV << 12) | (MOV_DST[dst] << 9) | (MOV_OPS[op] << 7) | (MOV_SRC[src] << 4)
            | (t & 0xF))


def enc_alu(reg: str, func: str, imm: int = 0) -> int:
    if reg not in ("x", "y"):
        raise IsaError("ALU register must be x|y")
    if func not in ALU_FUNCS or func == "crc":
        raise IsaError(f"bad ALU function {func}")
    if func in ("dec", "inc"):
        imm = 0
    imm = int(imm)
    if not -128 <= imm <= 255:
        raise IsaError("immediate out of 8-bit range")
    return (OP_ALU << 12) | ((reg == "y") << 11) | (ALU_FUNCS[func] << 8) | (imm & 0xFF)


def enc_crc(msb: bool = False) -> int:
    return (OP_ALU << 12) | (6 << 8) | (1 if msb else 0)


def enc_dly(n: Optional[int] = None, ticks: bool = False, xy: bool = False) -> int:
    if xy:
        if n is not None:
            raise IsaError("dly xy takes no immediate")
        return (OP_DLY << 12) | (int(ticks) << 11) | (1 << 10)
    _chk("immediate delay", n, 0, 1023)
    return (OP_DLY << 12) | (int(ticks) << 11) | n


# ---------------------------------------------------------------------------
# Disassembler. Exact: assemble(disassemble(w)) == w for all 65536 words.
# Non-canonical encodings (reserved bits set) are emitted as `.word`.
# ---------------------------------------------------------------------------
def disassemble(w: int, side_count: int = 0) -> str:
    w &= 0xFFFF
    op = (w >> 12) & 0xF
    word = f".word 0x{w:04x}"
    suffix = ""
    if op in CLASS_A:
        side, delay = split_tail(w & 0xF, side_count)
        if side is not None:
            suffix += f" side {side}"
        if delay:
            suffix += f" [{delay}]"
    if op == OP_CTL:
        f, a = (w >> 8) & 0xF, (w >> 4) & 0xF
        a5, a4 = (a >> 1) & 1, a & 1
        if f in (CTL_NOP, CTL_HALT, CTL_IRQ, CTL_HCLR) and a == 0:
            return {0: "nop", 1: "halt", 2: "irq", 6: "hclr"}[f] + suffix
        if f in (CTL_PUSH, CTL_PULL) and a < 4:
            kw = "iffull" if f == CTL_PUSH else "ifempty"
            return ("push" if f == CTL_PUSH else "pull") + (f" {kw}" if a5 else "") + \
                ("" if a4 else " noblock") + suffix
        if f == CTL_SYNC and a < 2:
            return "sync" + (" half" if a4 else "") + suffix
        if f == CTL_CLR and 0 < a < 4:
            return "clr " + {1: "isr", 2: "osr", 3: "all"}[a] + suffix
        return word
    if op == OP_JMP:
        c, tg = (w >> 9) & 7, (w >> 4) & 31
        return (f"jmp {JMP_COND_NAMES[c]}, {tg}" if c else f"jmp {tg}") + suffix
    if op == OP_JPIN:
        if (w >> 5) & 3:
            return word
        return f"jpin {(w >> 8) & 15}, {(w >> 7) & 1}, {w & 31}"
    if op == OP_WAIT:
        pol, edge, syn, half = (w >> 7) & 1, (w >> 6) & 1, (w >> 5) & 1, (w >> 4) & 1
        if half and not syn:
            return word
        kind = ("rise" if pol else "fall") if edge else ("high" if pol else "low")
        opt = (" synchalf" if half else " sync") if syn else ""
        return f"wait {kind} {(w >> 8) & 15}{opt}" + suffix
    if op in (OP_IN, OP_OUT):
        mn = "in" if op == OP_IN else "out"
        if not (w >> 11) & 1:
            return f"{mn} pins, {(w >> 7) & 15}, {((w >> 4) & 7) + 1}" + suffix
        tab = IN_SRC if op == OP_IN else OUT_DST
        name = [k for k, v in tab.items() if v == (w >> 9) & 3][0]
        return f"{mn} {name}, {((w >> 4) & 31) or 32}" + suffix
    if op == OP_SETP:
        f, v = (w >> 6) & 3, (w >> 5) & 1
        if (w >> 4) & 1 or (f == 2 and v):
            return word
        pin = (w >> 8) & 15
        if f == 2:
            return f"toggle {pin}" + suffix
        return f"{['set', 'dir', '', 'drive'][f]} {pin}, {v}" + suffix
    if op == OP_MOV:
        d = [k for k, v in MOV_DST.items() if v == (w >> 9) & 7][0]
        s = [k for k, v in MOV_SRC.items() if v == (w >> 4) & 7][0]
        o = {0: "", 1: "~", 2: "rev ", 3: "par "}[(w >> 7) & 3]
        return f"mov {d}, {o}{s}" + suffix
    if op == OP_ALU:
        r = "y" if (w >> 11) & 1 else "x"
        f, imm = (w >> 8) & 7, w & 0xFF
        if f == 6:
            if (w >> 11) & 1 or imm > 1:
                return word
            return "crc " + ("msb" if imm else "lsb")
        if f == 7:
            return word
        if f in (4, 5):
            return word if imm else f"{['', '', '', '', 'dec', 'inc'][f]} {r}"
        return f"{['ldi', 'and', 'or', 'xor'][f]} {r}, 0x{imm:02x}"
    if op == OP_DLY:
        mn = "dlyt" if (w >> 11) & 1 else "dly"
        if (w >> 10) & 1:
            return word if w & 0x3FF else f"{mn} xy"
        return f"{mn} {w & 0x3FF}"
    return word


# ---------------------------------------------------------------------------
# Configuration registers
# ---------------------------------------------------------------------------
CFG_NAMES = [
    "div_int_l", "div_int_h", "div_frac", "shiftctl", "pull_thresh", "push_thresh",
    "sidectl", "wrap_top", "wrap_bot", "entry", "od_mask", "crc_poly_l", "crc_poly_h",
    "init_bio_out", "init_bio_oe", "init_tout",
]
CFG_DEFAULTS = [0, 0, 0, 0x03, 8, 8, 0, 31, 0, 0, 0, 0, 0, 0, 0, 0]
CFG_MASKS = [0xFF, 0xFF, 0xFF, 0x0F, 0x1F, 0x1F, 0xFF, 0x1F, 0x1F, 0x1F, 0xFF, 0xFF, 0xFF,
             0xFF, 0xFF, 0x7F]


def divider_from_ratio(ratio: float):
    """clk/tick ratio -> (DIV_INT, DIV_FRAC). ratio < 1 means tick every clk."""
    if ratio < 1.0:
        return 0, 0
    i = int(ratio)
    f = int(round((ratio - i) * 256))
    if f == 256:
        i, f = i + 1, 0
    if i > 0xFFFF:
        raise IsaError("divider too large (max 65535.996)")
    return i, f


@dataclass
class Config:
    """The 16 PESM configuration registers (docs/ISA.md section 5)."""
    div_int: int = 0
    div_frac: int = 0
    out_right: bool = True
    in_right: bool = True
    autopull: bool = False
    autopush: bool = False
    pull_thresh: int = 8
    push_thresh: int = 8
    side_count: int = 0
    side_pindir: bool = False
    side_base: int = 0
    wrap_top: int = 31
    wrap_bot: int = 0
    entry: int = 0
    od_mask: int = 0
    crc_poly: int = 0
    init_bio_out: int = 0
    init_bio_oe: int = 0
    init_tout: int = 0

    def to_bytes(self) -> List[int]:
        b = [
            self.div_int & 0xFF, (self.div_int >> 8) & 0xFF, self.div_frac & 0xFF,
            (int(self.out_right) | (int(self.in_right) << 1) | (int(self.autopull) << 2)
             | (int(self.autopush) << 3)),
            self.pull_thresh & 31, self.push_thresh & 31,
            ((self.side_base & 15) << 4) | (int(self.side_pindir) << 3) | (self.side_count & 7),
            self.wrap_top & 31, self.wrap_bot & 31, self.entry & 31, self.od_mask & 0xFF,
            self.crc_poly & 0xFF, (self.crc_poly >> 8) & 0xFF,
            self.init_bio_out & 0xFF, self.init_bio_oe & 0xFF, self.init_tout & 0x7F,
        ]
        return b

    @classmethod
    def from_bytes(cls, b: Sequence[int]) -> "Config":
        b = [int(x) & m for x, m in zip(b, CFG_MASKS)]
        return cls(div_int=b[0] | (b[1] << 8), div_frac=b[2], out_right=bool(b[3] & 1),
                   in_right=bool(b[3] & 2), autopull=bool(b[3] & 4), autopush=bool(b[3] & 8),
                   pull_thresh=b[4], push_thresh=b[5], side_count=b[6] & 7,
                   side_pindir=bool(b[6] & 8), side_base=b[6] >> 4, wrap_top=b[7], wrap_bot=b[8],
                   entry=b[9], od_mask=b[10], crc_poly=b[11] | (b[12] << 8), init_bio_out=b[13],
                   init_bio_oe=b[14], init_tout=b[15])

    def set_tick_hz(self, hz: float, clk_hz: float = CLK_HZ_DEFAULT) -> "Config":
        self.div_int, self.div_frac = divider_from_ratio(clk_hz / float(hz))
        return self

    def tick_period(self) -> float:
        return max(1.0, self.div_int + self.div_frac / 256.0) if self.div_int else 1.0


# ---------------------------------------------------------------------------
# Program image and output formats
# ---------------------------------------------------------------------------
@dataclass
class Image:
    """A loadable program: 32 instruction words + 16 config bytes."""
    words: List[int] = field(default_factory=lambda: [INSTR_HALT] * IMEM_DEPTH)
    cfg: List[int] = field(default_factory=lambda: list(CFG_DEFAULTS))
    labels: Dict[str, int] = field(default_factory=dict)
    length: int = 0
    source: Dict[int, str] = field(default_factory=dict)
    name: str = "pesm"

    def __post_init__(self):
        if len(self.words) != IMEM_DEPTH or len(self.cfg) != 16:
            raise IsaError("image needs 32 words and 16 config bytes")

    @property
    def config(self) -> Config:
        return Config.from_bytes(self.cfg)

    @property
    def side_count(self) -> int:
        return min(self.cfg[6] & 7, 4)

    # ---- formats ----
    def to_bin(self) -> bytes:
        """80 bytes: 32 big-endian words (WRITE_IMEM payload) + 16 cfg bytes (WRITE_CFG payload)."""
        out = bytearray()
        for w in self.words:
            out += bytes([(w >> 8) & 0xFF, w & 0xFF])
        out += bytes(c & 0xFF for c in self.cfg)
        return bytes(out)

    @classmethod
    def from_bin(cls, data: bytes, name: str = "pesm") -> "Image":
        if len(data) != 80:
            raise IsaError("PESM .bin must be exactly 80 bytes")
        words = [(data[2 * i] << 8) | data[2 * i + 1] for i in range(IMEM_DEPTH)]
        return cls(words=words, cfg=list(data[64:80]), length=IMEM_DEPTH, name=name)

    def to_mem(self) -> str:
        """$readmemh image of the instruction memory (one 16-bit word per line)."""
        return "// PESM imem, $readmemh\n@0\n" + "".join(f"{w:04x}\n" for w in self.words)

    def to_cfg_mem(self) -> str:
        return "// PESM cfg, $readmemh\n@0\n" + "".join(f"{c:02x}\n" for c in self.cfg)

    def to_py(self) -> str:
        ws = ", ".join(f"0x{w:04x}" for w in self.words)
        cs = ", ".join(f"0x{c:02x}" for c in self.cfg)
        return f"{self.name.upper()}_IMEM = [{ws}]\n{self.name.upper()}_CFG = [{cs}]\n"

    def to_json(self) -> str:
        return json.dumps({"name": self.name, "imem": self.words, "cfg": self.cfg,
                           "cfg_fields": vars(self.config), "labels": self.labels},
                          indent=2)

    def listing(self) -> str:
        inv: Dict[int, List[str]] = {}
        for k, v in self.labels.items():
            inv.setdefault(v, []).append(k)
        out = []
        for a in range(max(self.length, 1)):
            for lab in inv.get(a, []):
                out.append(f"{lab}:")
            w = self.words[a]
            src = self.source.get(a, "")
            out.append(f"  {a:2d}  {w:04x}   {disassemble(w, self.side_count):32s}"
                       + (f" ; {src}" if src else ""))
        out.append("config: " + " ".join(f"{n}={v:#04x}" for n, v in zip(CFG_NAMES, self.cfg)))
        return "\n".join(out)

    def to_asm(self) -> str:
        """Re-assemblable source (raw .cfg directives + disassembly)."""
        lines = [f"; {self.name}"] + [f".cfg {i} 0x{c:02x}" for i, c in enumerate(self.cfg)]
        inv = {v: k for k, v in self.labels.items()}
        for a in range(self.length):
            if a in inv:
                lines.append(f"{inv[a]}:")
            lines.append(f"    {disassemble(self.words[a], self.side_count)}")
        return "\n".join(lines) + "\n"
