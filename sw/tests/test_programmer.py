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
    assert hp.frame_write_imem(63, [0xBEEF]) == [0x3F, 0xBE, 0xEF]
    assert hp.frame_read_imem(31, 1) == [0x5F, 0, 0]
    assert hp.frame_read_imem(63, 1) == [0x7F, 0, 0]
    assert hp.frame_write_cfg(9, [5]) == [0x89, 5]
    assert hp.frame_write_cfg(19, [5]) == [0x93, 5]
    assert hp.frame_read_cfg(18, 2) == [0xB2, 0, 0]
    assert hp.frame_write_tx([1, 2]) == [0xC0, 1, 2]
    assert hp.frame_read_rx(2) == [0xC8, 0, 0]
    assert hp.frame_status() == [0xD0, 0, 0, 0, 0, 0, 0]
    assert hp.frame_control(hp.CTRL_FLUSH_TX | hp.CTRL_HFLAG_SET) == [0xE5]
    st = hp.Status.decode([0, 0b10100001, 0x35, 47, 1, 2, 0x30])
    assert st.running and st.irq and st.hflag and st.tx_level == 3 and st.rx_level == 5
    assert st.pc == 47 and st.chip_id == isa.CHIP_ID


def test_load_verify_and_patch():
    t = EmulatorTransport()
    p = Programmer(t)
    img = compile_source(os.path.join(REPO, "firmware", "uart_tx.pasm"))
    p.load(img)
    assert p.read_imem() == img.words
    p.patch(31, [0xBEEF])
    assert p.read_imem(31, 1) == [0xBEEF]
    p.patch(63, [0x1234, 0x5678])                # wraps to address 0
    assert p.read_imem(63, 1) == [0x1234] and p.read_imem(0, 1) == [0x5678]
    p.patch(0, [img.words[0]])
    assert p.identify() == isa.CHIP_ID
    assert p.read_config() == img.cfg and len(img.cfg) == isa.NUM_CFG
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


def test_monitor_reports_status_changes_and_drains_rx():
    """Runtime monitoring: echo program, bytes pushed while the monitor polls."""
    p = PESMProgram("echo").label("l").pull().shift_out("x", 8).mov("isr", "x").push().jmp("l")
    t = EmulatorTransport()
    prog = Programmer(t)
    prog.load(p.compile())
    prog.run()
    prog.write_tx(b"PESM")
    seen, chunks = [], []
    m = prog.monitor(duration=0.0005, interval=0.00005, on_status=seen.append,
                     on_rx=chunks.append)
    assert bytes(m.rx) == b"PESM" and sum(chunks, []) == m.rx
    assert m.polls >= 2 and m.final.running and m.final.rx_level == 0
    assert seen == m.statuses and len(seen) >= 1
    # until_halt ends early and picks up bytes pushed just before the halt
    q = PESMProgram("two").ldi("x", 0x5A).mov("isr", "x").push().push().halt()
    prog.load(q.compile())
    prog.run()
    m = prog.monitor(duration=1.0, interval=0.0001, until_halt=True)
    assert m.final.halted and m.rx == [0x5A, 0x00] and m.polls < 20
    # sticky error flags are reported, not cleared
    prog.load(PESMProgram("bad").raw(0xF000).compile())
    prog.run()
    m = prog.monitor(max_polls=3, interval=0.0001)
    assert m.final.err and m.final.halted and prog.status().err
    with pytest.raises(ProgrammerError, match="needs"):
        prog.monitor()


def test_pipeline_monitor_save_and_no_flash(tmp_path, capsys):
    from pesm.run_pipeline import main as pipe_main
    src = os.path.join(REPO, "sw", "examples", "crc16_usb.py")
    hist = []
    res = run_pipeline(src, EmulatorTransport(), tx=b"123456789", hflag=True, wait_halt=True,
                       monitor=0.01, interval=0.0002, on_status=hist.append,
                       save=str(tmp_path / "crc"))
    assert res.rx == [0xC8, 0xB4] and res.status.halted and hist == res.history
    assert sorted(os.path.basename(f) for f in res.files) == [
        "crc.asm", "crc.bin", "crc.json", "crc.lst", "crc.mem", "crc.py", "crc_cfg.mem"]
    # compile-only pipeline: no transport needed
    res = run_pipeline(src, None, save=str(tmp_path / "only"))
    assert res.status is None and len(res.files) == 7
    # the saved .bin and .json flash and run like the source
    for ext in (".bin", ".json", ".asm"):
        r = run_pipeline(str(tmp_path / "crc") + ext, EmulatorTransport(), tx=b"123456789",
                         hflag=True, wait_halt=True, rx=2)
        assert r.rx == [0xC8, 0xB4], ext
    # CLI
    assert pipe_main([src, "--backend", "emulator", "--tx", "123456789", "--hflag",
                      "--wait-halt", "--monitor", "0.01", "--interval", "0.0002"]) == 0
    out = capsys.readouterr().out
    assert "rx: c8 b4" in out and "halted" in out
    assert pipe_main([src, "--no-flash", "--save", str(tmp_path / "cli")]) == 0
    assert (tmp_path / "cli.bin").exists()


def test_programmer_cli_monitor(capsys):
    from pesm.programmer import main as prog_main
    src = os.path.join(REPO, "firmware", "frac_div.pasm")
    assert prog_main([src, "--backend", "emulator", "--monitor", "0.0002",
                      "--interval", "0.0001"]) == 0
    out = capsys.readouterr().out
    assert "verified" in out and "running" in out


def test_identify_rejects_a_wrong_chip():
    class V2Chip(EmulatorTransport):
        def xfer(self, mosi):
            r = super().xfer(mosi)
            if mosi and mosi[0] == hp.CMD_READ_STAT and len(r) > 6:
                r[6] = 0x00
            return r
    with pytest.raises(ProgrammerError, match="chip ID"):
        Programmer(V2Chip()).load(isa.Image())
    Programmer(V2Chip()).load(isa.Image(), verify=False)      # explicit opt-out


def test_emulator_background_clock_and_pattern():
    """New v3 features end to end on the emulator (model + host port)."""
    p = PESMProgram("bg").clock(divider=4)
    p.background_clock(pin="tout2", div=2, auto=True)
    p.pattern_pins({"tin0": 1, "tin1": 0}, base="tin0")
    p.label("w").jmp_if_pattern("w", base="tin0", match=False).irq().halt()
    t = EmulatorTransport()
    wave = []
    t.emu.pin_hook = lambda e: wave.append((e.model.out_pad() >> 10) & 1)
    pr = Programmer(t)
    pr.load(p.compile())
    wave.clear()
    pr.run()
    t.delay(4e-6)                                   # 200 clk
    edges = [i for i in range(1, len(wave)) if wave[i] != wave[i - 1]]
    assert len(edges) > 8 and all(b - a == 12 for a, b in zip(edges, edges[1:]))
    assert not pr.status().irq
    t.emu.tin = 0b0001                              # TIN0 = 1, TIN1 = 0: pattern matches
    t.delay(1e-6)
    st = pr.status()
    assert st.irq and st.halted
    n = len(wave)
    t.delay(2e-6)                                   # the clock keeps running after HALT
    assert any(wave[i] != wave[i - 1] for i in range(n, len(wave)))
