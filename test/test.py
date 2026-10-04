# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""
Directed black-box tests. Only top-level pins are touched, so the same
file runs against RTL and the gate-level netlist (make GATES=yes).
"""

from __future__ import annotations

import random

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge

from pesm_tb import PESM, assemble_file, asm

CLK_HZ = 50_000_000


# ---------------------------------------------------------------------------
# Host interface
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_loader_random_access(dut):
    """Burst + random-access imem/cfg writes, readback, defaults, chip ID."""
    h = PESM(dut)
    await h.start()
    await h.boot()
    N, C = asm.IMEM_DEPTH, asm.NUM_CFG

    # reset contents
    assert await h.read_imem(0, N) == [0x0100] * N, "imem must reset to HALT"
    assert await h.read_cfg(0, C) == asm.CFG_DEFAULTS, "cfg reset defaults"
    assert (await h.status())["id"] == asm.CHIP_ID

    rng = random.Random(1)
    words = [rng.randrange(1 << 16) for _ in range(N)]
    await h.write_imem(0, words)
    assert await h.read_imem(0, N) == words

    # random-access patches in both halves of the memory
    await h.write_imem(N - 1, [0xBEEF])
    await h.write_imem(7, [0x1234, 0x5678])
    await h.write_imem(31, [0x0F0F, 0xF0F0])           # crosses the bit-5 boundary
    words[N - 1], words[7], words[8], words[31], words[32] = 0xBEEF, 0x1234, 0x5678, 0x0F0F, 0xF0F0
    assert await h.read_imem(0, N) == words
    assert await h.read_imem(N - 1, 1) == [0xBEEF]
    assert await h.read_imem(31, 2) == [0x0F0F, 0xF0F0]

    # imem address wraps 63 -> 0 in a burst (write and read)
    await h.write_imem(N - 1, [0xAAAA, 0x5555])
    words[N - 1], words[0] = 0xAAAA, 0x5555
    assert await h.read_imem(0, N) == words
    assert await h.read_imem(N - 1, 2) == [0xAAAA, 0x5555]

    # cfg: burst, masks, single register patch, unimplemented addresses read 0
    cfg = [rng.randrange(256) for _ in range(C)]
    await h.write_cfg(0, cfg)
    exp = [c & m for c, m in zip(cfg, asm.CFG_MASKS)]
    assert await h.read_cfg(0, C) == exp
    await h.write_cfg(9, [0x2B])
    exp[9] = 0x2B
    assert await h.read_cfg(9, 1) == [0x2B]
    await h.write_cfg(C, [0xFF] * (32 - C))             # ignored
    assert await h.read_cfg(0, 32) == exp + [0] * (32 - C)
    # cfg address wraps 31 -> 0
    await h.write_cfg(31, [0xEE, 0x42])
    exp[0] = 0x42
    assert await h.read_cfg(0, C) == exp

    # reserved command 110 11--- is ignored: no FIFO traffic, no flags, no writes
    await h.xfer([0xD8, 0xFF, 0xFF])
    st = await h.status()
    assert st["tx_level"] == 0 and st["rx_unf"] == 0 and st["err"] == 0 and st["hflag"] == 0
    assert await h.read_imem(0, N) == words and await h.read_cfg(0, C) == exp


@cocotb.test()
async def test_write_protect_while_running(dut):
    h = PESM(dut)
    await h.start()
    prog = asm.assemble("loop:\n jmp loop\n")
    await h.load(prog)
    await h.run()
    st = await h.status()
    assert st["running"] == 1 and st["err"] == 0
    await h.write_imem(0, [0xFFFF])
    await h.write_cfg(0, [0x55])
    st = await h.status()
    assert st["err"] == 1, "write while running must flag ERR"
    await h.boot()
    assert await h.read_imem(0, 1) == [prog.words[0]], "imem modified while running"
    assert await h.read_cfg(0, 1) == [prog.cfg[0]], "cfg modified while running"
    await h.control(asm.CTRL_CLR_FLAGS)
    st = await h.status()
    assert st["err"] == 0


@cocotb.test()
async def test_fifo_host_paths_and_flags(dut):
    h = PESM(dut)
    await h.start()
    await h.boot()
    await h.write_tx(list(range(10)))          # 2 dropped
    st = await h.status()
    assert st["tx_level"] == 8 and st["tx_ovf"] == 1
    rx = await h.read_rx(2)
    assert rx == [0, 0]
    st = await h.status()
    assert st["rx_unf"] == 1
    await h.control(asm.CTRL_FLUSH_TX | asm.CTRL_CLR_FLAGS | asm.CTRL_HFLAG_SET)
    st = await h.status()
    assert st["tx_level"] == 0 and st["tx_ovf"] == 0 and st["rx_unf"] == 0 and st["hflag"] == 1
    await h.control(asm.CTRL_HFLAG_CLR)
    assert (await h.status())["hflag"] == 0

    # Echo program: TX FIFO -> RX FIFO via OSR/X/ISR. Early CS de-assert must not lose data.
    prog = asm.assemble("""
    loop:
        pull
        out x, 8
        mov isr, x
        push
        jmp loop
    """)
    await h.load(prog)
    await h.run()
    await h.write_tx([0x11, 0x22, 0x33, 0x44, 0x55])
    await ClockCycles(dut.clk, 50)
    assert (await h.status())["rx_level"] == 5
    assert await h.read_rx(2) == [0x11, 0x22]
    # frame with only the command byte: nothing may be popped
    await h.xfer([asm.CMD_READ_RX])
    assert await h.read_rx(3) == [0x33, 0x44, 0x55]
    st = await h.status()
    assert st["rx_level"] == 0 and st["rx_unf"] == 0


@cocotb.test()
async def test_illegal_opcode_halts(dut):
    h = PESM(dut)
    await h.start()
    for op in (0xC, 0xD, 0xE, 0xF):
        prog = asm.assemble(f"nop\n nop\n .word {(op << 12) | 0x123:#06x}\n nop\n")
        await h.load(prog)
        await h.control(asm.CTRL_CLR_FLAGS)
        await h.run()
        await ClockCycles(dut.clk, 20)
        st = await h.status()
        assert st["halted"] == 1 and st["err"] == 1 and st["pc"] == 2 and st["running"] == 0, (op, st)


# ---------------------------------------------------------------------------
# Clock divider
# ---------------------------------------------------------------------------
async def _measure_toggles(dut, h, n):
    edges = []
    last = h.tout() & 1
    cyc = 0
    while len(edges) < n + 1:
        await FallingEdge(dut.clk)
        cyc += 1
        v = h.tout() & 1
        if v != last:
            edges.append(cyc)
            last = v
    return [b - a for a, b in zip(edges, edges[1:])]


@cocotb.test()
async def test_fractional_divider_exact(dut):
    """Tick grid: each period is I or I+1, 256 periods sum to exactly 256*I+F."""
    h = PESM(dut)
    await h.start()
    for div_i, div_f in [(4, 128), (7, 1), (3, 255), (2, 0), (10, 0), (2, 85)]:
        prog = assemble_file("frac_div.pasm")
        prog.cfg[0], prog.cfg[1], prog.cfg[2] = div_i & 0xFF, div_i >> 8, div_f
        await h.load(prog)
        await h.run()
        per = await _measure_toggles(dut, h, 256 + 8)
        per = per[4:4 + 256]
        assert set(per) <= {div_i, div_i + 1}, f"I={div_i} F={div_f}: periods {sorted(set(per))}"
        assert sum(per) == 256 * div_i + div_f, f"I={div_i} F={div_f}: sum {sum(per)}"
        if div_f == 0:
            assert set(per) == {div_i}
    # DIV_INT = 0: a tick on every cycle -> toggle loop is limited by the jmp (2 cycles)
    prog = assemble_file("frac_div.pasm")
    prog.cfg[0] = prog.cfg[1] = prog.cfg[2] = 0
    await h.load(prog)
    await h.run()
    per = await _measure_toggles(dut, h, 32)
    assert set(per) == {2}


@cocotb.test()
async def test_setup_instructions_run_at_clk(dut):
    """Regression for the v1 tick-gated FSM: setup ops execute 1/clk even with a slow divider."""
    h = PESM(dut)
    await h.start()
    prog = asm.assemble("""
    .div_raw 5000 0
        set tout0, 1
        set tout0, 0
        set tout0, 1
        set tout0, 0
        halt
    """)
    await h.load(prog)
    await h.run()
    seen = []
    for _ in range(40):
        await FallingEdge(dut.clk)
        seen.append(h.tout() & 1)
    # expect the pattern 1,0,1,0 on 4 consecutive cycles
    s = "".join(map(str, seen))
    assert "01010" in s, s


# ---------------------------------------------------------------------------
# UART
# ---------------------------------------------------------------------------
def _uart_period(prog):
    i = prog.cfg[0] | (prog.cfg[1] << 8)
    return i + prog.cfg[2] / 256.0


@cocotb.test()
async def test_uart_tx_115200(dut):
    h = PESM(dut)
    await h.start()
    prog = assemble_file("uart_tx.pasm")
    P = _uart_period(prog)
    await h.load(prog)
    assert h.tout() & 1 == 1, "TX must idle high in BOOT (init_tout)"
    await h.run()
    data = [0x55, 0xA3, 0x00, 0xFF, 0x80]
    edges = []
    done = {"stop": False}

    async def mon():
        last, cyc = 1, 0
        while not done["stop"]:
            await FallingEdge(dut.clk)
            cyc += 1
            v = h.tout() & 1
            if v != last:
                edges.append((cyc, v))
                last = v

    cocotb.start_soon(mon())
    await h.write_tx(data)
    await ClockCycles(dut.clk, int(P * 10 * (len(data) + 1)))
    done["stop"] = True

    # decode: find start bits, sample mid-bit with nominal period
    def level_at(t):
        lv = 1
        for c, v in edges:
            if c <= t:
                lv = v
            else:
                break
        return lv

    got = []
    idx = 0
    while idx < len(edges):
        c0, v0 = edges[idx]
        if v0 != 0:
            idx += 1
            continue
        byte = 0
        for b in range(8):
            byte |= level_at(c0 + P * (1.5 + b)) << b
        assert level_at(c0 + P * 9.5) == 1, "stop bit"
        got.append(byte)
        # every edge inside the frame must lie on the tick grid (< 1 clk error)
        for c, _ in edges[idx:]:
            if c > c0 + P * 9.6:
                break
            k = round((c - c0) / P)
            assert abs((c - c0) - k * P) < 1.0, f"edge off grid by {(c - c0) - k * P:.3f} clk"
        while idx < len(edges) and edges[idx][0] < c0 + P * 9.6:
            idx += 1
    assert got == data, f"{[hex(x) for x in got]}"


async def _uart_drive(dut, h, data, period_clk, stop_bit=1, gap=3):
    for byte in data:
        bits = [0] + [(byte >> i) & 1 for i in range(8)] + [stop_bit]
        acc = 0.0
        for b in bits:
            h.set_tin(b)
            acc += period_clk
            n = int(acc)
            acc -= n
            await ClockCycles(dut.clk, n)
        h.set_tin(1)
        await ClockCycles(dut.clk, int(gap * period_clk))


@cocotb.test()
async def test_uart_rx_115200(dut):
    h = PESM(dut)
    await h.start()
    h.set_tin(1)
    prog = assemble_file("uart_rx.pasm")
    await h.load(prog)
    await h.run()
    await ClockCycles(dut.clk, 37)  # arbitrary phase
    P = CLK_HZ / 115200
    data = [0x55, 0x00, 0xFF, 0x3C, 0xA5, 0x81]
    # +/-2 % baud mismatch is tolerated
    await _uart_drive(dut, h, data[:3], P * 1.02)
    await _uart_drive(dut, h, data[3:], P * 0.98)
    st = await h.status()
    assert st["rx_level"] == len(data) and st["irq"] == 0
    assert await h.read_rx(len(data)) == data

    # framing error -> IRQ
    await _uart_drive(dut, h, [0x42], P, stop_bit=0, gap=0)
    await ClockCycles(dut.clk, int(P))
    h.set_tin(1)
    await ClockCycles(dut.clk, int(3 * P))
    st = await h.status()
    assert st["irq"] == 1


# ---------------------------------------------------------------------------
# SPI master
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_spi_master_mode0(dut):
    h = PESM(dut)
    await h.start()
    prog = assemble_file("spi_master.pasm")
    await h.load(prog)
    await h.run()

    SCLK, MOSI, CS = 1, 2, 3       # tout bits
    replies = [0xC3, 0x5A, 0x01, 0xFE]
    got_mosi = []
    sclk_rises = []
    state = {"cs": 1, "sclk": 0, "bit": 0, "byte": 0, "rep": 0, "stop": False, "cyc": 0}

    def miso_bit():
        r = replies[state["rep"] % len(replies)]
        return (r >> (7 - state["bit"])) & 1

    async def slave():
        while not state["stop"]:
            await FallingEdge(dut.clk)
            state["cyc"] += 1
            t = h.tout()
            cs, sclk, mosi = (t >> CS) & 1, (t >> SCLK) & 1, (t >> MOSI) & 1
            if state["cs"] and not cs:
                state["bit"], state["byte"] = 0, 0
                h.set_tin((h.ui >> 4) & ~2 | (miso_bit() << 1))
            if not cs:
                if sclk and not state["sclk"]:
                    sclk_rises.append(state["cyc"])
                    state["byte"] = (state["byte"] << 1) | mosi
                    state["bit"] += 1
                    if state["bit"] == 8:
                        got_mosi.append(state["byte"])
                        state["bit"], state["byte"] = 0, 0
                        state["rep"] += 1
                if not sclk and state["sclk"]:
                    h.set_tin((h.ui >> 4) & ~2 | (miso_bit() << 1))
            state["cs"], state["sclk"] = cs, sclk

    cocotb.start_soon(slave())
    tx = [0xA5, 0x3C, 0xFF, 0x00]
    await h.write_tx(tx)
    await ClockCycles(dut.clk, 50 * 8 * 5)
    state["stop"] = True
    assert got_mosi == tx, [hex(b) for b in got_mosi]
    assert h.tout() >> CS & 1 == 1, "CS must return high"
    assert await h.read_rx(4) == replies
    # SCLK period: 2 ticks of 25 clk inside a byte
    d = [b - a for a, b in zip(sclk_rises, sclk_rises[1:])]
    assert d[:7] == [50] * 7, d[:8]


# ---------------------------------------------------------------------------
# I2C master (open drain, clock stretching, NACK)
# ---------------------------------------------------------------------------
class I2CSlave:
    SDA, SCL = 0, 1

    def __init__(self, dut, h, addr, stretch=0):
        self.dut, self.h, self.addr, self.stretch = dut, h, addr, stretch
        self.frames = []        # list of (bytes, acks)
        self.stop = False
        self.pull_sda = 0
        self.pull_scl = 0

    def _apply(self):
        ext = 0xFF
        if self.pull_sda:
            ext &= ~(1 << self.SDA)
        if self.pull_scl:
            ext &= ~(1 << self.SCL)
        self.h.set_ext(ext)

    async def run(self):
        prev_sda, prev_scl = 1, 1
        active = False
        nbits, cur, data, acks = 0, 0, [], []
        addressed = False
        ack_phase = False
        stretch_left = 0
        while not self.stop:
            await FallingEdge(self.dut.clk)
            pad = self.h.pad()
            sda, scl = (pad >> self.SDA) & 1, (pad >> self.SCL) & 1
            if stretch_left:
                stretch_left -= 1
                if stretch_left == 0:
                    self.pull_scl = 0
                    self._apply()
            if scl and prev_scl and prev_sda and not sda:          # START
                active, nbits, cur, data, acks = True, 0, 0, [], []
                addressed = False
            elif scl and prev_scl and not prev_sda and sda:        # STOP
                if active:
                    self.frames.append((data, acks))
                active = False
                self.pull_sda = 0
                self._apply()
            elif active and scl and not prev_scl:                  # SCL rise
                if not ack_phase:
                    cur = (cur << 1) | sda
                    nbits += 1
            elif active and not scl and prev_scl:                  # SCL fall
                if ack_phase:
                    ack_phase = False
                    self.pull_sda = 0
                    self._apply()
                    if self.stretch:
                        self.pull_scl = 1
                        stretch_left = self.stretch
                        self._apply()
                elif nbits == 8:
                    data.append(cur)
                    if not data[1:]:
                        addressed = (cur >> 1) == self.addr
                    ack = addressed
                    acks.append(ack)
                    nbits, cur = 0, 0
                    ack_phase = True
                    self.pull_sda = 1 if ack else 0
                    self._apply()
            prev_sda, prev_scl = sda, scl


@cocotb.test()
async def test_i2c_master_write(dut):
    h = PESM(dut)
    await h.start()
    prog = assemble_file("i2c_master_write.pasm")
    await h.load(prog)
    assert dut.uio_oe.value.to_unsigned() & 3 == 0, "open drain released in BOOT"
    await h.run()
    sl = I2CSlave(dut, h, addr=0x50, stretch=400)
    cocotb.start_soon(sl.run())
    payload = [0x50 << 1, 0x12, 0xA5, 0x00]
    await h.write_tx(payload)
    for _ in range(100):
        if sl.frames:
            break
        await ClockCycles(dut.clk, 1000)
    assert sl.frames, "no I2C frame seen"
    data, acks = sl.frames[0]
    assert data == payload, [hex(b) for b in data]
    assert all(acks)
    assert (await h.status())["irq"] == 0
    # SDA/SCL never driven high (open drain)
    assert int(dut.uio_out.value) & 3 == 0

    # NACK path
    await h.write_tx([0x51 << 1])
    for _ in range(100):
        if len(sl.frames) > 1:
            break
        await ClockCycles(dut.clk, 1000)
    data, acks = sl.frames[-1]
    assert data == [0x51 << 1] and acks == [False], (data, acks)
    st = await h.status()
    assert st["irq"] == 1
    sl.stop = True


# ---------------------------------------------------------------------------
# CRC accelerator
# ---------------------------------------------------------------------------
def crc16_usb(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


@cocotb.test()
async def test_crc16_usb(dut):
    h = PESM(dut)
    await h.start()
    prog = assemble_file("crc16_usb.pasm")
    await h.load(prog)
    await h.run()
    msg = list(b"123456789")
    await h.write_tx(msg[:8])
    await ClockCycles(dut.clk, 300)
    await h.write_tx(msg[8:])
    await ClockCycles(dut.clk, 100)
    await h.control(asm.CTRL_HFLAG_SET)
    await ClockCycles(dut.clk, 50)
    st = await h.status()
    assert st["halted"] == 1 and st["hflag"] == 0
    lo, hi = await h.read_rx(2)
    assert crc16_usb(msg) == 0xB4C8
    assert (hi << 8) | lo == 0xB4C8, hex((hi << 8) | lo)


# ---------------------------------------------------------------------------
# WAIT edge + SYNC half re-phasing, side-set timing
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_wait_edge_sync_and_sideset(dut):
    """After WAIT rise SYNCHALF the next grid point is exactly I/2 later;
    side-set changes in the same cycle as the main op."""
    h = PESM(dut)
    await h.start()
    prog = asm.assemble("""
    .div_raw 40 0
    .side 1 base=tout1
    loop:
        wait rise tin0 synchalf side 0
        set tout0, 1 side 1 [1]      ; both pins in the same cycle, on the half grid point
        set tout0, 0 side 0 [1]      ; one full period later
        jmp loop
    """)
    await h.load(prog)
    await h.run()
    await ClockCycles(dut.clk, 13)
    h.set_tin(1)
    t_edge = None
    t_hi = None
    t_lo = None
    for cyc in range(200):
        await FallingEdge(dut.clk)
        t = h.tout()
        if t_hi is None and (t & 1):
            t_hi = cyc
            assert (t >> 1) & 1, "side-set pin must rise in the same cycle"
        if t_hi is not None and t_lo is None and not (t & 1):
            t_lo = cyc
            assert not (t >> 1) & 1
    assert t_lo - t_hi == 40, t_lo - t_hi
    # edge applied at cycle 0; 2-flop sync + edge detect + I/2 = 20 + const
    assert 20 <= t_hi <= 24, t_hi


# ---------------------------------------------------------------------------
# USB low-speed: CRC5 + CRC16 computed on chip, NRZI/bit-stuffed packets
# transmitted on chip, decoded and checked by an independent receiver model.
# ---------------------------------------------------------------------------
def crc5_usb_ref(bits):
    crc = 0x1F
    for b in bits:
        crc = (crc >> 1) ^ 0x14 if (crc ^ b) & 1 else crc >> 1
    return crc ^ 0x1F


def _bits_lsb(data, n=None):
    out = []
    for byte in data:
        out += [(byte >> i) & 1 for i in range(8)]
    return out if n is None else out[:n]


class USBLSDecoder:
    """Samples D+ (uio[0]) / D- (uio[1]) every clk, then decodes offline."""

    def __init__(self, dut, h):
        self.dut, self.h = dut, h
        self.trace = []      # (cycle, state) on change; state in J, K, SE0, SE1
        self.stop = False

    @staticmethod
    def _state(dp, dm):
        return {(0, 1): "J", (1, 0): "K", (0, 0): "SE0", (1, 1): "SE1"}[(dp, dm)]

    async def run(self):
        cyc, last = 0, None
        while not self.stop:
            await FallingEdge(self.dut.clk)
            cyc += 1
            oe = int(self.dut.uio_oe.value)
            out = int(self.dut.uio_out.value)
            assert oe & 3 == 3, "USB lines must be driven"
            s = self._state(out & 1, (out >> 1) & 1)
            if s != last:
                self.trace.append((cyc, s))
                last = s

    def packets(self, period):
        """Return list of (bytes, eop_ok, grid_errs)."""
        tr = self.trace
        pkts = []
        i = 0
        while i < len(tr):
            c0, s0 = tr[i]
            if s0 != "K" or i == 0 or tr[i - 1][1] != "J":
                i += 1
                continue

            def state_at(t):
                st = tr[0][1]
                for c, s in tr:
                    if c <= t:
                        st = s
                    else:
                        break
                return st

            prev = "J"
            bits, ones, k = [], 0, 0
            stuffed_err = False
            while True:
                s = state_at(c0 + (k + 0.5) * period)
                if s == "SE0":
                    break
                b = 1 if s == prev else 0
                prev = s
                k += 1
                if ones == 6:
                    if b != 0:
                        stuffed_err = True
                    ones = 0
                    continue           # drop stuff bit
                bits.append(b)
                ones = ones + 1 if b else 0
            se0_start = c0 + k * period
            eop_ok = (state_at(se0_start + 0.5 * period) == "SE0" and
                      state_at(se0_start + 1.5 * period) == "SE0" and
                      state_at(se0_start + 2.5 * period) == "J")
            nbytes = len(bits) // 8
            data = [sum(bits[8 * j + i] << i for i in range(8)) for j in range(nbytes)]
            # every transition of this packet must sit on the tick grid
            errs = []
            for c, _ in tr:
                if c0 <= c <= se0_start + 2.6 * period:
                    q = (c - c0) / period
                    errs.append(abs(q - round(q)) * period)
            pkts.append({"data": data, "eop_ok": eop_ok, "stuff_err": stuffed_err,
                         "trailing_bits": len(bits) % 8, "max_grid_err": max(errs)})
            while i < len(tr) and tr[i][0] < se0_start + 3 * period:
                i += 1
        return pkts


@cocotb.test()
async def test_usb_ls_tx_with_crc(dut):
    h = PESM(dut)
    await h.start()

    # --- CRC5 of a SETUP token, computed by the engine ---
    addr, endp = 0x15, 0xE
    tok = [(addr & 0x7F) | ((endp & 1) << 7), endp >> 1]
    assert crc5_usb_ref(_bits_lsb(b"123456789")) == 0x19        # catalogue check value
    exp5 = crc5_usb_ref(_bits_lsb(tok, 11))
    # USB 2.0 spec example (addr 15h, endp Eh -> 10111b, written in transmit order)
    assert int(f"{exp5:05b}"[::-1], 2) == 0x17
    await h.load(assemble_file("crc5_usb.pasm"))
    await h.run()
    await h.write_tx(tok)
    await ClockCycles(dut.clk, 100)
    st = await h.status()
    assert st["halted"] == 1
    (crc5,) = await h.read_rx(1)
    assert crc5 == exp5, f"crc5 {crc5:#x} != {exp5:#x}"

    # --- CRC16 of the DATA0 payload, computed by the engine ---
    payload = [0x80, 0x06, 0x00, 0x01, 0xFF, 0xFF, 0x7E, 0x3F]   # long 1-runs force stuffing
    await h.control(asm.CTRL_FLUSH_RX)
    await h.load(assemble_file("crc16_usb.pasm"))
    await h.run()
    await h.write_tx(payload)
    await ClockCycles(dut.clk, 400)
    await h.control(asm.CTRL_HFLAG_SET)
    await ClockCycles(dut.clk, 50)
    lo, hi = await h.read_rx(2)
    assert (hi << 8) | lo == crc16_usb(payload)

    # --- transmit SETUP token and DATA0 packet on the bus ---
    prog = assemble_file("usb_ls_tx.pasm")
    period = (prog.cfg[0] | prog.cfg[1] << 8) + prog.cfg[2] / 256.0
    await h.load(prog)
    await h.run()
    dec = USBLSDecoder(dut, h)
    cocotb.start_soon(dec.run())
    await ClockCycles(dut.clk, 200)

    token = [0x80, 0x2D, tok[0], tok[1] | (crc5 << 3)]
    await h.write_tx(token)
    await ClockCycles(dut.clk, int(period * (len(token) * 8 + 8)))
    data_pkt = [0x80, 0xC3] + payload + [lo, hi]
    await h.write_tx(data_pkt[:8])
    await ClockCycles(dut.clk, int(period * 20))
    await h.write_tx(data_pkt[8:])
    await ClockCycles(dut.clk, int(period * (len(data_pkt) * 8 + 30)))
    dec.stop = True

    pkts = dec.packets(period)
    assert len(pkts) == 2, pkts
    for p, ref in zip(pkts, [token, data_pkt]):
        assert p["data"] == ref, f"{[hex(b) for b in p['data']]} != {[hex(b) for b in ref]}"
        assert p["eop_ok"] and not p["stuff_err"] and p["trailing_bits"] == 0, p
        assert p["max_grid_err"] < 1.0, p
    # receiver-side validation, independent of the engine
    d = pkts[0]["data"]
    rx_addr, rx_endp = d[2] & 0x7F, (d[2] >> 7) | ((d[3] & 7) << 1)
    assert (d[3] >> 3) == crc5_usb_ref(_bits_lsb(d[2:4], 11))
    # residual check over field + CRC as received: 01100b (spec order)
    crc = 0x1F
    for b in _bits_lsb(d[2:4], 16):
        crc = (crc >> 1) ^ 0x14 if (crc ^ b) & 1 else crc >> 1
    assert crc == 0b00110
    assert (rx_addr, rx_endp) == (addr, endp)
    dd = pkts[1]["data"]
    assert crc16_usb(dd[2:-2]) == dd[-2] | (dd[-1] << 8)
    # bit stuffing really happened (0xFF 0xFF run)
    dut._log.info(f"USB LS: token + DATA0 decoded, grid error <= {max(p['max_grid_err'] for p in pkts):.2f} clk")


@cocotb.test()
async def test_rx_port_back_to_back_full(dut):
    """Registered RX write port: back-to-back autopushes must stop at exactly 8
    entries (the pending write counts as occupied), and one host pop must let
    exactly one more push through."""
    h = PESM(dut)
    await h.start()
    prog = asm.assemble("""
    .autopush 8
    .shift in=left
        ldi x, 0xa5
    """ + "\n".join(["    in x, 8"] * 12) + "\n    halt\n")
    await h.load(prog)
    await h.control(asm.CTRL_FLUSH_RX | asm.CTRL_CLR_FLAGS)
    await h.run()
    await ClockCycles(dut.clk, 40)
    st = await h.status()
    assert st["rx_level"] == 8 and st["rx_ovf"] == 0, st
    assert st["pc"] == 9 and st["running"] == 1, st          # 9th IN (addr 9) stalled
    assert await h.read_rx(1) == [0xA5]
    await ClockCycles(dut.clk, 10)
    st = await h.status()
    assert st["rx_level"] == 8 and st["pc"] == 10, st        # exactly one more push


@cocotb.test()
async def test_pinmap_isolation(dut):
    """Host traffic lives only on ui_in[3:0] / uo_out[0]: during heavy SPI
    traffic (BOOT and RUN) no uio pad and no TOUT pin moves, and toggling the
    target inputs ui_in[7:4] / uio never disturbs the host port."""
    h = PESM(dut)
    await h.start()
    seen = {"bad": []}

    async def watch(n):
        for _ in range(n):
            await FallingEdge(dut.clk)
            oe, out, tout = int(dut.uio_oe.value), int(dut.uio_out.value), h.tout()
            if oe != 0 or tout != 0:
                seen["bad"].append((oe, out, tout))

    w = cocotb.start_soon(watch(60000))
    rng = random.Random(3)
    await h.boot()
    for _ in range(3):
        h.set_ext(rng.randrange(256))
        h.set_tin(rng.randrange(16))
        words = [rng.randrange(1 << 16) for _ in range(32)]
        await h.write_imem(0, words)
        assert await h.read_imem(0, 32) == words
    # RUN a program that never touches pins, keep hammering the host port
    await h.load(asm.assemble("l:\n pull\n out x, 8\n mov isr, x\n push\n jmp l\n"))
    await h.run()
    for i in range(4):
        h.set_tin(rng.randrange(16))
        h.set_ext(rng.randrange(256))
        await h.write_tx([i, i + 1])
        await ClockCycles(dut.clk, 20)
        assert await h.read_rx(2) == [i, i + 1]
    w.kill()
    assert not seen["bad"], seen["bad"][:5]


# ---------------------------------------------------------------------------
# ISA v3: 64-word memory, pattern branch, background clock
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_imem64_far_jumps_entry_and_wrap(dut):
    """Every branch form reaches both halves of the 64-word memory; ENTRY,
    WRAP_TOP and WRAP_BOT are 6 bits; execution falls through 31 -> 32."""
    h = PESM(dut)
    await h.start()
    prog = asm.assemble("""
