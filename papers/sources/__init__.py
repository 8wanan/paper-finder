"""数据源适配器。

每个适配器都遵守同一契约：
    fetch(query, limit, ...) -> list[dict]   # 统一的 Paper 结构
并且**必须能独立失败**：某个源挂了不能拖垮整轮运行。

默认启用哪些源，是成本与召回之间的取舍（依据 DESIGN.md §0.1 实测，2026-10-30 复测）：

    arxiv      —— 永远启用。免费、无需 key、有摘要。
                  但只覆盖预印本：实测顶会论文里只有约 1/4 有 arXiv 版本。
    openalex   —— **默认启用**。有 key 走游标深翻；无 key 走 page 浅翻。
                  2026-10-30 复测无 key 也可用（摘要/venue/DOI 齐全），
                  但配额是"同网络共享"，429 随时可能出现，所以按单源降级处理。
    crossref   —— 默认关闭。可用但大量记录无摘要，且会显著拖慢一轮运行。

会议整本采集走另一条路径（proceedings.py + pipeline.collect_conferences），
不经过关键词检索 —— 会议的正式论文清单是有限且确定的。
"""

from .arxiv import ArxivSource
from .crossref import CrossrefSource
from .openalex import OpenAlexSource
from .proceedings import AclAnthologyToc, UsenixToc

__all__ = [
    "ArxivSource",
    "OpenAlexSource",
    "CrossrefSource",
    "UsenixToc",
    "AclAnthologyToc",
    "build_sources",
    "build_toc_source",
    "DEFAULT_SOURCE_NAMES",
]

DEFAULT_SOURCE_NAMES = ("arxiv", "openalex")

_REGISTRY = {
    "arxiv": ArxivSource,
    "openalex": OpenAlexSource,
    "crossref": CrossrefSource,
}

# 会议官网采集器：按 profile 里 conferences[kind] 选
TOC_REGISTRY = {
    "usenix": UsenixToc,
    "acl": AclAnthologyToc,
}


def build_sources(env: dict, client, names=None, log=None, profile=None):
    """按名字构建数据源列表。

    返回 (sources, skipped)。skipped 里是被跳过的源及原因，doctor 会显示出来，
    避免"以为在检索其实没检索"的静默失败。

    OpenAlex 不再因为"没有 key"被跳过：无 key 只是能力降级（不能深翻），
    不是不可用。真正的 429 会在请求时被 pipeline 按单源降级处理。
    """
    log = log or (lambda msg: None)
    requested = list(names) if names else list(getattr(profile, "source_names", None) or DEFAULT_SOURCE_NAMES)
    sources, skipped = [], []

    for name in requested:
        name = str(name).strip().lower()
        if not name:
            continue
        if name not in _REGISTRY:
            skipped.append((name, "未知的数据源名"))
            continue
        if name == "openalex":
            api_key = (env.get("OPENALEX_API_KEY") or "").strip()
            if not api_key:
                log("  · OpenAlex 未配置 key：走浅翻（够日报用），429 时自动降级")
            sources.append(
                OpenAlexSource(
                    client,
                    api_key=api_key or None,
                    log=log,
                    contact_email=(env.get("ARXIV_CONTACT_EMAIL") or env.get("UNPAYWALL_EMAIL") or ""),
                )
            )
            continue
        sources.append(_REGISTRY[name](client, log=log))

    return sources, skipped


def build_toc_source(kind: str, client, log=None):
    """按 kind 构建会议官网采集器；不认识返回 None（由调用方报告）。"""
    factory = TOC_REGISTRY.get(str(kind or "").strip().lower())
    if factory is None:
        return None
    return factory(client, log=log)
