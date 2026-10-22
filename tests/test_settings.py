import json
import subprocess
import unittest
from pathlib import Path


SETTINGS = Path.home() / ".claude/agy-settings.json"


class SettingsTests(unittest.TestCase):
    def test_agy_settings_and_alias(self):
        if not SETTINGS.is_file():
            self.skipTest("Local agy settings are not installed")
        config = json.loads(SETTINGS.read_text())
        env = config["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8877")
        self.assertEqual(env["ANTHROPIC_MODEL"], "gemini-3.8-flash")
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "claude-sonnet-5-5")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "gemini-3.8-flash")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "unused")
        self.assertFalse(config["enabledPlugins"]["telegram@claude-plugins-official"])
        models = {option["model"] for option in config["modelPicker"]["options"]}
        self.assertEqual(models, {"claude-opus-5-5", "claude-sonnet-5-5", "gemini-4-argon", "gemini-3.8-flash"})

        command = 'claude() { printf "arg:%s\\n" "$@"; }; eval "agy-claude --version"'
        shell = subprocess.run(["bash", "-ic", command], text=True, capture_output=True, check=True)
        self.assertEqual(shell.stdout.splitlines()[-4:], [
            "arg:--dangerously-skip-permissions",
            "arg:--settings",
            f"arg:{SETTINGS}",
            "arg:--version",
        ])


if __name__ == "__main__":
    unittest.main()
