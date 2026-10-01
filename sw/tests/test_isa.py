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
    """Each v2 operation has at least one canonical (non-.word) encoding."""
    seen = set()
    for w in range(1 << 16):
        t = isa.disassemble(w)
        if not t.startswith(".word"):
            seen.add(t.split()[0])
    expected = {"nop", "halt", "irq", "push", "pull", "sync", "hclr", "clr", "jmp", "jpin",
                "wait", "in", "out", "set", "dir", "toggle", "drive", "mov", "ldi", "and",
                "or", "xor", "dec", "inc", "crc", "dly", "dlyt"}
    assert seen == expected
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
    for w in (0xA000, 0xF123, 0x0810, 0x0700, 0x2060, 0x3010, 0x6010, 0x68A0,
              0x8700, 0x8401, 0x8E00, 0x8602, 0x9401):
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
    ("jmp 32", "jump target"),
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
    with pytest.raises(AsmError, match="32"):
        assemble("\n".join(["nop"] * 33))
