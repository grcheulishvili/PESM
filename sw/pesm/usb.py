# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""
USB low-/full-speed packet helpers (host side, pure Python): PIDs, CRC5,
CRC16, packet assembly, and the NRZI / bit-stuffed line encoding.

Used by PESMProgram.macro_usb_ls_token() (which resolves the whole token at
compile time) and to build the byte stream for the FIFO-fed transmitter
(macro_usb_ls_tx). Bits are sent LSB first within each byte.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Union

SYNC = 0x80          # KJKJKJKK on the wire

PID = {
    "out": 0x1, "in": 0x9, "sof": 0x5, "setup": 0xD,
    "data0": 0x3, "data1": 0xB,
    "ack": 0x2, "nak": 0xA, "stall": 0xE,
    "pre": 0xC,
}
TOKEN_PIDS = ("out", "in", "setup")

J, K, SE0 = "J", "K", "0"


def pid_byte(pid: Union[str, int]) -> int:
    """4-bit PID -> PID byte (low nibble PID, high nibble its complement)."""
    p = PID[pid.lower()] if isinstance(pid, str) else int(pid)
    if not 0 <= p <= 15:
        raise ValueError(f"PID out of range: {pid}")
    return p | ((~p & 0xF) << 4)


def crc5(value: int, nbits: int = 11) -> int:
    """USB CRC5 (x^5 + x^2 + 1) over `nbits` of `value`, LSB first.
    Returns the 5-bit field as transmitted (inverted remainder, LSB first)."""
    crc = 0x1F
    for i in range(nbits):
        b = (value >> i) & 1
        fb = b ^ (crc & 1)
        crc >>= 1
        if fb:
            crc ^= 0x14        # reflected 0x05
    return ~crc & 0x1F


def crc16(data: Iterable[int]) -> int:
    """USB CRC16 (x^16 + x^15 + x^2 + 1) over the payload bytes.
    Returns the 16-bit field; send low byte first."""
    crc = 0xFFFF
    for byte in data:
        for i in range(8):
            fb = ((byte >> i) & 1) ^ (crc & 1)
            crc >>= 1
            if fb:
                crc ^= 0xA001
    return ~crc & 0xFFFF


def token_fields(addr: int, endp: int) -> int:
    """16-bit token payload {CRC5[4:0], ENDP[3:0], ADDR[6:0]} (sent LSB first)."""
    if not 0 <= addr <= 127:
        raise ValueError("USB address 0..127")
    if not 0 <= endp <= 15:
        raise ValueError("USB endpoint 0..15")
    v = addr | (endp << 7)
    return v | (crc5(v, 11) << 11)


def token_packet(pid: Union[str, int], addr: int, endp: int, sync: bool = True) -> List[int]:
    """SYNC, PID, 2 bytes of ADDR/ENDP/CRC5."""
    f = token_fields(addr, endp)
    return ([SYNC] if sync else []) + [pid_byte(pid), f & 0xFF, f >> 8]


def data_packet(pid: Union[str, int], payload: Sequence[int], sync: bool = True) -> List[int]:
    """SYNC, PID (DATA0/DATA1), payload, CRC16 (low byte first)."""
    c = crc16(payload)
    return ([SYNC] if sync else []) + [pid_byte(pid)] + [b & 0xFF for b in payload] + \
        [c & 0xFF, c >> 8]


def handshake_packet(pid: Union[str, int], sync: bool = True) -> List[int]:
    return ([SYNC] if sync else []) + [pid_byte(pid)]


def bits_lsb_first(data: Iterable[int]) -> List[int]:
    return [(b >> i) & 1 for b in data for i in range(8)]


def stuff(bits: Sequence[int]) -> List[int]:
    """Insert a 0 after six consecutive 1s."""
    out, run = [], 0
    for b in bits:
        out.append(b)
        run = run + 1 if b else 0
        if run == 6:
            out.append(0)
            run = 0
    return out


def nrzi(bits: Sequence[int], start: str = J) -> List[str]:
    """Bits -> line states: 0 toggles J<->K, 1 keeps the state. Idle is J."""
    out, cur = [], start
    for b in bits:
        if not b:
            cur = K if cur == J else J
        out.append(cur)
    return out


def line_states(packet: Sequence[int], eop: bool = True) -> List[str]:
    """Full line-state sequence of a packet (one entry per bit time):
    NRZI of the stuffed bits, then SE0 SE0 J."""
    return nrzi(stuff(bits_lsb_first(packet))) + ([SE0, SE0, J] if eop else [])


def runs(states: Sequence[str]) -> List[tuple]:
    """Run-length encode a line-state sequence: [(state, bit_times), ...]."""
    out: List[list] = []
    for s in states:
        if out and out[-1][0] == s:
            out[-1][1] += 1
        else:
            out.append([s, 1])
    return [tuple(r) for r in out]


def decode_line(states: Sequence[str]) -> List[int]:
    """Inverse of line_states() up to the EOP: line states -> packet bytes
    (including SYNC). Raises ValueError on a stuffing or framing error."""
    bits, prev, run = [], J, 0
    it = list(states)
    n = it.index(SE0) if SE0 in it else len(it)
    skip = False
    for s in it[:n]:
        b = 1 if s == prev else 0
        prev = s
        if skip:
            if b:
                raise ValueError("bit-stuff error")
            skip, run = False, 0
            continue
        bits.append(b)
        run = run + 1 if b else 0
        if run == 6:
            skip = True
    if len(bits) % 8:
        raise ValueError(f"packet is not a whole number of bytes ({len(bits)} bits)")
    return [sum(bits[i + k] << k for k in range(8)) for i in range(0, len(bits), 8)]
