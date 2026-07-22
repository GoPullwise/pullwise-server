from __future__ import annotations

import json
from pathlib import Path
import types
import unittest

from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper


ROOT = Path(__file__).resolve().parents[1]
FAMILY_ROOT = ROOT / 'contracts/agent-first/current/source/families'


class AgentFirstContractBundlePythonFixturesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.families = [
            json.loads(path.read_text(encoding='utf-8'))
            for path in sorted(FAMILY_ROOT.glob('*.json'))
        ]
        canonical = json.dumps(
            {'families': cls.families},
            ensure_ascii=False,
            allow_nan=False,
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf-8')
        wrapper_bytes = render_python_wrapper(
            '@pullwise/agent-task-contract',
            '0.1.0',
            '0' * 64,
            '1' * 64,
            canonical,
        )
        cls.wrapper = types.ModuleType('_agent_first_fixture_wrapper')
        exec(wrapper_bytes, cls.wrapper.__dict__)

    def assert_family_fixtures_execute(self, family_id: str) -> None:
        family = next(
            item for item in self.families if item['family_id'] == family_id
        )
        for fixture in family['fixtures']:
            with self.subTest(fixture_id=fixture['fixture_id']):
                if fixture['fixture_class'] == 'negative':
                    with self.assertRaises(
                        self.wrapper.ContractValidationError
                    ) as raised:
                        self.wrapper.validate_document(
                            fixture['schema_id'],
                            fixture['document'],
                        )
                    self.assertEqual(
                        fixture['expected_code'],
                        raised.exception.code,
                    )
                else:
                    validated = self.wrapper.validate_document(
                        fixture['schema_id'],
                        fixture['document'],
                    )
                    self.assertEqual(fixture['document'], validated)

    def test_task_request_fixtures_execute(self) -> None:
        self.assert_family_fixtures_execute('task-request')

    def test_effective_execution_policy_fixtures_execute(self) -> None:
        self.assert_family_fixtures_execute('effective-execution-policy')

    def test_change_set_patch_fixtures_execute(self) -> None:
        self.assert_family_fixtures_execute('change-set-patch')


if __name__ == '__main__':
    unittest.main()
