"""数据源适配器。

每个适配器都遵守同一契约：
    fetch(query, limit, ...) -> list[dict]   # 统一的 Paper 结构
并且**必须能独立失败**：某个源挂了不能拖垮整轮运行。

默认启用哪些源，是成本与召回之间的取舍（依据 DESIGN.md §0.1 实测）：
    arxiv      —— 永远启用。免费、无需 key、有摘要，是唯一可靠的源。
    openalex   —— 有 OPENALEX_API_KEY 时启用。有摘要、引用数、支持游标翻页（需 key）。
    crossref   —— **默认关闭**。可用但大量记录无摘要，且会显著拖慢一轮运行。
                  需要补 venue/DOI 时用 `--sources arxiv,crossref` 显式打开。

启用 crossref 的代价：每个查询多约 1.2 秒限速 + 一次分页请求。
"""

from .arxiv import ArxivSource
from .crossref import CrossrefSource
from .openalex import OpenAlexSource

__all__ = [
    "ArxivSource",
    "OpenAlexSource",
    "CrossrefSource",
    "build_sources",
    "DEFAULT_SOURCE_NAMES",
]

DEFAULT_SOURCE_NAMES = ("arxiv",)

_REGISTRY = {
    "arxiv": ArxivSource,
    "openalex": OpenAlexSource,
    "crossref": CrossrefSource,
}


def build_sources(env: dict, client, names=None, log=None):
    """按名字构建数据源列表。

    返回 (sources, skipped)。skipped 里是被跳过的源及原因，doctor 会显示出来，
    避免"以为在检索其实没检索"的静默失败。
    """
    log = log or (lambda msg: None)
    requested = list(names) if names else list(DEFAULT_SOURCE_NAMES)
    sources, skipped = [], []

    for name in requested:
        name = name.strip().lower()
        if not name:
            continue
        if name not in _REGISTRY:
            skipped.append((name, "未知的数据源名"))
            continue
        if name == "openalex":
            api_key = (env.get("OPENALEX_API_KEY") or "").strip()
            if not api_key:
                skipped.append(
                    (
                        "openalex",
                        "未配置 OPENALEX_API_KEY（无 key 时 OpenAlex 返回 429，"
                        "每日预算被同网络所有人共享）",
                    )
                )
                continue
            sources.append(OpenAlexSource(client, api_key=api_key, log=log))
            continue
        sources.append(_REGISTRY[name](client, log=log))

    return sources, skipped
