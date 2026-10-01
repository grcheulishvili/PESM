# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""
Toolchain on the real design (black-box, RTL and gate level):
  * pesm.programmer / pesm.run_pipeline driving the SPI host port through a
    cocotb-backed Transport (the same code that drives an FT232H),
  * Python-DSL macros (UART TX byte, SPI transfer, I2C read) checked
    against protocol-level device models.
"""

from __future__ import annotations

import os

import cocotb
from cocotb.task import bridge, resume
from cocotb.triggers import ClockCycles, FallingEdge
from cocotb.utils import get_sim_time

from pesm_tb import HERE, PESM

from pesm.builder import PESMProgram          # noqa: E402  (path set by pesm_tb)
from pesm.programmer import Programmer, Transport
from pesm.run_pipeline import run_pipeline

EXAMPLES = os.path.join(HERE, "..", "sw", "examples")


class CocotbTransport(Transport):
    """Transport whose blocking calls run inside a cocotb.bridge thread."""

    def __init__(self, h: PESM):
        self.h = h

    def xfer(self, mosi):
        return resume(self.h.xfer)(list(mosi))

    def set_mode(self, boot: bool) -> None:
        async def _m():
            self.h.set_ui_bit(3, int(boot))
            await ClockCycles(self.h.dut.clk, 4)
        resume(_m)()

    def delay(self, seconds: float) -> None:
        async def _d():
            await ClockCycles(self.h.dut.clk, max(1, int(seconds * 50e6)))
        resume(_d)()

    def now(self) -> float:
        return get_sim_time("ns") * 1e-9


def crc16_usb(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


@cocotb.test()
async def test_programmer_pipeline_on_chip(dut):
    """programmer.py: load + readback verify + random-access patch + run +
    flow-controlled TX + host flag + wait_halt + RX, all over SPI."""
    h = PESM(dut, sck_half=4)
    await h.start()

    @bridge
    def host():
        t = CocotbTransport(h)
        res = run_pipeline(os.path.join(EXAMPLES, "crc16_usb.py"), t, tx=b"123456789",
                           hflag=True, wait_halt=True, rx=2, timeout=0.01)
        p = Programmer(t)
        p.boot()
        p.patch(31, [0x0100])
        return res, p.read_imem(31, 1), p.status()

    res, patched, st = await host()
    assert (res.rx[1] << 8) | res.rx[0] == crc16_usb(b"123456789") == 0xB4C8
    assert res.status.halted and patched == [0x0100] and not st.running


async def _uart_decode(dut, h, nframes, period, max_cycles):
    edges, last, cyc = [], 1, 0
    while cyc < max_cycles:
        await FallingEdge(dut.clk)
        cyc += 1
        v = h.tout() & 1
        if v != last:
            edges.append(cyc)
            last = v
    return edges


@cocotb.test()
async def test_dsl_uart_tx_byte_macro(dut):
    h = PESM(dut)
    await h.start()
    p = PESMProgram("utx").clock(tick_hz=1_000_000).init_pins(tout=0x01)
    p.macro_uart_tx_byte("tout0", 0x5A).macro_uart_tx_byte("tout0", 0xC3, stop_bits=2).halt()
    await h.load(p.compile())
    await h.run()
    levels = []
    for _ in range(50 * 25):
        await FallingEdge(dut.clk)
        levels.append(h.tout() & 1)
    got = []
    i = 0
    while len(got) < 2:
        s = levels.index(0, i)
        bits = [levels[s + 25 + 50 * k] for k in range(10)]
        assert bits[0] == 0 and bits[9] == 1
        # every bit lasts exactly 50 clk (integer divider -> exact grid)
        for k in range(1, 9):
            assert levels[s + 50 * k - 1] == bits[k - 1] and levels[s + 50 * k] == bits[k]
        got.append(sum(b << n for n, b in enumerate(bits[1:9])))
        i = s + 500
    assert got == [0x5A, 0xC3]


@cocotb.test()
async def test_dsl_spi_transfer_macro(dut):
    h = PESM(dut)
    await h.start()
    SCLK, MOSI, CS, MISO = "tout1", "tout2", "tout3", "tin1"
    p = PESMProgram("spi").clock(tick_hz=2_000_000).init_pins(tout=0x08)
    p.set_pin(CS, 0, delay=1)
    p.macro_spi_transfer(SCLK, MOSI, MISO).macro_spi_transfer(SCLK, MOSI, MISO)
    p.set_pin(CS, 1, delay=1).halt()
    await h.load(p.compile())
    await h.write_tx([0x3C, 0xA5])
    replies = [0x81, 0x7E]
    st = {"cs": 1, "sclk": 0, "bit": 0, "byte": 0, "n": 0, "mosi": []}

    def miso_bit():
        return (replies[st["n"] % 2] >> (7 - st["bit"])) & 1

    async def slave():
        for _ in range(4000):
            await FallingEdge(dut.clk)
            t = h.tout()
            cs, sclk, mosi = (t >> 3) & 1, (t >> 1) & 1, (t >> 2) & 1
            if st["cs"] and not cs:
                h.set_tin((h.ui >> 4) & ~2 | (miso_bit() << 1))
            if not cs and sclk and not st["sclk"]:
                st["byte"] = (st["byte"] << 1) | mosi
                st["bit"] += 1
                if st["bit"] == 8:
                    st["mosi"].append(st["byte"])
                    st.update(bit=0, byte=0, n=st["n"] + 1)
            if not cs and not sclk and st["sclk"]:
                h.set_tin((h.ui >> 4) & ~2 | (miso_bit() << 1))
            st["cs"], st["sclk"] = cs, sclk

    await h.run()
    await slave()
    assert st["mosi"] == [0x3C, 0xA5]
    assert (await h.status())["halted"] == 1
    assert await h.read_rx(2) == replies


class I2CDevice:
    """I2C slave with write and read support (open drain via uio_ext)."""
    SDA, SCL = 0, 1

    def __init__(self, dut, h, addr, reply):
        self.dut, self.h, self.addr, self.reply = dut, h, addr, reply
        self.log = []          # ("start"|"stop"|("byte", v, acked)|("mack", bool))
        self.pull_sda = 0

    def _drive(self, low):
        self.pull_sda = low
        self.h.set_ext(0xFF & ~(low << self.SDA))

    async def run(self, cycles):
        psda, pscl = 1, 1
        st, n, cur, addressed, rd = "idle", 0, 0, False, 0
        for _ in range(cycles):
            await FallingEdge(self.dut.clk)
            pad = self.h.pad()
            sda, scl = pad & 1, (pad >> 1) & 1
            if scl and pscl and psda and not sda:
                self.log.append("start")
                st, n, cur = "addr", 0, 0
                self._drive(0)
            elif scl and pscl and not psda and sda:
                self.log.append("stop")
                st = "idle"
                self._drive(0)
            elif scl and not pscl:                                   # rise
                if st in ("addr", "wdata"):
                    cur, n = (cur << 1) | sda, n + 1
                elif st == "mack":
                    self.log.append(("mack", sda == 0))
                    st = "idle"
            elif not scl and pscl:                                   # fall
                if st in ("addr", "wdata") and n == 8:
                    if st == "addr":
                        addressed, rd = (cur >> 1) == self.addr, cur & 1
                    self.log.append(("byte", cur, addressed))
                    self._drive(1 if addressed else 0)
                    st, n, cur = "sack", 0, 0
                elif st == "sack":
                    if addressed and rd:
                        st, n = "rdata", 0
                        self._drive(0 if (self.reply >> 7) & 1 else 1)
                    else:
                        self._drive(0)
                        st = "wdata" if addressed else "idle"
                elif st == "rdata":
                    n += 1
                    if n < 8:
                        self._drive(0 if (self.reply >> (7 - n)) & 1 else 1)
                    else:
                        self._drive(0)
                        st = "mack"
            psda, pscl = sda, scl


@cocotb.test()
async def test_dsl_i2c_read_macro(dut):
    h = PESM(dut)
    await h.start()
    SDA, SCL = "bio0", "bio1"
    p = PESMProgram("i2c_rd").clock(tick_hz=400_000)
    p.macro_i2c_start(SDA, SCL)
    p.macro_i2c_write_byte(SDA, SCL)               # address + R from the TX FIFO
    p.macro_i2c_read_byte(SDA, SCL, ack=False)
    p.macro_i2c_stop(SDA, SCL).halt()
    img = p.compile()
    assert img.length <= 32
    await h.load(img)
    await h.write_tx([(0x29 << 1) | 1])
    dev = I2CDevice(dut, h, 0x29, reply=0xB6)
    await h.run()
    await dev.run(125 * 4 * 22)
    assert dev.log[0] == "start" and dev.log[-1] == "stop", dev.log
    assert ("byte", 0x53, True) in dev.log
    assert ("mack", False) in dev.log                 # master NACKs the last byte
    assert (await h.status())["halted"] == 1
    assert await h.read_rx(1) == [0xB6]