.pattern 0x01 0x01          ; window bit 0 must be 1
.entry e
.org 0
low:    set tout1, 1        ; 0
        jpin tin0, 1, up    ; 1   JPIN  -> 52
        halt
.org 30
        set tout6, 1        ; 30
        nop                 ; 31  falls through into the upper half
        jmp back            ; 32  JMP   -> 58 (opcode 9)
.org 33
e:      set tout0, 1        ; 33  ENTRY >= 32
        ldi x, 44
        mov pc, x           ; 35  computed jump, 6-bit target
.wrap_target
wt:     set tout4, 1        ; 36  WRAP_BOT
        jmp low             ; 37  JMP   -> 0 (opcode 1, from the upper half)
.org 44
        set tout2, 1        ; 44
        jmp top             ; 45  JMP   -> 62
.org 52
up:     set tout3, 1        ; 52
        jpat tin0, 30       ; 53  JPAT  -> 30 (window = TIN0.., lower half)
        halt
.org 58
back:   set tout5, 1        ; 58
        halt                ; 59
.org 62
top:    nop                 ; 62
        nop                 ; 63  WRAP_TOP: falls through to WRAP_BOT
.wrap
""")
    assert prog.cfg[7] == 63 and prog.cfg[8] == 36 and prog.cfg[9] == 33
    await h.load(prog)
    h.set_tin(0b0001)
    await h.run()
    await ClockCycles(dut.clk, 40)
    st = await h.status()
    assert st["halted"] == 1 and st["err"] == 0 and st["pc"] == 59, st
    assert h.tout() == 0x7F, f"not every block was visited: tout={h.tout():#04x}"

    # the same program with TIN0 low stops at the first conditional branch
    await h.boot()
    h.set_tin(0)
    await h.run()
    await ClockCycles(dut.clk, 40)
    st = await h.status()
    assert st["halted"] == 1 and st["pc"] == 2 and h.tout() == 0b0010111, (st, h.tout())


@cocotb.test()
async def test_pattern_branch_single_cycle(dut):
    """JPAT: masked multi-pin compare and branch in one instruction, on BIO
    and on the TIN/flag window, against PAT_VAL and against X."""
    h = PESM(dut)
    await h.start()
    # window = BIO2..BIO9; watch BIO2, BIO3, BIO5: expect BIO2=1 BIO3=0 BIO5=1
    prog = asm.assemble("""
