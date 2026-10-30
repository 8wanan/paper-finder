"""领域配置加载。

设计目标：换研究方向时**只改 YAML，不改代码**。
配置里的每一项都有代码内默认值，所以新增 profile 时可以从最小配置起步。
"""

from __future__ import annotations

import os

from .yamlish import load

__all__ = ["Profile", "load_profile", "DEFAULT_PROFILE", "PROFILE_DIR"]

DEFAULT_PROFILE = "llm-speech-security"
PROFILE_DIR = "profiles"

# 术语权重：拆成「威胁轴」与「主题轴」两条，两者都命中才得高分。
# 依据是实测发现的设计缺陷：把主题词与威胁词混在一张表里，
# 会让 "…for Large Audio-Language Models" 这类只谈模型不谈安全的论文打满 S_lex。
# 详见 screen.score_lexical 的说明。
DEFAULT_THREAT_WEIGHTS = {
    "high": [
        "audio jailbreak", "voice jailbreak", "spoken jailbreak", "prompt injection",
        "jailbreak", "adversarial", "spoofing", "backdoor", "poisoning",
        "over-the-air", "membership inference", "voice cloning", "impersonation",
        "deepfake", "model extraction", "data extraction",
    ],
    "mid": [
        "attack", "vulnerability", "adversarial example",
        "red teaming", "guardrail", "defense", "detection",
        "security", "privacy", "threat", "evasion", "hijack", "manipulation",
        "misuse", "injection", "anti-spoofing", "watermark",
        "misinformation", "harm", "toxicity",
    ],
    "low": [
        # 实测降权：这几个词在**任何**音频 ML 论文里都会出现，不是安全信号。
        # 一篇 LALM 幻觉评测论文靠 hallucination + detection 就能把威胁轴打满，
        # 混进"强烈建议读"。降权后它们只是辅助信号，不足以进强推桶。
        "perturbation", "alignment", "hallucination",
        "robustness", "safety", "harmful", "malicious", "risk", "leakage",
        "provenance", "authentication", "trustworthy", "unsafe",
    ],
}

# 核心安全信号：进"强烈建议读"桶的**必要条件**。
# 威胁轴得分只能说明"出现了一些安全词汇"，而这类词汇在通用音频 ML 论文里
# 到处都是。实测 watch 运行前 15 名里 11 篇连一个明确安全信号都没有
# （水下声学识别、音乐评审、声乐教练、声码器探针）。所以强推桶加一道硬门槛。
# 不命中的论文不会被丢弃，只是最多进"值得扫一眼"。
DEFAULT_CORE_TERMS = [
    "audio jailbreak", "voice jailbreak", "spoken jailbreak", "jailbreak",
    "prompt injection", "adversarial", "spoofing", "anti-spoofing",
    "backdoor", "poisoning", "over-the-air", "membership inference",
    "voice cloning", "impersonation", "deepfake", "model extraction",
    "data extraction", "attack", "vulnerability", "threat", "evasion",
    "hijack", "manipulation", "misuse", "injection", "red teaming",
    "unauthorized", "privacy leakage",
]

DEFAULT_TOPIC_WEIGHTS = {
    "high": [
        "large audio-language model", "audio-language model", "speech language model",
        "lalm", "voice assistant", "smart speaker", "speech recognition",
        "automatic speech recognition", "asr", "text-to-speech", "tts",
        "speaker verification", "speech synthesis", "audio llm",
    ],
    "mid": [
        "speech", "audio", "voice", "acoustic", "spoken", "whisper",
        "transcription", "utterance", "waveform", "multimodal large language model",
        "speech-to-text",
    ],
    "low": [
        "language model", "llm", "foundation model", "transformer", "neural network",
    ],
}

DEFAULT_CATEGORY_WEIGHTS = {
    "eess.AS": 0.40, "cs.CR": 0.35, "cs.SD": 0.30,
    "cs.CL": 0.20, "cs.LG": 0.15, "cs.MM": 0.15,
}

DEFAULT_VENUE_SCORES = {"whitelist": 1.0, "preprint": 0.4, "other": 0.2}

