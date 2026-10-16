"""网络层测试：SSL 证书回退与错误判定。

这一组对应一个实测踩到的真问题：某些 Python 发行版没有可用的 CA 证书。

    实测 MSYS2 Python 3.11.7：
        ssl.get_default_verify_paths().cafile -> None
        openssl_cafile -> E:/msys2/ucrt64/etc/ssl/cert.pem   ← 文件不存在
        hasattr(ssl, "enum_certificates") -> False            ← 读不了系统证书存储

    结果：任何 HTTPS 请求都报 CERTIFICATE_VERIFY_FAILED，而同一台机器上
    另一个 Python（3.12）却完全正常。用户第一次跑 `census` 就是这么挂的。

全部离线：只测判定逻辑与文件查找，不发网络请求。
"""

import os
import ssl
import sys
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from papers.net import _is_cert_error, ensure_default_ca, find_ca_bundle  # noqa: E402


class TestCertErrorDetection(unittest.TestCase):
    def test_detects_bare_cert_error(self):
        exc = ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED")
        self.assertTrue(_is_cert_error(exc))

    def test_detects_cert_error_wrapped_in_urlerror(self):
        inner = ssl.SSLCertVerificationError(
            1, "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate"
        )
        outer = urllib.error.URLError(inner)
        self.assertTrue(_is_cert_error(outer))

    def test_detects_string_reason(self):
        """回归：URLError.reason 链末端是字符串，实现里对它取
        `.__cause__` 会抛 AttributeError，把真正的错误盖掉。"""
        outer = urllib.error.URLError("CERTIFICATE_VERIFY_FAILED")
        self.assertTrue(_is_cert_error(outer))

    def test_rejects_unrelated_errors(self):
        self.assertFalse(_is_cert_error(urllib.error.URLError("connection refused")))
        self.assertFalse(_is_cert_error(TimeoutError("timed out")))
        self.assertFalse(_is_cert_error(ssl.SSLError(1, "wrong version number")))
        self.assertFalse(_is_cert_error(ValueError("nope")))

    def test_does_not_crash_on_weird_chain(self):
        """异常链可能很脏（字符串、None、自引用），判定不能崩。"""
        exc = urllib.error.URLError(ValueError(None))
        self.assertFalse(_is_cert_error(exc))

    def test_handles_cyclic_chain(self):
        first = urllib.error.URLError("a")
        first.__cause__ = first  # 人为制造环
        self.assertFalse(_is_cert_error(first))


class TestCaBundleLookup(unittest.TestCase):
    def test_returns_none_or_existing_file(self):
        bundle = find_ca_bundle()
        if bundle is not None:
            self.assertTrue(os.path.isfile(bundle), f"{bundle} 不是文件")
            self.assertGreater(os.path.getsize(bundle), 0)

    def test_respects_ssl_cert_file_env(self):
        """环境变量指定了有效路径时，必须优先用它。"""
        original = os.environ.get("SSL_CERT_FILE")
        # 借用一个真实存在的文件（证书内容不重要，这里只测“优先取环境变量”）
        sentinel = os.path.abspath(__file__)
        try:
            os.environ["SSL_CERT_FILE"] = sentinel
            self.assertEqual(find_ca_bundle(), sentinel)
        finally:
            if original is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = original

    def test_ignores_nonexistent_env_path(self):
        original = os.environ.get("SSL_CERT_FILE")
        try:
            os.environ["SSL_CERT_FILE"] = os.path.join(ROOT, "definitely-not-here.pem")
            # 不该返回不存在的路径
            bundle = find_ca_bundle()
            self.assertNotEqual(bundle, os.environ["SSL_CERT_FILE"])
        finally:
            if original is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = original


class TestEnsureDefaultCa(unittest.TestCase):
    def test_idempotent_and_side_effect_free_when_ca_ok(self):
        """默认 CA 可用时不能去改 SSL_CERT_FILE（否则会覆盖用户的选择）。"""
        paths = ssl.get_default_verify_paths()
        ca_looks_ok = bool(paths.cafile and os.path.exists(paths.cafile))

        original = os.environ.get("SSL_CERT_FILE")
        try:
            result = ensure_default_ca()
            if ca_looks_ok:
                self.assertIsNone(result)
                self.assertEqual(os.environ.get("SSL_CERT_FILE"), original)
            # 再调一次不应抛异常
            ensure_default_ca()
        finally:
            if original is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = original

    def test_respects_preexisting_env_override(self):
        """用户已显式设置 SSL_CERT_FILE 时，工具不该再动手。"""
        original = os.environ.get("SSL_CERT_FILE")
        sentinel = os.path.abspath(__file__)
        try:
            os.environ["SSL_CERT_FILE"] = sentinel
            self.assertIsNone(ensure_default_ca())
            self.assertEqual(os.environ["SSL_CERT_FILE"], sentinel)
        finally:
            if original is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = original


class TestHttpClientCaReporting(unittest.TestCase):
    def test_client_exposes_ca_state(self):
        from papers.net import HttpClient

        client = HttpClient(contact_email="test@example.com")
        # 属性必须存在，doctor 靠它报告状态
        self.assertTrue(hasattr(client, "ca_bundle"))
        self.assertTrue(client.ca_bundle is None or os.path.isfile(client.ca_bundle))

    def test_user_agent_contains_contact(self):
        from papers.net import HttpClient

        client = HttpClient(contact_email="me@university.edu")
        self.assertIn("me@university.edu", client.user_agent())


if __name__ == "__main__":
    unittest.main()
