"""Regression tests for the public/private repository boundary guard."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_open_core_boundary import inspect_imports, inspect_tracked_paths, inspect_packaging


class BoundaryTests(unittest.TestCase):
    def test_rejects_static_and_literal_dynamic_enterprise_imports(self):
        src = '''import pexisos_enterprise.rules
from pexisos_cloud import client
import importlib
x = importlib.import_module("pexisos_enterprise.rules")
'''
        findings = inspect_imports(src, "src/pexislink/example.py")
        self.assertEqual(len(findings), 3, findings)
        self.assertTrue(all("src/pexislink/example.py" in line for line in findings))
        self.assertTrue(any("pexisos_cloud" in line for line in findings))

    def test_rejects_private_tracked_paths(self):
        paths = ["private/engine.py", "src/pexisos_enterprise/__init__.py", "cloud/engine.py", "enterprise/billing.py", ".env", "nested/.env", "keys/root.pem", "server.key"]
        issues = inspect_tracked_paths(paths)
        self.assertEqual(len(issues), len(paths), issues)
        self.assertFalse(any("/" not in f and "server.key" not in f and ".env" not in f for f in issues))

    def test_normal_open_core_import_is_allowed(self):
        self.assertEqual(inspect_imports("from pexislink.fabric import Fabric\n", "src/pexislink/valid.py"), [])
        self.assertEqual(inspect_tracked_paths(["src/pexislink/core.py", "docs/README.md", "requirements/lab-can.txt"]), [])

    def test_rejects_banned_build_and_runtime_dependencies(self):
        manifest = {
            "build-system": {"requires": ["setuptools", "pexisos_cloud>=1"]},
            "project": {"dependencies": ["pexisos_enterprise==0.1"], "scripts": {"launch": "pexisos_cloud:main"}},
            "tool": {"setuptools": {"packages": ["pexisos_enterprise"]}},
        }
        result = inspect_packaging(manifest)
        self.assertGreaterEqual(len(result), 4, result)
        self.assertTrue(any("build-system" in line for line in result))

    def test_dynamic_private_import_and_syntax_error_tripwire(self):
        errors = inspect_imports('from importlib import import_module\nimport_module("pexisos_cloud.client")\n', "src/pexislink/dynamic.py")
        self.assertTrue(errors, errors)
        errors2 = inspect_imports('import importlib as il\nil.import_module("pexisos_enterprise.foo")\n', "src/pexislink/dynamic.py")
        self.assertTrue(errors2, errors2)
        errors3 = inspect_imports('import pexisos_enterprise\n    illegal indent', "src/pexislink/broken.py")
        self.assertTrue(any("syntax_error" in line for line in errors3), errors3)

    def test_malformed_packaging_fails_closed_without_exception(self):
        findings = inspect_packaging({"project": None, "tool": 3})
        self.assertTrue(any("invalid_pyproject" in f for f in findings), findings)

    def test_diagnostics_do_not_echo_secret_source_text(self):
        source = '''import pexisos_cloud\nsecret_password = "SHOULD_NOT_APPEAR_IN_LOGS"\n'''
        self.assertNotIn("SHOULD_NOT_APPEAR_IN_LOGS", "\n".join(inspect_imports(source, "src/pexislink/secrets.py")))


if __name__ == "__main__":
    unittest.main()