.pattern 0x0b 0x09
top:    jnpat bio2, top     ; wait for the pattern (one word)
        set tout0, 1
hold:   jpat bio2, hold     ; wait until it no longer matches
        set tout0, 0
        jmp top
""")
    await h.load(prog)
    h.set_ext(0x00)
    await h.run()
    await ClockCycles(dut.clk, 8)

    def match(v):
        return int((v >> 2) & 1 == 1 and (v >> 3) & 1 == 0 and (v >> 5) & 1 == 1)

    rng = random.Random(5)
    seen = set()
    cur = 0
    for i in range(160):
        v = rng.randrange(256) if i >= 64 else (i * 4) & 0xFF      # all 64 combos of BIO2..7 first
        await FallingEdge(dut.clk)
        h.set_ext(v)
        # 2 synchronizer flops + branch + SET: output valid after the 4th edge.
        # (jmp top adds one cycle only when leaving the 'hold' state, i.e. on a 1 -> 0 -> 1 pulse)
        for k in range(6):
            await FallingEdge(dut.clk)
            if (h.tout() & 1) == match(v):
                break
        lat = k + 1
        if match(v) != cur:
            assert lat == 4 or (lat == 5 and match(v) == 1), f"latency {lat} for {v:#04x}"
            seen.add((cur, match(v)))
        assert (h.tout() & 1) == match(v), f"pads {v:#04x}: tout0={h.tout() & 1}"
        cur = match(v)
        await ClockCycles(dut.clk, 3)
    assert seen == {(0, 1), (1, 0)}

    # compare against X on the window TIN0..3, TXNE, RXNF, HFLAG, BGCLK
    prog = asm.assemble("""
