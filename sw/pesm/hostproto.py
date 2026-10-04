# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""
Host SPI protocol of PESM v3 (docs/ISA.md section 6): frame builders and
status decoding. Pure functions, no I/O.

SPI mode 0, MSB first, CS_N frames one command. Byte 0 is the command;
MISO returns STATUS0 while the command byte is shifted in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

from . import isa

CMD_WRITE_IMEM = 0x00    # 00 aaaaaa
CMD_READ_IMEM = 0x40     # 01 aaaaaa
CMD_WRITE_CFG = 0x80     # 100 aaaaa
CMD_READ_CFG = 0xA0      # 101 aaaaa
CMD_WRITE_TX = 0xC0      # 110 00---
CMD_READ_RX = 0xC8       # 110 01---
CMD_READ_STAT = 0xD0     # 110 10---
CMD_CONTROL = 0xE0       # 111 fffff

CTRL_FLUSH_TX = 0x01
CTRL_FLUSH_RX = 0x02
CTRL_HFLAG_SET = 0x04
CTRL_HFLAG_CLR = 0x08
CTRL_CLR_FLAGS = 0x10

FIFO_DEPTH = 8
MAX_SCK_RATIO = 8      # f_SCK <= f_clk / 8


def frame_write_imem(addr: int, words: Sequence[int]) -> List[int]:
    out = [CMD_WRITE_IMEM | (addr & 63)]
    for w in words:
        out += [(w >> 8) & 0xFF, w & 0xFF]
    return out


def frame_read_imem(addr: int, n: int) -> List[int]:
    return [CMD_READ_IMEM | (addr & 63)] + [0] * (2 * n)


def parse_read_imem(miso: Sequence[int], n: int) -> List[int]:
    d = list(miso[1:])
    return [(d[2 * i] << 8) | d[2 * i + 1] for i in range(n)]


def frame_write_cfg(addr: int, data: Sequence[int]) -> List[int]:
    return [CMD_WRITE_CFG | (addr & 31)] + [int(d) & 0xFF for d in data]


def frame_read_cfg(addr: int, n: int) -> List[int]:
    return [CMD_READ_CFG | (addr & 31)] + [0] * n


def frame_write_tx(data: Sequence[int]) -> List[int]:
    return [CMD_WRITE_TX] + [int(d) & 0xFF for d in data]


def frame_read_rx(n: int) -> List[int]:
    return [CMD_READ_RX] + [0] * n


def frame_status() -> List[int]:
    return [CMD_READ_STAT, 0, 0, 0, 0, 0, 0]


def frame_control(bits: int) -> List[int]:
    return [CMD_CONTROL | (bits & 0x1F)]


def frames_for_image(img: isa.Image) -> List[List[int]]:
    return [frame_write_cfg(0, img.cfg), frame_write_imem(0, img.words)]


@dataclass
class Status:
    running: bool
    halted: bool
    irq: bool
    err: bool
    tx_ovf: bool
    rx_ovf: bool
    rx_unf: bool
    hflag: bool
    tx_level: int
    rx_level: int
    pc: int
    x: int
    y: int
    chip_id: int = isa.CHIP_ID

    @classmethod
    def decode(cls, miso: Sequence[int]) -> "Status":
        s0, s1, pc, x, y = list(miso[1:6])
        cid = miso[6] if len(miso) > 6 else isa.CHIP_ID
        return cls(running=bool(s0 & 0x80), halted=bool(s0 & 0x40), irq=bool(s0 & 0x20),
                   err=bool(s0 & 0x10), tx_ovf=bool(s0 & 0x08), rx_ovf=bool(s0 & 0x04),
                   rx_unf=bool(s0 & 0x02), hflag=bool(s0 & 0x01), tx_level=s1 >> 4,
                   rx_level=s1 & 15, pc=pc & 63, x=x, y=y, chip_id=cid)

    def __str__(self) -> str:
        flags = [n for n in ("running", "halted", "irq", "err", "tx_ovf", "rx_ovf", "rx_unf",
                             "hflag") if getattr(self, n)]
        return (f"pc={self.pc} x=0x{self.x:02x} y=0x{self.y:02x} tx={self.tx_level} "
                f"rx={self.rx_level} [{' '.join(flags) or '-'}]")
