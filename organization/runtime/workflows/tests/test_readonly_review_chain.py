#!/usr/bin/env python3
"""Issue #106 declaration/selection contracts, not multi-step runtime execution."""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
WF = ROOT / 'organization/runtime/workflows'
sys.path.insert(0, str(WF / 'scripts'))
sys.path.insert(0, str(Path(__file__).parent))
import workflow_selector as selector
import template_role_validator
import work_order_builder
from test_workflow_selector import typed_classification

ID = 'readonly_review_chain'
OPS = dict.fromkeys(('edit', 'commit', 'push', 'network'), False)


def schema(name):
    return json.loads((WF / 'schemas' / name).read_text())


def errors(value, spec):
    return work_order_builder._validate_schema_fragment(value, spec, '$')


class ReadonlyChainTests(unittest.TestCase):
    def template(self):
        value = selector.load_template(ID)
        self.assertIsNotNone(value, 'readonly chain must be registered')
        return value

    def test_chain_contract_and_roles(self):
        t = self.template()
        self.assertEqual([], selector.validate_template(t, WF / 'templates' / (ID + '.yaml'), selector.load_registry()))
        self.assertEqual([], errors(t, schema('workflow-template.schema.json')))
        self.assertEqual('readonly', t['safety_class'])
        self.assertEqual(('research', 3), (t['initial_step'], t['max_steps']))
        self.assertEqual(['research', 'review', 'final_evidence'], [s['id'] for s in t['steps']])
        for step, event, target, adapter in zip(t['steps'], ('research_complete', 'review_complete', 'final_evidence_valid'), ('review', 'final_evidence', 'complete'), ('bounded_provider', 'bounded_provider', 'harness_gate')):
            self.assertEqual('readonly', step['permission_mode'])
            self.assertEqual(OPS, step['allowed_ops'])
            self.assertEqual(adapter, step['provider_route']['adapter_kind'])
            self.assertIn({'on': event, 'to': target}, step['transitions'])
            self.assertTrue((ROOT / t['output_contracts'][step['output_contract']]['schema_path']).is_file())
        self.assertNotIn('tmux_interactive', t['provider_adapter']['allowed_transports'])
        self.assertNotIn('tmux', json.dumps(t))
        self.assertEqual('ok', template_role_validator.validate_template_roles()['decision'])
        self.assertEqual('ok', selector.validate_contracts()['decision'])

    def test_selection_and_unchanged_precedence(self):
        c = typed_classification('research', external_provider_required=True, expected_artifacts=['research_report', 'typed_report', 'final_evidence'])
        for _ in range(3):
            result = selector.select_workflow(c)
            self.assertEqual(ID, selector.candidate_workflow_id(c))
            self.assertEqual('blocked', result['decision'])
            self.assertEqual('readonly_chain_runtime_unavailable', result['workflow_selection']['reason'])
            self.assertEqual([ID], result['workflow_selection']['candidates'])
            scope = selector.activation_scope_for_selection(result['workflow_selection'], c, allowed_paths=['evidence'], expires_at='2099-01-01T00:00:00Z')
            self.assertEqual(OPS, scope['allowed_ops'])
            self.assertEqual(1, scope['step_budget'])
        self.assertEqual('research_only', selector.candidate_workflow_id(typed_classification('research')))
        for updates, expected in [({'security_sensitive': True}, 'security_sensitive_change'), ({'publication_required': True}, 'publication_required')]:
            self.assertEqual(expected, selector.candidate_workflow_id(dict(c, **updates)))
        self.assertEqual('blocked', selector.select_workflow(dict(c, destructive_operation=True))['decision'])
        self.assertNotEqual('selected', selector.select_workflow(dict(c, external_provider_required=False))['decision'])

    def test_template_rejects_write_ops_and_missing_permissions(self):
        for index in range(3):
            for op in OPS:
                for invalid in (True, 0, None, 'false'):
                    with self.subTest(index=index, op=op, invalid=invalid):
                        t = self.template()
                        t['steps'][index]['allowed_ops'][op] = invalid
                        self.assertTrue(selector.validate_template(t, Path('test'), selector.load_registry()))
            for field in ('allowed_ops', 'permission_mode'):
                t = self.template()
                del t['steps'][index][field]
                self.assertTrue(selector.validate_template(t, Path('test'), selector.load_registry()))
            t = self.template()
            t['steps'][index]['permission_mode'] = 'edit'
            self.assertTrue(selector.validate_template(t, Path('test'), selector.load_registry()))
            self.assertTrue(errors(t, schema('workflow-template.schema.json')))
            t = self.template()
            t['steps'][index]['allowed_ops']['shell'] = True
            self.assertTrue(selector.validate_template(t, Path('test'), selector.load_registry()))

    def test_work_order_step_constraints(self):
        # Exercise the schema's workflow/step conditional, without constructing a runtime run.
        spec = {'allOf': schema('work-order.schema.json')['allOf']}
        for step in self.template()['steps']:
            order = {'workflow_id': ID, 'step_id': step['id'], 'assignment_role': step['assignment_role'], 'to_role': step['role'], 'expected_output': step['output_contract'], 'permission_mode': 'readonly', 'external_provider_allowed': step['id'] != 'final_evidence', 'activation_scope': {'step_budget': 3, 'allowed_ops': OPS.copy()}}
            self.assertEqual([], errors(order, spec))
            for op in OPS:
                changed = copy.deepcopy(order)
                changed['activation_scope']['allowed_ops'][op] = True
                self.assertTrue(errors(changed, spec))
            for key, value in [('permission_mode', 'edit'), ('step_id', 'implement'), ('to_role', 'git-publisher'), ('expected_output', 'code_change_report')]:
                self.assertTrue(errors(dict(order, **{key: value}), spec))

    def test_research_schema_reuse(self):
        report = {'report_version': '1', 'workflow_id': ID, 'step_id': 'research', 'result': 'findings', 'source_refs': ['evidence/source'], 'findings': [{'summary': 'found', 'evidence_refs': ['evidence/source']}], 'uncertainty': [], 'no_diff_completion': True}
        s = schema('research-report.schema.json')
        self.assertEqual([], errors(report, s))
        self.assertTrue(errors(dict(report, step_id='review'), s))
        self.assertTrue(errors(dict(report, no_diff_completion=False), s))
        del report['step_id']
        self.assertTrue(errors(report, s))
        self.assertEqual([], errors(dict(report, workflow_id='research_only'), s))

    def test_final_evidence_acceptance_is_fail_closed(self):
        s = schema('readonly-final-evidence-report.schema.json')
        report = {'report_version': '1', 'workflow_id': ID, 'step_id': 'final_evidence', 'result': 'complete', 'research_report_ref': 'reports/research.json', 'review_report_ref': 'reports/review.json', 'review_status': 'pass', 'validation_status': 'passed', 'evidence_refs': ['evidence/validation'], 'no_diff_completion': True}
        self.assertEqual([], errors(report, s))
        for key in report:
            value = report.copy()
            del value[key]
            self.assertTrue(errors(value, s), key)
        for key, value in [('review_status', 'changes_requested'), ('validation_status', 'failed'), ('validation_status', 'not_run'), ('no_diff_completion', False), ('evidence_refs', []), ('step_id', 'review')]:
            self.assertTrue(errors(dict(report, **{key: value}), s))

    def test_review_report_schema_reuse(self):
        report = json.loads((WF / 'tests/fixtures/run-artifacts/complete-pass/reports/run-complete-pass/review-external-review-report.json').read_text())
        s = schema('external-review-report.schema.json')
        self.assertEqual([], errors(report, s))
        report['workflow_id'] = ID
        self.assertEqual([], errors(report, s))
        for key, value in [('workflow_id', 'unknown'), ('step_id', 'research'), ('result', 'findings')]:
            self.assertTrue(errors(dict(report, **{key: value}), s))

    def test_final_evidence_no_diff_requires_boolean_true(self):
        spec = schema('readonly-final-evidence-report.schema.json')
        report = {
            'report_version': '1',
            'workflow_id': ID,
            'step_id': 'final_evidence',
            'result': 'complete',
            'research_report_ref': 'reports/research.json',
            'review_report_ref': 'reports/review.json',
            'review_status': 'pass',
            'validation_status': 'passed',
            'evidence_refs': ['evidence/validation'],
            'no_diff_completion': True,
        }
        self.assertEqual([], errors(report, spec))
        for value in (1, 1.0):
            with self.subTest(value=value, type=type(value).__name__):
                changed = dict(report, no_diff_completion=value)
                self.assertTrue(errors(changed, spec))


if __name__ == '__main__':
    unittest.main(verbosity=2)
