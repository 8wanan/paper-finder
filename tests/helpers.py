"""测试辅助：临时目录与清理。

两个都是**实测踩出来的环境坑**，写在这里免得后人重复踩：

1. **不要用 `tempfile.mkdtemp()`**。在本机沙箱下，mkdtemp 创建的目录
   完全不可写 —— 连 `open(..., "w")` 都 PermissionError，SQLite 更是
   `unable to open database file`。而 `os.makedirs()` 创建的目录正常可写。
   两者权限位都是 0777，所以不是权限位问题。结论：用 makedirs 自己拼唯一名字。

2. **不要用系统临时目录**（`%LOCALAPPDATA%\\Temp`）。沙箱可能不允许在那里
   创建 SQLite 文件，一坏就是几十个测试一起报错，把真实失败淹没。
   所以测试用仓库内的 `tests/.tmp/`（已在 .gitignore 里）。

3. **不要依赖 `TemporaryDirectory` 的自动清理**。它的退出终结器在 Windows 上
   会因为 SQLite 的 -wal/-shm 文件而抛权限错误。这里自己用 `ignore_errors=True` 清。
"""

import itertools
import os
import shutil

_TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp")
_counter = itertools.count(1)


def temp_dir(prefix: str = "pf-") -> str:
    """在仓库内创建一个**可写**的临时目录，返回路径。"""
    os.makedirs(_TMP_ROOT, exist_ok=True)
    for _ in range(200):
        candidate = os.path.join(_TMP_ROOT, f"{prefix}{os.getpid()}-{next(_counter)}")
        try:
            os.makedirs(candidate, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError(f"无法在 {_TMP_ROOT} 下创建唯一临时目录")


def cleanup(path: str) -> None:
    """尽力清理，绝不因为清理失败而让测试变红。"""
    shutil.rmtree(path, ignore_errors=True)
