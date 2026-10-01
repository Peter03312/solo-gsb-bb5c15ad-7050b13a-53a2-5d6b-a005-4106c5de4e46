"""REVIEW_CREDENTIALS 配置校验的单元测试。"""

import json
import os
import unittest

from pydantic import ValidationError


def _settings(credentials):
    """用给定凭据构造 Settings（绕开单例与进程环境变量）。"""
    from app.config import Settings

    return Settings(service_key="k", review_credentials=credentials)


class ReviewCredentialsTests(unittest.TestCase):
    def test_empty_allowed(self):
        settings = _settings([])
        self.assertEqual(settings.review_credentials, [])

    def test_valid_credentials(self):
        settings = _settings(
            [
                {"username": "alice", "password": "pw", "roles": ["operator"]},
                {"username": "bob", "password": "pw2", "roles": ["reviewer"]},
                {"username": "carol", "password": "pw3", "roles": ["operator", "reviewer"]},
            ]
        )
        self.assertEqual(
            [c.username for c in settings.review_credentials], ["alice", "bob", "carol"]
        )

    def test_empty_username(self):
        with self.assertRaises(ValidationError):
            _settings([{"username": "  ", "password": "pw", "roles": ["operator"]}])

    def test_missing_password(self):
        with self.assertRaises(ValidationError):
            _settings([{"username": "alice", "password": "", "roles": ["operator"]}])

    def test_duplicate_username(self):
        with self.assertRaises(ValidationError):
            _settings(
                [
                    {"username": "alice", "password": "pw", "roles": ["operator"]},
                    {"username": "alice", "password": "other", "roles": ["reviewer"]},
                ]
            )

    def test_no_roles(self):
        with self.assertRaises(ValidationError):
            _settings([{"username": "alice", "password": "pw", "roles": []}])

    def test_unknown_role(self):
        with self.assertRaises(ValidationError):
            _settings([{"username": "alice", "password": "pw", "roles": ["admin"]}])

    def test_error_messages_do_not_contain_password(self):
        try:
            _settings([{"username": "alice", "password": "super-secret", "roles": ["admin"]}])
        except ValidationError as exc:
            self.assertNotIn("super-secret", str(exc))
        else:  # pragma: no cover
            self.fail("expected ValidationError")

    def test_json_env_parsing(self):
        """与生产一致：JSON 数组通过环境变量注入。"""
        from importlib import reload

        from app import config

        os.environ["SERVICE_KEY"] = "k"
        os.environ["REVIEW_CREDENTIALS"] = json.dumps(
            [{"username": "dave", "password": "pw", "roles": ["reviewer"]}]
        )
        try:
            reloaded = reload(config)
            self.assertEqual(reloaded.settings.review_credentials[0].username, "dave")
        finally:
            del os.environ["REVIEW_CREDENTIALS"]
            reload(config)  # 恢复模块单例，避免污染其他测试


if __name__ == "__main__":
    unittest.main()
