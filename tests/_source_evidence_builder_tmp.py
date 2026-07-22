from __future__ import annotations

import base64
import copy
import hashlib
import json


SAFE_INTEGER = 9007199254740991
DRAFT = 'https://json-schema.org/draft/2020-12/schema'


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')


def sha(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def digest_schema(schema_id: str, field: str, semantic: str, properties: dict, required: list[str]):
    return {
        '$schema': DRAFT,
        '$id': schema_id,
        'type': 'object',
        'additionalProperties': False,
        'x-pullwise-digest': {'field': field, 'domain': f'pullwise:{schema_id}'},
        'x-pullwise-semantics': [semantic],
        'required': required,
        'properties': properties,
    }


def typed_ref(schema_id: str) -> dict:
    return {
        '$ref': 'content-ref/v1',
        'x-pullwise-content-schema-id': schema_id,
    }


def string(**rule) -> dict:
    return {'type': 'string', **rule}


def digest_string() -> dict:
    return string(pattern='^[0-9a-f]{64}$')


def safe_integer() -> dict:
    return {'type': 'integer', 'minimum': 0, 'maximum': SAFE_INTEGER}


def entry_rule() -> dict:
    return {
        'type': 'object',
        'additionalProperties': False,
        'required': ['path', 'type'],
        'properties': {
            'path': string(minLength=1, maxLength=4096, pattern='^[^\\x00\\\\]+$'),
            'type': {'enum': ['file', 'gitlink', 'symlink']},
            'size_bytes': safe_integer(),
            'sha256': digest_string(),
            'executable': {'type': 'boolean'},
            'target': string(maxLength=4096),
            'commit_sha': string(pattern='^[0-9a-f]{40}$'),
        },
    }


def change_items() -> dict:
    return {
        'type': 'array',
        'maxItems': 1000000,
        'uniqueItems': True,
        'items': {
            'type': 'object',
            'additionalProperties': False,
            'required': [],
            'properties': {
                'before': entry_rule(),
                'after': entry_rule(),
            },
        },
    }


def schemas() -> list[dict]:
    patch = digest_schema(
        'change-set-patch/v1',
        'patch_digest',
        'change_set_patch',
        {
            'schema_id': {'const': 'change-set-patch/v1'},
            'format': {'const': 'unified_diff'},
            'media_type': {'const': 'text/x-diff'},
            'encoding': {'const': 'base64'},
            'data_base64': string(pattern='^[A-Za-z0-9+/]*={0,2}$'),
            'byte_sha256': digest_string(),
            'size_bytes': safe_integer(),
            'patch_digest': digest_string(),
        },
        ['schema_id', 'format', 'media_type', 'encoding', 'data_base64', 'byte_sha256', 'size_bytes', 'patch_digest'],
    )
    change = digest_schema(
        'change-set/v1',
        'change_set_digest',
        'change_set',
        {
            'schema_id': {'const': 'change-set/v1'},
            'original_source_state_id': digest_string(),
            'final_source_state_id': digest_string(),
            'added': change_items(),
            'modified': change_items(),
            'deleted': change_items(),
            'type_changed': change_items(),
            'patch_ref': typed_ref('change-set-patch/v1'),
            'change_set_digest': digest_string(),
        },
        ['schema_id', 'original_source_state_id', 'final_source_state_id', 'added', 'modified', 'deleted', 'type_changed', 'patch_ref', 'change_set_digest'],
    )
    profile = digest_schema(
        'execution-profile/v1',
        'profile_digest',
        'execution_profile',
        {
            'schema_id': {'const': 'execution-profile/v1'},
            'image_identity': string(pattern='^sha256:[0-9a-f]{64}$'),
            'operating_system': {'const': 'linux'},
            'sandbox_identity': string(minLength=1, maxLength=160, pattern='^[A-Za-z0-9][A-Za-z0-9._:/-]*$'),
            'cpu_architecture': {'enum': ['aarch64', 'x86_64']},
            'profile_digest': digest_string(),
        },
        ['schema_id', 'image_identity', 'operating_system', 'sandbox_identity', 'cpu_architecture', 'profile_digest'],
    )
    tool = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['tool_id', 'binary_identity', 'version', 'binary_sha256'],
        'properties': {
            'tool_id': string(minLength=1, maxLength=160, pattern='^[A-Za-z0-9][A-Za-z0-9._:/-]*$'),
            'binary_identity': string(minLength=2, maxLength=4096, pattern='^/[^\\x00]+$'),
            'version': string(minLength=1, maxLength=160),
            'binary_sha256': digest_string(),
        },
    }
    service = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['service_id', 'version', 'endpoint_class', 'fingerprint'],
        'properties': {
            'service_id': string(minLength=1, maxLength=160, pattern='^[A-Za-z0-9][A-Za-z0-9._:/-]*$'),
            'version': string(minLength=1, maxLength=160),
            'endpoint_class': {'enum': ['local_process', 'loopback', 'private_network', 'public_network']},
            'fingerprint': digest_string(),
        },
    }
    environment = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['kind', 'key'],
        'properties': {
            'kind': {'enum': ['secret_ref', 'value']},
            'key': string(minLength=1, maxLength=160, pattern='^[A-Z_][A-Z0-9_]*$'),
            'value': string(maxLength=4096),
            'secret_key_id': string(pattern='^secret_[0-9a-f]{32}$'),
            'secret_version': string(minLength=1, maxLength=160),
        },
    }
    execution = digest_schema(
        'execution-state-manifest/v1',
        'manifest_digest',
        'execution_state_manifest',
        {
            'schema_id': {'const': 'execution-state-manifest/v1'},
            'source_state_id': digest_string(),
            'execution_profile_ref': typed_ref('execution-profile/v1'),
            'execution_profile_digest': digest_string(),
            'toolchain': {'type': 'array', 'maxItems': 4096, 'uniqueItems': True, 'items': tool},
            'config_and_fixtures': {'type': 'array', 'maxItems': 4096, 'uniqueItems': True, 'items': typed_ref('source-content/v1')},
            'services': {'type': 'array', 'maxItems': 4096, 'uniqueItems': True, 'items': service},
            'environment': {'type': 'array', 'maxItems': 4096, 'uniqueItems': True, 'items': environment},
            'locale': string(minLength=1, maxLength=64),
            'timezone': string(minLength=1, maxLength=64),
            'execution_state_id': digest_string(),
            'manifest_digest': digest_string(),
        },
        ['schema_id', 'source_state_id', 'execution_profile_ref', 'execution_profile_digest', 'toolchain', 'config_and_fixtures', 'services', 'environment', 'locale', 'timezone', 'execution_state_id', 'manifest_digest'],
    )
    policy = digest_schema(
        'source-selection-policy/v1',
        'policy_digest',
        'source_selection_policy',
        {
            'schema_id': {'const': 'source-selection-policy/v1'},
            'root_identity': string(pattern='^root_[0-9a-f]{32}$'),
            'include': {'const': 'all_repository_regular_files'},
            'excluded_control_roots': {'type': 'array', 'minItems': 1, 'maxItems': 128, 'uniqueItems': True, 'items': string(minLength=2, maxLength=4096)},
            'ephemeral_patterns': {'type': 'array', 'maxItems': 128, 'uniqueItems': True, 'items': string(minLength=1, maxLength=4096)},
            'symlink_policy': {'const': 'record_target_no_follow'},
            'case_collision_policy': {'const': 'reject'},
            'policy_digest': digest_string(),
        },
        ['schema_id', 'root_identity', 'include', 'excluded_control_roots', 'ephemeral_patterns', 'symlink_policy', 'case_collision_policy', 'policy_digest'],
    )
    tree = digest_schema(
        'source-tree-manifest/v1',
        'manifest_digest',
        'source_tree_manifest',
        {
            'schema_id': {'const': 'source-tree-manifest/v1'},
            'base_revision': string(pattern='^(?:[0-9a-f]{40}|unversioned:[0-9a-f]{64})$'),
            'selection_policy_ref': typed_ref('source-selection-policy/v1'),
            'selection_policy_digest': digest_string(),
            'entries': {'type': 'array', 'maxItems': 1000000, 'uniqueItems': True, 'items': entry_rule()},
            'entry_count': safe_integer(),
            'total_bytes': safe_integer(),
            'source_state_id': digest_string(),
            'manifest_digest': digest_string(),
        },
        ['schema_id', 'base_revision', 'selection_policy_ref', 'selection_policy_digest', 'entries', 'entry_count', 'total_bytes', 'source_state_id', 'manifest_digest'],
    )
    return [patch, change, profile, execution, policy, tree]


