# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""
Constrained-random lockstep verification against sw/pesm/model.py.

Each iteration: random config + random 64-word program + random TX FIFO
preload, loaded through the real SPI host port. The core is released and
the pins (uo_out[7:1], uio_out, uio_oe) are compared against the model on
every clock for CRV_CYCLES cycles while the uio pads and TIN inputs are
randomly toggled. At the end the RX FIFO content and the TX FIFO level
are compared through the SPI port. Black-box: also runs on the netlist.

Environment: CRV_ITERS (default 40), CRV_CYCLES (default 1500), CRV_SEED.
"""

from __future__ import annotations

import os
import random
from collections import Counter

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge

from pesm_tb import PESM, asm  # sets sys.path for the pesm package
from pesm.model import PESMModel  # noqa: E402

ITERS = int(os.environ.get("CRV_ITERS", "40"))
CYCLES = int(os.environ.get("CRV_CYCLES", "1500"))
SEED = int(os.environ.get("CRV_SEED", "20261001"))


def rand_tail(rng):
    r = rng.random()
    if r < 0.6:
        return 0
    if r < 0.85:
        return rng.randrange(16) & 0x3
    return rng.randrange(16)


def rand_instr(rng: random.Random, cfg) -> int:
    """Weighted random instruction. Mostly legal, rarely HALT/illegal.
    Biased towards corner cases: side-set pins targeted by the main op,
    shift counts near the thresholds, state made observable on the pins."""
    op = rng.choices(
        population=list(range(16)),
        weights=[8, 7, 6, 8, 10, 10, 10, 12, 10, 4, 4, 5, 0.2, 0.2, 0.2, 0.2],
    )[0]
    t = rand_tail(rng)
    side_base = (cfg[6] >> 4) & 15
    side_cnt = min(cfg[6] & 7, 4)

    def pin():
        if side_cnt and rng.random() < 0.3:
            return (side_base + rng.randrange(side_cnt)) & 15
        return rng.randrange(16)

    def count(th):
        th = th or 32
        return rng.choice([1, 1, 1, 8, th, max(1, th - 1), min(32, th + 1), rng.randrange(1, 33)]) & 31

    if op == 0:
        f = rng.choices(range(16), weights=[6, 0.5, 2, 6, 6, 3, 2, 3, 3] + [0.3] * 7)[0]
        return (op << 12) | (f << 8) | (rng.randrange(4) << 4) | t
    if op in (1, 9):
        return (op << 12) | (rng.randrange(8) << 9) | (rng.randrange(32) << 4) | t
    if op == 2:
        return (op << 12) | (rng.randrange(16) << 8) | (rng.randrange(2) << 7) | rng.randrange(64)
    if op == 11:
        return (op << 12) | (rng.randrange(4) << 10) | (rng.randrange(16) << 6) | rng.randrange(64)
    if op == 3:
        p = rng.choice(list(range(12)) + [12, 13, 14, 15])
        return (op << 12) | (p << 8) | (rng.randrange(16) << 4) | t
    if op in (4, 5):
        if rng.random() < 0.5:
            return (op << 12) | (pin() << 7) | (rng.randrange(8) << 4) | t
        th = cfg[5] if op == 4 else cfg[4]
        return (op << 12) | (1 << 11) | (rng.randrange(4) << 9) | (count(th) << 4) | t
    if op == 6:
        return (op << 12) | (pin() << 8) | (rng.randrange(4) << 6) | (rng.randrange(2) << 5) | t
    if op == 7:
        dst = rng.choices(range(8), weights=[3, 3, 2, 2, 4, 3, 4, 1])[0]
        return (op << 12) | (dst << 9) | (rng.randrange(4) << 7) | (rng.randrange(8) << 4) | t
    if op == 8:
        return (op << 12) | rng.randrange(1 << 12)
    if op == 10:
        unit = rng.randrange(2)
        if rng.random() < 0.05:
            return (op << 12) | (unit << 11) | (1 << 10)
        return (op << 12) | (unit << 11) | rng.choice([0, 1, 1, 2, 3, rng.randrange(24)])
    return (op << 12) | rng.randrange(1 << 12)


def rand_cfg(rng: random.Random):
    cfg = [0] * asm.NUM_CFG
    div = rng.choice([0, 1, 2, 3, 4, 5, 7])
    cfg[0], cfg[1], cfg[2] = div, 0, rng.choice([0, 0, 1, 128, 255, rng.randrange(256)])
    cfg[3] = rng.randrange(16)
    cfg[4] = rng.choice([0, 1, 3, 7, 8, 8, 16, 31])
    cfg[5] = rng.choice([0, 1, 3, 7, 8, 8, 16, 31])
    cfg[6] = (rng.randrange(16) << 4) | (rng.randrange(2) << 3) | rng.choice([0, 0, 1, 2, 3, 4, 5, 7])
    cfg[7] = rng.choice([63, 63, rng.randrange(64)])
    cfg[8] = rng.choice([0, 0, rng.randrange(64)])
    cfg[9] = rng.choice([0, 0, 0, rng.randrange(64)])
    cfg[10] = rng.choice([0, 0, rng.randrange(256)])
    cfg[11], cfg[12] = rng.randrange(256), rng.randrange(256)
    cfg[13], cfg[14], cfg[15] = rng.randrange(256), rng.randrange(256), rng.randrange(128)
    # pattern compare: sparse masks so that matches actually happen
    cfg[16] = rng.choice([0, 0x01, 0x03, 0x81, rng.randrange(256) & rng.randrange(256)])
    cfg[17] = rng.randrange(256)
    # background clock: {idle, auto, en, pin[3:0]}, divider
    cfg[18] = (rng.randrange(2) << 6) | (rng.randrange(2) << 5) | (rng.randrange(2) << 4) | rng.randrange(16)
    cfg[19] = rng.choice([0, 0, 1, 2, 3, rng.randrange(256)])
    return cfg


@cocotb.test()
async def test_crv_lockstep(dut):
    h = PESM(dut, sck_half=4)
    await h.start()
    rng = random.Random(SEED)
    cov = Counter()
    total_exec = 0

    for it in range(ITERS):
        cfg = rand_cfg(rng)
        words = [rand_instr(rng, cfg) for _ in range(asm.IMEM_DEPTH)]
        txd = [rng.randrange(256) for _ in range(rng.randrange(9))]

        # ---- load through the real host port ----
        await h.boot()
        await h.control(asm.CTRL_FLUSH_TX | asm.CTRL_FLUSH_RX | asm.CTRL_HFLAG_CLR | asm.CTRL_CLR_FLAGS)
        await h.write_cfg(0, cfg)
        await h.write_imem(0, words)
        if txd:
            await h.write_tx(txd)
        assert await h.read_imem(0, asm.IMEM_DEPTH) == words

        ext = rng.randrange(256)
        tin = rng.randrange(16)
        h.set_ext(ext)
        h.set_tin(tin)
        await ClockCycles(dut.clk, 6)

        m = PESMModel(words, cfg, txd)
        pad = m.pad(ext)
        m.s1_bio = m.s2_bio = pad
        m.s1_tin = m.s2_tin = tin
        m.in_prev = m._in_vec()

        await FallingEdge(dut.clk)
        # ---- lockstep ----
        toggle_p = rng.choice([0.02, 0.1, 0.3])
        for cyc in range(CYCLES + 6):
            if cyc == 0:
                h.set_ui_bit(3, 0)                       # MODE -> RUN
            if cyc == CYCLES:
                h.set_ui_bit(3, 1)                       # back to BOOT
            if rng.random() < toggle_p:
                ext ^= 1 << rng.randrange(8)
                h.set_ext(ext)
            if rng.random() < toggle_p:
                tin ^= 1 << rng.randrange(4)
                h.set_tin(tin)
            ui = h.ui
            await RisingEdge(dut.clk)
            m.step(ui, ext)
            await ReadOnly()
            uo = int(dut.uo_out.value) & 0xFE
            uio_o = int(dut.uio_out.value)
            uio_e = int(dut.uio_oe.value)
            exp = (m.uo_out_hi(), m.uio_out(), m.uio_oe())
            if (uo, uio_o, uio_e) != exp:
                dis = asm.disassemble(words[m.pc], min(cfg[6] & 7, 4))
                raise AssertionError(
                    f"iter {it} cycle {cyc}: DUT uo={uo:#04x} uio_out={uio_o:#04x} uio_oe={uio_e:#04x} "
                    f"model uo={exp[0]:#04x} uio_out={exp[1]:#04x} uio_oe={exp[2]:#04x} "
                    f"(model pc={m.pc} '{dis}')\nprogram={[hex(w) for w in words]}\ncfg={cfg}")
            await FallingEdge(dut.clk)

        # ---- end-of-run FIFO comparison ----
        st = await h.status()
        assert st["running"] == 0
        assert st["tx_level"] == len(m.tx), f"iter {it}: tx_level {st['tx_level']} != {len(m.tx)}"
        assert st["rx_level"] == len(m.rx), f"iter {it}: rx_level {st['rx_level']} != {len(m.rx)}"
        assert st["irq"] == m.irq and st["rx_ovf"] == m.rx_ovf, f"iter {it}: flags {st}"
        assert st["halted"] == 0  # boot clears halt
        if m.rx:
            got = await h.read_rx(len(m.rx))
            assert got == m.rx, f"iter {it}: rx {got} != {m.rx}"
        cov.update(m.cov)
        total_exec += sum(v for k, v in m.cov.items() if k.startswith("op"))

    dut._log.info(f"CRV: {ITERS} programs x {CYCLES} cycles, {total_exec} instructions executed")
    ops = sorted(k for k in cov if k.startswith("op"))
    dut._log.info("coverage bins hit: " + ", ".join(f"{k}:{cov[k]}" for k in ops))
    dut._log.info("events: " + ", ".join(f"{k}:{v}" for k, v in sorted(cov.items()) if not k.startswith("op")))
    # minimum functional coverage goals (only meaningful for a full-length run)
    if ITERS < 30:
        return
    for must in ["txpop.op0", "txpop.op5", "rxpush.op0", "rxpush.op4", "stall.op3", "stall.opa",
                 "sync.full", "sync.half", "predelay", "sideset.4", "taken.op2", "taken.op7",
                 "taken.op1", "taken.op9", "taken.opb", "op0.8", "stall.rxfull", "bg.toggle",
                 "bg.pin"]:
        assert cov[must] > 0, f"coverage hole: {must}"
