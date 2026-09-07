"""Declared review producers must precede every path to the consumer."""
import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import template_role_validator as validator
import workflow_selector

class ReviewPhaseTests(unittest.TestCase):
    def setUp(self):
        self.template = workflow_selector.load_template('standard_code_change')
    def test_current_code_change_preconditions_are_reachable(self):
        self.assertEqual(validator.validate_phase_prerequisites(self.template), [])
    def test_later_qa_cannot_be_required_before_review(self):
        self.template['steps'][1]['requires_prior_steps'] = ['qa']
        self.assertIn('phase_prerequisite_unreachable:review:qa', validator.validate_phase_prerequisites(self.template))
    def test_missing_specialist_cannot_be_required(self):
        self.template['steps'][1]['requires_prior_steps'] = ['tech-qa']
        self.assertTrue(validator.validate_phase_prerequisites(self.template))
    def test_branch_bypassing_producer_is_rejected(self):
        entry = copy.deepcopy(self.template['steps'][0]); entry['id'] = 'entry'
        entry['transitions'] = [{'to':'implement'}, {'to':'review'}]
        self.template['steps'].insert(0, entry); self.template['initial_step'] = 'entry'
        self.assertIn('phase_prerequisite_unreachable:review:implement', validator.validate_phase_prerequisites(self.template))
    def test_runtime_contract_validation_rejects_invalid_order(self):
        self.template['steps'][1]['requires_prior_steps'] = ['qa']
        errors = workflow_selector.validate_template(self.template, workflow_selector.REPO_ROOT / 'template.json', workflow_selector.load_registry())
        self.assertIn('phase_prerequisite_unreachable:review:qa', errors)
    def test_malformed_and_self_dependencies_are_rejected(self):
        for value in ('qa', ['review'], [False], ['implement','implement']):
            with self.subTest(value=value):
                self.template['steps'][1]['requires_prior_steps'] = value
                self.assertTrue(validator.validate_phase_prerequisites(self.template))

if __name__ == '__main__': unittest.main()
