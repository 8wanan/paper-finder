# paper-finder 详细设计文档

**领域**：LLM 语音识别安全（Speech / Audio LLM Security）
**作者**：8wanan
**版本**：v0.1 设计稿 · 2026-10-16
**状态**：设计完成，待实现

---

## 0. 设计依据：实测数据（先读这一节）

本文档的所有关键决策都基于 2026-10-16 在本机实测的数据，不是推测。如果你要改设计，请先复核这些数字。

### 0.1 数据源可用性实测

| 数据源 | 无 key 实测 | 有 key | 对本文档的影响 |
|---|---|---|---|
| **arXiv** | ✅ 200 / 1.5s / **免费无限** | 无需 | **作为主数据源**，核心功能不依赖任何 key |
| **OpenAlex** | ❌ **429**，`Insufficient budget`，`Retry-After: 53575` | 免费 $1/天，30 req/s | 无 key 时**不可用**；且 **cursor paging 必须有 key** |
| **Semantic Scholar** | ❌ **429** | 需申请 | 列为可选增强源 |
| **Crossref** | ✅ 200（polite pool） | 无需 | **但大量记录无摘要**（实测 `abstract_present=False`），只能做元数据补充 |
| **Unpaywall** | ✅ 可达，需真实邮箱 | 免费 | 用于取 OA 全文链接 |

**结论**：原方案"优先用 OpenAlex 或 Semantic Scholar"在无 key 情况下第一步就会失败。**新方案以 arXiv 为骨架，OpenAlex 为扩展，且必须实现指数退避 + 本地缓存。**

> OpenAlex key 可由 agent 一次 POST 申请（用真实邮箱，点邮件链接激活）：
> `POST https://api.openalex.org/api-keys` body `{"email":"...","agent":"Codex"}`

### 0.2 领域规模实测（arXiv，10 条查询，去重后）

| 查询主题 | 命中数 | 说明 |
|---|---|---|
| `audio/voice/spoken jailbreak` | **18** | 全量，未截断 |
| `prompt injection` × audio/speech/voice | **17** | 全量，未截断 |
| `Whisper` × attack/adversarial/jailbreak | **50** | 全量，未截断 |
| `voice assistant` × security/privacy/attack | **96** | 全量，未截断 |
| `speech LLM` × 安全类词 | 156 | 全量 |
| `speaker verification` × spoofing | 200+ | **已截断** |
| `deepfake` × audio/speech | 200+ | **已截断** |
| `adversarial` × ASR | 200+ | **已截断** |
| `cat:eess.AS AND cat:cs.CR` | 200+ | **已截断** |
| **并集去重** | **1,196** | 受截断影响，真实规模更大 |

**年份分布**（并集）：2026: 286 · 2025: 256 · 2024: 162 · 2023: 103 · 2022: 128 · ≤2021: 245

### 0.3 从实测得出的三条核心设计判断

**判断一：这个领域是双层结构，需要双模式工具。**

- **Tier A（核心新领域）**：LLM 语音安全——越狱 18、音频提示注入 17、Whisper 攻击 50、语音助手安全 96。**总量在几百篇量级，可以穷举。**
- **Tier B（成熟邻域）**：deepfake 音频检测、说话人验证反欺骗、ASR 对抗样本——每类 ≥200 且被截断，**无法穷举，必须排序**。

⇒ **Tier A 用「追踪模式」**（全量入账本 + 增量告警 + 手工读完），**Tier B 用「检索模式」**（关键词召回 + 相关度排序 + Top-N）。把两者混在一个"输出 Top 10"的接口里是原方案的病根。

**判断二：2025→2026 年产量从 256 涨到 286（10 个月，年化约 343），增长快。**

⇒ **增量追踪的价值高于一次性检索**。工具的核心产出应该是"**这周新出现了什么**"，而不是"给我一堆论文"。

**判断三：`cat:eess.AS AND cat:cs.CR`（131 篇，2024+）召回远低于纯关键词查询（如 `adversarial`×ASR 的 273 篇）。**

⇒ **分类交集不能单独用作召回手段**，它只能作为打分特征。召回必须靠**多查询并集**。这是很多人搭学术检索工具时最常犯的错。

