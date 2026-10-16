# 查询清单与术语扩展表 · LLM 语音识别安全

> 下面每一条查询的命中数均为 **2026-10-16 在本机实测**（arXiv API，去重前）。标注"已截断"表示实际数量超过所列值。
> **维护要求：每季度复查一次**，因为该领域术语漂移很快（见文末）。

---

## 1. 分层查询清单

### 第一层：Tier A 核心（全量穷举，量小，必须一条不漏）

| ID | 查询 | 实测命中 | 用途 |
|---|---|---|---|
| A1 | `all:"audio jailbreak" OR all:"voice jailbreak" OR all:"spoken jailbreak"` | **18** | 语音 LLM 越狱 |
| A2 | `all:"prompt injection" AND (all:"audio" OR all:"speech" OR all:"voice")` | **17** | 音频模态提示注入 |
| A3 | `all:"Whisper" AND (all:"attack" OR all:"adversarial" OR all:"jailbreak")` | **50** | Whisper 专项攻击 |
| A4 | `abs:"voice assistant" AND (abs:"security" OR abs:"privacy" OR abs:"attack")` | **96** | 语音助手/智能音箱安全 |

**A 层合计约 180 篇（去重后更少）——这就是"可以穷举"的依据。**

### 第二层：Tier B 邻域（需排序，不能穷举）

| ID | 查询 | 实测命中 | 用途 |
|---|---|---|---|
| B1 | `all:"speech" AND all:"large language model" AND (all:"jailbreak" OR all:"attack" OR all:"safety" OR all:"adversarial")` | 156 | 语音 LLM 安全泛查 |
| B2 | `all:"audio-language model" OR all:"large audio-language model" OR all:"speech language model"` | 200+（已截断） | LALM 全景 |
| B3 | `abs:"adversarial" AND (abs:"speech recognition" OR abs:"automatic speech recognition" OR abs:"ASR")` | 273 | ASR 对抗样本 |
| B4 | `abs:"speaker verification" AND (abs:"spoofing" OR abs:"adversarial" OR abs:"attack")` | 200+（已截断） | 说话人验证攻击 |
| B5 | `abs:"deepfake" AND (abs:"audio" OR abs:"speech")` | 449 | 音频深伪检测 |

### 第三层：分类交集（**仅作补充，不得单独用于召回**）

| ID | 查询 | 实测命中 | 说明 |
|---|---|---|---|
| C1 | `cat:eess.AS AND cat:cs.CR` | 131（2024+）/ 200+（全时段，已截断） | 音频×安全 |
| C2 | `cat:cs.SD AND cat:cs.CR` | 192（2024+） | 声音×安全 |

> ⚠️ **实测反例**：C1 在 2024+ 只召回 131 篇，而纯关键词的 B3 召回 273 篇。**分类交集召回明显不足**，因为大量音频安全论文只挂 `cs.CR`+`cs.LG`，或只挂 `eess.AS` 不挂 `cs.CR`。**结论：分类只能作为打分特征，不能作为召回门槛。**

---

## 2. 术语扩展表（查询展开用）

规则：**每个槽位任取一个词，与其它槽位做有限组合**，总查询数控制在 ≤ 40 条。

### 槽位 1：模态 / 载体
`audio` · `speech` · `voice` · `spoken` · `acoustic` · `audio-language` · `speech-to-text` · `ASR` · `automatic speech recognition`

### 槽位 2：攻击 / 威胁
`jailbreak` · `adversarial` · `attack` · `prompt injection` · `hijack` · `manipulation` · `backdoor` · `poisoning` · `spoofing` · `impersonation`

### 槽位 3：模型 / 系统
`large language model` · `LLM` · `large audio-language model` · `LALM` · `speech language model` · `multimodal LLM` · `voice assistant` · `smart speaker` · `Whisper` · `GPT-4o` · `speaker verification` · `ASR system`

### 槽位 4：安全属性（用于限定检索范围）
`security` · `privacy` · `safety` · `alignment` · `robustness` · `trustworthiness`

**组合示例（3 条，覆盖术语漂移的三个世代）**：

