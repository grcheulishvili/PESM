"""ISA completeness: every canonical 16-bit word round-trips through the
disassembler and the text assembler, for every side-set count."""
import pytest

from pesm import isa
from pesm.assembler import AsmError, assemble, encode_line


@pytest.mark.parametrize("sc", [0, 1, 2, 3, 4])
def test_all_words_roundtrip(sc):
    for w in range(1 << 16):
        text = isa.disassemble(w, sc)
        assert encode_line(text, sc, {}, {}) == w, (hex(w), text)


def test_every_opcode_and_function_reachable():
    """Each v3 operation has at least one canonical (non-.word) encoding."""
    seen = set()
    for w in range(1 << 16):
        t = isa.disassemble(w)
        if not t.startswith(".word"):
            seen.add(t.split()[0])
    expected = {"nop", "halt", "irq", "push", "pull", "sync", "hclr", "clr", "jmp", "jpin",
                "wait", "in", "out", "set", "dir", "toggle", "drive", "mov", "ldi", "and",
                "or", "xor", "dec", "inc", "crc", "dly", "dlyt", "bgclk", "jpat", "jnpat",
                "jpatx", "jnpatx"}
    assert seen == expected
    # both halves of the 64-word space are reachable from every branch type
    for mn in ("jmp", "jmp x--,", "jpin 3, 1,", "jpat 2,", "jnpatx 9,"):
        for tgt in (0, 31, 32, 63):
            w = encode_line(f"{mn} {tgt}", 0, {}, {})
            assert isa.disassemble(w) == f"{mn} {tgt}"
    # every jmp condition, mov dst/src/op, alu func, wait kind
    texts = {isa.disassemble(w) for w in range(1 << 16)}
    for c in isa.JMP_CONDS:
        if c != "always":
            assert any(t.startswith(f"jmp {c},") for t in texts)
    for d in isa.MOV_DST:
        for s in isa.MOV_SRC:
            for o in ("", "~", "rev ", "par "):
                assert f"mov {d}, {o}{s}" in texts
    for k in ("high", "low", "rise", "fall"):
        assert any(t.startswith(f"wait {k}") for t in texts)


def test_reserved_encodings_are_words():
    for w in (0xC000, 0xF123, 0x0840, 0x0900, 0x0700, 0x2040, 0x3010, 0x6010, 0x68A0,
              0x8700, 0x8401, 0x8E00, 0x8602, 0xA401):
        assert isa.disassemble(w).startswith(".word"), hex(w)


def test_tail_limits():
    assert isa.tail(1, 7, 1) == 0b1111
    with pytest.raises(isa.IsaError):
        isa.tail(None, 8, 1)
    with pytest.raises(isa.IsaError):
        isa.tail(2, 0, 1)
    with pytest.raises(isa.IsaError):
        isa.tail(1, 0, 0)


@pytest.mark.parametrize("src,err", [
    ("jmp nowhere", "bad number"),
    ("jmp 64", "jump target"),
    ("jpat 0, 64", "jump target"),
    ("jpat 16, 0", "pin"),
    ("jpat 0, 1 [1]", "no side-set"),
    ("bgclk maybe", "bgclk"),
    (".bgclk pin=15", "output pin"),
    (".bgclk div=256", "div"),
    (".cfg 20 0", "address"),
    ("ldi z, 1", "x|y"),
    ("out pins, 0, 9", "pin count"),
    ("in x, 33", "bit count"),
    ("jpin 0, 2, 0", "level"),
    ("crc mid", "crc"),
    ("set 16, 1", "pin"),
    ("ldi x, 1 [1]", "no side-set"),
    ("dly 1024", "immediate"),
])
def test_assembler_errors(src, err):
    with pytest.raises(AsmError, match=err):
        assemble(src)


def test_program_too_long():
    assert assemble("\n".join(["nop"] * 64)).length == 64
    with pytest.raises(AsmError, match="64"):
        assemble("\n".join(["nop"] * 65))


def test_v3_encodings_are_pinned():
    """Bit-exact encodings of the v3 additions (docs/ISA.md section 2)."""
    assert isa.enc_jmp("always", 5) == 0x1050
    assert isa.enc_jmp("always", 37) == 0x9050            # opcode 9 = target + 32
    assert isa.enc_jmp("x--", 63, 0xF) == 0x95FF
    assert isa.enc_jpin(9, 1, 45) == 0x29AD
    assert isa.enc_jpat(33) == 0xB021
    assert isa.enc_jpat(1, base=8, use_x=True, invert=True) == 0xBE01
    assert isa.enc_bgclk(True) == 0x0810
    assert isa.enc_bgclk(False, reset=True, t=3) == 0x0823
    assert isa.enc_dly(5) == 0xA005 and isa.enc_dly(5, ticks=True) == 0xA805
    assert isa.pin_index("bgclk") == 15


def test_directives_pattern_and_bgclk():
    img = assemble(""".tick_hz 1000000
.pattern 0x0f 0x05
.bgclk pin=tout2 hz=125000 auto idle=1
nop
""")
    c = img.config
    assert (c.pat_mask, c.pat_val) == (0x0F, 0x05)
    assert (c.bg_pin, c.bg_en, c.bg_auto, c.bg_idle, c.bg_div) == (10, True, True, 1, 3)
    assert abs(c.bgclk_hz() - 125000) < 1
    assert img.cfg[18] == 0x7A and img.cfg[19] == 3
    assert isa.Config.from_bytes(img.cfg).to_bytes() == img.cfg
