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
    """Burst + random-access imem/cfg writes, readback, defaults."""
    h = PESM(dut)
    await h.start()
    await h.boot()

    # reset contents
    assert await h.read_imem(0, 32) == [0x0100] * 32, "imem must reset to HALT"
    assert await h.read_cfg(0, 16) == asm.CFG_DEFAULTS, "cfg reset defaults"

    rng = random.Random(1)
    words = [rng.randrange(1 << 16) for _ in range(32)]
    await h.write_imem(0, words)
    assert await h.read_imem(0, 32) == words

    # random-access single-word patch at 0x1F and 0x07
    await h.write_imem(31, [0xBEEF])
    await h.write_imem(7, [0x1234, 0x5678])
    words[31], words[7], words[8] = 0xBEEF, 0x1234, 0x5678
    assert await h.read_imem(0, 32) == words
    assert await h.read_imem(31, 1) == [0xBEEF]

    # imem address wraps 31 -> 0 in a burst
    await h.write_imem(31, [0xAAAA, 0x5555])
    words[31], words[0] = 0xAAAA, 0x5555
    assert await h.read_imem(0, 32) == words

    # cfg: single register patch
    cfg = [rng.randrange(256) for _ in range(16)]
    await h.write_cfg(0, cfg)
    masks = [0xFF, 0xFF, 0xFF, 0x0F, 0x1F, 0x1F, 0xFF, 0x1F, 0x1F, 0x1F, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x7F]
    exp = [c & m for c, m in zip(cfg, masks)]
    assert await h.read_cfg(0, 16) == exp
    await h.write_cfg(9, [0x13])
    exp[9] = 0x13
    assert await h.read_cfg(9, 1) == [0x13]
    assert await h.read_cfg(0, 16) == exp


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
    prog = asm.assemble("nop\n nop\n .word 0xA123\n nop\n")
    await h.load(prog)
    await h.run()
    await ClockCycles(dut.clk, 20)
    st = await h.status()
    assert st["halted"] == 1 and st["err"] == 1 and st["pc"] == 2 and st["running"] == 0


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
