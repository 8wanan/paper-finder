#!/usr/bin/env python3
"""paper-finder 命令行入口。

真正的实现放在 `papers/cli.py`。这里保持极薄，是为了让
`python papers.py <子命令>` 这条最自然的使用方式成立，
同时避免脚本 `papers.py` 与包 `papers/` 同名带来的导入歧义。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from papers.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