**判断四：术语在快速漂移。** 同一个问题在文献里叫 `ASR adversarial example` → `audio adversarial attack` → `speech LLM jailbreak` → `audio-language model hijacking`。用单一关键词必然漏检。必须维护术语扩展表（见 `references/queries.md`）。

---

## 1. 领域模型：LLM 语音识别安全的文献结构

设计召回策略前必须先定义领域边界。本领域可拆成六条子脉络：

| # | 子脉络 | 典型术语 | 实测规模 | 层级 |
|---|---|---|---|---|
| 1 | **语音/音频 LLM 越狱** | audio jailbreak, voice jailbreak, spoken jailbreak, alignment bypass | ~18（全量） | Tier A |
| 2 | **音频模态提示注入 / 上下文劫持** | audio prompt injection, context-aggressive hijacking, cross-modal injection | ~17（全量） | Tier A |
| 3 | **ASR 定向对抗样本** | adversarial audio, over-the-air attack, psychoacoustic hiding, targeted transcription | 273+ | Tier B |
| 4 | **说话人验证欺骗与反欺骗** | ASVspoof, replay attack, voice cloning, SASV | 200+ | Tier B |
| 5 | **音频深伪检测** | deepfake audio detection, synthetic speech detection, ADD challenge | 449+ | Tier B |
| 6 | **语音助手 / 智能音箱安全** | DolphinAttack, CommanderSong, hidden voice command, skill squatting | 96 | 混合 |

**arXiv 分类分布**（用于打分，不用于召回）：`eess.AS`、`cs.SD`、`cs.CR`、`cs.CL`、`cs.LG`、`cs.MM`。

**关键 venue 白名单**：S&P、CCS、USENIX Security、NDSS、ICML、NeurIPS、ICLR、ACL、EMNLP、Interspeech、ICASSP、ASRU、TASLP、TDSC。

**种子论文**（冷启动引文滚雪球，arXiv ID 已逐条核实，完整版见 `profiles/llm-speech-security.yaml`）：

| 论文 | 年份 | arXiv ID |
|---|---|---|
| Carlini & Wagner, *Audio Adversarial Examples: Targeted Attacks on Speech-to-Text* | 2018 | `1801.01944` |
| Zhang et al., *DolphinAttack* | 2017 | `1708.09537` |
| Yuan et al., *CommanderSong* | 2018 | `1801.08535` |
| *Careless Whisper: Speech-to-Text Hallucination Harms* | 2024 | `2402.08021` |
| *Voice Jailbreak Attacks Against GPT-4o* | 2024 | `2405.19103` |
| *Muting Whisper: A Universal Acoustic Adversarial Attack* | 2024 | `2405.06134` |
| *StyleBreak*（LALM 对齐脆弱性） | 2025 | `2511.10692` |
| *Hijacking Large Audio-Language Models via Context-Agnostic…* | 2025 | 待核实 |
| ASVspoof / SASV challenge 系列 | 2019–2022 | 非单篇 |
| *Hidden Voice Commands* | 2016 | 非 arXiv（USENIX Sec'16） |

> ⚠️ **两个已踩的坑，直接印证了去重设计**：
> 1. DolphinAttack 在 arXiv 上的标题被**拼错**为 `DolphinAtack`（少一个 t）——按正确拼写做标题检索会 NOT FOUND，纯标题匹配必然漏检。
> 2. 搜 `CarelessWhisper`（连写）得 0 条，搜 `Careless Whisper`（带空格）得 2 条——**连写/分写差异会造成静默漏检**。
>
> 因此：去重必须走 **ID 指纹 + 归一化标题模糊匹配**，术语表必须覆盖拼写变体。

---

## 2. 系统架构

### 2.1 数据流

