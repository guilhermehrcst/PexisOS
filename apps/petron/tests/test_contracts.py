import itertools
import unittest

from petron.mission import (TERMINAL, TRANSITIONS, IllegalTransition, MissionSpec, MissionState, PlanRejected,
                            check_transition, validate_plan)
from petron.schema import SchemaError, closed_object, validate
from petron.tools import tool_schemas

from support import E001, e001_spec


class SchemaTests(unittest.TestCase):
    def test_closed_object_rejects_extra_property(self):
        schema = closed_object({"path": {"type": "string"}}, ["path"])
        with self.assertRaises(SchemaError):
            validate(schema, {"path": "a", "permissions": ["shell"]})

    def test_open_object_schema_is_a_programming_error(self):
        with self.assertRaises(TypeError):
            validate({"type": "object", "properties": {}}, {})

    def test_unknown_keyword_is_a_programming_error(self):
        with self.assertRaises(TypeError):
            validate({"type": "string", "format": "email"}, "x")

    def test_bool_is_not_integer(self):
        with self.assertRaises(SchemaError):
            validate({"type": "integer"}, True)


class SpecTests(unittest.TestCase):
    def test_fixture_spec_parses(self):
        spec = MissionSpec.parse(e001_spec(E001 / "project"))
        self.assertEqual(spec.mission_id, "petron-e001")
        self.assertEqual(len(spec.sha256), 64)

    def test_extra_top_level_field_rejected(self):
        with self.assertRaises(SchemaError):
            MissionSpec.parse({**e001_spec(E001), "admin": True})

    def test_writable_inside_protected_rejected(self):
        spec = e001_spec(E001)
        spec["workspace"]["writable"] = ["tests/test_stats.py"]
        with self.assertRaises(SchemaError):
            MissionSpec.parse(spec)

    def test_dotdot_path_rejected(self):
        spec = e001_spec(E001)
        spec["workspace"]["writable"] = ["calc/../../etc/passwd"]
        with self.assertRaises(SchemaError):
            MissionSpec.parse(spec)

    def test_docker_requires_digest_pinned_image(self):
        spec = e001_spec(E001, sandbox={"backend": "docker", "image": "python:3.11-slim"})
        with self.assertRaises(SchemaError):
            MissionSpec.parse(spec)

    def test_plan_mode_keeps_the_v01_model_call_bound(self):
        spec = e001_spec(E001)
        spec["budget"]["max_model_calls"] = 9
        with self.assertRaises(SchemaError):
            MissionSpec.parse(spec)

    def test_real_provider_requires_model_call_budget(self):
        spec = e001_spec(E001, provider={"kind": "anthropic"})
        with self.assertRaises(SchemaError):
            MissionSpec.parse(spec)


class StateMachineTests(unittest.TestCase):
    def test_every_pair_matches_table(self):
        for current, target in itertools.product(MissionState, MissionState):
            if target in TRANSITIONS[current]:
                check_transition(current, target)
            else:
                with self.assertRaises(IllegalTransition):
                    check_transition(current, target)

    def test_terminal_states_have_no_exit(self):
        for state in TERMINAL:
            self.assertEqual(TRANSITIONS[state], frozenset())

    def test_no_path_skips_approval(self):
        self.assertNotIn(MissionState.RUNNING, TRANSITIONS[MissionState.PLANNING])
        self.assertNotIn(MissionState.RUNNING, TRANSITIONS[MissionState.CREATED])
        self.assertNotIn(MissionState.COMPLETED, TRANSITIONS[MissionState.RUNNING])


class PlanValidationTests(unittest.TestCase):
    def setUp(self):
        self.spec = MissionSpec.parse(e001_spec(E001 / "project"))
        self.schemas = tool_schemas()

    def plan(self, steps):
        return {"schema": "petron.plan.v0", "rationale": "", "steps": steps}

    def reason(self, plan):
        with self.assertRaises(PlanRejected) as caught:
            validate_plan(plan, self.spec, self.schemas)
        return caught.exception.reason

    def test_extra_plan_field_rejected(self):
        self.assertEqual(self.reason({**self.plan([{"tool": "list_files", "args": {}}]), "grants": ["*"]}),
                         "plan_shape")

    def test_argument_escalation_rejected(self):
        steps = [{"tool": "read_file", "args": {"path": "calc/stats.py", "permissions": ["shell"]}}]
        self.assertTrue(self.reason(self.plan(steps)).startswith("invalid_arguments:0"))

    def test_unpermitted_registered_tool_rejected(self):
        self.assertEqual(self.reason(self.plan([{"tool": "pexisos.list_entities", "args": {}}])),
                         "tool_not_permitted:0")

    def test_step_budget_enforced(self):
        self.assertEqual(self.reason(self.plan([{"tool": "list_files", "args": {}}] * 9)), "plan_step_count")

    def test_empty_plan_rejected(self):
        self.assertEqual(self.reason(self.plan([])), "plan_step_count")


if __name__ == "__main__":
    unittest.main()
