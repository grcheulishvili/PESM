#!/usr/bin/env python3
# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""
PESM v2 assembler / disassembler / host-frame builder.

    python3 tools/pesm_asm.py firmware/uart_tx.pasm            # listing
    python3 tools/pesm_asm.py firmware/uart_tx.pasm -o out.hex # hex words
    python3 tools/pesm_asm.py firmware/uart_tx.pasm --spi      # host SPI frames

See docs/ISA.md for the instruction set.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

IMEM_DEPTH = 32
CLK_HZ_DEFAULT = 50_000_000

# ---------------------------------------------------------------------------
# Encoding constants
# ---------------------------------------------------------------------------
OP_CTL, OP_JMP, OP_JPIN, OP_WAIT, OP_IN, OP_OUT, OP_SETP, OP_MOV, OP_ALU, OP_DLY = range(10)
CLASS_A = {OP_CTL, OP_JMP, OP_WAIT, OP_IN, OP_OUT, OP_SETP, OP_MOV}

CTL_FUNCS = {"nop": 0, "halt": 1, "irq": 2, "push": 3, "pull": 4, "sync": 5, "hclr": 6, "clr": 7}
JMP_CONDS = {"": 0, "always": 0, "!x": 1, "x--": 2, "!y": 3, "y--": 4, "x!=y": 5, "!osre": 6, "lb": 7}
IN_SRC = {"x": 0, "y": 1, "null": 2, "osr": 3}
OUT_DST = {"x": 0, "y": 1, "null": 2, "pindirs": 3}
SETP_FUNCS = {"set": 0, "dir": 1, "toggle": 2, "drive": 3}
MOV_DST = {"x": 0, "y": 1, "isr": 2, "osr": 3, "pins": 4, "pindirs": 5, "tout": 6, "pc": 7}
MOV_SRC = {"x": 0, "y": 1, "isr": 2, "osr": 3, "pins": 4, "pinshi": 5, "null": 6, "lb": 7}
MOV_OPS = {"": 0, "~": 1, "!": 1, "rev": 2, "::": 2, "par": 3}
ALU_FUNCS = {"ldi": 0, "and": 1, "or": 2, "xor": 3, "dec": 4, "inc": 5, "crc": 6}

# Config register map (address -> name)
CFG_NAMES = [
    "div_int_l", "div_int_h", "div_frac", "shiftctl", "pull_thresh", "push_thresh",
    "sidectl", "wrap_top", "wrap_bot", "entry", "od_mask", "crc_poly_l", "crc_poly_h",
    "init_bio_out", "init_bio_oe", "init_tout",
]
CFG_DEFAULTS = [0, 0, 0, 0x03, 8, 8, 0, 31, 0, 0, 0, 0, 0, 0, 0, 0]

PIN_ALIASES: Dict[str, int] = {}
for _i in range(8):
    PIN_ALIASES[f"bio{_i}"] = _i
for _i in range(7):
    PIN_ALIASES[f"tout{_i}"] = 8 + _i
for _i in range(4):
    PIN_ALIASES[f"tin{_i}"] = 8 + _i
PIN_ALIASES.update({"txne": 12, "rxnf": 13, "hflag": 14})


class AsmError(Exception):
    pass


