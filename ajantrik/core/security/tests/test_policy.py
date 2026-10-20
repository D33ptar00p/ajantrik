"""Tests for policy.py: patterns, .env values, redaction, scanning and the allow list."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import policy

POLICY = Path(policy.__file__)
FAKE_AWS = "AKIA" + "Q" * 16
FAKE_GH = "ghp_" + "a" * 36
FAKE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----"


class PatternTest(unittest.TestCase):
    def test_detects(self):
        for text, rule in ((f"id={FAKE_AWS}", "aws-access-key-id"), (FAKE_GH, "github-token"),
                           (FAKE_KEY, "private-key"), ("const password = 'hunter2hunter2'", "generic-secret"),
                           ("API_TOKEN=abcd1234efgh", "generic-secret")):
            with self.subTest(rule=rule):
                self.assertEqual([h["rule"] for h in policy.find_secrets(text, allow=set())], [rule])

    def test_ignores_placeholders_and_code(self):
        for text in ("password: '<your-password>'", "const token = await getAccessTokenSilently()",
                     "API_KEY=${API_KEY}", "secret = process.env.SECRET", "password: 'xxxxxxxx'",
                     "VITE_AUTH0_DOMAIN=auth.platform-test.move.ai", "const API_TOKEN = getToken()",
                     "headers: { Authorization: `Bearer ${token}` }"):
            with self.subTest(text=text):
                self.assertEqual(policy.find_secrets(text, allow=set()), [])

    def test_allow_list(self):
        fp = policy.fingerprint(FAKE_GH)
        self.assertEqual(policy.find_secrets(FAKE_GH, allow={fp}), [])

    def test_secret_paths(self):
        for p in (".env", "a/.env.local", "prod.env", "~/.aws/credentials", "/home/u/.ssh/id_rsa", "x/server.pem",
                  ".npmrc", "/proc/1/environ"):
            with self.subTest(p=p):
                self.assertTrue(policy.is_secret_path(p))
        for p in (".env.example", "src/env.ts", "README.md", ".envrc.md", "environment.ts"):
            with self.subTest(p=p):
                self.assertFalse(policy.is_secret_path(p))


class EnvTest(unittest.TestCase):
    def test_env_secrets_only_secret_keys(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".env").write_text("VITE_API=https://api.x\nVITE_E2E_PASSWORD='s3cret-value-1'\n"
                                       "VITE_DEBUG=true\nSHORT_TOKEN=abc\nexport HUBSPOT_API_KEY=hk_9876543210\n")
            Path(d, ".env.example").write_text("VITE_E2E_PASSWORD=changeme-please\n")
            self.assertEqual(policy.env_secrets(Path(d)),
                             [("HUBSPOT_API_KEY", "hk_9876543210"), ("VITE_E2E_PASSWORD", "s3cret-value-1")])

    def test_redact(self):
        env = [("VITE_E2E_PASSWORD", "s3cret-value-1")]
        out = policy.redact(f"login s3cret-value-1 then {FAKE_AWS}\n", env)
        self.assertEqual(out, "login «redacted:env-value:VITE_E2E_PASSWORD» then «redacted:aws-access-key-id»\n")


class CheckEnvTest(unittest.TestCase):
    def test_check_env_never_prints_values(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".env").write_text("VITE_E2E_PASSWORD=very-secret-pw\nVITE_AUTH0_DOMAIN=auth.move.ai\n"
                                       "VITE_BILLING_BASE_URL=https://billing.platform-test.move.ai\n")
            run = lambda *a: subprocess.run([sys.executable, str(POLICY), "check-env", *a], capture_output=True,
                                            text=True, env={**os.environ, "CLAUDE_PROJECT_DIR": d})
            r = run("--require", "VITE_E2E_PASSWORD", "--match", "VITE_BILLING_BASE_URL", "--pattern", "platform-test")
            self.assertEqual(r.returncode, 0, r.stderr)
            r = run("--require", "VITE_E2E_PASSWORD,VITE_X", "--match", "VITE_AUTH0_DOMAIN", "--pattern", "platform-test")
            self.assertEqual(r.returncode, 1)
            self.assertIn("VITE_X missing", r.stderr)
            self.assertIn("VITE_AUTH0_DOMAIN does not match", r.stderr)
            self.assertNotIn("auth.move.ai", r.stdout + r.stderr)
            self.assertNotIn("very-secret-pw", r.stdout + r.stderr)


class ScanTest(unittest.TestCase):
    def git(self, d, *args):
        subprocess.run(["git", "-C", d, *args], check=True, capture_output=True)

    def test_scan_git_history_and_files(self):
        with tempfile.TemporaryDirectory() as d:
            self.git(d, "init", "-q", "-b", "main")
            self.git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
            base = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            Path(d, "a.ts").write_text(f"export const k = '{FAKE_GH}'\n")
            self.git(d, "add", "a.ts")
            self.git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "add")
            Path(d, "a.ts").write_text("export const k = ''\n")  # removed later: still in history
            self.git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-am", "remove")
            Path(d, "pr.md").write_text("Clean PR body\n")
            out = Path(d, "secrets.json")
            env = {**os.environ, "CLAUDE_PROJECT_DIR": d}
            r = subprocess.run([sys.executable, str(POLICY), "scan", "--out", str(out), "--git", d, base,
                                "--file", str(Path(d, "pr.md"))], capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 1, r.stderr)
            report = json.loads(out.read_text())
            self.assertEqual([(f["file"], f["line"], f["rule"]) for f in report["findings"]], [("a.ts", 1, "github-token")])
            self.assertNotIn(FAKE_GH, out.read_text() + r.stderr)

    def test_redact_cli_streams(self):
        r = subprocess.run([sys.executable, str(POLICY), "redact"], input=f"a {FAKE_AWS} b\n", capture_output=True,
                           text=True, env={**os.environ, "CLAUDE_PROJECT_DIR": tempfile.gettempdir()})
        self.assertEqual(r.stdout, "a «redacted:aws-access-key-id» b\n")


if __name__ == "__main__":
    unittest.main()