def seal(schema_map: dict[str, dict], schema_id: str, document: dict) -> dict:
    result = copy.deepcopy(document)
    spec = schema_map[schema_id]['x-pullwise-digest']
    unsigned = {key: value for key, value in result.items() if key != spec['field']}
    result[spec['field']] = hashlib.sha256(
        spec['domain'].encode('utf-8') + b'\0' + canonical(unsigned)
    ).hexdigest()
    return result


def content_ref(artifact_id: str, schema_id: str, document: dict) -> dict:
    raw = canonical(document)
    return {
        'schema_id': 'content-ref/v1',
        'artifact_id': artifact_id,
        'content_schema_id': schema_id,
        'sha256': hashlib.sha256(raw).hexdigest(),
        'size_bytes': len(raw),
        'media_type': 'application/json',
        'encoding': 'utf-8',
    }


def source_state_id(document: dict) -> str:
    return sha({
        'base_revision': document['base_revision'],
        'selection_policy_digest': document['selection_policy_digest'],
        'entries': document['entries'],
    })


def execution_state_id(document: dict) -> str:
    return sha({
        key: value for key, value in document.items()
        if key not in {'execution_state_id', 'manifest_digest'}
    })


def file_entry(path: str, data: bytes, executable: bool = False) -> dict:
    return {
        'path': path,
        'type': 'file',
        'size_bytes': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
        'executable': executable,
    }