@dataclass
class Program:
    words: List[int] = field(default_factory=lambda: [0x0100] * IMEM_DEPTH)
    used: List[bool] = field(default_factory=lambda: [False] * IMEM_DEPTH)
    cfg: List[int] = field(default_factory=lambda: list(CFG_DEFAULTS))
    labels: Dict[str, int] = field(default_factory=dict)
    source: List[Tuple[int, str]] = field(default_factory=list)  # (addr, text)
    length: int = 0

    @property
    def side_count(self) -> int:
        return min(self.cfg[6] & 7, 4)

    def cfg_dict(self) -> Dict[str, int]:
        return {n: v for n, v in zip(CFG_NAMES, self.cfg)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_int(tok: str, defines: Dict[str, int]) -> int:
    t = tok.strip().lower()
    if t in defines:
        return defines[t]
    if t in PIN_ALIASES:
        return PIN_ALIASES[t]
    try:
        return int(t, 0)
    except ValueError as exc:
        raise AsmError(f"bad number '{tok}'") from exc


def parse_pin(tok: str, defines: Dict[str, int]) -> int:
    v = parse_int(tok, defines)
    if not 0 <= v <= 15:
        raise AsmError(f"pin out of range: {tok}")
    return v


def div_from_float(div: float) -> Tuple[int, int]:
    if div < 1.0:
        return 0, 0
    i = int(div)
    f = int(round((div - i) * 256))
    if f == 256:
        i, f = i + 1, 0
    if i > 0xFFFF:
        raise AsmError("divider too large")
    return i, f


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------
def _split_suffixes(text: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Strip '[d]' and 'side v' suffixes. Returns (body, side, delay)."""
    delay = None
    m = re.search(r"\[\s*([^\]]+)\s*\]\s*$", text)
    if m:
        delay = m.group(1)
        text = text[: m.start()].rstrip()
    side = None
    m = re.search(r"\bside\s+(\S+)\s*$", text)
    if m:
        side = m.group(1)
        text = text[: m.start()].rstrip()
    m = re.search(r"\[\s*([^\]]+)\s*\]\s*$", text)
    if m and delay is None:
        delay = m.group(1)
        text = text[: m.start()].rstrip()
    return text, side, delay


def _args(s: str) -> List[str]:
    s = s.strip()
    if not s:
        return []
    return [a.strip() for a in re.split(r"[,\s]+", s) if a.strip()]


def assemble(text: str, clk_hz: int = CLK_HZ_DEFAULT) -> Program:
    prog = Program()
    defines: Dict[str, int] = {}
    lines: List[Tuple[int, str]] = []
    for ln, raw in enumerate(text.splitlines(), 1):
        line = raw.split(";")[0].split("//")[0].split("#")[0].strip()
        if line:
            lines.append((ln, line))

    # ---------------- pass 1: directives, labels, addresses ----------------
    addr = 0
    pending: List[Tuple[int, int, str]] = []  # (lineno, addr, instr)
    wrap_target: Optional[int] = None
    wrap_top: Optional[int] = None
    entry_label: Optional[str] = None
    for ln, line in lines:
        try:
            while True:
                m = re.match(r"^([A-Za-z_][\w]*):\s*(.*)$", line)
                if not m:
                    break
                name = m.group(1).lower()
                if name in prog.labels:
                    raise AsmError(f"duplicate label {name}")
                prog.labels[name] = addr
                line = m.group(2).strip()
            if not line:
                continue
            if line.startswith(".") and not line.lower().startswith(".word"):
                parts = line.split()
                d = parts[0].lower()
                a = parts[1:]
                if d == ".define":
                    defines[a[0].lower()] = parse_int(a[1], defines)
                elif d == ".clk_hz":
                    clk_hz = int(float(a[0]))
                elif d == ".div":
                    i, f = div_from_float(float(a[0]))
                    prog.cfg[0], prog.cfg[1], prog.cfg[2] = i & 0xFF, i >> 8, f
                elif d == ".div_raw":
                    i, f = parse_int(a[0], defines), parse_int(a[1], defines)
                    prog.cfg[0], prog.cfg[1], prog.cfg[2] = i & 0xFF, (i >> 8) & 0xFF, f & 0xFF
                elif d == ".tick_hz":
                    i, f = div_from_float(clk_hz / float(a[0]))
                    prog.cfg[0], prog.cfg[1], prog.cfg[2] = i & 0xFF, i >> 8, f
                elif d == ".shift":
                    for kv in a:
                        k, v = kv.lower().split("=")
                        bit = {"out": 0, "in": 1}[k]
                        if v == "right":
                            prog.cfg[3] |= 1 << bit
                        elif v == "left":
                            prog.cfg[3] &= ~(1 << bit)
                        else:
                            raise AsmError("shift dir must be left|right")
                elif d == ".autopull":
                    prog.cfg[3] |= 4
                    if a:
                        prog.cfg[4] = parse_int(a[0], defines) & 31
                elif d == ".autopush":
                    prog.cfg[3] |= 8
                    if a:
                        prog.cfg[5] = parse_int(a[0], defines) & 31
                elif d == ".pull_thresh":
                    prog.cfg[4] = parse_int(a[0], defines) & 31
                elif d == ".push_thresh":
                    prog.cfg[5] = parse_int(a[0], defines) & 31
                elif d == ".side":
                    cnt = parse_int(a[0], defines)
                    if not 0 <= cnt <= 4:
                        raise AsmError("side count 0..4")
                    base, pindir = 0, 0
                    for kv in a[1:]:
                        if kv.lower() == "pindir":
                            pindir = 1
                        elif kv.lower().startswith("base="):
                            base = parse_pin(kv.split("=")[1], defines)
                        else:
                            raise AsmError(f"bad .side option {kv}")
                    prog.cfg[6] = (base << 4) | (pindir << 3) | cnt
                elif d == ".wrap_target":
                    wrap_target = addr
                elif d == ".wrap":
                    if addr == 0:
                        raise AsmError(".wrap before any instruction")
                    wrap_top = addr - 1
                elif d == ".entry":
                    entry_label = a[0].lower()
                elif d == ".org":
                    addr = parse_int(a[0], defines)
                elif d == ".od":
                    prog.cfg[10] = parse_int(a[0], defines) & 0xFF
                elif d == ".crc_poly":
                    p = parse_int(a[0], defines) & 0xFFFF
                    prog.cfg[11], prog.cfg[12] = p & 0xFF, p >> 8
                elif d == ".init_out":
                    prog.cfg[13] = parse_int(a[0], defines) & 0xFF
                elif d == ".init_oe":
                    prog.cfg[14] = parse_int(a[0], defines) & 0xFF
                elif d == ".init_tout":
                    prog.cfg[15] = parse_int(a[0], defines) & 0x7F
                elif d == ".cfg":
                    prog.cfg[parse_int(a[0], defines) & 15] = parse_int(a[1], defines) & 0xFF
                else:
                    raise AsmError(f"unknown directive {d}")
                continue
            if addr >= IMEM_DEPTH:
                raise AsmError("program exceeds 32 instructions")
            pending.append((ln, addr, line))
            addr += 1
        except AsmError as e:
            raise AsmError(f"line {ln}: {e}") from None

    if wrap_target is not None:
        prog.cfg[8] = wrap_target
    if wrap_top is not None:
        prog.cfg[7] = wrap_top
    if entry_label is not None:
        if entry_label not in prog.labels:
            raise AsmError(f"unknown entry label {entry_label}")
        prog.cfg[9] = prog.labels[entry_label]

    # ---------------- pass 2: encode ----------------
    sc = prog.side_count
    for ln, a, line in pending:
        try:
            w = encode_line(line, sc, prog.labels, defines)
        except AsmError as e:
            raise AsmError(f"line {ln}: {e}") from None
        if prog.used[a]:
            raise AsmError(f"line {ln}: address {a} used twice")
        prog.words[a] = w
        prog.used[a] = True
        prog.source.append((a, line))
        prog.length = max(prog.length, a + 1)
    return prog


def _target(tok: str, labels: Dict[str, int], defines: Dict[str, int]) -> int:
    t = tok.lower()
    if t in labels:
        return labels[t]
    v = parse_int(t, defines)
    if not 0 <= v < IMEM_DEPTH:
        raise AsmError(f"jump target out of range: {tok}")
    return v


def _tail(op: int, side: Optional[str], delay: Optional[str], sc: int, defines) -> int:
    if op not in CLASS_A:
        if side is not None or delay is not None:
            raise AsmError("this instruction takes no side-set / delay")
        return 0
    sv = 0
    if side is not None:
        if sc == 0:
            raise AsmError("side-set used but .side count is 0")
        sv = parse_int(side, defines)
        if not 0 <= sv < (1 << sc):
            raise AsmError(f"side value {sv} does not fit in {sc} bit(s)")
    dv = 0
    if delay is not None:
        dv = parse_int(delay, defines)
        if not 0 <= dv < (1 << (4 - sc)):
            raise AsmError(f"delay {dv} does not fit in {4 - sc} bit(s) (side count {sc})")
    return ((sv << (4 - sc)) | dv) & 0xF


def encode_line(line: str, sc: int, labels: Dict[str, int], defines: Dict[str, int]) -> int:
    body, side, delay = _split_suffixes(line)
    toks = body.split(None, 1)
    mn = toks[0].lower()
    rest = toks[1] if len(toks) > 1 else ""
    args = _args(rest)
    la = [x.lower() for x in args]

    def tail(op):
        return _tail(op, side, delay, sc, defines)

    def need(n):
        if len(args) != n:
            raise AsmError(f"'{mn}' expects {n} operand(s), got {len(args)}")

    # ---- CTL ----
    if mn in ("nop", "halt", "irq", "hclr"):
        need(0)
        return (OP_CTL << 12) | (CTL_FUNCS[mn] << 8) | tail(OP_CTL)
    if mn in ("push", "pull"):
        cond_kw = "iffull" if mn == "push" else "ifempty"
        flags = set(la)
        if not flags <= {cond_kw, "noblock", "block"}:
            raise AsmError(f"bad {mn} options {args}")
        a5 = 1 if cond_kw in flags else 0
        a4 = 0 if "noblock" in flags else 1
        return (OP_CTL << 12) | (CTL_FUNCS[mn] << 8) | (a5 << 5) | (a4 << 4) | tail(OP_CTL)
    if mn == "sync":
        if la not in ([], ["half"]):
            raise AsmError("sync [half]")
        return (OP_CTL << 12) | (5 << 8) | ((1 if la else 0) << 4) | tail(OP_CTL)
    if mn == "clr":
        need(1)
        bits = {"isr": 1, "osr": 2, "all": 3}.get(la[0])
        if bits is None:
            raise AsmError("clr isr|osr|all")
        return (OP_CTL << 12) | (7 << 8) | (bits << 4) | tail(OP_CTL)

    # ---- JMP ----
    if mn == "jmp":
        if len(args) == 1:
            cond, tgt = "", args[0]
        elif len(args) == 2:
            cond, tgt = la[0], args[1]
        else:
            raise AsmError("jmp [cond,] target")
        if cond not in JMP_CONDS:
            raise AsmError(f"bad jmp condition '{cond}'")
        t = _target(tgt, labels, defines)
        return (OP_JMP << 12) | (JMP_CONDS[cond] << 9) | (t << 4) | tail(OP_JMP)

    if mn == "jpin":
        need(3)
        pin = parse_pin(args[0], defines)
        lvl = parse_int(args[1], defines)
        if lvl not in (0, 1):
            raise AsmError("jpin level must be 0|1")
        t = _target(args[2], labels, defines)
        tail(OP_JPIN)
        return (OP_JPIN << 12) | (pin << 8) | (lvl << 7) | t

    # ---- WAIT ----
    if mn == "wait":
        if len(args) not in (2, 3):
            raise AsmError("wait high|low|rise|fall pin [sync|synchalf]")
        kind = la[0]
        pol, edge = {"high": (1, 0), "low": (0, 0), "rise": (1, 1), "fall": (0, 1)}.get(kind, (None, None))
        if pol is None:
            raise AsmError("wait kind must be high|low|rise|fall")
        pin = parse_pin(args[1], defines)
        syn, half = 0, 0
        if len(args) == 3:
            if la[2] == "sync":
                syn = 1
            elif la[2] == "synchalf":
                syn, half = 1, 1
            else:
                raise AsmError("wait option must be sync|synchalf")
        return ((OP_WAIT << 12) | (pin << 8) | (pol << 7) | (edge << 6) | (syn << 5) | (half << 4)
                | tail(OP_WAIT))

    # ---- IN / OUT ----
    if mn in ("in", "out"):
        op = OP_IN if mn == "in" else OP_OUT
        if la and la[0] == "pins":
            need(3)
            base = parse_pin(args[1], defines)
            n = parse_int(args[2], defines)
            if not 1 <= n <= 8:
                raise AsmError("pin count 1..8")
            return (op << 12) | (base << 7) | ((n - 1) << 4) | tail(op)
        need(2)
        table = IN_SRC if op == OP_IN else OUT_DST
        if la[0] not in table:
            raise AsmError(f"bad {mn} operand '{args[0]}'")
        n = parse_int(args[1], defines)
        if not 1 <= n <= 32:
            raise AsmError("bit count 1..32")
        return (op << 12) | (1 << 11) | (table[la[0]] << 9) | ((n & 31) << 4) | tail(op)

    # ---- SETP ----
    if mn in SETP_FUNCS:
        f = SETP_FUNCS[mn]
        if f == 2:
            need(1)
            v = 0
        else:
            need(2)
            v = parse_int(args[1], defines)
            if v not in (0, 1):
                raise AsmError("value must be 0|1")
        pin = parse_pin(args[0], defines)
        return (OP_SETP << 12) | (pin << 8) | (f << 6) | (v << 5) | tail(OP_SETP)

    # ---- MOV ----
    if mn == "mov":
        m = re.match(r"^\s*(\w+)\s*,\s*(~|!|::|rev\s+|par\s+)?\s*(\w+)\s*$", rest.lower())
        if not m:
            raise AsmError("mov dst, [~|rev|par] src")
        dst, opn, src = m.group(1), (m.group(2) or "").strip(), m.group(3)
        if dst not in MOV_DST or src not in MOV_SRC or opn not in MOV_OPS:
            raise AsmError("bad mov operands")
        return ((OP_MOV << 12) | (MOV_DST[dst] << 9) | (MOV_OPS[opn] << 7) | (MOV_SRC[src] << 4)
                | tail(OP_MOV))

    # ---- ALU ----
    if mn in ("ldi", "and", "or", "xor"):
        need(2)
        if la[0] not in ("x", "y"):
            raise AsmError("ALU destination must be x|y")
        imm = parse_int(args[1], defines)
        if not -128 <= imm <= 255:
            raise AsmError("immediate out of 8-bit range")
        tail(OP_ALU)
        return (OP_ALU << 12) | ((la[0] == "y") << 11) | (ALU_FUNCS[mn] << 8) | (imm & 0xFF)
    if mn in ("dec", "inc"):
        need(1)
        if la[0] not in ("x", "y"):
            raise AsmError("ALU destination must be x|y")
        tail(OP_ALU)
        return (OP_ALU << 12) | ((la[0] == "y") << 11) | (ALU_FUNCS[mn] << 8)
    if mn == "crc":
        need(1)
        if la[0] not in ("lsb", "msb"):
            raise AsmError("crc lsb|msb")
        tail(OP_ALU)
        return (OP_ALU << 12) | (6 << 8) | (1 if la[0] == "msb" else 0)

    # ---- DLY ----
    if mn in ("dly", "dlyt"):
        need(1)
        unit = 1 if mn == "dlyt" else 0
        tail(OP_DLY)
        if la[0] == "xy":
            return (OP_DLY << 12) | (unit << 11) | (1 << 10)
        n = parse_int(args[0], defines)
        if not 0 <= n <= 1023:
            raise AsmError("immediate delay 0..1023 (use xy for longer)")
        return (OP_DLY << 12) | (unit << 11) | n

    if mn == ".word":
        need(1)
        return parse_int(args[0], defines) & 0xFFFF

    raise AsmError(f"unknown mnemonic '{mn}'")


# ---------------------------------------------------------------------------
# Disassembler
# ---------------------------------------------------------------------------
def disassemble(w: int, sc: int = 0) -> str:
    op = (w >> 12) & 0xF
    tail = w & 0xF
    suffix = ""
    if op in CLASS_A:
        sv = tail >> (4 - sc) if sc else 0
        dv = tail & ((1 << (4 - sc)) - 1)
        if sc:
            suffix += f" side {sv}"
        if dv:
            suffix += f" [{dv}]"
    if op == OP_CTL:
        f = (w >> 8) & 0xF
        a5, a4 = (w >> 5) & 1, (w >> 4) & 1
        if f == 3:
            s = "push" + (" iffull" if a5 else "") + ("" if a4 else " noblock")
        elif f == 4:
            s = "pull" + (" ifempty" if a5 else "") + ("" if a4 else " noblock")
        elif f == 5:
            s = "sync" + (" half" if a4 else "")
        elif f == 7:
            s = "clr " + {0: "none", 1: "isr", 2: "osr", 3: "all"}[(a5 << 1) | a4]
        else:
            s = {0: "nop", 1: "halt", 2: "irq", 6: "hclr"}.get(f, f"nop ; reserved ctl {f}")
        return s + suffix
    if op == OP_JMP:
        c = (w >> 9) & 7
        cn = [k for k, v in JMP_CONDS.items() if v == c and k not in ("", "always")]
        t = (w >> 4) & 31
        return (f"jmp {cn[0]}, {t}" if c else f"jmp {t}") + suffix
    if op == OP_JPIN:
        return f"jpin {(w >> 8) & 15}, {(w >> 7) & 1}, {w & 31}"
    if op == OP_WAIT:
        pol, edge, syn, half = (w >> 7) & 1, (w >> 6) & 1, (w >> 5) & 1, (w >> 4) & 1
        kind = ("rise" if pol else "fall") if edge else ("high" if pol else "low")
        opt = (" synchalf" if half else " sync") if syn else ""
        return f"wait {kind} {(w >> 8) & 15}{opt}" + suffix
    if op in (OP_IN, OP_OUT):
        mn = "in" if op == OP_IN else "out"
        if not (w >> 11) & 1:
            return f"{mn} pins, {(w >> 7) & 15}, {((w >> 4) & 7) + 1}" + suffix
        tab = IN_SRC if op == OP_IN else OUT_DST
        name = [k for k, v in tab.items() if v == (w >> 9) & 3][0]
        n = (w >> 4) & 31
        return f"{mn} {name}, {n or 32}" + suffix
    if op == OP_SETP:
        f = (w >> 6) & 3
        name = [k for k, v in SETP_FUNCS.items() if v == f][0]
        pin = (w >> 8) & 15
        if f == 2:
            return f"toggle {pin}" + suffix
        return f"{name} {pin}, {(w >> 5) & 1}" + suffix
    if op == OP_MOV:
        d = [k for k, v in MOV_DST.items() if v == (w >> 9) & 7][0]
        s = [k for k, v in MOV_SRC.items() if v == (w >> 4) & 7][0]
        o = {0: "", 1: "~", 2: "rev ", 3: "par "}[(w >> 7) & 3]
        return f"mov {d}, {o}{s}" + suffix
    if op == OP_ALU:
        r = "y" if (w >> 11) & 1 else "x"
        f = (w >> 8) & 7
        imm = w & 0xFF
        if f == 6:
            return "crc " + ("msb" if imm & 1 else "lsb")
        if f == 7:
            return "nop ; reserved alu"
        name = [k for k, v in ALU_FUNCS.items() if v == f][0]
        if f in (4, 5):
            return f"{name} {r}"
        return f"{name} {r}, 0x{imm:02x}"
    if op == OP_DLY:
        mn = "dlyt" if (w >> 11) & 1 else "dly"
        if (w >> 10) & 1:
            return f"{mn} xy"
        return f"{mn} {w & 0x3FF}"
    return f".word 0x{w:04x} ; illegal"


# ---------------------------------------------------------------------------
# Host SPI frames
# ---------------------------------------------------------------------------
CMD_WRITE_IMEM = 0x00
CMD_READ_IMEM = 0x20
CMD_WRITE_CFG = 0x40
CMD_READ_CFG = 0x60
CMD_WRITE_TX = 0x80
CMD_READ_RX = 0xA0
CMD_READ_STAT = 0xC0
CMD_CONTROL = 0xE0

CTRL_FLUSH_TX = 0x01
CTRL_FLUSH_RX = 0x02
CTRL_HFLAG_SET = 0x04
CTRL_HFLAG_CLR = 0x08
CTRL_CLR_FLAGS = 0x10


def frame_write_imem(addr: int, words: List[int]) -> List[int]:
    out = [CMD_WRITE_IMEM | (addr & 31)]
    for w in words:
        out += [(w >> 8) & 0xFF, w & 0xFF]
    return out


def frame_write_cfg(addr: int, data: List[int]) -> List[int]:
    return [CMD_WRITE_CFG | (addr & 15)] + [d & 0xFF for d in data]


def frames_for_program(prog: Program) -> List[List[int]]:
    """Frames that load a full program + config (core must be in BOOT)."""
    return [frame_write_cfg(0, prog.cfg), frame_write_imem(0, prog.words)]


def listing(prog: Program) -> str:
    out = []
    src = dict(prog.source)
    inv = {}
    for k, v in prog.labels.items():
        inv.setdefault(v, []).append(k)
    for a in range(prog.length):
        for lab in inv.get(a, []):
            out.append(f"{lab}:")
        w = prog.words[a]
        out.append(f"  {a:2d}  {w:04x}   {disassemble(w, prog.side_count):34s} ; {src.get(a, '')}")
    out.append("config: " + " ".join(f"{n}={v:#x}" for n, v in prog.cfg_dict().items()))
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("-o", "--output", help="write 32 hex words (one per line)")
    ap.add_argument("--spi", action="store_true", help="print host SPI frames")
    ap.add_argument("--clk-hz", type=float, default=CLK_HZ_DEFAULT)
    a = ap.parse_args(argv)
    try:
        prog = assemble(open(a.source).read(), int(a.clk_hz))
    except AsmError as e:
        print(f"{a.source}: {e}", file=sys.stderr)
        return 1
    print(listing(prog))
    if a.output:
        with open(a.output, "w") as f:
            for w in prog.words:
                f.write(f"{w:04x}\n")
    if a.spi:
        for fr in frames_for_program(prog):
            print("CS: " + " ".join(f"{b:02x}" for b in fr))
    return 0


if __name__ == "__main__":
    sys.exit(main())