```
                     ┌─────────────── profiles/llm-speech-security.yaml ───────────────┐
                     │  查询模板 · 术语扩展表 · venue 白名单 · 种子论文 · 负向词          │
                     └────────────────────────────┬───────────────────────────────────┘
                                                  ▼
   ┌──────────────── 三路召回（并集，不是交集） ────────────────┐
   │ ① 关键词并集（arXiv / OpenAlex）   ② 分类交集（仅作补充）   ③ 引文滚雪球（OpenAlex）│
   └────────────────────────────┬──────────────────────────────┘
                                ▼
                   归一化 Normalize（统一 Paper 结构）
                                ▼
                   去重 Dedup（三级指纹 + union-find）
                                ▼
        ┌──────────── SQLite 账本（本工具的核心资产）────────────┐
        │  papers · aliases · runs · run_papers · decisions · llm_cache · citations │
        └────────────────────────────┬──────────────────────────────┘
                                     ▼
                   增量判定：is_new = (first_seen == 本次 run)
                                     ▼
   ┌────────── 三级筛选漏斗 ──────────┐
   │ L0 元数据过滤 → L1 规则打分 → L2 摘要 LLM 精排 → L3 全文（按需）│
   └────────────────────────────┬─────┘
                                ▼
              render.py → Markdown 简报（含「决策列」+「为什么相关」）
                                ▼
              你打 k/m/d → 回写 decisions 表 → 反馈闭环
```

### 2.2 模块职责

| 模块 | 职责 | 关键约束 |
|---|---|---|
| `papers.py` | **唯一 CLI 入口** | 子命令式，参数与结果均走 JSON，便于 agent 调用 |
| `papers/sources/*.py` | 各源适配，输出**统一 Paper 结构** | 每个源必须独立可禁用；失败不得中断整体 |
| `papers/normalize.py` | 指纹计算、摘要还原、字段清洗 | OpenAlex 的 `abstract_inverted_index` 在此还原 |
| `papers/store.py` | SQLite 读写、增量判定、迁移 | 所有写操作走事务；`first_seen` 只写一次 |
| `papers/screen.py` | L0/L1/L2 三级漏斗 | L2 必须查 `llm_cache`，命中则零成本 |
| `papers/render.py` | Markdown 简报生成 | 输出必须可直接读，不需要二次加工 |
| `profiles/*.yaml` | 领域配置（可多领域） | 换领域只改配置，不改代码 |
| `tests/` | 离线回归测试 | **用 fixtures 回放，绝不打网络** |

### 2.3 统一 Paper 结构（各源适配器的输出契约）

```json
{
  "doi": null,
  "arxiv_id": null,
  "title": "",
  "abstract": null,
  "year": null,
  "venue": null,
  "authors": [],
  "categories": [],
  "citation_count": null,
  "oa_url": null,
  "sources": ["arxiv"],
  "raw_ids": { "arxiv": "2401.12345", "openalex": "W123", "s2": "abc" }
}
```

### 2.4 去重：三级指纹 + union-find

1. 有 DOI → `doi:<小写，去 https://doi.org/ 前缀>`
2. 无 DOI 但有 arXiv ID → `arxiv:<去版本号 vN>`
3. 都没有 → `title:<sha1(norm_title | 首作者姓 | year)>`

`norm_title` = 小写 → 去标点 → 折叠空白 → 去 LaTeX 残留。

**跨源合并走 union-find**，把所有别名写入 `aliases` 表。**必须处理预印本↔正式版**（arXiv 版与 ACL/NeurIPS 版 DOI 不同甚至无 DOI——实测 arXiv 结果 `doi=-` 是常态）。

### 2.5 SQLite Schema

```sql
CREATE TABLE papers (
  pid            TEXT PRIMARY KEY,   -- 规范指纹
  doi            TEXT, arxiv_id TEXT,
  title          TEXT NOT NULL, norm_title TEXT NOT NULL,
  abstract       TEXT,               -- 已还原的纯文本
  year           INTEGER, venue TEXT,
  authors        TEXT, categories TEXT,      -- JSON array
  citation_count INTEGER, oa_url TEXT,
  first_seen     TEXT NOT NULL,      -- 首次进入账本（增量判定的依据）
  last_seen      TEXT NOT NULL,
  sources        TEXT                -- JSON array
);
CREATE INDEX idx_papers_norm_title ON papers(norm_title);
CREATE INDEX idx_papers_year       ON papers(year);
CREATE INDEX idx_papers_first_seen ON papers(first_seen);

CREATE TABLE aliases (              -- 跨源 ID 映射，去重的关键
  pid TEXT NOT NULL, id_type TEXT NOT NULL, id_val TEXT NOT NULL,
  PRIMARY KEY (id_type, id_val)
);

CREATE TABLE runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  started TEXT, finished TEXT,
  mode TEXT,                        -- census | search
  profile TEXT, queries TEXT,       -- JSON
  n_fetched INTEGER, n_new INTEGER, n_new_strong INTEGER
);

CREATE TABLE run_papers (           -- 实现「本轮新增」
  run_id INTEGER, pid TEXT, is_new INTEGER,
  l1 REAL, l2 REAL, llm_json TEXT,
  PRIMARY KEY (run_id, pid)
);

CREATE TABLE decisions (            -- 你的人工判定 = 唯一真值
  pid TEXT PRIMARY KEY,
  decision TEXT NOT NULL,           -- keep | maybe | drop
  reason TEXT, tags TEXT, decided_at TEXT
);

CREATE TABLE llm_cache (            -- 永不重复付费
  pid TEXT NOT NULL, prompt_hash TEXT NOT NULL, model TEXT NOT NULL,
  response TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (pid, prompt_hash, model)
);

CREATE TABLE citations (            -- 引文滚雪球
  src_pid TEXT, dst_pid TEXT, src_source TEXT,
  PRIMARY KEY (src_pid, dst_pid, src_source)
);
```