DEFAULT_THRESHOLDS = {
    # ── 按名次分桶（L1-only 模式的主机制）──────────────────────────
    # 为什么不用绝对分数：实测 848 篇候选的 L1 分布高度压缩
    # （p10=0.662、p25=0.609、p50=0.552），任何绝对阈值都会在这堆论文里
    # 一刀切出 106 篇"强烈建议读" —— 等于没筛，用户读不完。
    # 按名次切保证简报永远是"能读完的长度"，且与语料规模无关。
    "strong_top": 15,     # 强烈建议读：前 15 名
    "maybe_top": 50,      # 值得扫一眼：第 16–50 名
    # ── 绝对阈值（无 LLM 时作兜底，或单篇调用时使用）────────────────
    "l1_strong": 0.65,
    "l1_maybe": 0.45,
    "l1_min_keep": 0.30,
    # ── L2 模式（有 LLM 精排）：12 分制，有实际含义，仍用绝对阈值 ──────
    "l2_strong": 8,
    "l2_maybe": 5,
    # 无摘要但 L1 达标的论文，单独交给人确认，不让 LLM 猜
    "needs_review_l1_min": 0.50,
    # 种子论文的分数下限：用户已明确声明这些是"必读"，工具必须保证它们浮上来。
    # 实测依据：DolphinAttack 因为标题被拼错、且是 2017 年的老论文，
    # 自然得分只有 0.40，排在 117 名，达不到 must_recall 的 top-50 要求。
    "seed_score_floor": 0.65,
}


