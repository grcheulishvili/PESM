import json
import os

from conftest import REPO
from pesm import isa
from pesm.assembler import assemble_file, main as asm_main


def _img():
    return assemble_file(os.path.join(REPO, "firmware", "spi_master.pasm"))


def test_bin_roundtrip(tmp_path):
    img = _img()
    b = img.to_bin()
    assert len(b) == isa.BIN_LEN == 8 + 2 * 64 + 20
    assert b[:8] == b"PESM" + bytes([3, 64, 20, 0])
    assert b[8:10] == bytes([img.words[0] >> 8, img.words[0] & 0xFF])
    assert b[8:136] == img.imem_bytes() and b[136:] == img.cfg_bytes()
    back = isa.Image.from_bin(b)
    assert back.words == img.words and back.cfg == img.cfg
    import pytest
    with pytest.raises(isa.IsaError, match="not a PESM"):
        isa.Image.from_bin(b[8:])                      # headerless / v2 image
    with pytest.raises(isa.IsaError, match="ISA v2"):
        isa.Image.from_bin(b[:4] + bytes([2]) + b[5:])


def test_mem_and_py_and_json(tmp_path):
    img = _img()
    mem = [l for l in img.to_mem().splitlines() if l and not l.startswith(("//", "@"))]
    assert [int(x, 16) for x in mem] == img.words
    ns = {}
    exec(img.to_py(), ns)
    assert ns["SPI_MASTER_IMEM"] == img.words and ns["SPI_MASTER_CFG"] == img.cfg
    d = json.loads(img.to_json())
    assert d["imem"] == img.words and d["cfg"] == img.cfg
    assert d["cfg_fields"]["side_count"] == 1
    back = isa.Image.from_json(img.to_json())
    assert back.words == img.words and back.cfg == img.cfg and back.labels == img.labels
    assert img.to_lists() == (img.words, img.cfg)
    cfg_mem = [l for l in img.to_cfg_mem().splitlines() if l and not l.startswith(("//", "@"))]
    assert [int(x, 16) for x in cfg_mem] == img.cfg


def test_asm_export_roundtrip():
    img = _img()
    from pesm.assembler import assemble
    back = assemble(img.to_asm())
    assert back.words == img.words and back.cfg == img.cfg


def test_cli_outputs(tmp_path):
    src = os.path.join(REPO, "firmware", "uart_tx.pasm")
    out = tmp_path / "u.bin"
    assert asm_main([src, "-f", "bin", "-o", str(out)]) == 0
    assert isa.Image.from_bin(out.read_bytes()).words == assemble_file(src).words
    out = tmp_path / "u.mem"
    assert asm_main([src, "-f", "mem", "-o", str(out)]) == 0
    assert (tmp_path / "u_cfg.mem").exists()


def test_cli_all_formats(tmp_path):
    src = os.path.join(REPO, "firmware", "uart_tx.pasm")
    base = tmp_path / "out" / "uart"
    assert asm_main([src, "--all", "-o", str(base)]) == 0
    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert names == ["uart.asm", "uart.bin", "uart.json", "uart.lst", "uart.mem", "uart.py",
                     "uart_cfg.mem"]
    ref = assemble_file(src)
    assert isa.Image.from_bin((tmp_path / "out" / "uart.bin").read_bytes()).words == ref.words
    from pesm.assembler import assemble_file as af
    assert af(str(tmp_path / "out" / "uart.asm")).words == ref.words


def test_builder_direct_outputs(tmp_path):
    from pesm.builder import PESMProgram
    p = PESMProgram("blink").clock(divider=4).toggle_pin("tout0", delay=1).jmp(0)
    assert p.to_bin() == p.compile().to_bin()
    assert p.to_lists()[0][:2] == p.compile().words[:2]
    assert "@0" in p.to_mem()
    files = p.save(str(tmp_path / "blink"))
    assert len(files) == 7 and all(os.path.exists(f) for f in files)