### 2.6 必踩的坑：OpenAlex 摘要还原

OpenAlex 的摘要不是文本，是倒排索引。**必须还原**，否则 Tier B 候选全部无摘要：

```python
def reconstruct_abstract(inv):
    if not inv:
        return None
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))
```

---

## 3. 三路召回策略

**总原则：召回靠并集，分类靠打分。** 实测证明分类交集召回不足。

| 路 | 手段 | 覆盖 | 成本 | 优先级 |
|---|---|---|---|---|
| ① 关键词并集 | 术语扩展表展开成多条查询，结果取并集 | 主力 | 低 | **P0** |
| ② 分类交集 | `cat:eess.AS AND cat:cs.CR` 等 | 补充，可发现关键词漏网 | 低 | P1 |
| ③ 引文滚雪球 | 种子论文的**前向引证 + 后向参考文献** | 发现术语完全不同的同主题论文 | 中（需 OpenAlex key） | P2 |

**查询展开规则**：每个"概念槽位"用扩展表做笛卡尔积，但**限制总查询数 ≤ 40**（否则触发限流）。实际做法是**按子脉络各起 2–4 条查询**，而不是无脑全组合。

完整的经实测的查询清单见 `references/queries.md`。

---

## 4. 三级筛选漏斗

| 级别 | 输入量级 | 处理手段 | 输出量级 | 成本 |
|---|---|---|---|---|
| **L0 元数据** | 1,000–2,000 | 年份过滤、去重、负向词剔除（如 `music generation`、`speech synthesis` 但无安全词）、领域白名单 | ~600 | 0 |
| **L1 规则** | ~600 | 术语命中 + 分类 + venue + 时间衰减 + 汇合度（被几条独立查询命中） | ~120 | 0 |
| **L2 摘要 LLM** | ~120 | 摘要 → LLM 按统一 rubric 打分，**批量 + 缓存** | ~20 | 低（有缓存则趋近 0） |
| **L3 全文** | ~20 | 仅对你标 `keep` 的论文，拉 OA 全文做深读 | ~5 | 高（按需） |

**成本估算**：L2 每篇摘要约 600 token 输入 + 120 token 输出，120 篇 ≈ 8.6 万输入 token/次。用便宜模型单次成本可忽略；关键是**二次运行命中缓存后为 0**。若不做缓存，每天跑一次会浪费一个数量级。

**LLM 输出的 rubric**（每项 0–3 分，共 12 分，强制 JSON）：

| 维度 | 0 分 | 3 分 |
|---|---|---|
| **威胁模型明确性** | 泛泛而谈 | 明确攻击者能力/目标/约束 |
| **是否针对语音或多模态 LLM** | 纯文本模型 | 明确针对 speech/audio LLM |
| **方法可复现性** | 仅概念 | 有数据集、指标、代码 |
| **与我的课题相关度** | 无关的语音任务 | 直接可用于我的研究 |

**强制要求**：模型必须同时输出**一句从摘要中引用的具体理由**，禁止输出"本文研究了语音安全"这类废话。这是节省你阅读时间的核心机制。

---

## 5. 评分公式

