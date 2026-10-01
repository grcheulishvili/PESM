# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""
PESM v2 text assembler.

    python -m pesm.assembler prog.pasm                       # listing
    python -m pesm.assembler prog.pasm -f bin  -o prog.bin   # 80-byte image
    python -m pesm.assembler prog.pasm -f mem  -o prog.mem   # $readmemh (+ prog_cfg.mem)
    python -m pesm.assembler prog.pasm -f py                 # Python lists
    python -m pesm.assembler prog.pasm -f json -o prog.json
    python -m pesm.assembler prog.pasm --spi                 # host SPI frames

Syntax summary (full reference: docs/ISA.md, docs/DSL_GUIDE.md):

    label:                       ; labels end with ':'
    .define NAME value           ; constants / pin aliases
    .tick_hz 115200  | .div 434.03 | .div_raw I F | .clk_hz 50e6
    .shift out=right in=left     .autopull [th]   .autopush [th]
    .pull_thresh n  .push_thresh n  .side n [base=pin] [pindir]
    .wrap_target    .wrap        .entry label    .org n
    .od mask  .crc_poly p  .init_out v  .init_oe v  .init_tout v  .cfg addr value
    .word 0x1234                 ; raw instruction word

Every class-A instruction accepts `side v` and `[delay]` suffixes.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

from . import isa


class AsmError(isa.IsaError):
    pass


def _int(tok: str, defines: Dict[str, int]) -> int:
    t = tok.strip().lower()
    if t in defines:
        return defines[t]
    if t in isa.PIN_ALIASES:
        return isa.PIN_ALIASES[t]
    try:
        return int(t, 0)
    except ValueError:
        raise AsmError(f"bad number '{tok}'") from None


def _pin(tok: str, defines: Dict[str, int]) -> int:
    v = _int(tok, defines)
    if not 0 <= v <= 15:
        raise AsmError(f"pin out of range: {tok}")
    return v


def _split_suffixes(text: str) -> Tuple[str, Optional[str], Optional[str]]:
    delay = side = None
    for _ in range(2):
        m = re.search(r"\[\s*([^\]]+)\s*\]\s*$", text)
        if m and delay is None:
            delay = m.group(1)
            text = text[: m.start()].rstrip()
            continue
        m = re.search(r"\bside\s+(\S+)\s*$", text)
        if m and side is None:
            side = m.group(1)
            text = text[: m.start()].rstrip()
    return text, side, delay


def _args(s: str) -> List[str]:
    return [a for a in re.split(r"[,\s]+", s.strip()) if a]


