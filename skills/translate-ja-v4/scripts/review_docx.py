#!/usr/bin/env python3
"""英日PDFレビューCLIを公開pathから起動する。"""

from __future__ import annotations

import sys
from time import perf_counter

from translate_ja_v4.io import LOGGER
from translate_ja_v4.review_docx import main

if __name__ == "__main__":
    started_at = perf_counter()
    exit_code = main()
    LOGGER.debug("Elapsed time %.3f seconds", perf_counter() - started_at)
    sys.exit(exit_code)
