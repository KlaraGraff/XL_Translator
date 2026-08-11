"""Contracts for scripts/inject_embedded_key.py and its release-time guard."""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

import scripts.inject_embedded_key as inject_embedded_key
import scripts.verify_release_metadata as verify_release_metadata


def _real_key_b64() -> str:
    return base64.b64encode(X25519PrivateKey.generate().private_bytes_raw()).decode()


class _TempRootTestCase(unittest.TestCase):
    """每个用例都在临时目录里操作，绝不碰工作区里真正的 core/_embedded_key.py。

    发版构建会在打包前把真实私钥注入那个文件。测试如果直接读写它，两头都会
    出事：断言「文件里是 dev 兜底密钥」在注入之后并不成立，会让发布构建挂；
    而写它则可能在守卫检查通过之后又把真钥换成测试钥，产物里带的就不是发布
    密钥了。所以这里一律用临时目录复刻出一份，工作区文件只读不写。
    """

    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)
        (self.root / "core").mkdir()
        self.target = self.root / "core" / "_embedded_key.py"
        self._write_dev_fallback()

    def _write_dev_fallback(self) -> None:
        self.target.write_text(
            "from __future__ import annotations\n\n"
            'EMBEDDED_KEY_ID = "dev"\n'
            f'EMBEDDED_PRIVATE_KEY_B64 = "{_real_key_b64()}"\n',
            encoding="utf-8",
        )

    def _module_constants(self) -> dict[str, Any]:
        namespace: dict[str, Any] = {}
        exec(compile(self.target.read_text(encoding="utf-8"), str(self.target), "exec"), namespace)
        return namespace


class InjectEmbeddedKeyTests(_TempRootTestCase):
    def test_missing_key_id_env_var_exits_nonzero(self) -> None:
        with patch.dict(
            "os.environ",
            {"XLT_EMBEDDED_PRIVATE_KEY_B64": _real_key_b64()},
            clear=False,
        ):
            os.environ.pop("XLT_EMBEDDED_KEY_ID", None)
            with self.assertRaises(SystemExit) as ctx:
                inject_embedded_key.main()
            self.assertNotEqual(ctx.exception.code, 0)

    def test_missing_private_key_env_var_exits_nonzero(self) -> None:
        with patch.dict("os.environ", {"XLT_EMBEDDED_KEY_ID": "2026-01"}, clear=False):
            os.environ.pop("XLT_EMBEDDED_PRIVATE_KEY_B64", None)
            with self.assertRaises(SystemExit) as ctx:
                inject_embedded_key.main()
            self.assertNotEqual(ctx.exception.code, 0)

    def test_malformed_base64_private_key_exits_nonzero(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            inject_embedded_key.inject("2026-01", "not base64!!", target_path=self.target)
        self.assertNotEqual(ctx.exception.code, 0)

    def test_wrong_length_private_key_exits_nonzero(self) -> None:
        too_short = base64.b64encode(b"not-32-bytes").decode()
        with self.assertRaises(SystemExit) as ctx:
            inject_embedded_key.inject("2026-01", too_short, target_path=self.target)
        self.assertNotEqual(ctx.exception.code, 0)

    def test_dev_key_id_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            inject_embedded_key.inject("dev", _real_key_b64(), target_path=self.target)
        self.assertNotEqual(ctx.exception.code, 0)

    def test_a_rejected_injection_leaves_the_module_untouched(self) -> None:
        before = self.target.read_bytes()
        with self.assertRaises(SystemExit):
            inject_embedded_key.inject("dev", _real_key_b64(), target_path=self.target)
        self.assertEqual(self.target.read_bytes(), before)

    def test_successful_injection_rewrites_module_with_expected_contract(self) -> None:
        key_b64 = _real_key_b64()
        inject_embedded_key.inject("2026-01", key_b64, target_path=self.target)

        constants = self._module_constants()
        self.assertEqual(constants["EMBEDDED_KEY_ID"], "2026-01")
        self.assertEqual(constants["EMBEDDED_PRIVATE_KEY_B64"], key_b64)

    def test_injection_does_not_print_the_private_key(self) -> None:
        key_b64 = _real_key_b64()
        with patch("builtins.print") as printed:
            inject_embedded_key.inject("2026-01", key_b64, target_path=self.target)
        printed_text = " ".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        self.assertIn("2026-01", printed_text)
        self.assertNotIn(key_b64, printed_text)


class VerifyReleaseMetadataEmbeddedKeyTests(_TempRootTestCase):
    def test_dev_fallback_key_is_flagged_as_an_error(self) -> None:
        errors = verify_release_metadata.embedded_key_errors(self.root)
        self.assertTrue(
            any("dev" in error for error in errors),
            f"expected the dev fallback key to be flagged, got {errors!r}",
        )

    def test_injected_key_passes_the_check(self) -> None:
        inject_embedded_key.inject("2026-01", _real_key_b64(), target_path=self.target)
        self.assertEqual(verify_release_metadata.embedded_key_errors(self.root), [])

    def test_missing_module_is_flagged_as_an_error(self) -> None:
        self.target.unlink()
        self.assertTrue(verify_release_metadata.embedded_key_errors(self.root))

    def test_non_literal_key_id_is_flagged_as_an_error(self) -> None:
        self.target.write_text(
            "import os\n\nEMBEDDED_KEY_ID = os.environ['X']\nEMBEDDED_PRIVATE_KEY_B64 = ''\n",
            encoding="utf-8",
        )
        self.assertTrue(verify_release_metadata.embedded_key_errors(self.root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