.pattern 0x4f 0x00          ; TIN[3:0] and HFLAG
        ldi x, 0x42         ; TIN == 2 and HFLAG == 1
top:    jnpatx tin0, top
        irq
        halt
""")
    await h.load(prog)
    await h.control(asm.CTRL_CLR_FLAGS | asm.CTRL_HFLAG_CLR)
    h.set_tin(2)
    await h.run()
    await ClockCycles(dut.clk, 20)
    st = await h.status()
    assert st["halted"] == 0 and st["irq"] == 0 and st["pc"] == 1
    h.set_tin(3)
    await h.control(asm.CTRL_HFLAG_SET)
    await ClockCycles(dut.clk, 20)
    assert (await h.status())["halted"] == 0, "TIN mismatch must not branch"
    h.set_tin(2)
    await ClockCycles(dut.clk, 10)
    st = await h.status()
    assert st["halted"] == 1 and st["irq"] == 1 and st["x"] == 0x42, st


async def _edges(dut, sample, n, max_cycles=20000):
    """Cycle numbers of the next n changes of sample()."""
    out, last, cyc = [], sample(), 0
    while len(out) < n:
        await FallingEdge(dut.clk)
        cyc += 1
        v = sample()
        if v != last:
            out.append(cyc)
            last = v
        assert cyc < max_cycles, "signal stopped toggling"
    return out


@cocotb.test()
async def test_background_clock(dut):
    """Background clock: exact period on the tick grid (integer and
    fractional divider), pin takeover, start/stop/reset from the program,
    level readable as input pin 15, internal-timebase mode."""
    h = PESM(dut)
    await h.start()

    # --- free running from program start, integer grid: 3 ticks x 4 clk per half period
    prog = asm.assemble("""
