# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""
Cycle-accurate reference model of the PESM v2 chip (core side).

Written from docs/ISA.md, not from the RTL. Used by the CRV testbench
and by pesm.emulator. The host SPI engine is not
modelled: program, config and TX FIFO contents are loaded by backdoor and
the testbench uses the real SPI path to put the same content into the DUT.

Call step(ui_in, uio_ext) once per rising clk edge with the pad values
present *before* that edge. After the call, outputs() returns the
registered outputs the DUT drives *after* that edge.
"""

from __future__ import annotations

from collections import Counter
from typing import List

M32 = 0xFFFFFFFF


def _bit(v: int, i: int) -> int:
    return (v >> i) & 1


def _rev8(v: int) -> int:
    return int(f"{v & 0xFF:08b}"[::-1], 2)


def _par8(v: int) -> int:
    return bin(v & 0xFF).count("1") & 1


class PESMModel:
    def __init__(self, imem: List[int], cfg: List[int], tx_fifo: List[int] | None = None):
        assert len(imem) == 32 and len(cfg) == 16
        self.imem = list(imem)
        self.cfg = list(cfg)
        self.tx = list(tx_fifo or [])
        self.rx: List[int] = []
        self.rx_pending = None   # registered RX write port (lands one cycle later)
        # synchronizers (model starts from steady pads)
        self.s1_mode = 1
        self.s2_mode = 1
        self.s1_tin = 0
        self.s2_tin = 0
        self.s1_bio = 0
        self.s2_bio = 0
        self.hflag = 0
        # sticky flags
        self.irq = 0
        self.err = 0
        self.rx_ovf = 0
        self.halted = 0
        self._core_reset()
        self.in_prev = 0
        self.div_cnt = 0
        self.div_acc = 0
        self.cov: Counter = Counter()

    # ------------------------------------------------------------------
    # config accessors
    # ------------------------------------------------------------------
    @property
    def div_int(self):
        return self.cfg[0] | (self.cfg[1] << 8)

    @property
    def div_frac(self):
        return self.cfg[2]

    @property
    def out_right(self):
        return self.cfg[3] & 1

    @property
    def in_right(self):
        return (self.cfg[3] >> 1) & 1

    @property
    def autopull(self):
        return (self.cfg[3] >> 2) & 1

    @property
    def autopush(self):
        return (self.cfg[3] >> 3) & 1

    @property
    def pull_th(self):
        return (self.cfg[4] & 31) or 32

    @property
    def push_th(self):
        return (self.cfg[5] & 31) or 32

    @property
    def side_count(self):
        return min(self.cfg[6] & 7, 4)

    @property
    def side_pindir(self):
        return (self.cfg[6] >> 3) & 1

    @property
    def side_base(self):
        return (self.cfg[6] >> 4) & 15

    @property
    def od(self):
        return self.cfg[10]

    @property
    def crc_poly(self):
        return self.cfg[11] | (self.cfg[12] << 8)

    def _core_reset(self):
        self.pc = self.cfg[9] & 31
        self.x = 0
        self.y = 0
        self.isr = 0
        self.osr = 0
        self.isr_cnt = 0
        self.osr_cnt = 32
        self.lastbit = 0
        self.pd_active = 0
        self.pd_done = 0
        self.pd_cnt = 0
        self.dl_active = 0
        self.dl_cnt = 0
        self.halted = 0
        self.out = (self.cfg[13] | ((self.cfg[15] & 0x7F) << 8)) & 0x7FFF
        self.oe = self.cfg[14] & 0xFF

    # ------------------------------------------------------------------
    # outputs
    # ------------------------------------------------------------------
    def uio_out(self) -> int:
        return (self.out & 0xFF) & ~self.od & 0xFF

    def uio_oe(self) -> int:
        return self.oe & ~(self.od & self.out & 0xFF) & 0xFF

    def uo_out_hi(self) -> int:
        """uo_out[7:1] (uo_out[0] is MISO, not modelled)."""
        return ((self.out >> 8) & 0x7F) << 1

    def pad(self, ext: int) -> int:
        oe = self.uio_oe()
        return ((self.uio_out() & oe) | (ext & ~oe)) & 0xFF

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _in_vec(self) -> int:
        txne = 1 if self.tx else 0
        rxnf = 0 if self._rx_full() else 1
        return (self.s2_bio | (self.s2_tin << 8) | (txne << 12) | (rxnf << 13)
                | (self.hflag << 14)) & 0xFFFF

    def _rx_full(self) -> bool:
        return len(self.rx) + (self.rx_pending is not None) >= 8

    def _tick(self, boot: int) -> int:
        return 1 if (not boot and self.div_cnt == 0) else 0

    def _isr_byte(self, v):
        return (v >> 24) & 0xFF if self.in_right else v & 0xFF

    def _osr_byte(self, v):
        return v & 0xFF if self.out_right else (v >> 24) & 0xFF

    def _place_out(self, b):
        return b & 0xFF if self.out_right else (b & 0xFF) << 24

    def _place_in(self, b):
        return (b & 0xFF) << 24 if self.in_right else b & 0xFF

    def _set_out_pin(self, out, p, v):
        if p == 15:
            return out
        return (out & ~(1 << p)) | ((v & 1) << p)

    # ------------------------------------------------------------------
    # one clock edge
    # ------------------------------------------------------------------
    def step(self, ui_in: int, uio_ext: int, hflag_set: int = 0, hflag_clr_host: int = 0):
        boot = self.s2_mode
        in_vec = self._in_vec()
        tick = self._tick(boot)
        pads = self.pad(uio_ext)

        div_sync = None  # None | 'full' | 'half'
        hclr_core = 0
        tx_pop = 0
        rx_push = None

        if boot:
            self._core_reset()
        else:
            run = not self.halted
            if run:
                w = self.imem[self.pc]
                op = (w >> 12) & 0xF
                class_a = op <= 7 and op != 2
                sc = self.side_count
                tail = w & 0xF
                dly = (tail & ((1 << (4 - sc)) - 1)) if class_a else 0
                sval = (tail >> (4 - sc)) if (class_a and sc) else 0
                rem = self.pd_cnt if self.pd_active else dly
                ex = dly == 0 or self.pd_done or (tick and rem == 1)
                if not ex:
                    self.pd_active = 1
                    self.pd_cnt = rem - tick
                else:
                    r = self._execute(w, op, class_a, sc, sval, in_vec, tick)
                    stall, taken, tgt, div_sync, hclr_core, tx_pop, rx_push = r
                    self._cover(w, op, dly, stall, taken, tx_pop, rx_push, div_sync, sc)
                    self.pd_active = 0
                    if stall:
                        self.pd_done = 1 if dly else 0
                    else:
                        self.pd_done = 0
                        self.dl_active = 0
                        if taken:
                            self.pc = tgt
                        elif self.pc == (self.cfg[7] & 31):
                            self.pc = self.cfg[8] & 31
                        else:
                            self.pc = (self.pc + 1) & 31

        # in_prev registers in_vec every cycle
        self.in_prev = in_vec

        # FIFOs
        if tx_pop:
            self.tx.pop(0)
        if self.rx_pending is not None:
            self.rx.append(self.rx_pending)
        self.rx_pending = rx_push

        # host flag
        if hflag_set:
            self.hflag = 1
        elif hflag_clr_host or hclr_core:
            self.hflag = 0

        # divider (uses pre-edge enable = not boot)
        if boot:
            self.div_cnt, self.div_acc = 0, 0
        elif div_sync is not None:
            i = self.div_int
            if div_sync == "half":
                h = i >> 1
                self.div_cnt = 0 if h == 0 else h - 1
            else:
                self.div_cnt = 0 if i == 0 else i - 1
            self.div_acc = 0
        elif self.div_cnt == 0:
            if self.div_int == 0:
                self.div_acc = 0
            else:
                s = self.div_acc + self.div_frac
                self.div_cnt = self.div_int - 1 + (s >> 8)
                self.div_acc = s & 0xFF
        else:
            self.div_cnt -= 1

        # synchronizers
        self.s2_mode, self.s1_mode = self.s1_mode, (ui_in >> 3) & 1
        self.s2_tin, self.s1_tin = self.s1_tin, (ui_in >> 4) & 0xF
        self.s2_bio, self.s1_bio = self.s1_bio, pads

    # ------------------------------------------------------------------
    def _cover(self, w, op, dly, stall, taken, tx_pop, rx_push, div_sync, sc):
        c = self.cov
        sub = {0: (w >> 8) & 15, 1: (w >> 9) & 7, 3: (w >> 4) & 15, 4: (w >> 9) & 7,
               5: (w >> 9) & 7, 6: (w >> 6) & 3, 7: (w >> 9) & 7, 8: (w >> 8) & 15,
               9: (w >> 10) & 3}.get(op, 0)
        c[f"op{op:x}.{sub}"] += 1
        if stall:
            c[f"stall.op{op:x}"] += 1
            if op in (0, 4) and self._rx_full():
                c["stall.rxfull"] += 1
                if self.rx_pending is not None:
                    c["stall.rxfull_pending"] += 1
        if taken:
            c[f"taken.op{op:x}"] += 1
        if tx_pop:
            c[f"txpop.op{op:x}"] += 1
        if rx_push is not None:
            c[f"rxpush.op{op:x}"] += 1
        if div_sync:
            c[f"sync.{div_sync}"] += 1
        if dly:
            c["predelay"] += 1
        if sc:
            c[f"sideset.{sc}"] += 1

    def _execute(self, w, op, class_a, sc, sval, in_vec, tick):
        stall = 0
        taken = 0
        tgt = 0
        div_sync = None
        hclr = 0
        tx_pop = 0
        rx_push = None

        out = self.out
        oe = self.oe
        # side-set
        if class_a and sc:
            for k in range(sc):
                p = (self.side_base + k) & 15
                v = (sval >> k) & 1
                if self.side_pindir:
                    if p < 8:
                        oe = (oe & ~(1 << p)) | (v << p)
                else:
                    out = self._set_out_pin(out, p, v)

        x, y = self.x, self.y
        isr, osr = self.isr, self.osr
        isr_cnt, osr_cnt = self.isr_cnt, self.osr_cnt
        lb = self.lastbit
        txe = len(self.tx) == 0
        rxf = self._rx_full()

        if op == 0:  # CTL
            f = (w >> 8) & 15
            a5, a4 = (w >> 5) & 1, (w >> 4) & 1
            if f == 1:
                self.halted = 1
                stall = 1
            elif f == 2:
                self.irq = 1
            elif f == 3:
                if not a5 or isr_cnt >= self.push_th:
                    if rxf:
                        if a4:
                            stall = 1
                        else:
                            self.rx_ovf = 1
                            isr, isr_cnt = 0, 0
                    else:
                        rx_push = self._isr_byte(isr)
                        isr, isr_cnt = 0, 0
            elif f == 4:
                if not a5 or osr_cnt >= self.pull_th:
                    if txe:
                        if a4:
                            stall = 1
                        else:
                            osr, osr_cnt = self._place_out(x), 0
                    else:
                        tx_pop = 1
                        osr, osr_cnt = self._place_out(self.tx[0]), 0
            elif f == 5:
                div_sync = "half" if a4 else "full"
            elif f == 6:
                hclr = 1
            elif f == 7:
                if a4:
                    isr, isr_cnt = 0, 0
                if a5:
                    osr, osr_cnt = 0, 32
        elif op == 1:  # JMP
            c = (w >> 9) & 7
            tgt = (w >> 4) & 31
            if c == 0:
                taken = 1
            elif c == 1:
                taken = x == 0
            elif c == 2:
                taken = x != 0
                x = (x - 1) & 0xFF
            elif c == 3:
                taken = y == 0
            elif c == 4:
                taken = y != 0
                y = (y - 1) & 0xFF
            elif c == 5:
                taken = x != y
            elif c == 6:
                taken = osr_cnt < self.pull_th
            else:
                taken = lb == 1
        elif op == 2:  # JPIN
            tgt = w & 31
            taken = _bit(in_vec, (w >> 8) & 15) == (w >> 7) & 1
        elif op == 3:  # WAIT
            p = (w >> 8) & 15
            pol, edge = (w >> 7) & 1, (w >> 6) & 1
            cur, prv = _bit(in_vec, p), _bit(self.in_prev, p)
            if edge:
                sat = (cur and not prv) if pol else ((not cur) and prv)
            else:
                sat = cur == pol
            if not sat:
                stall = 1
            elif (w >> 5) & 1:
                div_sync = "half" if (w >> 4) & 1 else "full"
        elif op == 4:  # IN
            if not (w >> 11) & 1:
                n = ((w >> 4) & 7) + 1
                base = (w >> 7) & 15
                d = 0
                for k in range(n):
                    d |= _bit(in_vec, (base + k) & 15) << k
            else:
                n = (w >> 4) & 31 or 32
                s = (w >> 9) & 3
                d = [x, y, 0, osr][s]
            d &= (1 << n) - 1
            if self.in_right:
                sh = d if n == 32 else ((isr >> n) | (d << (32 - n))) & M32
            else:
                sh = d if n == 32 else ((isr << n) | d) & M32
            cn = min(isr_cnt + n, 32)
            if self.autopush and cn >= self.push_th:
                if rxf:
                    stall = 1
                else:
                    rx_push = (sh >> 24) & 0xFF if self.in_right else sh & 0xFF
                    isr, isr_cnt = 0, 0
                    lb = d & 1
            else:
                isr, isr_cnt = sh, cn
                lb = d & 1
        elif op == 5:  # OUT
            pins_mode = not (w >> 11) & 1
            n = ((w >> 4) & 7) + 1 if pins_mode else ((w >> 4) & 31 or 32)
            oe_ = osr
            oc = osr_cnt
            if self.autopull and osr_cnt >= self.pull_th:
                if txe:
                    stall = 1
                else:
                    tx_pop = 1
                    oe_ = self._place_out(self.tx[0])
                    oc = 0
            if not stall:
                if self.out_right:
                    d = oe_ & ((1 << n) - 1)
                    osr = 0 if n == 32 else oe_ >> n
                else:
                    d = oe_ >> (32 - n)
                    osr = 0 if n == 32 else (oe_ << n) & M32
                osr_cnt = min(oc + n, 32)
                lb = d & 1
                d8 = d & 0xFF
                if pins_mode:
                    base = (w >> 7) & 15
                    for k in range(n):
                        out = self._set_out_pin(out, (base + k) & 15, _bit(d8, k))
                else:
                    dst = (w >> 9) & 3
                    if dst == 0:
                        x = d8
                    elif dst == 1:
                        y = d8
                    elif dst == 3:
                        for k in range(min(n, 8)):
                            oe = (oe & ~(1 << k)) | (_bit(d8, k) << k)
        elif op == 6:  # SETP
            p = (w >> 8) & 15
            f = (w >> 6) & 3
            v = (w >> 5) & 1
            if f == 0:
                out = self._set_out_pin(out, p, v)
            elif f == 1:
                if p < 8:
                    oe = (oe & ~(1 << p)) | (v << p)
            elif f == 2:
                out = self._set_out_pin(out, p, 1 - _bit(self.out, p))
            else:
                out = self._set_out_pin(out, p, v)
                if p < 8:
                    oe |= 1 << p
        elif op == 7:  # MOV
            src = (w >> 4) & 7
            v = [x, y, self._isr_byte(isr), self._osr_byte(osr), in_vec & 0xFF,
                 (in_vec >> 8) & 0xFF, 0, lb][src]
            o = (w >> 7) & 3
            r = [v, (~v) & 0xFF, _rev8(v), _par8(v)][o]
            dst = (w >> 9) & 7
            if dst == 0:
                x = r
            elif dst == 1:
                y = r
            elif dst == 2:
                isr, isr_cnt = self._place_in(r), 8
            elif dst == 3:
                osr, osr_cnt = self._place_out(r), 0
            elif dst == 4:
                out = (out & ~0xFF) | r
            elif dst == 5:
                oe = r
            elif dst == 6:
                out = (out & 0xFF) | ((r & 0x7F) << 8)
            else:
                taken, tgt = 1, r & 31
        elif op == 8:  # ALU
            dsty = (w >> 11) & 1
            f = (w >> 8) & 7
            imm = w & 0xFF
            v = y if dsty else x
            if f == 6:
                crc = (y << 8) | x
                if imm & 1:
                    fb = ((crc >> 15) & 1) ^ lb
                    crc = ((crc << 1) & 0xFFFF) ^ (self.crc_poly if fb else 0)
                else:
                    fb = (crc & 1) ^ lb
                    crc = (crc >> 1) ^ (self.crc_poly if fb else 0)
                x, y = crc & 0xFF, crc >> 8
            elif f != 7:
                r = [imm, v & imm, v | imm, v ^ imm, (v - 1) & 0xFF, (v + 1) & 0xFF][f]
                if dsty:
                    y = r
                else:
                    x = r
        elif op == 9:  # DLY
            n = ((x << 8) | y) if (w >> 10) & 1 else (w & 0x3FF)
            ticks = (w >> 11) & 1
            if not self.dl_active:
                if n != 0:
                    if ticks:
                        if not (tick and n == 1):
                            stall = 1
                            self.dl_active = 1
                            self.dl_cnt = n - tick
                    else:
                        stall = 1
                        self.dl_active = 1
                        self.dl_cnt = n
            else:
                if ticks and not tick:
                    stall = 1
                elif self.dl_cnt != 1:
                    stall = 1
                    self.dl_cnt -= 1
        else:  # illegal
            self.err = 1
            self.halted = 1
            stall = 1

        # commit
        self.out, self.oe = out & 0x7FFF, oe & 0xFF
        self.x, self.y = x & 0xFF, y & 0xFF
        self.isr, self.osr = isr & M32, osr & M32
        self.isr_cnt, self.osr_cnt = isr_cnt, osr_cnt
        self.lastbit = lb
        return stall, taken, tgt, div_sync, hclr, tx_pop, rx_push
