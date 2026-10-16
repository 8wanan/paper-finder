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
        "attack", "vulnerability", "perturbation", "adversarial example",
        "red teaming", "guardrail", "alignment", "defense", "detection",
        "security", "privacy", "threat", "evasion", "hijack", "manipulation",
        "misuse", "injection", "anti-spoofing", "watermark",
        # 实测补入：Careless Whisper（转写幻觉危害）因为标题里只有
        # "Hallucination" 和 "Harms"，威胁轴得 0 分，排到了 701 名。
        "hallucination", "misinformation", "harm", "toxicity",
    ],
    "low": [
        "robustness", "safety", "harmful", "malicious", "risk", "leakage",
        "provenance", "authentication", "trustworthy", "unsafe",
    ],
}

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
        raw = self.scoring.get(section) or {}
        # 兼容旧格式：早期版本只有一张 term_weights 表，视为威胁轴
        if not raw and section == "threat_terms":
            raw = self.scoring.get("term_weights") or {}
        resolved = {}
        for level in ("high", "mid", "low"):
            values = raw.get(level) or defaults[level]
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
    def category_weights(self) -> dict:
        raw = self.scoring.get("category_weights") or {}
        return {k: float(v) for k, v in {**DEFAULT_CATEGORY_WEIGHTS, **raw}.items()}

    @property
    def recency_tau_days(self) -> float:
        return float(self.scoring.get("recency_tau_days") or 540)

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
