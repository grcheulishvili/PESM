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
      .shift_out("tout2", 3).shift_in("osr", 32).mov("x", "pins", "par"))
    assert len(p) == 30
    p.mov("pc", "x").inc("x")
    img = p.compile()
    assert assemble(p.to_asm()).words == img.words
    with pytest.raises(DslError, match="max 32"):
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