def encode_line(line: str, sc: int, labels: Dict[str, int], defines: Dict[str, int]) -> int:
    body, side, delay = _split_suffixes(line)
    toks = body.split(None, 1)
    mn = toks[0].lower()
    rest = toks[1] if len(toks) > 1 else ""
    args = _args(rest)
    la = [a.lower() for a in args]

    def t():
        return isa.tail(None if side is None else _int(side, defines),
                        None if delay is None else _int(delay, defines), sc)

    def no_tail():
        if side is not None or delay is not None:
            raise AsmError(f"'{mn}' takes no side-set / delay")

    def need(n):
        if len(args) != n:
            raise AsmError(f"'{mn}' expects {n} operand(s), got {len(args)}")

    def target(tok):
        k = tok.lower()
        if k in labels:
            return labels[k]
        v = _int(tok, defines)
        if not 0 <= v < isa.IMEM_DEPTH:
            raise AsmError(f"jump target out of range: {tok}")
        return v

    if mn == ".word":
        need(1)
        no_tail()
        return _int(args[0], defines) & 0xFFFF
    # ---- CTL ----
    if mn in ("nop", "halt", "irq", "hclr"):
        need(0)
        return {"nop": isa.enc_nop, "halt": isa.enc_halt, "irq": isa.enc_irq,
                "hclr": isa.enc_hclr}[mn](t())
    if mn in ("push", "pull"):
        kw = "iffull" if mn == "push" else "ifempty"
        if not set(la) <= {kw, "noblock", "block"}:
            raise AsmError(f"bad {mn} options {args}")
        f = isa.enc_push if mn == "push" else isa.enc_pull
        return f(kw in la, "noblock" not in la, t())
    if mn == "sync":
        if la not in ([], ["half"]):
            raise AsmError("sync [half]")
        return isa.enc_sync(bool(la), t())
    if mn == "clr":
        need(1)
        if la[0] not in ("isr", "osr", "all"):
            raise AsmError("clr isr|osr|all")
        return isa.enc_clr(la[0] in ("isr", "all"), la[0] in ("osr", "all"), t())
    # ---- branches ----
    if mn == "jmp":
        if len(args) == 1:
            return isa.enc_jmp("always", target(args[0]), t())
        need(2)
        if la[0] not in isa.JMP_CONDS:
            raise AsmError(f"bad jmp condition '{args[0]}'")
        return isa.enc_jmp(la[0], target(args[1]), t())
    if mn == "jpin":
        need(3)
        no_tail()
        return isa.enc_jpin(_pin(args[0], defines), _int(args[1], defines), target(args[2]))
    if mn == "wait":
        if len(args) not in (2, 3):
            raise AsmError("wait high|low|rise|fall pin [sync|synchalf]")
        sync = None
        if len(args) == 3:
            sync = {"sync": "full", "synchalf": "half"}.get(la[2])
            if sync is None:
                raise AsmError("wait option must be sync|synchalf")
        return isa.enc_wait(_pin(args[1], defines), la[0], sync, t())
    # ---- shifts ----
    if mn in ("in", "out"):
        if la and la[0] == "pins":
            need(3)
            f = isa.enc_in_pins if mn == "in" else isa.enc_out_pins
            return f(_pin(args[1], defines), _int(args[2], defines), t())
        need(2)
        f = isa.enc_in_reg if mn == "in" else isa.enc_out_reg
        return f(la[0], _int(args[1], defines), t())
    # ---- single pin ----
    if mn in isa.SETP_FUNCS:
        if mn == "toggle":
            need(1)
            return isa.enc_setp(_pin(args[0], defines), "toggle", 0, t())
        need(2)
        return isa.enc_setp(_pin(args[0], defines), mn, _int(args[1], defines), t())
    # ---- mov ----
    if mn == "mov":
        m = re.match(r"^\s*(\w+)\s*,\s*(~|!|::|rev\s+|par\s+)?\s*(\w+)\s*$", rest.lower())
        if not m:
            raise AsmError("mov dst, [~|rev|par] src")
        o = (m.group(2) or "").strip()
        o = {"!": "~", "::": "rev"}.get(o, o)
        return isa.enc_mov(m.group(1), m.group(3), o, t())
    # ---- ALU ----
    if mn in ("ldi", "and", "or", "xor"):
        need(2)
        no_tail()
        return isa.enc_alu(la[0], mn, _int(args[1], defines))
    if mn in ("dec", "inc"):
        need(1)
        no_tail()
        return isa.enc_alu(la[0], mn)
    if mn == "crc":
        need(1)
        no_tail()
        if la[0] not in ("lsb", "msb"):
            raise AsmError("crc lsb|msb")
        return isa.enc_crc(la[0] == "msb")
    if mn in ("dly", "dlyt"):
        need(1)
        no_tail()
        if la[0] == "xy":
            return isa.enc_dly(None, mn == "dlyt", True)
        return isa.enc_dly(_int(args[0], defines), mn == "dlyt")
    raise AsmError(f"unknown mnemonic '{mn}'")


