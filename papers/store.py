"""SQLite 账本 —— 本工具的核心资产。

为什么账本比检索重要（见 DESIGN.md §0.3 判断三）：
  检索结果是一次性的，而账本记录"哪些论文我见过、哪些我判定过"。
  有了它，第二次运行才能只告诉你**新增**的论文 —— 这才是"很快拿到论文"的来源。
  所以 papers / aliases / decisions 三张表是资产，不是缓存。

合并策略：跨源身份用 **union-find** 收敛到唯一 canonical pid。
同一篇论文的 arXiv 预印本与正式发表版常常 DOI 不同甚至没有 DOI，
必须靠 ntitle 别名把它们并起来（见 normalize.alias_keys）。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3

from .normalize import (
    alias_keys,
    clean_text,
    norm_title,
    pick_pid,
    reconstruct_abstract,
    short_id,
)

__all__ = ["Store", "SCHEMA_VERSION", "SUBSTANTIVE_MODES"]

SCHEMA_VERSION = 2

# 会产生"可读简报"的运行模式。`import` 与 `seed-sync` 只是记账，不算。
SUBSTANTIVE_MODES = ("census", "search", "watch")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS papers (
  pid            TEXT PRIMARY KEY,
  doi            TEXT,
  arxiv_id       TEXT,
  title          TEXT NOT NULL DEFAULT '',
  norm_title     TEXT NOT NULL DEFAULT '',
  abstract       TEXT,
  year           INTEGER,
  published      TEXT,
  venue          TEXT,
  authors        TEXT NOT NULL DEFAULT '[]',
  categories     TEXT NOT NULL DEFAULT '[]',
  citation_count INTEGER,
  oa_url         TEXT,
  url            TEXT,
  first_seen     TEXT NOT NULL,
  last_seen      TEXT NOT NULL,
  sources        TEXT NOT NULL DEFAULT '[]',
  l1             REAL,
  l1_detail      TEXT,
  l2             REAL,
  llm_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_l1       ON papers(l1 DESC);
CREATE INDEX IF NOT EXISTS idx_papers_year     ON papers(year);
CREATE INDEX IF NOT EXISTS idx_papers_seen     ON papers(first_seen);
CREATE INDEX IF NOT EXISTS idx_papers_ntitle   ON papers(norm_title);

CREATE TABLE IF NOT EXISTS aliases (
  id_type TEXT NOT NULL,
  id_val  TEXT NOT NULL,
  pid     TEXT NOT NULL,
  PRIMARY KEY (id_type, id_val)
);
CREATE INDEX IF NOT EXISTS idx_aliases_pid ON aliases(pid);

CREATE TABLE IF NOT EXISTS runs (
  run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  started    TEXT NOT NULL,
  finished   TEXT,
  mode       TEXT NOT NULL,
  profile    TEXT,
  queries    TEXT NOT NULL DEFAULT '[]',
  n_fetched  INTEGER DEFAULT 0,
  n_new      INTEGER DEFAULT 0,
  n_strong   INTEGER DEFAULT 0,
  note       TEXT
);

CREATE TABLE IF NOT EXISTS run_papers (
  run_id INTEGER NOT NULL,
  pid    TEXT NOT NULL,
  is_new INTEGER NOT NULL DEFAULT 0,
  l1     REAL,
  l2     REAL,
  PRIMARY KEY (run_id, pid)
);
CREATE INDEX IF NOT EXISTS idx_runpapers_run ON run_papers(run_id, is_new);

CREATE TABLE IF NOT EXISTS paper_queries (
  pid      TEXT NOT NULL,
  query_id TEXT NOT NULL,
  run_id   INTEGER,
  PRIMARY KEY (pid, query_id)
);

CREATE TABLE IF NOT EXISTS decisions (
  pid        TEXT PRIMARY KEY,
  decision   TEXT NOT NULL,
  reason     TEXT,
  tags       TEXT NOT NULL DEFAULT '[]',
  decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_cache (
  pid         TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  model       TEXT NOT NULL,
  response    TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (pid, prompt_hash, model)
);

CREATE TABLE IF NOT EXISTS citations (
  src_pid    TEXT NOT NULL,
  dst_pid    TEXT NOT NULL,
  src_source TEXT NOT NULL,
  PRIMARY KEY (src_pid, dst_pid, src_source)
);

-- 论文简介（"这篇在讲什么"）。
-- 为什么单独一张表而不是复用 llm_json：
--   llm_json 里的 L2 分数会影响**分桶**（走 l2 绝对阈值那条分支）。
--   而简介只该影响**展示** —— 如果复用，写一次简介就会把刚修好的
--   "按名次分桶"覆盖掉，漏斗重新失效。两者语义不同，必须分开存。
CREATE TABLE IF NOT EXISTS annotations (
  pid          TEXT PRIMARY KEY,
  summary      TEXT NOT NULL,   -- 一句话说清这篇在做什么
  why_relevant TEXT,            -- 可选：与研究者方向的关系
  quote        TEXT,            -- 可选：摘要原文片段，用于校验不是编的
  source       TEXT NOT NULL DEFAULT 'agent',  -- agent | llm | manual
  model        TEXT,
  created_at   TEXT NOT NULL
);
"""

