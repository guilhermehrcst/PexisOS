"""Deterministic public repository inventory; does not establish IP ownership.

This is repository/CI tooling only, never imported by the PexisOS runtime.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Sequence

_SCHEMA = "pexisos.public-source-inventory.v0"
_CLASSES = frozenset({"repository-license", "generated-synthetic", "third-party-reference", "review-required"})
_REVIEW_STATES = frozenset({"pending", "reviewed"})


def tracked_paths(repo_root: Path) -> tuple[str, ...]:
    """Get all git-index paths, in deterministic repository-relative order."""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        check=True,
        capture_output=True,
        timeout=20,
    )
    paths = (part.decode("utf-8") for part in proc.stdout.split(b"\0") if part)
    return tuple(sorted(paths))


def _safe_relative(path: object) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return False
    return all(segment not in ("", ".", "..") for segment in path.split("/"))


def classify_path(path: str, rules: Sequence[dict[str, str]]) -> str | None:
    """Choose an exact rule first, otherwise the longest matching directory root.

    An unmatched file is never automatically categorized as owned or private.
    """
    for rule in rules:
        if rule["match"] == "exact" and rule["path"] == path:
            return rule["classification"]
    prefixes = [
        rule for rule in rules
        if rule["match"] == "prefix" and path.startswith(rule["path"])
    ]
    return max(prefixes, key=lambda rule: len(rule["path"]))["classification"] if prefixes else None


def validate_inventory(paths: Sequence[str], manifest: dict) -> list[str]:
    """Return deterministic findings for malformed rules and uncovered git paths.

    An empty result proves *only* structural coverage, not authorship or consent.
    """
    issues: list[str] = []
    if not isinstance(manifest, dict):
        return ["invalid_manifest: expected an object"]
    if manifest.get("schema_version") != _SCHEMA:
        issues.append("invalid_schema_version")
    if manifest.get("legal_signoff") != "not_recorded":
        issues.append("legal_signoff: D1 cannot certify legal rights (expected not_recorded)")
    if not isinstance(manifest.get("license_identifier"), str) or not manifest["license_identifier"]:
        issues.append("invalid_license_identifier")
    raw_rules = manifest.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        issues.append("invalid_rules: expected a nonempty list")
        raw_rules = []
    valid_rules: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, rule in enumerate(raw_rules):
        if not isinstance(rule, dict):
            issues.append(f"invalid_rule[{index}]: expected an object")
            continue
        rule_path = rule.get("path")
        match = rule.get("match")
        classification = rule.get("classification")
        review = rule.get("ownership_review")
        good = True
        if match not in ("exact", "prefix"):
            issues.append(f"invalid_match[{index}]")
            good = False
        if (not isinstance(rule_path, str) or
            not _safe_relative(rule_path[:-1] if match == "prefix" and rule_path.endswith("/") else rule_path) or
            (match == "prefix" and not rule_path.endswith("/")) or
            (match == "exact" and rule_path.endswith("/"))):
            issues.append(f"invalid_rule_path[{index}]: {rule_path!r}")
            good = False
        if classification not in _CLASSES:
            issues.append(f"invalid_classification[{index}]: {classification!r}")
            good = False
        if review not in _REVIEW_STATES:
            issues.append(f"invalid_ownership_review[{index}]: {review!r}")
            good = False
        key = (str(match), str(rule_path))
        if key in seen:
            issues.append(f"duplicate_rule[{index}]: {rule_path!r}")
            good = False
        seen.add(key)
        if good:
            valid_rules.append(rule)
    for path in paths:
        if not _safe_relative(path):
            issues.append(f"unsafe_tracked_path: {path!r}")
        elif classify_path(path, valid_rules) is None:
            issues.append(f"unclassified_tracked_path: {path!r}")
    return sorted(issues)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate structural coverage of PexisOS public tracked files")
    parser.add_argument("--check", action="store_true", required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        manifest = json.loads((root / "docs/ip/public-source-inventory.json").read_text(encoding="utf-8"))
        paths = tracked_paths(root)
    except (OSError, UnicodeError, json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"inventory_unavailable: {type(exc).__name__}", file=sys.stderr)
        return 2
    if not paths:
        print("inventory_unavailable: empty git index", file=sys.stderr)
        return 2
    issues = validate_inventory(paths, manifest)
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print(f"inventory_complete: {len(paths)} tracked paths; legal_signoff=not_recorded (human review pending)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