def assemble(text: str, clk_hz: int = isa.CLK_HZ_DEFAULT, name: str = "pesm") -> isa.Image:
    img = isa.Image(name=name)
    cfg = img.cfg
    defines: Dict[str, int] = {}
    lines = []
    for ln, raw in enumerate(text.splitlines(), 1):
        line = re.split(r";|//|#", raw)[0].strip()
        if line:
            lines.append((ln, line))

    addr = 0
    pending: List[Tuple[int, int, str]] = []
    wrap_target = wrap_top = None
    entry = None
    for ln, line in lines:
        try:
            while True:
                m = re.match(r"^([A-Za-z_]\w*):\s*(.*)$", line)
                if not m:
                    break
                lab = m.group(1).lower()
                if lab in img.labels:
                    raise AsmError(f"duplicate label {lab}")
                img.labels[lab] = addr
                line = m.group(2).strip()
            if not line:
                continue
            if line.startswith(".") and not line.lower().startswith(".word"):
                d, *a = line.split()
                d = d.lower()
                if d == ".define":
                    defines[a[0].lower()] = _int(a[1], defines)
                elif d == ".clk_hz":
                    clk_hz = int(float(a[0]))
                elif d == ".div":
                    i, f = isa.divider_from_ratio(float(a[0]))
                    cfg[0], cfg[1], cfg[2] = i & 0xFF, i >> 8, f
                elif d == ".div_raw":
                    i, f = _int(a[0], defines), _int(a[1], defines)
                    cfg[0], cfg[1], cfg[2] = i & 0xFF, (i >> 8) & 0xFF, f & 0xFF
                elif d == ".tick_hz":
                    i, f = isa.divider_from_ratio(clk_hz / float(a[0]))
                    cfg[0], cfg[1], cfg[2] = i & 0xFF, i >> 8, f
                elif d == ".shift":
                    for kv in a:
                        k, v = kv.lower().split("=")
                        bit = {"out": 0, "in": 1}[k]
                        if v not in ("left", "right"):
                            raise AsmError("shift dir must be left|right")
                        cfg[3] = (cfg[3] | (1 << bit)) if v == "right" else (cfg[3] & ~(1 << bit))
                elif d == ".autopull":
                    cfg[3] |= 4
                    if a:
                        cfg[4] = _int(a[0], defines) & 31
                elif d == ".autopush":
                    cfg[3] |= 8
                    if a:
                        cfg[5] = _int(a[0], defines) & 31
                elif d == ".pull_thresh":
                    cfg[4] = _int(a[0], defines) & 31
                elif d == ".push_thresh":
                    cfg[5] = _int(a[0], defines) & 31
                elif d == ".side":
                    cnt = _int(a[0], defines)
                    if not 0 <= cnt <= 4:
                        raise AsmError("side count 0..4")
                    base, pindir = 0, 0
                    for kv in a[1:]:
                        if kv.lower() == "pindir":
                            pindir = 1
                        elif kv.lower().startswith("base="):
                            base = _pin(kv.split("=", 1)[1], defines)
                        else:
                            raise AsmError(f"bad .side option {kv}")
                    cfg[6] = (base << 4) | (pindir << 3) | cnt
                elif d == ".wrap_target":
                    wrap_target = addr
                elif d == ".wrap":
                    if addr == 0:
                        raise AsmError(".wrap before any instruction")
                    wrap_top = addr - 1
                elif d == ".entry":
                    entry = a[0].lower()
                elif d == ".org":
                    addr = _int(a[0], defines)
                elif d == ".od":
                    cfg[10] = _int(a[0], defines) & 0xFF
                elif d == ".crc_poly":
                    p = _int(a[0], defines) & 0xFFFF
                    cfg[11], cfg[12] = p & 0xFF, p >> 8
                elif d == ".init_out":
                    cfg[13] = _int(a[0], defines) & 0xFF
                elif d == ".init_oe":
                    cfg[14] = _int(a[0], defines) & 0xFF
                elif d == ".init_tout":
                    cfg[15] = _int(a[0], defines) & 0x7F
                elif d == ".cfg":
                    k = _int(a[0], defines) & 15
                    cfg[k] = _int(a[1], defines) & isa.CFG_MASKS[k]
                else:
                    raise AsmError(f"unknown directive {d}")
                continue
            if not 0 <= addr < isa.IMEM_DEPTH:
                raise AsmError("program exceeds 32 instructions")
            pending.append((ln, addr, line))
            addr += 1
        except isa.IsaError as e:
            raise AsmError(f"line {ln}: {e}") from None

    if wrap_target is not None:
        cfg[8] = wrap_target
    if wrap_top is not None:
        cfg[7] = wrap_top
    if entry is not None:
        if entry not in img.labels:
            raise AsmError(f"unknown entry label {entry}")
        cfg[9] = img.labels[entry]

    used = set()
    sc = img.side_count
    for ln, a, line in pending:
        try:
            w = encode_line(line, sc, img.labels, defines)
        except isa.IsaError as e:
            raise AsmError(f"line {ln}: {e}") from None
        if a in used:
            raise AsmError(f"line {ln}: address {a} used twice")
        used.add(a)
        img.words[a] = w
        img.source[a] = line
        img.length = max(img.length, a + 1)
    return img


def assemble_file(path: str, clk_hz: int = isa.CLK_HZ_DEFAULT) -> isa.Image:
    with open(path) as f:
        return assemble(f.read(), clk_hz, name=os.path.splitext(os.path.basename(path))[0])


def write_outputs(img: isa.Image, fmt: str, out: Optional[str]) -> None:
    if fmt == "bin":
        data = img.to_bin()
        if not out:
            raise SystemExit("-f bin needs -o")
        open(out, "wb").write(data)
        return
    if fmt == "mem":
        text = img.to_mem()
        if out:
            open(out, "w").write(text)
            base, _ = os.path.splitext(out)
            open(base + "_cfg.mem", "w").write(img.to_cfg_mem())
        else:
            print(text + img.to_cfg_mem())
        return
    text = {"py": img.to_py, "json": img.to_json, "list": img.listing, "asm": img.to_asm}[fmt]()
    if out:
        open(out, "w").write(text)
    else:
        print(text)


def main(argv=None) -> int:
    from . import hostproto
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("-f", "--format", default="list",
                    choices=["list", "bin", "mem", "py", "json", "asm"])
    ap.add_argument("-o", "--output")
    ap.add_argument("--spi", action="store_true", help="print the host SPI frames")
    ap.add_argument("--clk-hz", type=float, default=isa.CLK_HZ_DEFAULT)
    a = ap.parse_args(argv)
    try:
        img = assemble_file(a.source, int(a.clk_hz))
    except isa.IsaError as e:
        print(f"{a.source}: {e}", file=sys.stderr)
        return 1
    write_outputs(img, a.format, a.output)
    if a.spi:
        for fr in hostproto.frames_for_image(img):
            print("CS: " + " ".join(f"{b:02x}" for b in fr))
    return 0


if __name__ == "__main__":
    sys.exit(main())
