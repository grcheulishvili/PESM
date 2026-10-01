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
    assert len(b) == 80
    assert b[:2] == bytes([img.words[0] >> 8, img.words[0] & 0xFF])
    back = isa.Image.from_bin(b)
    assert back.words == img.words and back.cfg == img.cfg


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