# 允许被外部覆盖的字段（None / 空值不覆盖已有内容）
_MERGEABLE = (
    "doi", "arxiv_id", "title", "norm_title", "abstract", "year", "published",
    "venue", "authors", "categories", "citation_count", "oa_url", "url",
)


def iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _jload(value, fallback):
    if not value:
        return fallback
    try:
        parsed = json.loads(value)
        return parsed if parsed is not None else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


class Store:
    """论文账本。"""

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── 写入 ────────────────────────────────────────────────────────
    def upsert(self, paper: dict):
        """写入或合并一篇论文。

        返回 (pid, is_new)。is_new 为 True 表示这是账本第一次见到它 ——
        这就是"增量追踪"的判定依据。
        """
        now = iso_now()
        keys = alias_keys(paper)
        natural_pid = pick_pid(paper)

        found = self._lookup_pids(keys)
        if found:
            canonical = self._choose_canonical(found, natural_pid)
            for pid in found:
                if pid != canonical:
                    self._merge_into(canonical, pid)
            is_new = False
        else:
            canonical = natural_pid
            is_new = True

        self._write_paper(canonical, paper, now, is_new)
        for id_type, id_val in keys:
            self._put_alias(id_type, id_val, canonical)
        self.conn.commit()
        return canonical, is_new

    def _lookup_pids(self, keys) -> list:
        pids = []
        for id_type, id_val in keys:
            row = self.conn.execute(
                "SELECT pid FROM aliases WHERE id_type=? AND id_val=?", (id_type, id_val)
            ).fetchone()
            if row and row["pid"] not in pids:
                pids.append(row["pid"])
        return pids

    def _choose_canonical(self, found, natural_pid) -> str:
        """在多个候选中选一个规范 pid。

        优先级：已存在于 papers 表 > DOI 前缀 > arXiv 前缀 > 自然指纹。
        DOI 优先是因为它最稳定（预印本转正式发表后仍然对得上）。
        """
        existing = []
        for pid in found:
            row = self.conn.execute("SELECT pid FROM papers WHERE pid=?", (pid,)).fetchone()
            if row:
                existing.append(pid)
        pool = existing or found
        for prefix in ("doi:", "arxiv:"):
            for pid in pool:
                if pid.startswith(prefix):
                    return pid
        return pool[0] if pool else natural_pid

    def _put_alias(self, id_type, id_val, pid):
        self.conn.execute(
            "INSERT INTO aliases(id_type, id_val, pid) VALUES(?,?,?) "
            "ON CONFLICT(id_type, id_val) DO UPDATE SET pid=excluded.pid",
            (id_type, id_val, pid),
        )

    def _write_paper(self, pid, paper, now, is_new):
        row = self.conn.execute("SELECT * FROM papers WHERE pid=?", (pid,)).fetchone()
        incoming = self._normalize_incoming(paper)
        if row is None:
            cols = ["pid"] + list(_MERGEABLE) + ["first_seen", "last_seen", "sources"]
            values = [pid] + [incoming.get(c) for c in _MERGEABLE] + [now, now, json.dumps(incoming["_sources"])]
            placeholders = ",".join("?" * len(cols))
            self.conn.execute(
                f"INSERT INTO papers({','.join(cols)}) VALUES({placeholders})", values
            )
            return
        # 已存在：逐字段补齐（新值非空才覆盖），并合并 sources
        updates, params = [], []
        for column in _MERGEABLE:
            new_value = incoming.get(column)
            if _is_blank(new_value):
                continue
            old_value = row[column]
            if _is_blank(old_value):
                updates.append(f"{column}=?")
                params.append(new_value)
        merged_sources = sorted(set(_jload(row["sources"], [])) | set(incoming["_sources"]))
        updates.append("sources=?")
        params.append(json.dumps(merged_sources))
        updates.append("last_seen=?")
        params.append(now)
        params.append(pid)
        self.conn.execute(f"UPDATE papers SET {','.join(updates)} WHERE pid=?", params)

    def _normalize_incoming(self, paper) -> dict:
        authors = paper.get("authors") or []
        categories = paper.get("categories") or []
        sources = paper.get("sources") or []
        abstract = clean_text(paper.get("abstract")) or None
        return {
            "doi": clean_text(paper.get("doi")) or None,
            "arxiv_id": clean_text(paper.get("arxiv_id")) or None,
            "title": clean_text(paper.get("title")) or "",
            "norm_title": norm_title(paper.get("title")),
            "abstract": abstract,
            "year": paper.get("year"),
            "published": clean_text(paper.get("published")) or None,
            "venue": clean_text(paper.get("venue")) or None,
            "authors": json.dumps(authors, ensure_ascii=False),
            "categories": json.dumps(categories, ensure_ascii=False),
            "citation_count": paper.get("citation_count"),
            "oa_url": clean_text(paper.get("oa_url")) or None,
            "url": clean_text(paper.get("url")) or None,
            "_sources": [s for s in sources if s],
        }

    # ── 合并（union-find 的收敛步骤）────────────────────────────────
    def _merge_into(self, keep: str, drop: str):
        """把 drop 合并进 keep，并重指向所有引用。"""
        if keep == drop:
            return
        keep_row = self.conn.execute("SELECT * FROM papers WHERE pid=?", (keep,)).fetchone()
        drop_row = self.conn.execute("SELECT * FROM papers WHERE pid=?", (drop,)).fetchone()

        if drop_row is not None and keep_row is not None:
            # 保留更早的 first_seen、更晚的 last_seen
            first_seen = min(keep_row["first_seen"], drop_row["first_seen"])
            last_seen = max(keep_row["last_seen"], drop_row["last_seen"])
            updates, params = ["first_seen=?", "last_seen=?"], [first_seen, last_seen]
            for column in _MERGEABLE:
                if _is_blank(keep_row[column]) and not _is_blank(drop_row[column]):
                    updates.append(f"{column}=?")
                    params.append(drop_row[column])
            merged_sources = sorted(set(_jload(keep_row["sources"], [])) | set(_jload(drop_row["sources"], [])))
            updates.append("sources=?")
            params.append(json.dumps(merged_sources))
            # l1/l2 取更"新"的：有 l2 优先，其次较大的 l1
            if _is_blank(keep_row["l2"]) and not _is_blank(drop_row["l2"]):
                updates += ["l2=?", "llm_json=?"]
                params += [drop_row["l2"], drop_row["llm_json"]]
            if _is_blank(keep_row["l1"]) and not _is_blank(drop_row["l1"]):
                updates += ["l1=?", "l1_detail=?"]
                params += [drop_row["l1"], drop_row["l1_detail"]]
            params.append(keep)
            self.conn.execute(f"UPDATE papers SET {','.join(updates)} WHERE pid=?", params)
            self.conn.execute("DELETE FROM papers WHERE pid=?", (drop,))

        elif drop_row is not None and keep_row is None:
            # keep 还不存在：直接把 drop 的行改名为 keep
            self.conn.execute("UPDATE papers SET pid=? WHERE pid=?", (keep, drop))

        # 重指向引用表
        self.conn.execute("UPDATE OR IGNORE aliases SET pid=? WHERE pid=?", (keep, drop))
        self.conn.execute("DELETE FROM aliases WHERE pid=?", (drop,))
        self.conn.execute("UPDATE OR IGNORE run_papers SET pid=? WHERE pid=?", (keep, drop))
        self.conn.execute("DELETE FROM run_papers WHERE pid=?", (drop,))
        self.conn.execute("UPDATE OR IGNORE paper_queries SET pid=? WHERE pid=?", (keep, drop))
        self.conn.execute("DELETE FROM paper_queries WHERE pid=?", (drop,))
        self.conn.execute("UPDATE OR IGNORE llm_cache SET pid=? WHERE pid=?", (keep, drop))
        self.conn.execute("DELETE FROM llm_cache WHERE pid=?", (drop,))
        # 简介也要重指向，否则论文合并（预印本↔正式版）时简介会丢
        self.conn.execute("UPDATE OR IGNORE annotations SET pid=? WHERE pid=?", (keep, drop))
        self.conn.execute("DELETE FROM annotations WHERE pid=?", (drop,))
        self.conn.execute("UPDATE OR IGNORE citations SET src_pid=? WHERE src_pid=?", (keep, drop))
        self.conn.execute("UPDATE OR IGNORE citations SET dst_pid=? WHERE dst_pid=?", (keep, drop))
        self.conn.execute("DELETE FROM citations WHERE src_pid=? OR dst_pid=?", (drop, drop))

        # 判定：保留更晚做出的那个
        keep_decision = self.conn.execute("SELECT * FROM decisions WHERE pid=?", (keep,)).fetchone()
        drop_decision = self.conn.execute("SELECT * FROM decisions WHERE pid=?", (drop,)).fetchone()
        if drop_decision is not None:
            if keep_decision is None or drop_decision["decided_at"] > keep_decision["decided_at"]:
                self.conn.execute("DELETE FROM decisions WHERE pid=?", (keep,))
                self.conn.execute("UPDATE decisions SET pid=? WHERE pid=?", (keep, drop))
            else:
                self.conn.execute("DELETE FROM decisions WHERE pid=?", (drop,))

    # ── 运行记录 ────────────────────────────────────────────────────
    def start_run(self, mode, profile, queries) -> int:
        cursor = self.conn.execute(
            "INSERT INTO runs(started, mode, profile, queries) VALUES(?,?,?,?)",
            (iso_now(), mode, profile, json.dumps(queries, ensure_ascii=False)),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id, n_fetched=0, n_new=0, n_strong=0, note=None):
        self.conn.execute(
            "UPDATE runs SET finished=?, n_fetched=?, n_new=?, n_strong=?, note=? WHERE run_id=?",
            (iso_now(), n_fetched, n_new, n_strong, note, run_id),
        )
        self.conn.commit()

    def record_run_paper(self, run_id, pid, is_new, l1=None, l2=None):
        self.conn.execute(
            "INSERT INTO run_papers(run_id, pid, is_new, l1, l2) VALUES(?,?,?,?,?) "
            "ON CONFLICT(run_id, pid) DO UPDATE SET is_new=excluded.is_new, l1=excluded.l1, l2=excluded.l2",
            (run_id, pid, 1 if is_new else 0, l1, l2),
        )

    def record_paper_query(self, pid, query_id, run_id):
        self.conn.execute(
            "INSERT OR IGNORE INTO paper_queries(pid, query_id, run_id) VALUES(?,?,?)",
            (pid, query_id, run_id),
        )

    def commit(self):
        self.conn.commit()

    def latest_run(self, modes=None):
        """取最近一次运行。

        modes 用来过滤"有实质内容的运行"。为什么需要：`import` 与 `seed-sync`
        只是记账（把冷启动数据和种子论文塞进账本），它们产生的候选只有几篇。
        如果 `brief` 默认取"最新一次运行"，用户导入完种子再敲 `brief`，
        会看到一份 8 篇的简报，以为工具坏了。实测踩到过。
        """
        if modes:
            placeholders = ",".join("?" * len(modes))
            return self.conn.execute(
                f"SELECT * FROM runs WHERE mode IN ({placeholders}) "
                f"ORDER BY run_id DESC LIMIT 1",
                tuple(modes),
            ).fetchone()
        return self.conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()

    def get_run(self, run_id):
        return self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    # ── 打分写回 ────────────────────────────────────────────────────
    def set_l1(self, pid, score, detail):
        self.conn.execute(
            "UPDATE papers SET l1=?, l1_detail=? WHERE pid=?",
            (score, json.dumps(detail, ensure_ascii=False), pid),
        )

    def set_l2(self, pid, score, payload):
        self.conn.execute(
            "UPDATE papers SET l2=?, llm_json=? WHERE pid=?",
            (score, json.dumps(payload, ensure_ascii=False) if payload is not None else None, pid),
        )

    def query_count(self, pid) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM paper_queries WHERE pid=?", (pid,)
        ).fetchone()
        return int(row["n"]) if row else 0

    # ── 查询 ────────────────────────────────────────────────────────
    def get_paper(self, pid):
        return self.conn.execute("SELECT * FROM papers WHERE pid=?", (pid,)).fetchone()

    def resolve_pid(self, token: str):
        """把简短的引用还原成完整 pid。要求解析结果唯一。

        支持三种写法：
          1. 完整 pid            "arxiv:2501.00011"
          2. 短 id（简报里显示的）"2501.00011"   ← 是 pid 的后缀，不是前缀
          3. raw id 前缀          "10.1145/3133956"

        第 2 种不能靠 `pid LIKE 'token%'` 实现：短 id 是去掉 `arxiv:` 前缀后的
        后缀。实测踩到过 —— 简报里的 id 一律解析失败，`papers.py decide --pid`
        和救回归档论文都会失效。
        """
        token = clean_text(token)
        if not token:
            return None

        exact = self.conn.execute("SELECT pid FROM papers WHERE pid=?", (token,)).fetchone()
        if exact:
            return exact["pid"]

        matches = set()

        # 1) 完整 pid 前缀
        for row in self.conn.execute(
            "SELECT pid FROM papers WHERE pid LIKE ? LIMIT 5", (token + "%",)
        ).fetchall():
            matches.add(row["pid"])

        # 2) raw id 前缀（DOI / arXiv ID 存在独立列里）
        for row in self.conn.execute(
            "SELECT pid FROM papers WHERE arxiv_id LIKE ? OR doi LIKE ? LIMIT 5",
            (token + "%", token.lower() + "%"),
        ).fetchall():
            matches.add(row["pid"])

        # 3) 短 id 比对（最多扫全表，账本量级是几千，代价可忽略）
        for row in self.conn.execute("SELECT pid FROM papers").fetchall():
            sid = short_id(row["pid"])
            if sid == token or sid.startswith(token):
                matches.add(row["pid"])

        if len(matches) == 1:
            return next(iter(matches))
        return None

    def ranked(self, run_id=None, limit=None, min_l1=None, scored_only=False):
        """按 L1 降序返回论文；限定在某次运行内则只返回该次运行涉及的论文。

        scored_only 很重要：SQLite 在 `ORDER BY l1 DESC` 时把 NULL 排在**最后**，
        所以未打分的论文会沉到底部。做金标集评测时必须只对已打分的论文排名，
        否则一篇没打分的相关论文会显示成"第 1158 名"，得出完全错误的召回结论。
        """
        sql = "SELECT p.* FROM papers p"
        params = []
        if run_id is not None:
            sql += " JOIN run_papers rp ON rp.pid = p.pid AND rp.run_id = ?"
            params.append(run_id)
        conditions = []
        if min_l1 is not None:
            conditions.append("p.l1 >= ?")
            params.append(min_l1)
        if scored_only:
            conditions.append("p.l1 IS NOT NULL")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY (p.l2 IS NOT NULL) DESC, p.l2 DESC, p.l1 DESC, p.year DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def unscored_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM papers WHERE l1 IS NULL").fetchone()
        return int(row["n"]) if row else 0

    def run_paper_rows(self, run_id):
        return self.conn.execute(
            "SELECT p.*, rp.is_new FROM papers p "
            "JOIN run_papers rp ON rp.pid = p.pid WHERE rp.run_id=?",
            (run_id,),
        ).fetchall()

    def decisions(self):
        return {
            row["pid"]: row
            for row in self.conn.execute("SELECT * FROM decisions").fetchall()
        }

    # ── 论文简介 ────────────────────────────────────────────────────
    def set_annotation(self, pid, summary, why_relevant=None, quote=None,
                       source="agent", model=None):
        self.conn.execute(
            "INSERT INTO annotations(pid, summary, why_relevant, quote, source, model, created_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(pid) DO UPDATE SET summary=excluded.summary, "
            "why_relevant=excluded.why_relevant, quote=excluded.quote, "
            "source=excluded.source, model=excluded.model, created_at=excluded.created_at",
            (pid, clean_text(summary), clean_text(why_relevant) or None,
             clean_text(quote) or None, source, model, iso_now()),
        )
        self.conn.commit()

    def annotation(self, pid):
        return self.conn.execute(
            "SELECT * FROM annotations WHERE pid=?", (pid,)
        ).fetchone()

    def annotations(self) -> dict:
        return {
            row["pid"]: row
            for row in self.conn.execute("SELECT * FROM annotations").fetchall()
        }

    def set_decision(self, pid, decision, reason=None, tags=None):
        self.conn.execute(
            "INSERT INTO decisions(pid, decision, reason, tags, decided_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(pid) DO UPDATE SET decision=excluded.decision, reason=excluded.reason, "
            "tags=excluded.tags, decided_at=excluded.decided_at",
            (
                pid,
                decision,
                reason,
                json.dumps(tags or [], ensure_ascii=False),
                iso_now(),
            ),
        )
        self.conn.commit()

    def stats(self) -> dict:
        def scalar(sql, *params):
            row = self.conn.execute(sql, params).fetchone()
            return row[0] if row else 0

        return {
            "papers": scalar("SELECT COUNT(*) FROM papers"),
            "with_abstract": scalar(
                "SELECT COUNT(*) FROM papers WHERE abstract IS NOT NULL AND abstract <> ''"
            ),
            "aliases": scalar("SELECT COUNT(*) FROM aliases"),
            "runs": scalar("SELECT COUNT(*) FROM runs"),
            "decisions": scalar("SELECT COUNT(*) FROM decisions"),
            "scored": scalar("SELECT COUNT(*) FROM papers WHERE l1 IS NOT NULL"),
            "llm_cached": scalar("SELECT COUNT(*) FROM llm_cache"),
            "annotated": scalar("SELECT COUNT(*) FROM annotations"),
            "by_year": {
                str(row["year"]): row["n"]
                for row in self.conn.execute(
                    "SELECT year, COUNT(*) AS n FROM papers WHERE year IS NOT NULL "
                    "GROUP BY year ORDER BY year DESC LIMIT 12"
                ).fetchall()
            },
            "by_source": {
                row["value"]: row["n"]
                for row in self.conn.execute(
                    "SELECT value, COUNT(*) AS n FROM papers, json_each(papers.sources) "
                    "GROUP BY value ORDER BY n DESC"
                ).fetchall()
            },
        }


def _is_blank(value) -> bool:
    """判断字段是否"空"。

    关键：JSON 列（authors / categories）的空值是字符串 "[]" 而不是 None。
    如果只按"空字符串"判断，"[]" 会被当成有值，导致**后续抓取到的真实分类
    永远回填不进去** —— 表现为 S_cat 恒为 0。这个坑实测踩到过。
    """
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return stripped == "" or stripped in ("[]", "{}", "null", "None")
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False
