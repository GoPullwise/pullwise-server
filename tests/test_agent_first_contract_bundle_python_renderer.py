from __future__ import annotations

import json
import unittest

from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper


class AgentFirstContractBundlePythonRendererTest(unittest.TestCase):
    def test_rendered_wrapper_compiles(self) -> None:
        rendered = render_python_wrapper(
            '@pullwise/agent-task-contract',
            '0.1.0',
            '0' * 64,
            '1' * 64,
            b'{}',
        )

        compile(rendered, 'generated-wrapper.py', 'exec')

    def test_validate_document_executes_declared_actor_rule(self) -> None:
        actor_schema = {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            '$id': 'actor/v1',
            'type': 'object',
            'additionalProperties': False,
            'required': ['schema_id', 'kind', 'id', 'session_id'],
            'properties': {
                'schema_id': {'type': 'string', 'const': 'actor/v1'},
                'kind': {
                    'type': 'string',
                    'enum': ['task_owner', 'worker_control'],
                },
                'id': {'type': 'string', 'minLength': 1},
                'session_id': {'type': ['string', 'null']},
            },
            'x-pullwise-semantics': {
                'document_rules': ['actor'],
                'contextual_helpers': [],
            },
        }
        canonical = json.dumps(
            {'families': [{'schemas': [actor_schema], 'fixtures': []}]},
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf-8')
        rendered = render_python_wrapper(
            '@pullwise/agent-task-contract',
            '0.1.0',
            '0' * 64,
            '1' * 64,
            canonical,
        )
        namespace: dict[str, object] = {}
        exec(rendered, namespace)

        with self.assertRaises(namespace['ContractValidationError']) as raised:
            namespace['validate_document'](
                'actor/v1',
                {
                    'schema_id': 'actor/v1',
                    'kind': 'task_owner',
                    'id': 'owner_1',
                    'session_id': None,
                },
            )

        self.assertEqual('ACTOR_SESSION_INVALID', raised.exception.detail)


if __name__ == '__main__':
    unittest.main()
