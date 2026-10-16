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
import time
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["HttpClient", "HttpError", "RateLimited"]

# 超过这个秒数的 Retry-After 视为"今日额度耗尽"，不再重试
MAX_SENSIBLE_RETRY_AFTER = 300


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
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
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