def fixtures(schema_list: list[dict]) -> list[dict]:
    schema_map = {item['$id']: item for item in schema_list}
    policy = seal(schema_map, 'source-selection-policy/v1', {
        'schema_id': 'source-selection-policy/v1',
        'root_identity': 'root_' + '1' * 32,
        'include': 'all_repository_regular_files',
        'excluded_control_roots': ['.git/', '.pullwise-worker/'],
        'ephemeral_patterns': ['*.swp', '*.tmp'],
        'symlink_policy': 'record_target_no_follow',
        'case_collision_policy': 'reject',
        'policy_digest': '0' * 64,
    })
    patch_raw = b'--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n'
    patch = seal(schema_map, 'change-set-patch/v1', {
        'schema_id': 'change-set-patch/v1',
        'format': 'unified_diff',
        'media_type': 'text/x-diff',
        'encoding': 'base64',
        'data_base64': base64.b64encode(patch_raw).decode('ascii'),
        'byte_sha256': hashlib.sha256(patch_raw).hexdigest(),
        'size_bytes': len(patch_raw),
        'patch_digest': '0' * 64,
    })
    profile = seal(schema_map, 'execution-profile/v1', {
        'schema_id': 'execution-profile/v1',
        'image_identity': 'sha256:' + '1' * 64,
        'operating_system': 'linux',
        'sandbox_identity': 'bwrap:1.0',
        'cpu_architecture': 'x86_64',
        'profile_digest': '0' * 64,
    })
    original_entries = [
        file_entry('README.md', b'abc'),
        {'path': 'docs-link', 'type': 'symlink', 'target': 'docs'},
        file_entry('old.txt', b'old'),
        {'path': 'run.sh', 'type': 'symlink', 'target': 'scripts/run.sh'},
        {'path': 'vendor/lib', 'type': 'gitlink', 'commit_sha': 'a' * 40},
    ]
    tree = {
        'schema_id': 'source-tree-manifest/v1',
        'base_revision': 'b' * 40,
        'selection_policy_ref': content_ref('art_' + '1' * 32, 'source-selection-policy/v1', policy),
        'selection_policy_digest': policy['policy_digest'],
        'entries': original_entries,
        'entry_count': len(original_entries),
        'total_bytes': sum(item['size_bytes'] for item in original_entries if item['type'] == 'file'),
        'source_state_id': '0' * 64,
        'manifest_digest': '0' * 64,
    }
    tree['source_state_id'] = source_state_id(tree)
    tree = seal(schema_map, 'source-tree-manifest/v1', tree)
    final_entries = [
        file_entry('README.md', b'abcd'),
        {'path': 'docs-link', 'type': 'symlink', 'target': 'docs'},
        file_entry('new.txt', b'new'),
        file_entry('run.sh', b'abc', True),
        {'path': 'vendor/lib', 'type': 'gitlink', 'commit_sha': 'a' * 40},
    ]
    final_identity = {
        'base_revision': tree['base_revision'],
        'selection_policy_digest': tree['selection_policy_digest'],
        'entries': final_entries,
    }
    change = seal(schema_map, 'change-set/v1', {
        'schema_id': 'change-set/v1',
        'original_source_state_id': tree['source_state_id'],
        'final_source_state_id': sha(final_identity),
        'added': [{'after': file_entry('new.txt', b'new')}],
        'modified': [{'before': file_entry('README.md', b'abc'), 'after': file_entry('README.md', b'abcd')}],
        'deleted': [{'before': file_entry('old.txt', b'old')}],
        'type_changed': [{'before': {'path': 'run.sh', 'type': 'symlink', 'target': 'scripts/run.sh'}, 'after': file_entry('run.sh', b'abc', True)}],
        'patch_ref': content_ref('art_' + '2' * 32, 'change-set-patch/v1', patch),
        'change_set_digest': '0' * 64,
    })
    execution = {
        'schema_id': 'execution-state-manifest/v1',
        'source_state_id': tree['source_state_id'],
        'execution_profile_ref': content_ref('art_' + '3' * 32, 'execution-profile/v1', profile),
        'execution_profile_digest': profile['profile_digest'],
        'toolchain': [
            {'tool_id': 'git', 'binary_identity': '/usr/bin/git', 'version': '2.43.0', 'binary_sha256': '2' * 64},
            {'tool_id': 'python', 'binary_identity': '/usr/bin/python3', 'version': '3.10.12', 'binary_sha256': '3' * 64},
        ],
        'config_and_fixtures': [
            {'schema_id': 'content-ref/v1', 'artifact_id': 'art_' + '4' * 32, 'content_schema_id': 'source-content/v1', 'sha256': '4' * 64, 'size_bytes': 123, 'media_type': 'application/json', 'encoding': 'utf-8'},
            {'schema_id': 'content-ref/v1', 'artifact_id': 'art_' + '5' * 32, 'content_schema_id': 'source-content/v1', 'sha256': '5' * 64, 'size_bytes': 456, 'media_type': 'application/json', 'encoding': 'utf-8'},
        ],
        'services': [
            {'service_id': 'codex-app-server', 'version': '1.0.0', 'endpoint_class': 'local_process', 'fingerprint': '6' * 64},
        ],
        'environment': [
            {'kind': 'value', 'key': 'LANG', 'value': 'C.UTF-8'},
            {'kind': 'secret_ref', 'key': 'MODEL_TOKEN', 'secret_key_id': 'secret_' + '7' * 32, 'secret_version': 'v1'},
            {'kind': 'value', 'key': 'TZ', 'value': 'UTC'},
        ],
        'locale': 'C.UTF-8',
        'timezone': 'UTC',
        'execution_state_id': '0' * 64,
        'manifest_digest': '0' * 64,
    }
    execution['execution_state_id'] = execution_state_id(execution)
    execution = seal(schema_map, 'execution-state-manifest/v1', execution)
    golden = {
        'change-set-patch/v1': patch,
        'change-set/v1': change,
        'execution-profile/v1': profile,
        'execution-state-manifest/v1': execution,
        'source-selection-policy/v1': policy,
        'source-tree-manifest/v1': tree,
    }
    result = []
    names = {
        'change-set-patch/v1': 'patch',
        'change-set/v1': 'change_set',
        'execution-profile/v1': 'execution_profile',
        'execution-state-manifest/v1': 'execution_state',
        'source-selection-policy/v1': 'selection_policy',
        'source-tree-manifest/v1': 'source_tree',
    }
    for schema_id, document in golden.items():
        suffix = names[schema_id]
        result.append({'fixture_id': f'source_evidence_golden_{suffix}', 'fixture_class': 'golden', 'schema_id': schema_id, 'document': copy.deepcopy(document), 'expected_code': None})
        result.append({'fixture_id': f'source_evidence_idempotency_{suffix}', 'fixture_class': 'idempotency', 'schema_id': schema_id, 'document': copy.deepcopy(document), 'expected_code': None})
    bad = copy.deepcopy(change)
    bad['modified'] = []
    overlap = file_entry('new.txt', b'new')
    bad['added'] = [{'after': copy.deepcopy(overlap)}]
    bad['deleted'] = [{'before': copy.deepcopy(overlap)}]
    bad = seal(schema_map, 'change-set/v1', bad)
    result.append(negative('source_evidence_negative_change_set_overlap', 'change-set/v1', bad))
    bad = copy.deepcopy(profile)
    bad['image_identity'] = 'mutable:latest'
    result.append(negative('source_evidence_negative_execution_profile_mutable_image', 'execution-profile/v1', seal(schema_map, 'execution-profile/v1', bad)))
    bad = copy.deepcopy(execution)
    bad['toolchain'] = list(reversed(bad['toolchain']))
    bad['execution_state_id'] = execution_state_id(bad)
    result.append(negative('source_evidence_negative_execution_state_order', 'execution-state-manifest/v1', seal(schema_map, 'execution-state-manifest/v1', bad)))
    bad = copy.deepcopy(execution)
    bad['config_and_fixtures'][0]['content_schema_id'] = 'change-set-patch/v1'
    bad['execution_state_id'] = execution_state_id(bad)
    result.append(negative('source_evidence_negative_execution_state_wrong_target', 'execution-state-manifest/v1', seal(schema_map, 'execution-state-manifest/v1', bad)))
    bad = copy.deepcopy(patch)
    bad['byte_sha256'] = '0' * 64
    result.append(negative('source_evidence_negative_patch_bytes', 'change-set-patch/v1', seal(schema_map, 'change-set-patch/v1', bad)))
    bad = copy.deepcopy(policy)
    bad['excluded_control_roots'] = ['.pullwise-worker/']
    result.append(negative('source_evidence_negative_selection_missing_git', 'source-selection-policy/v1', seal(schema_map, 'source-selection-policy/v1', bad)))
    bad = copy.deepcopy(tree)
    bad['entries'] = final_entries
    bad['entry_count'] = len(final_entries)
    bad['total_bytes'] = sum(item['size_bytes'] for item in final_entries if item['type'] == 'file')
    result.append(negative('source_evidence_negative_source_tree_changed_state', 'source-tree-manifest/v1', seal(schema_map, 'source-tree-manifest/v1', bad)))
    bad = copy.deepcopy(tree)
    bad['selection_policy_ref']['content_schema_id'] = 'execution-profile/v1'
    result.append(negative('source_evidence_negative_source_tree_wrong_target', 'source-tree-manifest/v1', seal(schema_map, 'source-tree-manifest/v1', bad)))
    return sorted(result, key=lambda item: item['fixture_id'])


