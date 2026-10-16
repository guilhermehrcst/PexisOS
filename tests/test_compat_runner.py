"""Regression gates for the optional multi-client interoperability runner.

These checks are standard-library only. Missing optional clients must never
hide missing test IDs or make a passing subset look like complete conformance.
"""

from __future__ import annotations

import copy
import io
import shutil
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from compat import run_clients


class CompatibilityMatrixIntegrityTests(unittest.TestCase):
    @staticmethod
    def passing_codex_matrix():
        return {test_id: {"status": "PASS"} for test_id in run_clients.EXPECTED_TEST_IDS["codex-cli"]}

    @staticmethod
    def expected_methods(name="codex-cli"):
        return sorted(run_clients.EXPECTED_TEST_METHODS[name])

    @staticmethod
    def passing_method_statuses(name="codex-cli"):
        return {method: ("NOT_SUPPORTED" if method in run_clients.EXPECTED_METHOD_NOT_SUPPORTED[name]
                         else "PASS")
                for method in run_clients.EXPECTED_TEST_METHODS[name]}

    def test_full_expected_codex_matrix_passes(self):
        spec = run_clients.CLIENTS["codex-cli"]
        matrix = self.passing_codex_matrix()
        self.assertEqual(run_clients.matrix_integrity_errors("codex-cli", spec, matrix, self.expected_methods()), [])
        self.assertEqual(run_clients.required_client_errors("codex-cli", spec, matrix, self.expected_methods(),
                                                 self.passing_method_statuses()), [])

    def test_missing_required_C13_fails_even_if_every_remaining_id_passes(self):
        spec = run_clients.CLIENTS["codex-cli"]
        matrix = self.passing_codex_matrix()
        del matrix["C13"]
        errors = run_clients.required_client_errors("codex-cli", spec, matrix, self.expected_methods(),
                                                 self.passing_method_statuses())
        self.assertTrue(any("missing_test_ids: C13" in text for text in errors), errors)
        self.assertTrue(any("C13 must PASS (got MISSING)" in text for text in errors), errors)

    def test_renaming_a_test_cannot_silently_reduce_coverage(self):
        spec = run_clients.CLIENTS["codex-cli"]
        matrix = self.passing_codex_matrix()
        matrix["C99"] = matrix.pop("C13")
        errors = run_clients.matrix_integrity_errors("codex-cli", spec, matrix, self.expected_methods())
        self.assertTrue(any("missing_test_ids: C13" in text for text in errors), errors)
        self.assertTrue(any("unexpected_test_ids: C99" in text for text in errors), errors)

    def test_missing_or_unknown_case_mapping_is_rejected(self):
        spec = copy.deepcopy(run_clients.CLIENTS["codex-cli"])
        matrix = self.passing_codex_matrix()
        spec["cases"]["process_shutdown"] = []
        self.assertTrue(any("empty_case_category" in e for e in
                            run_clients.matrix_integrity_errors("codex-cli", spec, matrix, self.expected_methods())))
        spec["cases"]["process_shutdown"] = ["C99"]
        self.assertTrue(any("unrecognized_case_mappings: C99" in e for e in
                            run_clients.matrix_integrity_errors("codex-cli", spec, matrix, self.expected_methods())))

    def test_missing_revocation_method_fails_even_though_C07_still_passes(self):
        spec = run_clients.CLIENTS["codex-cli"]
        matrix = self.passing_codex_matrix()
        methods = self.expected_methods()
        methods.remove("test_C07_revocation_takes_effect_within_a_live_codex_session")
        errors = run_clients.required_client_errors("codex-cli", spec, matrix, methods,
                                                   self.passing_method_statuses())
        self.assertTrue(any("missing_test_methods" in error and
                            "test_C07_revocation_takes_effect_within_a_live_codex_session" in error
                            for error in errors), errors)
        self.assertEqual(matrix["C07"]["status"], "PASS")

    def test_revocation_not_supported_cannot_hide_behind_C07_expiry_pass(self):
        spec = run_clients.CLIENTS["codex-cli"]
        matrix = self.passing_codex_matrix()
        method_states = self.passing_method_statuses()
        method_states["test_C07_revocation_takes_effect_within_a_live_codex_session"] = "NOT_SUPPORTED"
        errors = run_clients.required_client_errors("codex-cli", spec, matrix, self.expected_methods(),
                                                    method_states)
        self.assertTrue(any("test_C07_revocation_takes_effect_within_a_live_codex_session must PASS" in e
                            for e in errors), errors)
        self.assertEqual(matrix["C07"]["status"], "PASS")

    def test_revocation_not_run_cannot_hide_behind_C07_expiry_pass(self):
        spec = run_clients.CLIENTS["codex-cli"]
        matrix = self.passing_codex_matrix()
        method_states = self.passing_method_statuses()
        method_states["test_C07_revocation_takes_effect_within_a_live_codex_session"] = "NOT_RUN"
        errors = run_clients.required_client_errors("codex-cli", spec, matrix, self.expected_methods(),
                                                    method_states)
        self.assertTrue(any("test_C07_revocation_takes_effect_within_a_live_codex_session must PASS" in e
                            for e in errors), errors)

    def test_renaming_only_a_subtest_fails_the_required_gate(self):
        spec = run_clients.CLIENTS["codex-cli"]
        matrix = self.passing_codex_matrix()
        methods = self.expected_methods()
        methods.remove("test_C01_negotiated_mcp_protocol_version_is_observable")
        methods.append("test_C01_renamed_proof")
        errors = run_clients.matrix_integrity_errors("codex-cli", spec, matrix, methods)
        self.assertTrue(any("missing_test_methods:" in e for e in errors), errors)
        self.assertTrue(any("unexpected_test_methods:" in e for e in errors), errors)

    def test_non_required_client_synthesized_fail_makes_cli_exit_nonzero(self):
        spec = run_clients.CLIENTS["claude-code"]
        matrix = {key: {"status": "PASS"} for key in run_clients.EXPECTED_TEST_IDS["claude-code"]}
        matrix["K01"]["status"] = "FAIL"  # e.g. unrecognized skip reason
        result = {"matrix": matrix, "methods": self.expected_methods("claude-code"), "failures": [],
                  "method_statuses": self.passing_method_statuses("claude-code")}
        with mock.patch.object(run_clients, "CLIENTS", {"claude-code": spec}), \
             mock.patch.object(run_clients, "run_module", return_value=result), \
             mock.patch.object(run_clients.sys, "argv", ["run_clients.py"]), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(run_clients.main(), 1)

    def test_unknown_skip_reason_is_a_failure(self):
        class BadSkip(unittest.TestCase):
            def test_C01_unapproved_skip(self):
                self.skipTest("skipping without a recognized reason")

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(BadSkip)
        with mock.patch.object(run_clients.unittest.defaultTestLoader, "loadTestsFromName", return_value=suite):
            matrix = run_clients.run_module("unused", "C")["matrix"]
        self.assertEqual(matrix["C01"]["status"], "FAIL")


