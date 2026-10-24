"""Repository inventory requirements; explicit IP review is never automated."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from ip_inventory import classify_path, tracked_paths, validate_inventory
MANIFEST = ROOT / "docs/ip/public-source-inventory.json"


def sample_manifest():
    return {
        "schema_version": "pexisos.public-source-inventory.v0",
        "license_identifier": "Apache-2.0",
        "legal_signoff": "not_recorded",
        "rules": [
            {"path": "LICENSE", "match": "exact", "classification": "repository-license", "ownership_review": "pending"},
            {"path": "src/", "match": "prefix", "classification": "review-required", "ownership_review": "pending"},
        ],
    }


class InventoryUnitTests(unittest.TestCase):
    def test_unclassified_tracked_file_is_a_failure(self):
        paths = ("LICENSE", "src/pexislink/core.py", "unexpected/algorithm.py")
        errors = validate_inventory(paths, sample_manifest())
        self.assertTrue(any("unexpected/algorithm.py" in issue for issue in errors), errors)

    def test_repository_file_order_does_not_change_results(self):
        paths = ("LICENSE", "src/pexislink/core.py", "uncovered/secret.py")
        self.assertEqual(
            validate_inventory(paths, sample_manifest()),
            validate_inventory(tuple(reversed(paths)), sample_manifest()),
        )

    def test_review_status_is_not_inferred_from_license_field(self):
        manifest = sample_manifest()
        initial = copy.deepcopy(manifest)
        self.assertEqual(validate_inventory(("LICENSE", "src/pexislink/core.py"), manifest), [])
        self.assertEqual(manifest, initial)
        self.assertEqual(manifest["legal_signoff"], "not_recorded")
        manifest["legal_signoff"] = "approved"
        errors = validate_inventory(("LICENSE", "src/pexislink/core.py"), manifest)
        self.assertTrue(any("legal_signoff" in issue for issue in errors), errors)

    def test_exact_match_precedes_prefix_rules(self):
        rules = sample_manifest()["rules"] + [
            {"path": "src/pexislink/core.py", "match": "exact", "classification": "repository-license", "ownership_review": "pending"}
        ]
        self.assertEqual(classify_path("src/pexislink/core.py", rules), "repository-license")
        self.assertEqual(classify_path("src/pexislink/other.py", rules), "review-required")
        self.assertIsNone(classify_path("src2/pexislink/core.py", rules))

    def test_rejects_unknown_classifications_and_unscoped_prefixes(self):
        manifest = sample_manifest()
        manifest["rules"][1]["classification"] = "proprietary"
        manifest["rules"].append({"path": "", "match": "prefix", "classification": "repository-license", "ownership_review": "reviewed"})
        errors = validate_inventory(("LICENSE",), manifest)
        self.assertTrue(any("classification" in issue for issue in errors), errors)
        self.assertTrue(any("path" in issue for issue in errors), errors)

    def test_unsafe_tracked_paths_are_rejected(self):
        errors = validate_inventory(("LICENSE", "../outside.py", "/absolute.py", "src/../secret.py"), sample_manifest())
        self.assertEqual(sum("unsafe_tracked_path" in e for e in errors), 3)

    def test_git_tracked_paths_are_sorted_even_with_spaces(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            for name in ("a b.txt", "z.txt", "a.txt"):
                (root / name).write_text("", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "--", "a b.txt", "z.txt", "a.txt"], check=True)
            self.assertEqual(tracked_paths(root), ("a b.txt", "a.txt", "z.txt"))


class LiveManifestTests(unittest.TestCase):
    def test_inventory_manifest_is_complete_for_git_index(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        paths = tracked_paths(ROOT)
        self.assertTrue(paths, "expected nonempty tracked tree")
        self.assertEqual(validate_inventory(paths, manifest), [])
        self.assertEqual(manifest["legal_signoff"], "not_recorded")
        self.assertTrue(all(r["ownership_review"] == "pending" for r in manifest["rules"]))


    def test_petron_console_paths_are_explicitly_classified_but_not_approved(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        rules = manifest["rules"]
        expected = {
            "apps/console/src/App.tsx": "repository-license",
            "apps/console/package-lock.json": "third-party-reference",
            "apps/console/docs/console-v1-1440x900.png": "review-required",
            "apps/petron/src/petron/engine.py": "repository-license",
            "apps/petron/brain/brain-mask.webp": "review-required",
            "apps/petron/brain/brain-texture.webp": "review-required",
            "apps/petron/docs/experiments/e001-evidence/report.json": "review-required",
            "apps/petron/docs/experiments/e002-evidence/report.json": "review-required",
            "apps/petron/fixtures/e002/mission.json": "review-required",
        }
        for path, classification in expected.items():
            with self.subTest(path=path):
                self.assertEqual(classify_path(path, rules), classification)
        self.assertIsNone(classify_path("apps/unknown/private.py", rules))
        self.assertEqual(manifest["legal_signoff"], "not_recorded")
        self.assertTrue(all(rule["ownership_review"] == "pending" for rule in rules))


if __name__ == "__main__":
    unittest.main()
