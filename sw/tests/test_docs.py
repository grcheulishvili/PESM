"""Every DSL code block in docs/DSL_GUIDE.md compiles."""
import os
import re

import pytest

from conftest import REPO
from pesm.builder import PESMProgram

GUIDE = open(os.path.join(REPO, "docs", "DSL_GUIDE.md")).read()
BLOCKS = [b for b in re.findall(r"```python\n(.*?)```", GUIDE, re.S)
          if "PESMProgram" in b and "Transport" not in b]


@pytest.mark.parametrize("i", range(len(BLOCKS)))
def test_guide_block_compiles(i):
    code = re.sub(r"^ {3}", "", BLOCKS[i], flags=re.M)
    ns = {"PESMProgram": PESMProgram}
    exec(code, ns)
    progs = [v for v in ns.values() if isinstance(v, PESMProgram)]
    assert progs
    for p in progs:
        img = p.compile()
        assert img.length <= 32
