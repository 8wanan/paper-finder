"""paper-finder —— 面向科研文献的分诊工具。

设计原则（见 DESIGN.md）：
  1. 零第三方依赖：只用 Python 标准库。环境越简单，工具活得越久。
  2. 账本是资产：papers/aliases/decisions 表是长期价值所在，不是缓存。
  3. 成本分级：L0/L1 用关键词与摘要（零成本），L2 才动用 LLM，且必须有缓存。
"""

__version__ = "0.1.0"
