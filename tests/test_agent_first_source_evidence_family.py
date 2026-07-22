from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = (
    REPO_ROOT
    / 'contracts'
    / 'agent-first'
    / 'current'
    / 'source'
    / 'families'
    / 'source-evidence.json'
)
SCHEMAS = (
    'change-set-patch/v1',
    'change-set/v1',
    'execution-profile/v1',
    'execution-state-manifest/v1',
    'source-selection-policy/v1',
    'source-tree-manifest/v1',
)
SEMANTICS = {
    'change-set-patch/v1': 'change_set_patch',
    'change-set/v1': 'change_set',
    'execution-profile/v1': 'execution_profile',
    'execution-state-manifest/v1': 'execution_state_manifest',
    'source-selection-policy/v1': 'source_selection_policy',
    'source-tree-manifest/v1': 'source_tree_manifest',
}


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')


def sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sealed(document: dict[str, object], schema: dict[str, object]) -> bool:
    spec = schema['x-pullwise-digest']
    field, domain = spec['field'], spec['domain']
    unsigned = {key: value for key, value in document.items() if key != field}
    expected = hashlib.sha256(
        domain.encode('utf-8') + b'\0' + canonical_bytes(unsigned)
    ).hexdigest()
    return document[field] == expected


def typed(reference: object, schema_id: str) -> bool:
    return (
        isinstance(reference, dict)
        and reference.get('schema_id') == 'content-ref/v1'
        and reference.get('content_schema_id') == schema_id
    )


def ordered_unique(values: list[object], key) -> bool:
    keys = [key(value) for value in values]
    return keys == sorted(keys) and len(keys) == len(set(keys))


def valid_entry(entry: object) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
        return False
    branch = entry.get('type')
    expected = {
        'file': {'path', 'type', 'size_bytes', 'sha256', 'executable'},
        'symlink': {'path', 'type', 'target'},
        'gitlink': {'path', 'type', 'commit_sha'},
    }.get(branch)
    return expected is not None and set(entry) == expected


def valid_source_tree(document: dict[str, object]) -> bool:
    entries = document['entries']
    assert isinstance(entries, list)
    if not typed(document['selection_policy_ref'], 'source-selection-policy/v1'):
        return False
    if not ordered_unique(entries, lambda item: item['path'].encode('utf-8')):
        return False
    if not all(valid_entry(item) for item in entries):
        return False
    if len({item['path'].casefold() for item in entries}) != len(entries):
        return False
    total = sum(item['size_bytes'] for item in entries if item['type'] == 'file')
    if document['entry_count'] != len(entries) or document['total_bytes'] != total:
        return False
    identity = {
        'base_revision': document['base_revision'],
        'selection_policy_digest': document['selection_policy_digest'],
        'entries': entries,
    }
    return document['source_state_id'] == sha256(identity)


def valid_change_set(document: dict[str, object]) -> bool:
    if not typed(document['patch_ref'], 'change-set-patch/v1'):
        return False
    paths: list[str] = []
    for group in ('added', 'modified', 'deleted', 'type_changed'):
        items = document[group]
        assert isinstance(items, list)
        item_paths: list[str] = []
        for item in items:
            expected = {'after'} if group == 'added' else {'before'}
            if group in ('modified', 'type_changed'):
                expected = {'before', 'after'}
            if set(item) != expected or not all(valid_entry(value) for value in item.values()):
                return False
            before, after = item.get('before'), item.get('after')
            path = (after or before)['path']
            if before and after:
                if before['path'] != after['path'] or before == after:
                    return False
                same_type = before['type'] == after['type']
                if same_type != (group == 'modified'):
                    return False
            item_paths.append(path)
        if item_paths != sorted(item_paths, key=lambda value: value.encode('utf-8')):
            return False
        paths.extend(item_paths)
    return (
        bool(paths)
        and len(paths) == len(set(paths))
        and document['original_source_state_id'] != document['final_source_state_id']
    )


def valid_execution_state(document: dict[str, object]) -> bool:
    if not typed(document['execution_profile_ref'], 'execution-profile/v1'):
        return False
    configs = document['config_and_fixtures']
    if not all(typed(item, 'source-content/v1') for item in configs):
        return False
    checks = (
        ordered_unique(document['toolchain'], lambda item: item['tool_id']),
        ordered_unique(
            configs,
            lambda item: (item['content_schema_id'], item['artifact_id'], item['sha256']),
        ),
        ordered_unique(document['services'], lambda item: item['service_id']),
        ordered_unique(document['environment'], lambda item: item['key']),
    )
    if not all(checks):
        return False
    for item in document['environment']:
        keys = set(item)
        if item['kind'] == 'value' and keys != {'kind', 'key', 'value'}:
            return False
        if item['kind'] == 'secret_ref' and keys != {
            'kind', 'key', 'secret_key_id', 'secret_version'
        }:
            return False
    unsigned = {
        key: value
        for key, value in document.items()
        if key not in {'execution_state_id', 'manifest_digest'}
    }
    return document['execution_state_id'] == sha256(unsigned)


