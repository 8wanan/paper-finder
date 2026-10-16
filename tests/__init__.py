"""paper-finder 测试包。

全部测试必须**离线可跑**（unittest 标准库，零第三方依赖）。
网络只出现在手工执行的 smoke 流程里，绝不进测试套件 ——
否则 arXiv 限速或 OpenAlex 429 会让 CI 随机变红，测试就失去意义了。

运行方式（在仓库根目录）：
    python -m unittest discover -s tests -t .
"""