def negative(fixture_id: str, schema_id: str, document: dict) -> dict:
    return {
        'fixture_id': fixture_id,
        'fixture_class': 'negative',
        'schema_id': schema_id,
        'document': document,
        'expected_code': 'CONTRACT_DOCUMENT_INVALID',
    }


def pretty(value: object, indent: int = 0, width: int = 108) -> list[str]:
    prefix = ' ' * indent
    compact = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if len(prefix) + len(compact) <= width:
        return [prefix + compact]
    if isinstance(value, list):
        lines = [prefix + '[']
        for index, item in enumerate(value):
            child = pretty(item, indent + 2, width)
            child[-1] += ',' if index + 1 < len(value) else ''
            lines.extend(child)
        lines.append(prefix + ']')
        return lines
    if isinstance(value, dict):
        lines = [prefix + '{']
        items = list(value.items())
        for index, (key, item) in enumerate(items):
            encoded_key = json.dumps(key) + ': '
            child = pretty(item, indent + 2 + len(encoded_key), width)
            child[0] = ' ' * (indent + 2) + encoded_key + child[0].lstrip()
            child[-1] += ',' if index + 1 < len(items) else ''
            lines.extend(child)
        lines.append(prefix + '}')
        return lines
    return [prefix + compact]


def insertion_patch(marker: str, value: dict, has_existing: bool) -> str:
    marker_line = '    ' + json.dumps(marker)
    old = marker_line + (',' if has_existing else '')
    rendered = pretty(value, 4)
    if has_existing:
        rendered[-1] += ','
    additions = [marker_line + ',', *rendered]
    return '\n'.join([
        '*** Begin Patch',
        '*** Update File: pullwise-server/contracts/agent-first/current/source/families/source-evidence.json',
        '@@',
        '-' + old,
        *['+' + line for line in additions],
        '*** End Patch',
    ])


def removal_patch(marker: str) -> str:
    old = '    ' + json.dumps(marker) + ','
    return '\n'.join([
        '*** Begin Patch',
        '*** Update File: pullwise-server/contracts/agent-first/current/source/families/source-evidence.json',
        '@@',
        '-' + old,
        '*** End Patch',
    ])


schema_list = schemas()
fixture_list = fixtures(schema_list)
patches = []
has_existing = False
for item in reversed(schema_list):
    patches.append(insertion_patch('__SCHEMA_MARKER__', item, has_existing))
    has_existing = True
has_existing = False
for item in reversed(fixture_list):
    patches.append(insertion_patch('__FIXTURE_MARKER__', item, has_existing))
    has_existing = True
patches.append(removal_patch('__SCHEMA_MARKER__'))
patches.append(removal_patch('__FIXTURE_MARKER__'))
print(json.dumps(patches))
