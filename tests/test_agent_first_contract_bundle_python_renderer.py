from __future__ import annotations

import json
import unittest
from unittest import mock

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

    def test_repeated_schema_validation_decodes_full_bundle_once(self) -> None:
        root_schema = {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            '$id': 'root/v1',
            'type': 'object',
            'additionalProperties': False,
            'required': ['schema_id', 'payload'],
            'properties': {
                'schema_id': {'type': 'string', 'const': 'root/v1'},
                'payload': {'$ref': 'value/v1'},
            },
        }
        value_schema = {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            '$id': 'value/v1',
            'type': 'object',
            'additionalProperties': False,
            'required': ['value'],
            'properties': {'value': {'type': 'string', 'minLength': 1}},
        }
        canonical = json.dumps(
            {
                'families': [
                    {
                        'schemas': [root_schema, value_schema],
                        'fixtures': [],
                    }
                ]
            },
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

        decode = mock.Mock(wraps=namespace['base64'].b64decode)
        loads = mock.Mock(wraps=namespace['json'].loads)
        base64_proxy = mock.Mock(wraps=namespace['base64'])
        json_proxy = mock.Mock(wraps=namespace['json'])
        base64_proxy.b64decode = decode
        json_proxy.loads = loads
        namespace['base64'] = base64_proxy
        namespace['json'] = json_proxy

        first_schema = namespace['schema']('root/v1')
        first_schema['properties']['schema_id']['const'] = 'mutated/v1'
        second_schema = namespace['schema']('root/v1')
        self.assertEqual(
            'root/v1',
            second_schema['properties']['schema_id']['const'],
        )
        document = {'schema_id': 'root/v1', 'payload': {'value': 'ok'}}
        namespace['validate_document']('root/v1', document)
        namespace['validate_document']('root/v1', document)

        canonical_text = canonical.decode('utf-8')
        full_bundle_parses = sum(
            call.args[0] == canonical_text
            for call in loads.call_args_list
            if call.args
        )
        self.assertEqual(1, decode.call_count)
        self.assertEqual(1, full_bundle_parses)

    def test_one_of_public_error_code_decodes_full_bundle_once(self) -> None:
        root_schema = {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            '$id': 'root/v1',
            'type': 'object',
            'additionalProperties': False,
            'required': ['schema_id', 'kind'],
            'properties': {
                'schema_id': {'type': 'string', 'const': 'root/v1'},
                'kind': {'type': 'string'},
            },
            'oneOf': [
                {
                    'type': 'object',
                    'required': ['kind'],
                    'properties': {'kind': {'const': 'alpha'}},
                },
                {
                    'type': 'object',
                    'required': ['kind'],
                    'properties': {'kind': {'const': 'beta'}},
                },
            ],
        }
        error_registry = {
            'fixture_id': 'error_golden_current_registry',
            'document': {
                'entries': [
                    {'code': 'CONTRACT_CONST_INVALID'},
                    {'code': 'CONTRACT_DOCUMENT_INVALID'},
                ]
            },
        }
        canonical = json.dumps(
            {
                'families': [
                    {
                        'schemas': [root_schema],
                        'fixtures': [error_registry],
                    }
                ]
            },
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

        decode = mock.Mock(wraps=namespace['base64'].b64decode)
        loads = mock.Mock(wraps=namespace['json'].loads)
        base64_proxy = mock.Mock(wraps=namespace['base64'])
        json_proxy = mock.Mock(wraps=namespace['json'])
        base64_proxy.b64decode = decode
        json_proxy.loads = loads
        namespace['base64'] = base64_proxy
        namespace['json'] = json_proxy

        document = {'schema_id': 'root/v1', 'kind': 'beta'}
        namespace['validate_document']('root/v1', document)
        namespace['validate_document']('root/v1', document)

        canonical_text = canonical.decode('utf-8')
        full_bundle_parses = sum(
            call.args[0] == canonical_text
            for call in loads.call_args_list
            if call.args
        )
        self.assertEqual(1, decode.call_count)
        self.assertEqual(1, full_bundle_parses)

    def test_missing_error_registry_uses_public_default_and_decodes_once(
        self,
    ) -> None:
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

        decode = mock.Mock(wraps=namespace['base64'].b64decode)
        loads = mock.Mock(wraps=namespace['json'].loads)
        base64_proxy = mock.Mock(wraps=namespace['base64'])
        json_proxy = mock.Mock(wraps=namespace['json'])
        base64_proxy.b64decode = decode
        json_proxy.loads = loads
        namespace['base64'] = base64_proxy
        namespace['json'] = json_proxy

        invalid_actor = {
            'schema_id': 'actor/v1',
            'kind': 'task_owner',
            'id': 'owner_1',
            'session_id': None,
        }
        for _ in range(2):
            with self.assertRaises(
                namespace['ContractValidationError']
            ) as raised:
                namespace['validate_document']('actor/v1', invalid_actor)
            self.assertEqual(
                'CONTRACT_DOCUMENT_INVALID',
                raised.exception.code,
            )
            self.assertEqual('ACTOR_SESSION_INVALID', raised.exception.detail)

        canonical_text = canonical.decode('utf-8')
        full_bundle_parses = sum(
            call.args[0] == canonical_text
            for call in loads.call_args_list
            if call.args
        )
        self.assertEqual(1, decode.call_count)
        self.assertEqual(1, full_bundle_parses)

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