评分规则详见 `references/scoring.md`。最终排序：

```
L1 = 0.35·S_lex + 0.15·S_cat + 0.10·S_venue + 0.15·S_rec + 0.10·S_seed + 0.15·S_hit
rank_score = 0.4·L1 + 0.6·(L2 / 12)
```

- `S_lex`：术语命中，标题权重 ×3，摘要 ×1，按术语权重加权
- `S_cat`：arXiv 分类命中 profile 定义的加权表
- `S_venue`：venue 白名单二值/分级
- `S_rec`：时间衰减 `exp(-Δt / τ)`，τ = 540 天
- `S_seed`：与种子论文的引文距离 / 共引强度
- `S_hit`：**汇合度**——被多少条独立查询命中（实测中这个信号很干净，因为 union 统计天然可得）

---

## 6. 简报输出格式（关键 UX）

这是整个工具的**真正产品界面**。设计目标：**你只需读这一张表，不需要点开任何论文**。

```markdown
# 语音安全论文简报 · 2026-10-16
新增 23 篇 / 候选 187 / 低成本筛除 164 / 累计账本 1,196

## 🔥 强烈建议读（L2 ≥ 8）
| # | 标题 | 年 | 源 | L1 | L2 | 为什么相关（引自摘要） | 决策 |
|---|------|----|----|----|----|----------------------|------|
| 1 | StyleBreak: ... | 26 | arXiv | .82 | 9 | 首次在 LALM 上把**说话风格**作为对齐绕过通道，报告 6 个模型的 ASR | [ ] |
| 2 | ... | | | | | | [ ] |

## 📌 值得扫一眼（5 ≤ L2 < 8）
（同上表格，只保留前 20）

## 🆕 本轮新出现但无摘要（需人工确认，共 3 篇）
- [标题](链接) — arXiv 刚上线，尚无摘要

## 🗄 已归档（L2 < 5，共 164 篇，默认折叠）
```

**决策列填写规则**（你只需在方括号里写一个字母）：

- `k` = keep（进入 L3 深读队列）
- `m` = maybe（下次再说）
- `d` = drop（永久下沉，永不再提）
- 附注可选：`k 综述引用` → 写入 `reason`

回写命令（设计契约）：`papers.py decide brief.md`，解析简报并更新 `decisions` 表。**这一步是闭环的关键**——没有它，第二阶段的所有"评分优化"都是盲调。

---

## 7. 两种运行模式

| 模式 | 命令（设计契约） | 用途 | 频率 |
|---|---|---|---|
| **search** | `papers.py search "audio jailbreak" --since 2024-01-01 --top 20` | 临时查一个具体问题 | 随手 |
| **census** | `papers.py census --profile llm-speech-security` | 全量穷举 Tier A，把领域一次性读完 | 初始化一次 |
| **watch** | `papers.py watch --profile llm-speech-security --days 7` | 增量：只看这周新出现的 | 每周 |

**watch 模式是本工具对你原始诉求（"很快得到论文"）的直接回答**：跑完之后你看到的 20 篇，全都是你**没见过**的。

---

## 8. 验收标准（可测量，替换掉"输出 Top 10"）

### 8.1 金标集方法

从你已读过的 LLM 语音安全论文里挑 **25 篇**（必须包含 Tier A 全部核心论文），写进 `tests/golden.yaml`。同时挑 **25 篇明确不相关**的论文（如纯语音合成、纯音乐生成）作为负样本。

### 8.2 硬指标

| 指标 | 定义 | 目标 |
|---|---|---|
| **Recall@50** | 金标 25 篇中有多少进入 L1 的前 50 | **≥ 0.92** |
| **Precision@10** | 最终简报前 10 篇中真相关比例 | **≥ 0.70** |
| **新增正确率** | watch 模式报"新增"的论文中，确实是首次出现 | **100%** |
| **二次运行成本** | 相同查询重跑的 LLM 调用次数 | **0** |
| **端到端耗时** | 关键词 → 简报，100 篇量级 | **< 60s** |
| **离线可测** | `pytest` 在不联网时全绿 | **必须** |

### 8.3 反指标（明确不追求的）

- ❌ 不追求覆盖所有学科
- ❌ 不追求简历级的"论文数量"
- ❌ 不追求 Web 界面

