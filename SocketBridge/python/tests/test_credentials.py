import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from combat.planner import DeepSeekClient, load_env


class TestLocalCredentials(unittest.TestCase):
    def test_explicit_reload_replaces_inherited_key_and_model(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "DEEPSEEK_API_KEY": "old-fixture", "DEEPSEEK_MODEL": "old-model"}):
            path = Path(directory)/".env.local"
            path.write_text('\ufeff# local configuration\nDEEPSEEK_API_KEY="new-fixture"\nDEEPSEEK_MODEL=next-model\nUNRELATED=ignored\n', encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                old_unrelated = os.environ.get("UNRELATED")
                load_env(path, override=True)
                client = DeepSeekClient()
                try:
                    self.assertEqual(client.key, "new-fixture")
                    self.assertEqual(client.model, "next-model")
                    self.assertEqual(os.environ.get("UNRELATED"), old_unrelated)
                finally: asyncio.run(client.aclose())

    def test_default_loader_preserves_explicit_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"DEEPSEEK_API_KEY": "external-fixture"}):
            path = Path(directory)/".env.local"
            path.write_text("DEEPSEEK_API_KEY=local-fixture\n", encoding="utf-8")
            load_env(path)
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "external-fixture")

    def test_missing_file_does_not_clear_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture"}):
            load_env(Path(directory)/"missing", override=True)
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "fixture")

    def test_blank_replacement_fails_instead_of_reusing_old_key(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "DEEPSEEK_API_KEY": "old-fixture", "DEEPSEEK_MODEL": "fixture"}):
            path = Path(directory)/".env.local"
            path.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
            load_env(path, override=True)
            with self.assertRaisesRegex(ValueError, "Configure DEEPSEEK"): DeepSeekClient()
