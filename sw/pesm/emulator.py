# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""
Pure-Python emulation of a PESM chip as seen from the host SPI port:
command decoding, imem/config storage, FIFOs, flags, plus the
cycle-accurate core model (pesm.model). Used as a programmer backend
for dry runs and CI without hardware.

Time advances in clk cycles: every transferred SPI byte costs
8 * clk_hz / sck_hz cycles, and Transport.delay() advances explicitly.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from . import hostproto as hp
from . import isa
from .model import PESMModel


class PESMEmulator:
    def __init__(self, clk_hz: float = isa.CLK_HZ_DEFAULT, sck_hz: float = 1e6):
        self.clk_hz = clk_hz
        self.cycles_per_byte = max(1, int(8 * clk_hz / sck_hz))
        self.model = PESMModel([isa.INSTR_HALT] * 32, list(isa.CFG_DEFAULTS), [])
        self.mode_boot = 1
        self.tin = 0
        self.uio_ext = 0xFF
        self.tx_ovf = self.rx_unf = self.wr_err = 0
        self.cycle = 0
        self.pin_hook: Optional[Callable[["PESMEmulator"], None]] = None

    # ------------------------------------------------------------------ time
    def step(self, n: int = 1) -> None:
        m = self.model
        for _ in range(n):
            if self.pin_hook:
                self.pin_hook(self)
            ui = (self.mode_boot << 3) | ((self.tin & 0xF) << 4) | 0x04  # CS_N idle high
            m.step(ui, self.uio_ext)
            self.cycle += 1

    def advance_seconds(self, s: float) -> None:
        self.step(max(1, int(s * self.clk_hz)))

    # ------------------------------------------------------------------ status
    @property
    def boot(self) -> bool:
        return bool(self.model.s2_mode)

    def _status0(self) -> int:
        m = self.model
        running = (not self.boot) and not m.halted
        return ((running << 7) | (m.halted << 6) | (m.irq << 5) | ((m.err | self.wr_err) << 4)
                | (self.tx_ovf << 3) | (m.rx_ovf << 2) | (self.rx_unf << 1) | m.hflag)

    def _status_bytes(self) -> List[int]:
        m = self.model
        return [self._status0(), (len(m.tx) << 4) | len(m.rx), m.pc, m.x, m.y]

    # ------------------------------------------------------------------ SPI
    def xfer(self, mosi: Sequence[int]) -> List[int]:
        m = self.model
        miso: List[int] = []
        if not mosi:
            return miso
        self.step(self.cycles_per_byte)
        miso.append(self._status0())
        cmd = mosi[0]
        op, addr = cmd >> 5, cmd & 31
        if op == 7:
            if cmd & hp.CTRL_FLUSH_TX:
                m.tx.clear()
            if cmd & hp.CTRL_FLUSH_RX:
                m.rx.clear()
            if cmd & hp.CTRL_HFLAG_SET:
                m.hflag = 1
            elif cmd & hp.CTRL_HFLAG_CLR:
                m.hflag = 0
            if cmd & hp.CTRL_CLR_FLAGS:
                m.irq = m.err = m.rx_ovf = 0
                self.tx_ovf = self.rx_unf = self.wr_err = 0
        phase, hold, sidx = 0, 0, 0
        for b in mosi[1:]:
            out = self._status0()
            if op == 1:
                w = m.imem[addr]
                out = (w & 0xFF) if phase else (w >> 8)
                if phase:
                    addr = (addr + 1) & 31
                phase ^= 1
            elif op == 3:
                out = m.cfg[addr & 15]
                addr = (addr & 15) + 1 & 15
            elif op == 5:
                if m.rx:
                    out = m.rx.pop(0)
                else:
                    out = 0
                    self.rx_unf = 1
            elif op == 6:
                st = self._status_bytes()
                out = st[sidx] if sidx < 5 else 0
                sidx += 1
            self.step(self.cycles_per_byte)
            miso.append(out)
            if op == 0:
                if not phase:
                    hold, phase = b, 1
                else:
                    if self.boot:
                        m.imem[addr] = (hold << 8) | b
                    else:
                        self.wr_err = 1
                    addr, phase = (addr + 1) & 31, 0
            elif op == 2:
                if self.boot:
                    m.cfg[addr & 15] = b & isa.CFG_MASKS[addr & 15]
                else:
                    self.wr_err = 1
                addr = (addr & 15) + 1 & 15
            elif op == 4:
                if len(m.tx) < hp.FIFO_DEPTH:
                    m.tx.append(b)
                else:
                    self.tx_ovf = 1
        return miso

    def set_mode(self, boot: bool) -> None:
        self.mode_boot = 1 if boot else 0
        self.step(4)
