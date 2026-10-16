"""环境变量加载：真实的系统环境优先，`.env` 只做补齐。

为什么系统环境优先：CI（GitHub Actions）里用 Secrets 注入变量，
本地用 .env 图方便。如果 .env 覆盖真实环境，CI 上就会出现
"本地文件把线上凭证盖掉"的诡异问题。

安全约定（DESIGN.md §10 风险 9）：`.env` 必须留在 .gitignore 里。
本模块只读取，从不写出。
"""

from __future__ import annotations

import os

__all__ = ["load_env", "MASK", "mask_secret"]

MASK = "***"


def mask_secret(value: str | None) -> str:
    """给日志/doctor 用的脱敏显示：只留长度信息，不泄露内容。"""
    if not value:
        return "(未设置)"
    value = str(value)
    if len(value) <= 8:
        return f"{MASK}（长度 {len(value)}）"
    return f"{value[:3]}{MASK}{value[-2:]}（长度 {len(value)}）"


def load_env(root: str = ".") -> dict:
    """返回合并后的环境字典。"""
    env = dict(os.environ)
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        return env
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                value = value.strip().strip('"').strip("'")
                # 真实环境优先：只在缺失时用 .env 补齐
                if key and not env.get(key):
                    env[key] = value
    except OSError:
        return env
    return env