```
# 世代 1（2017–2020）：经典音频对抗
abs:"adversarial" AND abs:"speech recognition" AND (abs:"over-the-air" OR abs:"imperceptible" OR abs:"psychoacoustic")

# 世代 2（2023–2024）：Whisper / ASR 模型攻击
all:"Whisper" AND (all:"jailbreak" OR all:"transcription" OR all:"hallucination")

# 世代 3（2024–今）：语音/音频 LLM 越狱与注入
(all:"audio" OR all:"speech" OR all:"voice") AND (all:"jailbreak" OR all:"prompt injection") AND (all:"large language model" OR all:"LALM" OR all:"multimodal")
```

---

## 3. 负向词（L0 剔除，降噪用）

出现以下词**且不含任何安全类词**时降权或剔除：

`music generation` · `text-to-speech synthesis`（纯合成）· `speech enhancement` · `emotion recognition` · `speaker diarization` · `keyword spotting` · `code-switching`（纯语言学）· `hearing aid` · `clinical`/`pathology`（医学语音）

> 注意：`deepfake detection` 与 `speech synthesis` 高度重叠但**属于本领域**，因为合成是攻击手段。负向规则必须写成"**负向词 AND NOT 安全词**"的合取，不能单看负向词。

---

## 4. Venue 白名单（打分用）

**顶级安全会议**：IEEE S&P · ACM CCS · USENIX Security · NDSS · EuroS&P
**机器学习**：NeurIPS · ICML · ICLR · AAAI
**NLP**：ACL · EMNLP · NAACL · TACL
**语音**：Interspeech · ICASSP · ASRU · SLT · TASLP · Speech Communication

---

## 5. 种子论文（引文滚雪球起点）

冷启动用。建议你在 Phase 2 前人工确认并补充到 `profiles/llm-speech-security.yaml`。

| 论文 | 年份 | arXiv ID（已核实） | 脉络 |
|---|---|---|---|
| DolphinAttack（Zhang et al.） | 2017 | `1708.09537` | 语音助手 / 超声波注入 |
| Hidden Voice Commands（Carlini et al.） | 2016 | 非 arXiv（USENIX Sec'16） | 语音助手 |
| CommanderSong（Yuan et al.） | 2018 | `1801.08535` | 语音助手 / 歌曲载体攻击 |
| Audio Adversarial Examples: Targeted Attacks on Speech-to-Text（Carlini & Wagner） | 2018 | `1801.01944` | ASR 对抗样本 |
| ASVspoof / SASV Challenge 系列 | 2019–2022 | 非单篇 | 说话人验证反欺骗 |
| Careless Whisper: Speech-to-Text Hallucination Harms | 2024 | `2402.08021` | **转写幻觉危害**（非越狱） |
| Voice Jailbreak Attacks Against GPT-4o | 2024 | `2405.19103` | 语音 LLM 越狱 |
| Muting Whisper: A Universal Acoustic Adversarial Attack… | 2024 | `2405.06134` | 通用声学对抗攻击 |
| StyleBreak: Revealing Alignment Vulnerabilities in LALM | 2025 | `2511.10692` | LALM 对齐脆弱性 |
| Hijacking Large Audio-Language Models via Context-Agnostic… | 2025 | 待核实 | 音频提示注入 |

> ⚠️ **踩坑记录**：DolphinAttack 在 arXiv 上的标题被**拼错**为 `DolphinAtack`（少一个 t）。用正确拼写做标题检索会返回 NOT FOUND。
> 这说明**检索与去重都不能依赖精确标题匹配**，必须走 ID 指纹 + 归一化标题模糊匹配。
>
> 另一个坑：搜 `CarelessWhisper`（连写）得 0 条，搜 `Careless Whisper`（带空格）得 2 条。**术语的连写/分写差异必须用变体表覆盖**，否则静默漏检。

---

## 6. 术语漂移记录（每季度复查）

同一个问题在不同时期的命名：

```
2016–2017  hidden voice command / inaudible voice command
2018–2020  audio adversarial example / over-the-air attack
2021–2023  speech adversarial robustness / anti-spoofing
2023–2024  ASR jailbreak / Whisper attack
2024–2025  audio jailbreak / voice jailbreak / spoken jailbreak
2024–2026  audio prompt injection / LALM alignment vulnerability / context hijacking
```

**漂移带来的实际风险**：只用 `adversarial audio` 检索，会漏掉全部 `jailbreak` 世代的论文；只用 `audio jailbreak`，会漏掉全部经典对抗样本脉络。**两代都要查，取并集。**

**建议的复查动作**：每季度把当期新论文的标题喂给 LLM，让它提取"当前高频术语"，与本文档比对，补充扩展表。
