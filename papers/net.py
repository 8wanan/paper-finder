"""HTTP 客户端 —— 带限速、退避与友好错误。

实测约束（2026-10-16，见 DESIGN.md §0.1）：
  - arXiv：免费、稳定，但官方建议请求间隔 >= 3 秒。
  - OpenAlex：无 key 直接 429，`Retry-After` 可达 53575 秒（约 15 小时）。
    遇到这种量级的 Retry-After 必须**立刻放弃**而不是傻等。
  - Semantic Scholar：无 key 同样 429。
  - Crossref：需要 User-Agent 里带真实邮箱才进 polite pool。
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["HttpClient", "HttpError", "RateLimited", "find_ca_bundle", "ensure_default_ca"]

# 超过这个秒数的 Retry-After 视为"今日额度耗尽"，不再重试
MAX_SENSIBLE_RETRY_AFTER = 300

# 回退用的 SSL 上下文（延迟构建并缓存）
_FALLBACK_SSL_CONTEXT = None
_FALLBACK_SOURCE = None
_FALLBACK_TRIED = False

# 常见 CA bundle 的相对路径。从解释器所在目录逐级向上拼接，
# 这样不用硬编码盘符或安装位置，换个环境仍然能命中。
_CA_BUNDLE_RELATIVE = (
    os.path.join("etc", "pki", "ca-trust", "extracted", "pem", "tls-ca-bundle.pem"),  # MSYS2 / Fedora
    os.path.join("etc", "ssl", "certs", "ca-bundle.crt"),                            # MSYS2 / CentOS
    os.path.join("etc", "ssl", "cert.pem"),                                          # Alpine / 通用
    os.path.join("ssl", "certs", "ca-bundle.crt"),                                   # Git for Windows / mingw
    os.path.join("Library", "ssl", "cacert.pem"),                                    # conda
    os.path.join("Lib", "site-packages", "certifi", "cacert.pem"),                   # pip 装的 certifi
)


def find_ca_bundle():
    """找一个可用的 CA bundle 文件，找不到返回 None。

    先看环境变量，再从解释器目录逐级向上找常见位置。
    刻意不硬编码盘符 —— 硬编码换个环境就失效。
    """
    explicit = os.environ.get("SSL_CERT_FILE")
    if explicit and os.path.exists(explicit):
        return explicit

    roots = []
    current = os.path.dirname(os.path.abspath(os.sys.executable))
    for _ in range(5):  # bin -> ucrt64 -> msys2 -> 盘根 ...
        roots.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    for root in roots:
        for relative in _CA_BUNDLE_RELATIVE:
            candidate = os.path.join(root, relative)
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                return candidate

    # 最后再看一眼 certifi（如果用户环境里恰好装了）
    try:
        import certifi  # noqa: PLC0415

        if os.path.isfile(certifi.where()):
            return certifi.where()
    except ImportError:
        pass
    return None


def ensure_default_ca():
    """默认 CA 路径不可用时，用找到的 bundle 修正 `SSL_CERT_FILE`。

    为什么要在**开始请求之前**就修，而不是等失败了再回退：实测 MSYS2 的
    Python 每个 HTTPS 请求都会先证书失败一次、再走回退重试 —— **请求数直接翻倍**。
    对 arXiv 这种限速 3.2 秒/次的 API 是白白的浪费，也可能撞上配额限制。

    `SSL_CERT_FILE` 是 OpenSSL 官方支持的配置方式（实测设了之后默认上下文即可
    正常校验，一次成功），所以这里直接设它，而不是每次请求都手工构造 context。

    返回修正用的 bundle 路径；默认 CA 本来可用时返回 None。
    """
    paths = ssl.get_default_verify_paths()
    if paths.cafile and os.path.exists(paths.cafile):
        return None
    if paths.capath and os.path.isdir(paths.capath):
        try:
            if os.listdir(paths.capath):
                return None
        except OSError:
            pass
    if os.environ.get("SSL_CERT_FILE"):
        return None

    bundle = find_ca_bundle()
    if bundle:
        os.environ["SSL_CERT_FILE"] = bundle
    return bundle


def _fallback_ssl_context():
    """构建回退 SSL 上下文，返回 (context, 来源说明)；都失败返回 (None, 原因)。

    这是 `ensure_default_ca()` 之后的**第二道保险**：万一设了 SSL_CERT_FILE
    仍不生效（企业代理、自签根证书等），还有这两条路。两条都要，因为
    **不同 Python 发行版的能力不一样**（实测结论）：

      1. `ssl.enum_certificates` —— 读 Windows 系统证书存储。
         原生 Windows 版 CPython 有；**MSYS2 版没有**（hasattr 直接 False）。
      2. 磁盘上的 CA bundle 文件。MSYS2 虽缺 enum_certificates，
         但 `etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem` 存在且可用。

    只做第 1 条会在 MSYS2 上失效（踩过），只做第 2 条会漏掉系统存储里的
    企业根证书。所以两条都试。
    """
    global _FALLBACK_SSL_CONTEXT, _FALLBACK_SOURCE, _FALLBACK_TRIED
    if _FALLBACK_TRIED:
        return _FALLBACK_SSL_CONTEXT, _FALLBACK_SOURCE

    _FALLBACK_TRIED = True
    context = ssl.create_default_context()

    # 路径 1：Windows 证书存储
    if hasattr(ssl, "enum_certificates"):
        loaded = 0
        for storename in ("ROOT", "CA"):
            try:
                certificates = ssl.enum_certificates(storename)
            except Exception:  # noqa: BLE001 - 某些权限下不可用
                continue
            for cert_bytes, encoding, _trust in certificates:
                if encoding != "x509_asn":
                    continue
                try:
                    context.load_verify_locations(cadata=cert_bytes)
                    loaded += 1
                except Exception:  # noqa: BLE001 - 单张坏证书不该中断
                    continue
        if loaded:
            _FALLBACK_SSL_CONTEXT = context
            _FALLBACK_SOURCE = f"Windows 证书存储（{loaded} 张）"
            return _FALLBACK_SSL_CONTEXT, _FALLBACK_SOURCE

    # 路径 2：磁盘上的 CA bundle
    bundle = find_ca_bundle()
    if bundle:
        try:
            context.load_verify_locations(cafile=bundle)
            _FALLBACK_SSL_CONTEXT = context
            _FALLBACK_SOURCE = bundle
            return _FALLBACK_SSL_CONTEXT, _FALLBACK_SOURCE
        except Exception as exc:  # noqa: BLE001
            _FALLBACK_SOURCE = f"找到 {bundle} 但加载失败：{exc}"
            return None, _FALLBACK_SOURCE

    _FALLBACK_SOURCE = "既无 Windows 证书存储，也未找到 CA bundle 文件"
    return None, _FALLBACK_SOURCE


def _is_cert_error(exc: BaseException) -> bool:
    """判断异常链里是否是证书验证失败。

    遍历异常链时要防住非异常对象：实测 `URLError.reason` 链的末端会是一个
    纯字符串（"CERTIFICATE_VERIFY_FAILED"），对它取 `.__cause__` 会抛
    AttributeError。所以每一步都做类型检查。
    """
    seen = set()
    current: object = exc
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if isinstance(current, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(current):
            return True
        nxt = getattr(current, "reason", None)
        # URLError 的 reason 有时直接是字符串（实测链末端就是
        # "CERTIFICATE_VERIFY_FAILED"），这种也得算证书错误。
        if isinstance(nxt, str) and "CERTIFICATE_VERIFY_FAILED" in nxt:
            return True
        if not isinstance(nxt, BaseException):
            nxt = None
        current = (
            nxt
            or getattr(current, "__cause__", None)
            or getattr(current, "__context__", None)
        )
    return False


def _cert_error_hint(detail: str = "") -> str:
    """证书失败时给出可操作的提示，而不是甩一句 OpenSSL 错误。"""
    lines = [
        "SSL 证书验证失败：当前 Python 找不到可用的 CA 证书。",
        f"  解释器：{os.sys.executable}",
        f"  回退也失败：{detail or '未知'}",
        "修复方式（任选其一）：",
        "  1. 指定系统 CA bundle（永久生效）：",
        "     PowerShell:  $env:SSL_CERT_FILE='<你的 ca-bundle.crt 路径>'",
        "  2. 换一个带完整证书的 Python 解释器",
    ]
    return "\n".join(lines)


class HttpError(RuntimeError):
    """网络请求最终失败。"""

    def __init__(self, message, status=None, url=None):
        super().__init__(message)
        self.status = status
        self.url = url


class RateLimited(HttpError):
    """被限流，且等待不划算（如每日预算耗尽）。"""

    def __init__(self, message, status=429, url=None, retry_after=None):
        super().__init__(message, status=status, url=url)
        self.retry_after = retry_after


class HttpClient:
    """极简 HTTP 客户端：按主机限速 + 指数退避 + 可读错误。

    只用标准库，避免让工具依赖 requests。
    """

    def __init__(
        self,
        contact_email: str | None = None,
        min_interval: float = 3.2,
        timeout: float = 30.0,
        retries: int = 3,
        log=None,
    ):
        self.contact_email = contact_email or ""
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self._last_request_at: dict[str, float] = {}
        self._log = log or (lambda msg: None)

        # 提前把 CA 问题修掉：否则每个请求都要先失败一次再回退，请求数翻倍。
        # 对限速 3.2 秒/次的 arXiv 来说这是实打实的浪费。
        self.ca_bundle = ensure_default_ca()
        if self.ca_bundle:
            self._log(f"  · 当前 Python 缺少默认 CA 证书，已自动改用：{self.ca_bundle}")
            self._log(
                f"    建议设为环境变量永久生效："
                f"$env:SSL_CERT_FILE='{self.ca_bundle}'"
            )

    def user_agent(self, extra: str = "") -> str:
        """合规的 User-Agent：包含联系邮箱，才进得了 polite pool。"""
        base = "paper-finder/0.1 (+https://github.com/; research tool)"
        if self.contact_email:
            base += f" mailto:{self.contact_email}"
        return f"{base} {extra}".strip()

    def _throttle(self, host: str):
        last = self._last_request_at.get(host)
        if last is not None:
            wait = self.min_interval - (time.time() - last)
            if wait > 0:
                self._log(f"    · 限速等待 {wait:.1f}s（{host}）")
                time.sleep(wait)
        self._last_request_at[host] = time.time()

    def _open(self, request):
        """发起请求；证书校验失败时用系统证书存储重试一次。

        这一步让工具能在"没有 CA bundle 的 Python"上直接可用，
        而不是抛一个用户看不懂的 OpenSSL 错误。
        """
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.URLError as exc:
            if not _is_cert_error(exc):
                raise
            context, source = _fallback_ssl_context()
            if context is None:
                self._log("    ! " + _cert_error_hint(source))
                raise
            self._log(f"    · 默认 CA 校验失败，改用：{source}")
            return urllib.request.urlopen(request, timeout=self.timeout, context=context)

    def get(self, url: str, headers: dict | None = None, min_interval: float | None = None):
        """GET 一个 URL，返回 bytes。失败抛 HttpError / RateLimited。"""
        host = urllib.parse.urlparse(url).netloc
        interval = self.min_interval if min_interval is None else min_interval

        last_error = None
        for attempt in range(1, self.retries + 1):
            # 限速
            last = self._last_request_at.get(host)
            if last is not None:
                wait = interval - (time.time() - last)
                if wait > 0:
                    time.sleep(wait)
            self._last_request_at[host] = time.time()

            request = urllib.request.Request(url, headers={"User-Agent": self.user_agent(), **(headers or {})})
            try:
                with self._open(request) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                status = exc.code
                retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)

                if status == 429:
                    if retry_after is not None and retry_after > MAX_SENSIBLE_RETRY_AFTER:
                        hours = retry_after / 3600.0
                        raise RateLimited(
                            f"{host} 已耗尽配额（Retry-After {retry_after}s ≈ {hours:.1f} 小时），"
                            f"本轮放弃。这通常意味着没有配置 API key。",
                            status=status,
                            url=url,
                            retry_after=retry_after,
                        )
                    last_error = f"HTTP 429 限流"
                    backoff = retry_after if retry_after else 2 ** attempt
                    self._log(f"    ! {last_error}，{backoff}s 后重试（{attempt}/{self.retries}）")
                    time.sleep(min(backoff, 30))
                    continue

                if 500 <= status < 600:
                    last_error = f"HTTP {status}"
                    self._log(f"    ! {last_error}，退避重试（{attempt}/{self.retries}）")
                    time.sleep(2 ** attempt)
                    continue

                # 4xx 其余情况：读一点响应体，便于诊断（如 Unpaywall 的 422 提示换邮箱）
                detail = ""
                try:
                    detail = exc.read()[:200].decode("utf-8", "replace")
                except Exception:
                    pass
                raise HttpError(f"HTTP {status} {detail}".strip(), status=status, url=url)

            except urllib.error.URLError as exc:
                last_error = f"网络错误 {exc.reason}"
                self._log(f"    ! {last_error}，退避重试（{attempt}/{self.retries}）")
                time.sleep(2 ** attempt)
            except TimeoutError:
                last_error = "请求超时"
                self._log(f"    ! {last_error}，退避重试（{attempt}/{self.retries}）")
                time.sleep(2 ** attempt)

        raise HttpError(f"重试 {self.retries} 次后仍失败：{last_error}", url=url)

    def get_json(self, url: str, headers: dict | None = None, min_interval: float | None = None):
        raw = self.get(url, headers=headers, min_interval=min_interval)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise HttpError(f"响应不是合法 JSON：{exc}", url=url) from exc

    def get_text(self, url: str, headers: dict | None = None, min_interval: float | None = None) -> str:
        return self.get(url, headers=headers, min_interval=min_interval).decode("utf-8", "replace")


def _parse_retry_after(value):
    if not value:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