---

## 9. 实施路线图

### Phase 0 — 解锁（半天）
- [ ] 申请 OpenAlex key（`POST /api-keys`，点邮件激活）
- [ ] 建 **private** 仓库 + `.gitignore`（`.env` / `*.db` / `pdfs/`）
- [ ] 实现 `sources/arxiv.py` + `store.py` —— **arXiv 无需 key，此步即可端到端跑通**
- [ ] 导入 `data/seed-census.json`（已实测抓取的 1,196 篇）作为冷启动账本

### Phase 1 — MVP（1–2 天）
- [ ] `papers.py census` 跑通，Tier A 全量入库
- [ ] L0 + L1 漏斗 + 简报导出（含决策列）
- [ ] 决策回写 `decide` 子命令
- [ ] **验收**：census 一条命令出简报；重跑只显示新增；金标 Recall@50 ≥ 0.92

### Phase 2 — 精排与工程化（3–5 天）
- [ ] OpenAlex 源接入（含摘要还原）+ 指数退避
- [ ] L2 LLM 精排 + `llm_cache`
- [ ] 引文滚雪球
- [ ] Unpaywall 取 OA 全文链接
- [ ] 离线 fixtures 测试 + GitHub Actions CI
- [ ] **验收**：金标 Precision@10 ≥ 0.70；二次运行 LLM 调用为 0；CI 全绿

### Phase 3 — 扩展（可选）
- [ ] `watch` 周报模式 + GitHub Actions 定时（key 放 Secrets）
- [ ] 研究空白分析（引用网络里的低密度簇）
- [ ] 多 profile 支持（第二研究方向）

---

## 10. 风险与坑清单

| # | 风险 | 缓解 |
|---|---|---|
| 1 | **OpenAlex 无 key = 429** | arXiv 为骨架；key 走环境变量；指数退避 |
| 2 | **Crossref 无摘要** | 只做元数据补充，绝不作为摘要来源 |
| 3 | **OpenAlex 摘要为倒排索引** | `reconstruct_abstract()`，Phase 2 必须实现 |
| 4 | **arXiv 限流（建议 ≥3s/请求）** | 查询间 sleep 3.2s；分页不超过必要深度 |
| 5 | **预印本↔正式版重复** | 三级指纹 + union-find + aliases 表 |
| 5b | **标题拼错 / 连写分写差异导致静默漏检**（实测：`DolphinAtack` 拼错、`CarelessWhisper` vs `Careless Whisper`） | 术语变体表；标题模糊匹配（token 集合 Jaccard ≥ 0.9）；**绝不依赖精确标题相等** |
| 6 | **术语漂移导致漏检** | 维护 `references/queries.md`，每季度复查一次 |
| 7 | **LLM 重复付费** | `llm_cache` 按 `(pid, prompt_hash, model)` 缓存 |
| 8 | **抓 Google Scholar 被封** | **明确不做**。用 OpenAlex/S2 复刻覆盖面 |
| 9 | **凭证泄露 / 版权** | `.env` 入 `.gitignore`；**不提交 PDF 全文**；初期 private repo |
| 10 | **公开仓库暴露研究方向** | 先 private；确认要公开时再单独评估 |

---

## 11. 目录结构

```
paper-finder/
├── DESIGN.md                 # 本文档
├── SKILL.md                  # Skill 触发条件与 CLI 调用契约
├── README.md                 # 面向使用者的说明
├── papers.py                 # 唯一 CLI 入口（待实现）
├── papers/                   # 实现包（待实现）
│   ├── sources/{arxiv,openalex,s2,crossref}.py
│   ├── normalize.py          # 指纹 + 摘要还原
│   ├── store.py              # SQLite
│   ├── screen.py             # 三级漏斗
│   └── render.py             # 简报
├── profiles/
│   └── llm-speech-security.yaml
├── references/
│   ├── queries.md            # 经实测的查询清单与术语扩展表
│   └── scoring.md            # 评分规则
├── data/
│   └── seed-census.json      # 实测抓取的 1,196 篇冷启动账本
├── tests/
│   ├── fixtures/             # 离线回放数据
│   └── golden.yaml           # 金标集（待你填写）
├── .gitignore
├── .env.example
└── requirements.txt
```

---

