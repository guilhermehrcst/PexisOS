"""Non-authoritative public evidence-envelope *proposal* consistency checks."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import unittest

from pexislink.fabric.contracts import canonical_json, parse_observation

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/evidence/synthetic-bundle-v0.json"
PROPOSAL = ROOT / "docs/ip/OFFLINE-EVIDENCE-EXCHANGE-PROPOSAL.md"


def bundle():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


_URL_SCHEME = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://")


def remote_references(value):
    """Every string (key or value) anywhere in the document that contains a URL scheme."""
    if isinstance(value, str):
        return [value] if _URL_SCHEME.search(value) else []
    if isinstance(value, dict):
        return [hit for key, item in value.items() for hit in remote_references(key) + remote_references(item)]
    if isinstance(value, list):
        return [hit for item in value for hit in remote_references(item)]
    return []


def digest(observations):
    return hashlib.sha256(canonical_json(observations).encode("ascii")).hexdigest()


class ProposalTests(unittest.TestCase):
    def test_proposed_example_is_unattested_and_digest_matches(self):
        data = bundle()
        self.assertEqual(data["schema_version"], "pexisos.evidence-export.proposal.v0")
        self.assertEqual(data["mode"], "synthetic_only")
        self.assertEqual(data["purpose"], "offline-contract-validation")
        self.assertEqual(data["provenance"]["source"], "synthetic_fixture")
        self.assertEqual(data["provenance"]["license"], "project_original")
        self.assertEqual(data["operator_declaration"]["verification"], "not_authenticated")
        self.assertEqual(data["content_sha256"], digest(data["observations"]))
        self.assertEqual(len(data["observations"]), 1)
        for observation in data["observations"]:
            self.assertIsNotNone(parse_observation(observation))
            self.assertIn(observation["entity_id"], data["scope"]["entity_ids"])
            self.assertEqual(observation["provenance"]["source"], "synthetic_fixture")
            self.assertEqual(observation["provenance"]["license"], "project_original")
        self.assertEqual(data["scope"]["entity_ids"], sorted(set(x["entity_id"] for x in data["observations"])))

    def test_tampered_copy_does_not_match_digest(self):
        data = bundle()
        changed = deepcopy(data)
        changed["observations"][0]["payload"]["coolant_temp_c"] += 1.0
        self.assertNotEqual(data["content_sha256"], digest(changed["observations"]))

    def test_example_has_no_remote_or_claimed_authorization(self):
        data = bundle()
        dangerous_keys = {"url", "token", "password", "authorization", "credential", "access_key",
                          "verified_principal", "authenticated_principal", "device_command", "command", "endpoint"}
        def walk(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    self.assertNotIn(k.casefold(), dangerous_keys)
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)
        walk(data)
        self.assertEqual(remote_references(data), [])
        self.assertNotIn('"verified"', json.dumps(data).lower())

    def test_url_detector_finds_embedded_destinations(self):
        """PR21-10: a URL in the middle of any string is a remote reference."""
        for text in ("upload to https://exfil.example.invalid/drop", "see HTTP://x", "s3://bucket/key",
                     "relay via ftp://host"):
            with self.subTest(text=text):
                tampered = deepcopy(bundle())
                tampered["operator_declaration"]["intent"] = text
                self.assertEqual(remote_references(tampered), [text])
        self.assertEqual(remote_references({"note": "offline, no URL here: a/b"}), [])

    def test_proposal_is_explicitly_non_executable(self):
        body = PROPOSAL.read_text(encoding="utf-8").lower()
        self.assertIn("not implemented", body)
        self.assertIn("not an authorization", body)
        self.assertIn("manual", body)
        self.assertIn("operator", body)


if __name__ == "__main__":
    unittest.main()