.div_raw 4 0
.bgclk pin=tout2 div=2 auto
.init_tout 0x7b              ; OUT[tout2] = 0: the pad must follow the generator, not OUT
loop:   jmp loop
""")
    await h.load(prog)
    assert (h.tout() >> 2) & 1 == 0, "idle level 0 while in BOOT"
    await h.run()
    e = await _edges(dut, lambda: (h.tout() >> 2) & 1, 20)
    assert all(b - a == 12 for a, b in zip(e, e[1:])), e
    assert h.tout() & 0x7B == 0x7B, "other TOUT pins must keep their OUT value"

    # --- fractional grid 3.5 clk/tick, toggle every tick: intervals 3/4, exact on average
    prog = asm.assemble("""
.div_raw 3 128
.bgclk pin=tout0 div=0 auto idle=1
loop:   jmp loop
""")
    await h.load(prog)
    assert h.tout() & 1 == 1, "idle level 1 while in BOOT"
    await h.run()
    e = await _edges(dut, lambda: h.tout() & 1, 65)
    d = [b - a for a, b in zip(e, e[1:])]
    assert set(d) == {3, 4} and sum(d) == 64 * 7 // 2, d

    # --- program control on a BIO pin, and the level as input pin 15
    prog = asm.assemble("""
.div_raw 5 0
.bgclk pin=bio3 div=0 idle=1 ; stopped at the idle level until 'bgclk on'
.init_oe 0x08
        wait high hflag
        bgclk on reset