## 12. 一句话总结

原方案的骨架（GitHub 托管 + 本地运行 + API 取数 + 分阶段迭代）是对的，但三处会让你卡死：**① 无 key 的 OpenAlex/S2 直接 429；② "输出 Top 10" 复制了 Google Scholar 的病；③ 缺持久化判定账本，工具永远停在玩具阶段。**

而实测又揭示了第四个、也是最重要的修正：**你这个领域是双层的**——LLM 语音安全本身只有几百篇、**可以穷举**，所以正确的形态不是"检索排序器"，而是「**全量账本 + 每周增量告警 + 一句式相关理由**」。把你从"逐篇读摘要"里解放出来的，是**增量**和**理由**，不是**排序**。

---

## 13. 实现阶段的修正记录（v0.1 → 已实现）

上面 §1–§12 是**设计稿**。真正写代码并跑真实数据后，发现设计里有错误、也有遗漏。
这一节如实记下来 —— 一份"看起来完美"的设计文档价值远低于一份记录了哪里错的文档。

**状态：已实现，113 个离线测试全部通过。**

### 13.1 设计层面的错误（不是编码 bug，是想法错了）

| # | 原设计 | 错在哪 | 修正 |
|---|---|---|---|
| 1 | 单一术语权重表（`term_weights`） | **主题词与威胁词混在一起**。`Continuous Audio Thinking for Large Audio-Language Models` 只谈模型不谈安全，却因标题命中 `large audio-language model` 把 `S_lex` 打满。实测 347 篇候选里 `strong` 占了 57%，归档率只有 12% | 拆成**威胁轴 × 主题轴**门控：`S_lex = S_threat × (0.5 + 0.5·S_topic)`。62 篇被归零，`strong` 降到 10.7% |
| 2 | 术语命中按出现次数累加 | 摘要里说 9 次 `jailbreak` 就把威胁轴打满。**重复不是信号** | 改为**存在性计分**（每术语只算首次出现） |
| 3 | 未提及摘要注入风险 | L2 把**网上抓来的摘要原文**拼进 prompt。任何人投一篇摘要里写着 `Ignore all previous instructions…` 的论文就能操纵排序 | 加注意力边界（`<paper>` 标签 + system 声明）+ `detect_injection()` 启发式检测，命中则跳过 L2 进"需人工确认" |
| 4 | 未提及种子论文会被埋 | DolphinAttack 自然得分只有 0.40、排 117 名，落在 `must_recall` 的 top-50 之外（老论文衰减 + 标题被拼错 + 无摘要三重叠加） | 加 `seed_score_floor`（默认 0.65），它升到第 42 名 |
| 5 | 原设计假设用 PyYAML / requests / pytest | 实测环境**没有 PyYAML**，且沙箱不允许往解释器目录装包 | 改为**零第三方依赖**：自带 YAML 子集解析器、用 `urllib`、用 `unittest` |
| 6 | `rescore` 只覆盖单次运行 | 冷启动导入的 1,196 篇没有分数，而 SQLite 把 `l1 IS NULL` 排最后 → 金标集评测把相关老论文算成"第 1158 名"，得出**错误的低召回结论** | `rescore` 默认覆盖**整个账本**（纯计算，1,200 篇约 1 秒） |
| 7 | 未提及按关键词取不回经典文献 | 2016–2018 的奠基论文因相关度排序靠后而**根本取不回来**（实测 DolphinAttack、Hidden Voice Commands 缺失），`must_recall` 只有 3/8 通过 | 新增 `seed-sync` 子命令，按 arXiv `id_list` 精确抓取种子；核心召回升到 7/8 |

### 13.2 编码阶段发现的真 bug（每个都有回归测试守住）