class ReviewDd1576aTests(unittest.TestCase):
    """Codex review of dd1576a: pinned case mapping, PASS from recorded outcomes, honest ``executed``."""

    def test_remapping_a_case_to_another_valid_id_is_rejected(self):
        spec = copy.deepcopy(run_clients.CLIENTS["codex-cli"])
        spec["cases"]["expired_and_revoked"] = ["C06"]  # forged-identity evidence posing as revocation
        methods = sorted(run_clients.EXPECTED_TEST_METHODS["codex-cli"])
        matrix = {test_id: {"status": "PASS"} for test_id in run_clients.EXPECTED_TEST_IDS["codex-cli"]}
        errors = run_clients.matrix_integrity_errors("codex-cli", spec, matrix, methods)
        self.assertIn("codex-cli: case_mapping_differs_from_pinned_manifest: expired_and_revoked", errors)

    def test_declared_mappings_equal_the_pinned_manifest(self):
        for name, spec in run_clients.CLIENTS.items():
            self.assertEqual({case: tuple(ids) for case, ids in spec["cases"].items()},
                             run_clients.EXPECTED_CASE_MAPPINGS[name], name)

    def test_methods_without_a_recorded_outcome_are_never_pass(self):
        class SuiteLevelSkip(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise unittest.SkipTest("prerequisite missing")

            def test_C01_first(self):
                pass

            def test_C02_second(self):
                pass

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(SuiteLevelSkip)
        with mock.patch.object(run_clients.unittest.defaultTestLoader, "loadTestsFromName", return_value=suite):
            run = run_clients.run_module("unused", "C")
        self.assertEqual(run["failures"], [])  # unittest itself reports no failure here
        self.assertEqual({k: m["status"] for k, m in run["matrix"].items()}, {"C01": "FAIL", "C02": "FAIL"})
        self.assertEqual(set(run["method_statuses"].values()), {"FAIL"})

    def test_provider_independent_passes_do_not_mark_a_client_executed(self):
        statuses = {m: "NOT_RUN" for m in run_clients.EXPECTED_TEST_METHODS["openai-agents"]}
        for method in run_clients.PROVIDER_INDEPENDENT_METHODS["openai-agents"]:
            statuses[method] = "PASS"
        self.assertFalse(run_clients.client_executed("openai-agents", statuses))
        statuses["test_T03_authorized_synthetic_entity_is_readable"] = "PASS"
        self.assertTrue(run_clients.client_executed("openai-agents", statuses))


class WorkflowFailurePropagationTests(unittest.TestCase):
    def test_fabric_mcp_step_enables_pipefail_for_tee(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/tests.yml").read_text()
        fabric = workflow.split("\n  fabric-mcp:", 1)[1].split("\n  compat-openai-agents:", 1)[0]
        step = fabric.split("- name: Run the whole suite with the real SDK installed", 1)[1]
        step = step.split("- name:", 1)[0]
        self.assertIn("shell: bash", step)
        self.assertIn(" | tee ", step)

    @unittest.skipUnless(shutil.which("bash"), "Bash not available on this platform")
    def test_pipefail_exposes_a_failed_test_producer(self):
        run = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail",
                              "-c", "false | tee /dev/null"], capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)


if __name__ == "__main__":
    unittest.main()