def valid_document(document: dict[str, object], schema: dict[str, object]) -> bool:
    if set(document) != set(schema['required']) or not sealed(document, schema):
        return False
    schema_id = schema['$id']
    if schema_id == 'change-set-patch/v1':
        try:
            raw = base64.b64decode(document['data_base64'], validate=True)
        except (ValueError, TypeError):
            return False
        return (
            base64.b64encode(raw).decode('ascii') == document['data_base64']
            and len(raw) == document['size_bytes']
            and hashlib.sha256(raw).hexdigest() == document['byte_sha256']
        )
    if schema_id == 'change-set/v1':
        return valid_change_set(document)
    if schema_id == 'execution-profile/v1':
        return (
            document['operating_system'] == 'linux'
            and document['cpu_architecture'] in {'aarch64', 'x86_64'}
            and str(document['image_identity']).startswith('sha256:')
        )
    if schema_id == 'execution-state-manifest/v1':
        return valid_execution_state(document)
    if schema_id == 'source-selection-policy/v1':
        excluded = document['excluded_control_roots']
        ephemeral = document['ephemeral_patterns']
        return (
            document['include'] == 'all_repository_regular_files'
            and '.git/' in excluded
            and ordered_unique(excluded, lambda item: item.encode('utf-8'))
            and ordered_unique(ephemeral, lambda item: item.encode('utf-8'))
        )
    if schema_id == 'source-tree-manifest/v1':
        return valid_source_tree(document)
    return False


class AgentFirstSourceEvidenceFamilyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.family = json.loads(FAMILY_PATH.read_text(encoding='utf-8'))
        cls.schemas = {item['$id']: item for item in cls.family['schemas']}

    def test_family_has_closed_sorted_schema_and_semantic_registry(self) -> None:
        self.assertEqual('source-evidence', self.family['family_id'])
        self.assertEqual(list(SCHEMAS), [item['$id'] for item in self.family['schemas']])
        self.assertEqual(set(SCHEMAS), set(self.schemas))
        for schema_id, semantic in SEMANTICS.items():
            schema = self.schemas[schema_id]
            self.assertEqual('object', schema['type'])
            self.assertIs(False, schema['additionalProperties'])
            self.assertEqual([semantic], schema['x-pullwise-semantics'])
            self.assertEqual(set(schema['required']), set(schema['properties']))

    def test_content_references_have_finite_registered_targets(self) -> None:
        tree = self.schemas['source-tree-manifest/v1']['properties']
        change = self.schemas['change-set/v1']['properties']
        execution = self.schemas['execution-state-manifest/v1']['properties']
        self.assertEqual(
            'source-selection-policy/v1',
            tree['selection_policy_ref']['x-pullwise-content-schema-id'],
        )
        self.assertEqual(
            'change-set-patch/v1',
            change['patch_ref']['x-pullwise-content-schema-id'],
        )
        self.assertEqual(
            'execution-profile/v1',
            execution['execution_profile_ref']['x-pullwise-content-schema-id'],
        )
        self.assertEqual(
            'source-content/v1',
            execution['config_and_fixtures']['items']['x-pullwise-content-schema-id'],
        )

    def test_full_fixtures_execute_and_idempotency_is_byte_exact(self) -> None:
        fixtures = self.family['fixtures']
        self.assertEqual(
            sorted(item['fixture_id'] for item in fixtures),
            [item['fixture_id'] for item in fixtures],
        )
        by_schema: dict[str, dict[str, list[dict[str, object]]]] = {}
        for fixture in fixtures:
            schema_id, fixture_class = fixture['schema_id'], fixture['fixture_class']
            by_schema.setdefault(schema_id, {}).setdefault(fixture_class, []).append(fixture)
            document = fixture['document']
            self.assertEqual(schema_id, document['schema_id'])
            self.assertEqual(
                set(self.schemas[schema_id]['required']),
                set(document),
                fixture['fixture_id'],
            )
        for schema_id in SCHEMAS:
            classes = by_schema[schema_id]
            self.assertTrue(classes['golden'])
            self.assertTrue(classes['negative'])
            self.assertTrue(classes['idempotency'])
            golden = classes['golden'][0]['document']
            idempotent = classes['idempotency'][0]['document']
            self.assertTrue(valid_document(golden, self.schemas[schema_id]), schema_id)
            self.assertEqual(canonical_bytes(golden), canonical_bytes(idempotent))
            for fixture in classes['negative']:
                self.assertFalse(
                    valid_document(fixture['document'], self.schemas[schema_id]),
                    fixture['fixture_id'],
                )

    def test_adversarial_fixtures_cover_wrong_target_and_changed_tree(self) -> None:
        fixtures = {item['fixture_id']: item for item in self.family['fixtures']}
        wrong = fixtures['source_evidence_negative_source_tree_wrong_target']
        changed = fixtures['source_evidence_negative_source_tree_changed_state']
        self.assertEqual(
            'execution-profile/v1',
            wrong['document']['selection_policy_ref']['content_schema_id'],
        )
        self.assertNotEqual(
            changed['document']['source_state_id'],
            sha256(
                {
                    'base_revision': changed['document']['base_revision'],
                    'selection_policy_digest': changed['document']['selection_policy_digest'],
                    'entries': changed['document']['entries'],
                }
            ),
        )


if __name__ == '__main__':
    unittest.main()