l:      wait rise bgclk
        toggle tout0
        jpin hflag, 1, l
        bgclk off reset
        halt
""")
    await h.load(prog)
    await h.control(asm.CTRL_HFLAG_CLR)
    h.set_ext(0x00)
    await h.run()
    for _ in range(40):
        await FallingEdge(dut.clk)
        assert (h.pad() >> 3) & 1 == 1 and int(dut.uio_oe.value) == 0x08, "stopped: idle level"
    await h.control(asm.CTRL_HFLAG_SET)
    bg, t0, cyc = [], [], 0
    lb, lt = (h.pad() >> 3) & 1, h.tout() & 1
    while len(t0) < 12:
        await FallingEdge(dut.clk)
        cyc += 1
        b, t = (h.pad() >> 3) & 1, h.tout() & 1
        if b != lb:
            bg.append((cyc, b))
        if t != lt:
            t0.append(cyc)
        lb, lt = b, t
        assert cyc < 2000
    assert all(b[0] - a[0] == 5 for a, b in zip(bg, bg[1:])), bg     # (div+1) ticks x 5 clk
    assert bg[0][1] == 0, "first edge after 'on reset' leaves the idle level"
    rises = [c for c, v in bg if v == 1]
    # WAIT sees the edge one cycle after it happens, TOGGLE executes in the next one
    assert all(t - r == 2 for r, t in zip(rises, t0)), (rises, t0)
    await h.control(asm.CTRL_HFLAG_CLR)
    await ClockCycles(dut.clk, 40)
    st = await h.status()
    assert st["halted"] == 1
    for _ in range(30):
        await FallingEdge(dut.clk)
        assert (h.pad() >> 3) & 1 == 1, "bgclk off reset: back to the idle level"

    # --- no pin: internal timebase only (input pin 15), no pad is taken over
    prog = asm.assemble("""