| # | Bug | 症状 | 根因 |
|---|---|---|---|
| 1 | YAML 解析器重复解析 | 金标集里 1 个条目被读成 **5 个** | `_peek()` 只做只读预读、不前进游标。当"序列项内部夹注释、后面跟下一个序列项"时，`self.lines[self.pos]` 覆盖到了注释行。修法：`_peek` 永久消费空行与注释 |
| 2 | `categories` 永远回填不进去 | `S_cat` 恒为 0 | JSON 列的空值是字符串 `"[]"`，而 `_is_blank()` 只判空字符串，把 `"[]"` 当成"有值" |
| 3 | `strong` 阈值不可达 | 强推桶永远为空 | 阈值算成 `l2_strong/12 + 0.35 = 1.017`，而 L1 实际可达上限约 0.88。修法：改用标定过的 `l1_strong` / `l1_maybe` |
| 4 | 短 id 无法解析 | 简报 `decide --pid` 与救回归档论文全部失效 | 短 id 是 pid 去掉 `arxiv:` 前缀后的**后缀**，而 `resolve_pid` 用的是 `pid LIKE 'token%'` 前缀匹配 |
| 5 | 标题截断被误判"未入库" | `StyleBreak` 明明排第 1 却被报"未入库" | 金标集写的是截断标题，Jaccard 只有 0.71。修法：`is_same_paper` 增加"交集/较短一方 ≥ 0.9"的截断容错规则 |
| 6 | `must_recall` 匹配不到 | DolphinAttack 因 arXiv 标题被**拼错**成 `DolphinAtack` 而匹配失败 | `must_recall` 只传标题、用不上 `positives` 里的 `arxiv_id`。修法：先回连到 positives 再匹配 |
| 7 | 词汇缺口 | `Careless Whisper`（转写幻觉危害）排 701 名 | 标题里只有 `Hallucination` / `Harms`，威胁轴得 0 分。补入 `hallucination`/`misinformation`/`harm`/`toxicity` 后升到第 28 名 |
| 8 | 决策列无法写备注 | `[k] 综述必引` 解析失败 | 正则要求整格严格匹配。修法：允许尾随文本并写入 `decisions.reason` |
| 9 | `doctor` 只显示失败项 | 通过的检查全不显示（自检失去意义） | 日志行被误缩进到 `if not ok:` 里 |

### 13.3 环境层面的实测约束（写下来免得重复踩）

- **`tempfile.mkdtemp()` 创建的目录在本沙箱下完全不可写** —— 连 `open(...,"w")` 都
  PermissionError，SQLite 报 `unable to open database file`。而 `os.makedirs()` 创建的
  目录正常可写。**两者权限位都是 0777，不是权限位问题。** 测试因此改用
  `os.makedirs` 手工生成唯一目录。
- **系统临时目录可能不允许创建 SQLite 文件**，一坏就是几十个测试同时报错，
  把真实失败淹没。测试改用仓库内 `tests/.tmp/`（已 gitignore）。
- **`TemporaryDirectory` 的退出终结器不遵守 `ignore_cleanup_errors`**，
  Windows 上会因 SQLite 的 `-wal`/`-shm` 抛权限错误。
- **OpenAlex key 不应放在 URL 查询参数里**：URL 会被网关、代理和访问日志整条记录。
  已改为 `Authorization: Bearer` 头。

### 13.4 与原设计的其他偏离

- **CLI 位置**：原设计是仓库根的 `papers.py`。实际发现它与包 `papers/` **同名**，
  导致 `import papers` 的解析结果依赖解释器查找顺序（CPython 优先命中目录包），
  外部代码无法复用 CLI。改为实现放在 `papers/cli.py`，`papers.py` 只留薄入口。
- **新增 `decide --pid`**：原设计只能从简报回写，无法救回被自动归档的论文。
- **新增 `--max-per-bucket`**：search 模式一次可能带回上百篇候选，
  简报必须保持"5 分钟能读完"的长度。

### 13.5 当前实测指标

金标集为**模板**（9 正样本，非用户真实标注）：

```
Recall@50        0.78    （目标 0.92）
必读核心召回      7/8
Precision@10     1.00    （目标 0.70）
账本规模         1,198 篇，全部已打分
census 分桶       strong 37 / maybe 145 / archived 165
```

剩余 2 项缺失是**结构性**的，工具如实报告而非掩盖：
- `Hidden Voice Commands` 是 USENIX Security 2016，**不在 arXiv 上** → 需要 OpenAlex/Crossref
- `ASVspoof / SASV Challenge` **不是单篇论文** → 金标集条目本身需要改写

要达到 Recall@50 ≥ 0.92，下一步是接入 OpenAlex key 覆盖非 arXiv 文献，
以及让用户补全真实金标集（25 正 + 25 负）。
