"""Programmer + pipeline against the pure-Python chip emulator."""
import os

import pytest

from conftest import REPO
from pesm import hostproto as hp
from pesm import isa
from pesm.builder import PESMProgram
from pesm.programmer import (EmulatorTransport, Programmer, ProgrammerError, Transport,
                             VerifyError)
from pesm.run_pipeline import compile_source, run_pipeline


def crc16_usb(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def test_frames():
    assert hp.frame_write_imem(3, [0x1234]) == [0x03, 0x12, 0x34]
    assert hp.frame_read_imem(31, 1) == [0x3F, 0, 0]
    assert hp.frame_write_cfg(9, [5]) == [0x49, 5]
    assert hp.frame_control(hp.CTRL_FLUSH_TX | hp.CTRL_HFLAG_SET) == [0xE5]
    st = hp.Status.decode([0, 0b10100001, 0x35, 7, 1, 2])
    assert st.running and st.irq and st.hflag and st.tx_level == 3 and st.rx_level == 5


def test_load_verify_and_patch():
    t = EmulatorTransport()
    p = Programmer(t)
    img = compile_source(os.path.join(REPO, "firmware", "uart_tx.pasm"))
    p.load(img)
    assert p.read_imem() == img.words
    p.patch(31, [0xBEEF])
    assert p.read_imem(31, 1) == [0xBEEF]
    p.run()
    assert p.status().running
    p.write_imem([0x1111], 0)                  # write-protected while running
    assert p.status().err
    assert p.read_imem(0, 1) == [img.words[0]]


class FlakyTransport(EmulatorTransport):
    """Corrupts the first imem write burst to exercise verify + retry."""
    def __init__(self, fail_times):
        super().__init__()
        self.fail_times = fail_times

    def xfer(self, mosi):
        mosi = list(mosi)
        if mosi and mosi[0] == hp.CMD_WRITE_IMEM and self.fail_times:
            self.fail_times -= 1
            mosi[2] ^= 0xFF
        return super().xfer(mosi)


def test_verify_retry_and_failure():
    img = compile_source(os.path.join(REPO, "firmware", "crc16_usb.pasm"))
    Programmer(FlakyTransport(1)).load(img, retries=1)          # recovers
    with pytest.raises(VerifyError):
        Programmer(FlakyTransport(5)).load(img, retries=1)


def test_crc16_pipeline_on_emulator():
    msg = b"123456789"
    res = run_pipeline(os.path.join(REPO, "sw", "examples", "crc16_usb.py"),
                       EmulatorTransport(), tx=msg, hflag=True, wait_halt=True, rx=2)
    assert (res.rx[1] << 8) | res.rx[0] == crc16_usb(msg) == 0xB4C8
    assert res.status.halted and not res.status.hflag


def test_tx_flow_control_and_timeout():
    # echo: TX -> X -> RX; 20 bytes through 8-deep FIFOs with RX drained by the host
    p = PESMProgram("echo").label("l").pull().shift_out("x", 8).mov("isr", "x").push().jmp("l")
    prog = Programmer(EmulatorTransport())
    prog.load(p.compile())
    prog.run()
    data = list(range(20))
    got = []
    for i in range(0, 20, 4):
        prog.write_tx(data[i:i + 4])
        got += prog.read_rx(4)
    assert got == data
    # core halted -> TX FIFO never drains -> write_tx must time out, not overflow
    prog.load(PESMProgram("h").halt().compile())
    prog.run()
    with pytest.raises(ProgrammerError, match="TX FIFO"):
        prog.write_tx(list(range(12)), timeout=0.001)
    assert not prog.status().tx_ovf


def test_uart_tx_emulated_waveform():
    """End to end on the emulator: DSL macro -> image -> load -> run; sample TOUT0."""
    prog = PESMProgram("u").clock(tick_hz=1_000_000).macro_uart_tx("tout0")
    t = EmulatorTransport()
    samples = []
    t.emu.pin_hook = lambda e: samples.append((e.model.out >> 8) & 1)
    pr = Programmer(t)
    pr.load(prog.compile())
    samples.clear()
    pr.run()
    pr.write_tx([0xA5])
    t.delay(20e-6)
    # find the start bit and decode at mid-bit (50 clk per bit)
    s0 = samples.index(0)
    bits = [samples[s0 + 25 + 50 * k] for k in range(10)]
    assert bits[0] == 0 and bits[9] == 1
    assert sum(b << i for i, b in enumerate(bits[1:9])) == 0xA5
