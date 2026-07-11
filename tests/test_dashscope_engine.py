from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from engines.dashscope_engine import DashscopeEngine


class DashscopeEngineTests(unittest.TestCase):
    def test_api_key_is_request_scoped_not_process_global(self) -> None:
        calls: list[dict] = []

        class FakeGeneration:
            @staticmethod
            def call(**kwargs):
                calls.append(kwargs)
                message = types.SimpleNamespace(content='{"source": "target"}')
                choice = types.SimpleNamespace(message=message)
                return types.SimpleNamespace(
                    status_code=200,
                    output=types.SimpleNamespace(choices=[choice]),
                )

        module = types.SimpleNamespace(Generation=FakeGeneration, api_key="unchanged")
        with patch.dict(sys.modules, {"dashscope": module}):
            first = DashscopeEngine("key-a", "model-a")
            second = DashscopeEngine("key-b", "model-b")
            first._call_api("system", "user")
            second._call_api("system", "user")

        self.assertEqual(module.api_key, "unchanged")
        self.assertEqual([call["api_key"] for call in calls], ["key-a", "key-b"])
        self.assertEqual([call["model"] for call in calls], ["model-a", "model-b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
