"""Every Python block in docs/DSL_GUIDE.md runs, and every program in it compiles."""
import os
import re

import pytest

from conftest import REPO
from pesm.builder import PESMProgram

GUIDE = open(os.path.join(REPO, "docs", "DSL_GUIDE.md")).read()
# blocks that need real hardware are excluded
BLOCKS = [b for b in re.findall(r"```python\n(.*?)```", GUIDE, re.S) if "FtdiTransport" not in b]


def test_guide_has_blocks():
    assert len(BLOCKS) >= 14


@pytest.mark.parametrize("i", range(len(BLOCKS)))
def test_guide_block_runs(i, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                        # p.save() writes relative paths
    code = re.sub(r"^ {3}", "", BLOCKS[i], flags=re.M)
    # `p` / `img`: the program the "Compiling and output" block refers to
    p = PESMProgram("uart").clock(tick_hz=115200).macro_uart_tx("tout0")
    ns = {"PESMProgram": PESMProgram, "p": p, "img": p.compile()}
    exec(code, ns)
    for v in ns.values():
        if isinstance(v, PESMProgram):
            img = v.compile()
            assert 0 < img.length <= 64


def test_guide_documents_every_public_builder_method():
    """The API tables must not silently fall behind the code."""
    public = [n for n in dir(PESMProgram) if not n.startswith("_") and callable(getattr(PESMProgram, n))]
    missing = [n for n in public if n not in GUIDE]
    assert not missing, f"undocumented PESMProgram methods: {missing}"
