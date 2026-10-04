import glob
import os

import pytest

from conftest import REPO
from pesm import assemble, assemble_file, isa
from pesm.builder import DslError, PESMProgram
from pesm.run_pipeline import compile_source

EXAMPLES = sorted(glob.glob(os.path.join(REPO, "sw", "examples", "*.py")))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: os.path.basename(p))
def test_dsl_example_matches_firmware(path):
    """Each DSL example compiles to exactly the words + config of the
    simulation-verified firmware/*.pasm program."""
    name = os.path.splitext(os.path.basename(path))[0]
    a = assemble_file(os.path.join(REPO, "firmware", f"{name}.pasm"))
    b = compile_source(path)
    assert b.words == a.words
    assert b.cfg == a.cfg


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: os.path.basename(p))
def test_dsl_to_asm_roundtrip(path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("m", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    p = m.program
    img = p.compile()
    back = assemble(p.to_asm())
    assert back.words == img.words and back.cfg == img.cfg


def test_every_primitive_compiles():
    p = PESMProgram("all").sideset(1, base="tout1")
    (p.label("top").nop().halt().irq().hclr().push().push(iffull=True, block=False)
      .pull().pull(ifempty=True, block=False).sync_grid().sync_grid(half=True)
      .clear().clear(isr=False, osr=True).jmp("top").jmp_if_zero("x", "top")
      .jmp_dec("y", "top").jmp_if_ne("top").jmp_if_osr_not_empty("top")
      .jmp_if_lastbit("top").jmp_if_pin("tin0", 1, "top").jmp_reg("x")
      .wait_pin("bio0", "rise", sync="half", side=1, delay=3).wait_cycles(10).wait_ticks(5)
      .set_pin("tout0", 1).dir_pin("bio2").drive_pin("bio3", 0).toggle_pin("bio4")
      .shift_out("tout2", 3).shift_in("osr", 32).mov("x", "pins", "par")
      .bgclk_start().bgclk_start(reset=True).bgclk_stop().bgclk_stop(reset=True, side=1)
      .jmp_if_pattern("top").jmp_if_pattern("top", base="tin0", expect="x", match=False)
      .wait_pattern().wait_pin("bgclk", "rise"))
    assert len(p) == 38
    p.mov("pc", "x").inc("x")
    img = p.compile()
    assert assemble(p.to_asm()).words == img.words
    assert assemble(p.to_asm()).cfg == img.cfg
    while len(p) < 64:
        p.nop()
    p.compile()
    with pytest.raises(DslError, match="max 64"):
        p.nop().compile()


def test_alu_and_crc_semantics_text():
    p = PESMProgram().ldi("x", 0x80).and_("x", 0x0F).or_("y", 1).xor("y", 0xFF)
    p.inc("x").dec("y").crc_step(msb=True)
    lst = p.listing()
    for frag in ("ldi x, 0x80", "and x, 0x0f", "or y, 0x01", "xor y, 0xff", "inc x",
                 "dec y", "crc msb"):
        assert frag in lst


def test_wait_cycles_encodings():
    assert PESMProgram().wait_cycles(1).compile().words[0] == isa.enc_nop()
    assert PESMProgram().wait_cycles(1024).compile().words[0] == isa.enc_dly(1023)
    img = PESMProgram().wait_cycles(5000, use_xy=True).compile()
    k = 5000 - 3
    assert img.words[:3] == [isa.enc_alu("x", "ldi", k >> 8), isa.enc_alu("y", "ldi", k & 0xFF),
                             isa.enc_dly(None, False, True)]
    with pytest.raises(DslError):
        PESMProgram().wait_cycles(1025)


def test_errors():
    with pytest.raises(DslError, match="undefined label"):
        PESMProgram().jmp("nowhere").compile()
    with pytest.raises(DslError, match="duplicate"):
        PESMProgram().label("a").label("a")
    with pytest.raises(isa.IsaError, match="delay"):
        PESMProgram().sideset(2).nop(delay=4).compile()
    p = PESMProgram().shift(out="right")
    with pytest.raises(DslError, match="conflict"):
        p.macro_spi_transfer("tout1", "tout2", "tin1")


def test_macro_config_side_effects():
    p = PESMProgram().clock(tick_hz=400_000)
    p.macro_i2c_start("bio0", "bio1").macro_i2c_write_byte("bio0", "bio1")
    p.macro_i2c_stop("bio0", "bio1")
    c = p.compile().config
    assert c.od_mask == 0x03 and c.init_bio_oe & 3 == 3 and c.init_bio_out & 3 == 3
    assert c.out_right is False


def test_pattern_and_background_clock_config():
    p = PESMProgram().clock(tick_hz=1e6)
    p.pattern_pins({"bio2": 1, "bio3": 0, "bio5": 1}, base="bio2")
    p.background_clock(pin="tout3", hz=50e3, auto=False, idle=1)
    c = p.compile().config
    assert (c.pat_mask, c.pat_val) == (0b1011, 0b1001)
    assert (c.bg_en, c.bg_pin, c.bg_div, c.bg_auto, c.bg_idle) == (True, 11, 9, False, 1)
    assert abs(c.bgclk_hz() - 50e3) < 1
    t = PESMProgram().clock(divider=8).background_clock(div=0)       # internal timebase
    assert not t.compile().config.bg_en and t.compile().config.bg_auto
    with pytest.raises(DslError, match="window"):
        PESMProgram().pattern_pins({"bio0": 1}, base="bio4").pattern_pins({"tin3": 1}, base="bio0")
    with pytest.raises(DslError, match="exactly one"):
        PESMProgram().background_clock(pin="tout0")
    with pytest.raises(DslError, match="out of range"):
        PESMProgram().clock(divider=1).background_clock(hz=10)
    with pytest.raises(DslError, match="output pin"):
        PESMProgram().background_clock(pin=15, div=1)
    with pytest.raises(DslError, match="conflict"):
        PESMProgram().pattern(0x0F, 1).pattern(0x0F, 2)


def test_usb_ls_token_macro_matches_packet_encoder():
    """The macro's pin writes, replayed, are exactly the NRZI/stuffed line
    states of the token (independent encoder + decoder in pesm.usb)."""
    from pesm import usb
    for pid, addr, endp in (("setup", 0x15, 0xE), ("in", 0x7F, 0xF), ("out", 0, 0),
                            ("setup", 0x00, 0x1), ("in", 0x55, 0xA)):
        p = PESMProgram("tok").clock(tick_hz=1.5e6)
        p.macro_usb_ls_token(pid, addr, endp, dp="bio0", dm="bio1").halt()
        img = p.compile()
        states, level = [], "J"
        for w in img.words[2:len(p) - 1]:
            txt = isa.disassemble(w)
            delay = int(txt.split("[")[1].rstrip("]")) if "[" in txt else 0
            # the previous level lasts until this instruction's tick (delay ticks
            # after the previous write); one list entry per bit time
            states += [level] * (delay - 1)
            if txt.startswith("mov pins"):
                level = {"x": "J", "y": "K", "null": "0"}[txt.split(",")[1].split()[0]]
            states.append(level)
        got = states[states.index("K"):]               # SYNC starts with the first K
        exp = usb.line_states(usb.token_packet(pid, addr, endp))
        assert got[:len(exp)] == exp, (pid, addr, endp)
        assert usb.decode_line(got) == usb.token_packet(pid, addr, endp)
        assert 2 + 16 + 1 <= len(p) - 1 <= 2 + 28 + 1
        c = img.config
        assert c.init_bio_oe & 3 == 3 and c.init_bio_out & 3 == 2      # idle J: D- high


def test_usb_helpers():
    from pesm import usb
    assert usb.crc16(b"123456789") == 0xB4C8
    assert usb.token_packet("setup", 0x15, 0xE) == [0x80, 0x2D, 0x15, 0xEF]
    # USB 2.0 spec CRC5 example (addr 0x15, endp 0xE): 0b11101 in transmit order
    assert [(usb.crc5(0x15 | (0xE << 7)) >> i) & 1 for i in range(5)] == [1, 0, 1, 1, 1]
    assert usb.data_packet("data0", [])[-2:] == [0x00, 0x00]
    pkt = usb.data_packet("data1", [0xFF] * 4)                         # forces bit stuffing
    assert usb.decode_line(usb.line_states(pkt)) == pkt
    assert len(usb.stuff([1] * 12)) == 14
    with pytest.raises(ValueError):
        usb.decode_line(["K", "J", "K", "J", "K", "J", "K"] + ["K"] * 8)