class Profile:
    """一个研究方向的完整配置。"""

    def __init__(self, data: dict, path: str = ""):
        self.data = data or {}
        self.path = path

    # ── 元信息 ──────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return self.data.get("name") or DEFAULT_PROFILE

    @property
    def display_name(self) -> str:
        return self.data.get("display_name") or self.name

    @property
    def interest(self) -> str:
        return (self.data.get("interest") or "").strip()

    # ── 查询 ────────────────────────────────────────────────────────
    def _queries_from(self, container) -> list:
        if not container:
            return []
        queries = []
        for item in container:
            if not isinstance(item, dict) or not item.get("q"):
                continue
            queries.append(
                {
                    "id": str(item.get("id") or f"Q{len(queries) + 1}"),
                    "q": str(item["q"]),
                    "expect": item.get("expect"),
                    "tier": item.get("tier"),
                }
            )
        return queries

    @property
    def census_queries(self):
        """Tier A：全量穷举用。"""
        return self._queries_from((self.data.get("census") or {}).get("queries"))

    @property
    def census_depth(self) -> int:
        return int((self.data.get("census") or {}).get("depth") or 300)

    @property
    def source_names(self) -> list:
        """profile 声明要启用的数据源；空则用默认（arxiv + openalex）。

        为什么让 profile 决定：语音安全方向 arXiv 就够（会议论文都首发预印本），
        但顶会方向必须带 OpenAlex —— 同一条命令在不同 profile 下该用的源不一样。
        """
        raw = self.data.get("sources")
        if not raw:
            return []
        return [str(item).strip().lower() for item in raw if str(item).strip()]

    @property
    def watch_queries(self) -> list:
        """每日增量扫描用哪些查询。

        `watch_scope: census`（默认）只扫 Tier A —— 那才是"这个领域"的定义性查询；
        `watch_scope: all` 把 Tier B 与世代探针也扫进来，用于"方法论"这类
        没有明确边界的 profile（它的查询本身就是检索式的）。
        """
        scope = str(self.data.get("watch_scope") or "census").strip().lower()
        if scope == "all":
            return self.census_queries + self.search_queries + self.generation_probes
        return self.census_queries

    @property
    def search_queries(self):
        """Tier B：检索排序用。"""
        return self._queries_from((self.data.get("search") or {}).get("queries"))

    @property
    def generation_probes(self):
        """覆盖术语漂移的世代化查询。"""
        return self._queries_from((self.data.get("search") or {}).get("generation_probes"))

    @property
    def category_probes(self):
        container = self.data.get("category_probes") or {}
        return self._queries_from(container.get("queries") if isinstance(container, dict) else container)

    @property
    def search_default_top(self) -> int:
        return int((self.data.get("search") or {}).get("default_top") or 20)

    def all_queries(self, include_generation=True, include_category=True) -> list:
        """返回本次运行要执行的全部查询（按 id 去重）。"""
        collected = list(self.census_queries) + list(self.search_queries)
        if include_generation:
            collected += list(self.generation_probes)
        if include_category:
            collected += list(self.category_probes)
        seen, unique = set(), []
        for query in collected:
            if query["id"] in seen:
                continue
            seen.add(query["id"])
            unique.append(query)
        return unique

    # ── 打分 ────────────────────────────────────────────────────────
    @property
    def scoring(self) -> dict:
        return self.data.get("scoring") or {}

    def _resolved_terms(self, section: str, defaults: dict) -> dict:
        """解析一条轴的术语表。

        轴名有两种叫法（迁移到 AI 挖洞时引入的可读别名）：
          威胁轴 threat_terms  ≡ task_terms（做的是不是漏洞工作）
          主题轴 topic_terms   ≡ ai_terms（是不是 AI 方法）
        别名不是装饰：旧 profile 里"威胁 × 主题"是语音安全的语义，
        新方向里这两条轴变成"任务 × AI"，沿用旧字段名会让配置文件读不懂。

        只要 profile 显式提供了某一级，未提供的级别就按**空**处理，
        而不是回落到语音方向的默认词表 —— 否则新 profile 会悄悄带上
        "audio jailbreak" 这类完全不相关的词。
        """
        raw = self.scoring.get(section)
        if not raw:
            aliases = {"threat_terms": ("task_terms", "term_weights"), "topic_terms": ("ai_terms",)}
            for alias in aliases.get(section, ()):
                raw = self.scoring.get(alias)
                if raw:
                    break
        raw = raw or {}
        provided = bool(raw)
        resolved = {}
        for level in ("high", "mid", "low"):
            values = raw.get(level)
            if values is None:
                values = [] if provided else defaults[level]
            resolved[level] = [str(v).lower() for v in values if str(v).strip()]
        return resolved

    def _weight_map(self, section: str, defaults: dict) -> dict:
        """返回 {术语: 权重}，高/中/低分别 3.0 / 2.0 / 1.0。"""
        weights = {}
        resolved = self._resolved_terms(section, defaults)
        for level, value in (("high", 3.0), ("mid", 2.0), ("low", 1.0)):
            for term in resolved.get(level, []):
                weights.setdefault(term, value)
        return weights

    def threat_term_weight_map(self) -> dict:
        return self._weight_map("threat_terms", DEFAULT_THREAT_WEIGHTS)

    def topic_term_weight_map(self) -> dict:
        return self._weight_map("topic_terms", DEFAULT_TOPIC_WEIGHTS)

    @property
    def core_terms(self) -> list:
        """核心安全信号词表。profile 没配就用默认（见 DEFAULT_CORE_TERMS）。"""
        raw = self.scoring.get("core_terms")
        if raw is None:
            return list(DEFAULT_CORE_TERMS)
        return [str(v).lower() for v in raw if str(v).strip()]

    @property
    def category_weights(self) -> dict:
        raw = self.scoring.get("category_weights") or {}
        return {k: float(v) for k, v in {**DEFAULT_CATEGORY_WEIGHTS, **raw}.items()}

    @property
    def recency_tau_days(self) -> float:
        return float(self.scoring.get("recency_tau_days") or 540)

    @property
    def lex_denominator(self) -> float:
        """S_lex 的分母：一条轴上"多少加权命中算打满"。

        默认 9.0（= 最高权重 3 × 标题倍率 3）是语音方向的标定：一个高权重术语
        出现在标题里即打满。术语密集的方向（AI 挖洞）需要调高，否则几乎所有
        相关论文都并列 1.0，排名退化成按年份排序。
        """
        return float(self.scoring.get("lex_denominator") or 9.0)

    @property
    def venue_whitelist(self) -> list:
        return [str(v).lower() for v in (self.scoring.get("venue_whitelist") or [])]

    @property
    def venue_scores(self) -> dict:
        raw = self.scoring.get("venue_scores") or {}
        return {**DEFAULT_VENUE_SCORES, **{k: float(v) for k, v in raw.items()}}

    # ── 负向词 ──────────────────────────────────────────────────────
    @property
    def negative_terms(self) -> list:
        raw = (self.data.get("negative") or {}).get("terms") or []
        return [str(v).lower() for v in raw]

    @property
    def safety_overrides(self) -> list:
        raw = (self.data.get("negative") or {}).get("safety_overrides") or []
        return [str(v).lower() for v in raw]

    # ── 顶会口径（硬过滤，与 scoring.venue_whitelist 的加分逻辑分开）──────
    @property
    def venues(self) -> set:
        """白名单会议，归一化成 key 集合（如 {"ccs","sp","usenix","icse"}）。

        与 `scoring.venue_whitelist` 的区别：那个只影响打分（0.10 权重），
        这个决定**放不放行**。两者可以不一致 —— 例如白名单里放一堆会议，
        但只用其中几个做硬过滤。
        """
        from .venues import canonical, known_keys

        raw = self.data.get("venues") or []
        keys = set()
        for item in raw:
            text = str(item).strip().lower()
            key = text if text in known_keys() else canonical(item)
            if key:
                keys.add(key)
        return keys

    @property
    def venue_policy(self) -> str:
        """off / plus_preprint / strict，见 venues.check_venue_policy。"""
        policy = str(self.data.get("venue_policy") or "off").strip().lower()
        return policy if policy in ("off", "plus_preprint", "strict") else "off"

    # ── 顶会批量采集（census 之外的第二种来源形态）───────────────────
    @property
    def conferences(self) -> list:
        """待批量采集的会议清单。

        每条要么走 OpenAlex 的 source id（ccs/icse/fse 等有稳定收录的），
        要么走会议官网 TOC（USENIX / ACL Anthology 这类 OpenAlex 缺收的）。
        结构：
          {venue: ccs, year: 2025, kind: openalex, source_id: S4393917527}
          {venue: usenix, year: 2025, kind: usenix, url: https://...}
        """
        from .venues import canonical

        items = []
        for raw in self.data.get("conferences") or []:
            if not isinstance(raw, dict):
                continue
            key = canonical(raw.get("venue"))
            if not key:
                continue
            item = dict(raw)
            item["venue"] = key
            items.append(item)
        return items

    # ── L2 评分卡（每个维度 0-3 分，总分 3 × 维度数）────────────────────
    @property
    def rubric(self) -> dict | None:
        """profile 里声明的评分卡；没声明返回 None（走内置的旧 rubric）。"""
        raw = self.data.get("rubric")
        if not isinstance(raw, dict):
            return None
        dimensions = [d for d in (raw.get("dimensions") or []) if isinstance(d, dict) and d.get("key")]
        if len(dimensions) < 2:
            return None
        cleaned = []
        for index, dimension in enumerate(dimensions[:6], 1):
            anchors = dimension.get("anchors") or {}
            cleaned.append(
                {
                    "key": str(dimension["key"]).strip(),
                    "label": str(dimension.get("label") or dimension["key"]).strip(),
                    "desc": str(dimension.get("desc") or "").strip(),
                    "anchors": {str(k): str(v) for k, v in anchors.items()},
                    "index": index,
                }
            )
        return {"dimensions": cleaned, "max": 3 * len(cleaned)}

    # ── 阈值 ────────────────────────────────────────────────────────
    @property
    def thresholds(self) -> dict:
        raw = self.data.get("thresholds") or {}
        return {**DEFAULT_THRESHOLDS, **{k: (float(v) if isinstance(v, (int, float)) else v) for k, v in raw.items()}}

    # ── 种子论文 ────────────────────────────────────────────────────
    @property
    def seeds(self) -> list:
        container = self.data.get("seeds") or {}
        items = container.get("items") if isinstance(container, dict) else container
        return [item for item in (items or []) if isinstance(item, dict)]

    @property
    def seeds_frozen_recency(self) -> bool:
        container = self.data.get("seeds") or {}
        if isinstance(container, dict):
            return bool(container.get("frozen_recency", True))
        return True

    def seed_keys(self) -> set:
        """种子的 arxiv id 与归一化标题，用于 S_seed 与衰减豁免。"""
        from .normalize import norm_arxiv, norm_title

        keys = set()
        for seed in self.seeds:
            arxiv_id = norm_arxiv(seed.get("arxiv_id"))
            if arxiv_id:
                keys.add(("arxiv", arxiv_id))
            title = norm_title(seed.get("title"))
            if title:
                keys.add(("ntitle", title))
        return keys


def load_profile(name_or_path: str, root: str = ".") -> Profile:
    """按名字或路径加载 profile。"""
    if os.path.sep in name_or_path or name_or_path.endswith((".yaml", ".yml")):
        path = name_or_path if os.path.isabs(name_or_path) else os.path.join(root, name_or_path)
    else:
        path = os.path.join(root, PROFILE_DIR, f"{name_or_path}.yaml")
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到 profile：{path}")
    return Profile(load(path), path)
