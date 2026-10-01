# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""Shared cocotb helpers: reset, host SPI master, program loading."""

from __future__ import annotations

import os
import sys
from typing import List

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "sw"))
sys.path.insert(0, HERE)

from types import SimpleNamespace  # noqa: E402

from pesm import assembler as _assembler  # noqa: E402
from pesm import hostproto as _hp  # noqa: E402
from pesm import isa as _isa  # noqa: E402

# one namespace for the tests: encoders, assembler, host frames
asm = SimpleNamespace(**{k: v for m in (_isa, _hp, _assembler) for k, v in vars(m).items()
                         if not k.startswith("_")})

CLK_NS = 20  # 50 MHz
FW_DIR = os.path.join(HERE, "..", "firmware")

UI_SCK, UI_MOSI, UI_CSN, UI_MODE = 0, 1, 2, 3


def assemble_file(name: str):
    return _assembler.assemble_file(os.path.join(FW_DIR, name))


class PESM:
    """Board-level view of the chip: drives ui_in/uio_ext, talks SPI."""

    def __init__(self, dut, sck_half: int = 5):
        self.dut = dut
        self.ui = (1 << UI_CSN) | (1 << UI_MODE)  # CS_N high, BOOT
        self.sck_half = sck_half
        self.ext = 0xFF  # pulled-up bus by default

    # ------------------------------------------------------------------
    def _apply(self):
        self.dut.ui_in.value = self.ui

    def set_ui_bit(self, bit: int, v: int):
        if v:
            self.ui |= 1 << bit
        else:
            self.ui &= ~(1 << bit)
        self._apply()

    def set_tin(self, val: int):
        self.ui = (self.ui & 0x0F) | ((val & 0xF) << 4)
        self._apply()

    def set_ext(self, val: int):
        self.ext = val & 0xFF
        self.dut.uio_ext.value = self.ext

    async def start(self):
        cocotb.start_soon(Clock(self.dut.clk, CLK_NS, unit="ns").start())
        self.dut.ena.value = 1
        self._apply()
        self.set_ext(self.ext)
        self.dut.rst_n.value = 0
        await ClockCycles(self.dut.clk, 10)
        self.dut.rst_n.value = 1
        await ClockCycles(self.dut.clk, 5)

    # ------------------------------------------------------------------
    async def boot(self):
        self.set_ui_bit(UI_MODE, 1)
        await ClockCycles(self.dut.clk, 4)

    async def run(self):
        self.set_ui_bit(UI_MODE, 0)
        await ClockCycles(self.dut.clk, 1)

    # ------------------------------------------------------------------
    async def xfer(self, data: List[int]) -> List[int]:
        """One SPI frame (CS_N low for the whole list). Returns MISO bytes."""
        dut = self.dut
        out = []
        await FallingEdge(dut.clk)
        self.set_ui_bit(UI_CSN, 0)
        await ClockCycles(dut.clk, self.sck_half)
        for byte in data:
            rx = 0
            for b in range(7, -1, -1):
                self.set_ui_bit(UI_MOSI, (byte >> b) & 1)
                await ClockCycles(dut.clk, self.sck_half)
                rx = (rx << 1) | (int(dut.uo_out.value) & 1)  # sample before rising edge
                self.set_ui_bit(UI_SCK, 1)
                await ClockCycles(dut.clk, self.sck_half)
                self.set_ui_bit(UI_SCK, 0)
            out.append(rx)
        await ClockCycles(dut.clk, self.sck_half)
        self.set_ui_bit(UI_CSN, 1)
        self.set_ui_bit(UI_MOSI, 0)
        await ClockCycles(dut.clk, self.sck_half + 2)
        return out

    # ------------------------------------------------------------------
    async def write_imem(self, addr: int, words: List[int]):
        await self.xfer(asm.frame_write_imem(addr, words))

    async def read_imem(self, addr: int, n: int) -> List[int]:
        r = await self.xfer([asm.CMD_READ_IMEM | addr] + [0] * (2 * n))
        d = r[1:]
        return [(d[2 * i] << 8) | d[2 * i + 1] for i in range(n)]

    async def write_cfg(self, addr: int, data: List[int]):
        await self.xfer(asm.frame_write_cfg(addr, data))

    async def read_cfg(self, addr: int, n: int) -> List[int]:
        r = await self.xfer([asm.CMD_READ_CFG | addr] + [0] * n)
        return r[1:]

    async def write_tx(self, data: List[int]):
        await self.xfer([asm.CMD_WRITE_TX] + list(data))

    async def read_rx(self, n: int) -> List[int]:
        r = await self.xfer([asm.CMD_READ_RX] + [0] * n)
        return r[1:]

    async def status(self) -> dict:
        r = await self.xfer([asm.CMD_READ_STAT, 0, 0, 0, 0, 0])
        s0, s1, pc, x, y = r[1:6]
        return {
            "running": (s0 >> 7) & 1, "halted": (s0 >> 6) & 1, "irq": (s0 >> 5) & 1,
            "err": (s0 >> 4) & 1, "tx_ovf": (s0 >> 3) & 1, "rx_ovf": (s0 >> 2) & 1,
            "rx_unf": (s0 >> 1) & 1, "hflag": s0 & 1,
            "tx_level": s1 >> 4, "rx_level": s1 & 15, "pc": pc, "x": x, "y": y,
            "raw0": r[0],
        }

    async def control(self, bits: int):
        await self.xfer([asm.CMD_CONTROL | (bits & 0x1F)])

    async def load(self, prog):
        await self.boot()
        await self.write_cfg(0, prog.cfg)
        await self.write_imem(0, prog.words)
        rb = await self.read_imem(0, 32)
        assert rb == prog.words, f"imem readback mismatch {rb} != {prog.words}"
        rc = await self.read_cfg(0, 16)
        assert rc == prog.cfg, f"cfg readback mismatch {rc} != {prog.cfg}"

    # ------------------------------------------------------------------
    def tout(self) -> int:
        return (int(self.dut.uo_out.value) >> 1) & 0x7F

    def pad(self) -> int:
        return int(self.dut.uio_in.value)


async def wait_cycles(dut, n):
    await ClockCycles(dut.clk, n)


async def sample_after_edge(dut):
    await RisingEdge(dut.clk)
    await ReadOnly()
