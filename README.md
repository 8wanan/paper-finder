# paper-finder

把「逐篇读摘要筛论文」自动化掉的科研辅助工具。当前配置方向：**LLM 语音识别安全**。

> 从 Google Scholar 手动筛 → 到「每周读一张表」。

**状态：已实现并通过 113 个离线测试。** 核心功能零第三方依赖，arXiv 路径开箱即用。

---

## 这个工具解决什么问题

在 Google Scholar 里输关键词 + 年份，你会拿到几百到几万条结果，然后只能**一篇一篇点开看摘要**。
真正的瓶颈从来不是"找不到"，而是"**筛不完**"。

paper-finder 做三件事：

1. **全量普查（census）** — 你的领域其实不大。实测 LLM 语音安全的核心部分只有约 180 篇
   （越狱 18、音频提示注入 17、Whisper 攻击 50、语音助手 96），**可以一次性读完**。
2. **增量追踪（watch）** — 每周只告诉你「**这周新出现的**」。你说的"很快拿到论文"就是这个。
3. **一句式分诊** — 每篇给一条依据（L1 是关键词命中事实，L2 是带**摘要原文引用**的语义理由），
   你不需要点开就能决定读不读。

配套一个**本地账本**记录你的每一次判定，所以工具会随使用变准，而且永远不会重复推荐你否掉的论文。

---

## 快速开始

```bash
cd paper-finder
cp .env.example .env          # 填一个邮箱即可；arXiv 无需 API key
python papers.py doctor       # 自检环境与数据源
python papers.py import-census # 导入自带的 1,196 篇冷启动账本
python papers.py census        # 全量普查 Tier A（约 14 个 arXiv 请求，1–2 分钟）
python papers.py brief         # 生成 Markdown 简报
```

然后打开 `briefs/brief-<N>.md`，在最后一列把 `[ ]` 改成 `[k]`(读) / `[m]`(稍后) / `[d]`(丢弃)，
再回写：

```bash
python papers.py decide briefs/brief-4.md
```

**每周的常规用法**：

```bash
python papers.py watch --days 7     # 只看这周新增的
python papers.py brief
```

**评测（改完评分规则必做）**：

```bash
cp tests/golden.example.yaml tests/golden.yaml   # 先补全 25 正 + 25 负
python papers.py eval
```

---

## 命令一览

| 命令 | 作用 |
|---|---|
| `doctor` | 自检环境、配置、数据源连通性 |
| `import-census` | 导入冷启动账本 `data/seed-census.json` |
| `seed-sync` | 按 arXiv ID 精确抓取 profile 里的种子论文（补经典文献的召回漏洞） |
| `census` | Tier A 全量普查 |
| `search "<query>"` | 临时检索一个具体问题 |
| `watch --days N` | 增量追踪 |
| `rescore` | 不重新抓取，只重算分数（改完 profile 用，覆盖全账本约 1 秒） |
| `brief` | 生成 Markdown 简报 |
| `decide <brief.md>` | 把简报里的判定回写账本；`--pid <id> --decision k` 可救回归档论文 |
| `show <id>` | 查看单篇详情与历史判定 |
| `eval` | 金标集评测（Recall@N / 必读核心召回 / Precision@10） |
| `stats` | 账本统计 |

所有命令都支持 `--json`，便于脚本与 agent 调用。

---

## 实测记录（设计依据，不是推测）

2026-10-16 在本机实测：

| 数据源 | 无 key 实测结果 |
|---|---|
| **arXiv** | ✅ 可用、免费、约 1.5s 响应 —— **架构以它为骨架** |
| OpenAlex | ❌ **429**，`Insufficient budget`，每日预算被同网络所有人共享；`Retry-After` 可达约 15 小时 |
| Semantic Scholar | ❌ **429** |
| Crossref | ✅ 可用，**但大量记录无摘要**（实测一条 Elsevier 文章 abstract 为空），故默认关闭 |

**关键结论：核心功能不依赖任何 API key。** 完整实测数据与领域规模测量见
[`DESIGN.md`](DESIGN.md) §0。

---

## 关键设计取舍

1. **召回靠并集，分类靠打分。** 实测 `cat:eess.AS AND cat:cs.R` 只召回 131 篇，
   而纯关键词召回 273 篇 —— 用分类交集做召回门槛会漏掉一半。
2. **术语拆成「威胁轴 × 主题轴」。** 两者都命中才得高分。否则
   `Continuous Audio Thinking for Large Audio-Language Models` 这类只谈模型不谈安全的论文
   会因标题命中 `large audio-language model` 而混进"强烈建议读"。
3. **无摘要的论文不让 LLM 猜。** 直接进「需人工确认」桶。给模型一个标题让它判断相关性必然幻觉。
4. **账本是资产，不是缓存。** `decisions` 表积累的判定是唯一真值来源；没有它，所有"评分优化"都是盲调。
5. **零第三方依赖。** 只用标准库（连 YAML 都是自带的子集解析器）。这既是安全选择
   （没有安装脚本执行任意代码的风险），也保证三年后还能跑。

---

## 明确不做的事

- ❌ 不抓取 Google Scholar（无 API、反爬、违反 ToS）
- ❌ 不下载分发版权 PDF（只给开放获取链接）
- ❌ 不做 Web 界面（推迟到确实需要分享时）
- ❌ 不追求覆盖所有学科

---

## 文档

| 文档 | 内容 |
|---|---|
| [`DESIGN.md`](DESIGN.md) | 主设计文档：实测依据、架构、数据库 schema、路线图、风险清单，以及**实现阶段的修正记录** |
| [`SECURITY.md`](SECURITY.md) | **安全审查**：凭据、prompt injection（论文摘要是不可信输入）、发布检查清单、备份 |
| [`references/queries.md`](references/queries.md) | 经实测的查询清单、术语扩展表、术语漂移记录 |
| [`references/scoring.md`](references/scoring.md) | 评分公式（含双轴门控模型）、LLM rubric、标定与调参方法 |
| [`SKILL.md`](SKILL.md) | Skill 触发条件与 CLI 调用契约（供 agent 使用） |
| [`profiles/llm-speech-security.yaml`](profiles/llm-speech-security.yaml) | 领域配置 |

---

## 测试

```bash
python -m unittest discover -s tests -t .
# Ran 113 tests ... OK
```

**全部离线**（只依赖标准库，不打网络）。网络只出现在手工执行的流程里 ——
否则 arXiv 限速或 OpenAlex 429 会让 CI 随机变红，测试就失去意义了。

测试自带若干**回归测试**，每一个都对应一个实测踩到的真 bug，删掉就会复现：
YAML 解析器把同一行解析 5 次、`categories` 的 `"[]"` 被当成非空、`strong` 阈值算成不可达、
短 id 无法解析、未打分论文排序沉底、标题截断被误判为"未入库"。

---

## 许可与安全

- 本仓库**建议保持 private**，因为账本与提交历史会暴露你的研究方向。
- `.env` / API key / 数据库 / PDF **永不提交**（见 `.gitignore`）。
- 公开前请逐条过 [`SECURITY.md`](SECURITY.md) §5 的检查清单。
