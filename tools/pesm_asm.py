#!/usr/bin/env python3
# Copyright (c) 2026 PESM contributors
# SPDX-License-Identifier: Apache-2.0
"""Compatibility wrapper: the assembler lives in sw/pesm/assembler.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sw"))
from pesm.assembler import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