.div_raw 2 0
.bgclk div=3 auto
.init_tout 0x00
l:      wait rise bgclk
        toggle tout5
        jmp l
""")
    await h.load(prog)
    await h.run()
    e = await _edges(dut, lambda: h.tout(), 10)
    assert all(b - a == 16 for a, b in zip(e, e[1:])), e          # 2 x (3+1) ticks x 2 clk
    assert h.tout() & ~0x20 == 0 and int(dut.uio_oe.value) == 0


@cocotb.test()
async def test_write_gate_at_boot_exit(dut):
    """A WRITE_IMEM / WRITE_CFG byte that lands around the falling edge of
    MODE is either fully applied before the core loads its start state, or
    rejected and flagged. Sweeps the MODE edge across the write cycle and
    checks that the executed program always equals the readback."""
    h = PESM(dut, sck_half=4)
    await h.start()
    old = asm.assemble("set tout0, 1\nhalt\n")
    new_w0 = asm.enc_setp("tout1", "set", 1)
    outcomes = set()

    async def frame_with_mode_drop(data, offset):
        """Like PESM.xfer, but MODE falls `offset` clk after the last SCK rise
        (negative: before it)."""
        await FallingEdge(dut.clk)
        h.set_ui_bit(2, 0)
        await ClockCycles(dut.clk, h.sck_half)
        nbits = 8 * len(data)
        for i in range(nbits):
            byte, b = data[i // 8], 7 - i % 8
            h.set_ui_bit(1, (byte >> b) & 1)
            last = i == nbits - 1
            if last and offset < 0:
                await ClockCycles(dut.clk, h.sck_half + offset)
                h.set_ui_bit(3, 0)
                await ClockCycles(dut.clk, -offset)
            else:
                await ClockCycles(dut.clk, h.sck_half)
            h.set_ui_bit(0, 1)
            if last and 0 <= offset < h.sck_half:
                await ClockCycles(dut.clk, offset)
                h.set_ui_bit(3, 0)
                await ClockCycles(dut.clk, h.sck_half - offset)
            else:
                await ClockCycles(dut.clk, h.sck_half)
            h.set_ui_bit(0, 0)
        await ClockCycles(dut.clk, h.sck_half)
        h.set_ui_bit(2, 1)
        h.set_ui_bit(1, 0)
        await ClockCycles(dut.clk, h.sck_half + 2)

    for offset in range(-3, 4):
        # ---- imem word 0 ----
        await h.load(old)
        await h.control(asm.CTRL_CLR_FLAGS)
        await frame_with_mode_drop(asm.frame_write_imem(0, [new_w0]), offset)
        await ClockCycles(dut.clk, 12)
        st = await h.status()
        tout = h.tout()
        assert st["halted"] == 1 and st["running"] == 0 and tout in (0b01, 0b10), (offset, st, tout)
        await h.boot()
        (w0,) = await h.read_imem(0, 1)
        assert w0 in (old.words[0], new_w0)
        applied = w0 == new_w0
        assert tout == (0b10 if applied else 0b01), f"offset {offset}: executed != stored"
        assert st["err"] == (0 if applied else 1), f"offset {offset}: rejected write must flag ERR"
        outcomes.add(applied)

        # ---- cfg INIT_TOUT (loaded into OUT in the last BOOT cycle) ----
        await h.load(asm.assemble("halt\n"))
        await h.control(asm.CTRL_CLR_FLAGS)
        await frame_with_mode_drop(asm.frame_write_cfg(15, [0x55]), offset)
        await ClockCycles(dut.clk, 12)
        st = await h.status()
        tout = h.tout()
        await h.boot()
        (c15,) = await h.read_cfg(15, 1)
        assert c15 in (0x00, 0x55) and tout == c15, f"offset {offset}: OUT {tout:#x} != cfg {c15:#x}"
        assert st["err"] == (0 if c15 == 0x55 else 1)
        outcomes.add(c15 == 0x55)
    assert outcomes == {True, False}, "sweep must cover both accepted and rejected writes"


@cocotb.test()
async def test_fw_addr_strobe_capture(dut):
    """firmware/addr_strobe_capture.pasm: JPAT qualifies a parallel bus
    (address BIO4..7 == 0xA and strobe TIN0) and captures BIO0..7."""
    h = PESM(dut)
    await h.start()
    await h.load(assemble_file("addr_strobe_capture.pasm"))
    h.set_ext(0x00)
    h.set_tin(0)
    await h.run()
    rng = random.Random(11)
    expect = []
    for i in range(24):
        addr = 0xA if i % 3 == 0 else rng.choice([0x0, 0x2, 0x8, 0xB, 0xE, 0xF])
        data = rng.randrange(16)
        h.set_ext((addr << 4) | data)
        await ClockCycles(dut.clk, 6)
        h.set_tin(rng.randrange(8) << 1 | 1)          # strobe high, TIN1..3 are don't care
        await ClockCycles(dut.clk, 8)
        if addr == 0xA:
            expect.append((addr << 4) | data)
        h.set_tin(rng.randrange(8) << 1)              # strobe low
        await ClockCycles(dut.clk, 6)
        if len(expect) == 6:                          # keep the 8-deep FIFO from filling
            assert await h.read_rx(6) == expect
            expect = []
    st = await h.status()
    assert st["rx_level"] == len(expect) and st["rx_ovf"] == 0
    assert await h.read_rx(len(expect)) == expect


@cocotb.test()
async def test_fw_sync_serial_tx(dut):
    """firmware/sync_serial_tx.pasm: background clock = free-running bit
    clock on TOUT1, data on TOUT2 changes only after falling edges and is
    sampled on rising edges."""
    h = PESM(dut)
    await h.start()
    prog = assemble_file("sync_serial_tx.pasm")
    await h.load(prog)
    await h.run()
    data = [0xA5, 0x3C, 0xFF, 0x00, 0x81]
    bits, cyc = [], 0
    last_clk, last_d = (h.tout() >> 1) & 1, (h.tout() >> 2) & 1
    rise_cycles, d_changes, falls = [], [], []

    async def sampler():
        nonlocal cyc, last_clk, last_d
        while len(bits) < 8 * len(data) + 12:
            await FallingEdge(dut.clk)
            cyc += 1
            c, d = (h.tout() >> 1) & 1, (h.tout() >> 2) & 1
            if c and not last_clk:
                rise_cycles.append(cyc)
                bits.append(d)
            if last_clk and not c:
                falls.append(cyc)
            if d != last_d:
                d_changes.append(cyc)
            last_clk, last_d = c, d

    task = cocotb.start_soon(sampler())
    await ClockCycles(dut.clk, 120)                   # clock runs before any data
    await h.write_tx(data)
    await task
    # exact 1 MHz clock: 50 clk period
    assert all(b - a == 50 for a, b in zip(rise_cycles, rise_cycles[1:])), rise_cycles[:6]
    # data only moves 2 clk after a falling clock edge
    assert d_changes and all(any(c - f == 2 for f in falls) for c in d_changes), d_changes[:8]
    # find the byte stream in the sampled bits (idle bits before and after)
    exp = [(b >> (7 - k)) & 1 for b in data for k in range(8)]
    hits = [i for i in range(len(bits) - len(exp) + 1) if bits[i:i + len(exp)] == exp]
    assert hits, f"TX bytes not found in the serial stream: {bits}"
    st = await h.status()
    assert st["tx_level"] == 0 and st["halted"] == 0


@cocotb.test()
async def test_dly_and_dlyt_exact(dut):
    """DLY occupies exactly N+1 cycles; DLYT retires on the N-th tick counted
    from its first execution cycle (never earlier, even for N = 1)."""
    h = PESM(dut)
    await h.start()
    prog = asm.assemble("""
.div_raw 7 0
loop:   toggle tout0 [1]    ; on a tick, cycle T
        dlyt 1              ; first cycle T+1 is not a tick: retires on tick T+7
        toggle tout1        ; T+8
        dlyt 2              ; ticks T+14, T+21: retires at T+21
        toggle tout2        ; T+22
        dly 5               ; T+23 .. T+28
        toggle tout3        ; T+29
        jmp loop            ; T+30; next grid point is T+35
""")
    await h.load(prog)
    await h.run()
    ev, cyc, last = [], 0, h.tout() & 0xF
    while len(ev) < 16:
        await FallingEdge(dut.clk)
        cyc += 1
        v = h.tout() & 0xF
        if v != last:
            ev.append((cyc, v ^ last))
            last = v
        assert cyc < 400
    t0 = ev[0][0]
    exp = []
    for k in range(4):
        exp += [(t0 + 35 * k + off, bit) for off, bit in ((0, 1), (8, 2), (22, 4), (29, 8))]
    assert ev == exp, f"{ev} != {exp}"

