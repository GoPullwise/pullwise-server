"""Node facade semantics for source, execution, and observation state."""

from __future__ import annotations


NPM_SOURCE_EXECUTION_OBSERVATION = r'''
function seoRequire(condition, detail, path = "$", code = undefined) {
  if (!condition) fail(detail, path, code);
}

function seoVerifyEmbeddedDigest(schemaId, value) {
  const spec = schema(schemaId)["x-pullwise-digest"];
  const unsigned = Object.fromEntries(
    Object.entries(value).filter(([key]) => key !== spec.field),
  );
  const domain = encoder.encode(spec.domain);
  const document = canonicalDocumentBytes(unsigned);
  const input = new Uint8Array(domain.length + 1 + document.length);
  input.set(domain);
  input.set(document, domain.length + 1);
  seoRequire(
    value[spec.field] === sha256Sync(input),
    "CONTRACT_DIGEST_MISMATCH",
    "$." + spec.field,
  );
}

function seoTreeEntryValid(value) {
  if (value === null || Array.isArray(value) || typeof value !== "object") {
    return false;
  }
  const expected = {
    file: ["executable", "path", "sha256", "size_bytes", "type"],
    symlink: ["path", "target", "type"],
    gitlink: ["commit_sha", "path", "type"],
  }[value.type];
  return expected !== undefined &&
    JSON.stringify(Object.keys(value).sort()) === JSON.stringify(expected);
}

function seoRefMatchesDocument(ref, schemaId, document) {
  const raw = canonicalDocumentBytes(document);
  const expected = {
    content_schema_id: schemaId,
    sha256: sha256Sync(raw),
    size_bytes: raw.length,
    media_type: "application/json",
    encoding: "utf-8",
  };
  return Object.entries(expected).every(([key, value]) => ref[key] === value);
}

function seoCompareUtf8(left, right) {
  const a = encoder.encode(left);
  const b = encoder.encode(right);
  const limit = Math.min(a.length, b.length);
  for (let index = 0; index < limit; index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return a.length - b.length;
}

function seoChangeEntryPath(entry) {
  return (entry.after ?? entry.before).path;
}

function ruleChangeSetComplete(value) {
  const allPaths = [];
  for (const group of ["added", "modified", "deleted", "type_changed"]) {
    const paths = [];
    value[group].forEach((item, index) => {
      let expected = group === "added" ? ["after"] : ["before"];
      if (group === "modified" || group === "type_changed") {
        expected = ["after", "before"];
      }
      const path = "$." + group + "[" + index + "]";
      seoRequire(
        JSON.stringify(Object.keys(item).sort()) === JSON.stringify(expected),
        "CHANGE_SET_ENTRY_SHAPE_INVALID",
        path,
      );
      seoRequire(
        Object.values(item).every(seoTreeEntryValid),
        "CHANGE_SET_ENTRY_SHAPE_INVALID",
        path,
      );
      const before = item.before;
      const after = item.after;
      if (before !== undefined && after !== undefined) {
        seoRequire(
          before.path === after.path,
          "CHANGE_SET_PATH_INVALID",
          path,
        );
        seoRequire(
          canonicalString(before) !== canonicalString(after),
          "CHANGE_SET_EMPTY_MUTATION",
          path,
        );
        seoRequire(
          (before.type === after.type) === (group === "modified"),
          "CHANGE_SET_TYPE_BRANCH_INVALID",
          path,
        );
      }
      paths.push(seoChangeEntryPath(item));
    });
    seoRequire(
      JSON.stringify(paths) === JSON.stringify([...paths].sort(seoCompareUtf8)),
      "CHANGE_SET_ORDER_INVALID",
      "$." + group,
    );
    allPaths.push(...paths);
  }
  seoRequire(allPaths.length > 0, "CHANGE_SET_EMPTY");
  seoRequire(
    new Set(allPaths).size === allPaths.length,
    "CHANGE_SET_PATH_OVERLAP",
  );
  seoRequire(
    value.original_source_state_id !== value.final_source_state_id,
    "CHANGE_SET_STATE_UNCHANGED",
  );
  seoVerifyEmbeddedDigest("change-set/v1", value);
}

function seoExpectedChangeGroups(original, final) {
  const before = new Map(original.entries.map((entry) => [entry.path, entry]));
  const after = new Map(final.entries.map((entry) => [entry.path, entry]));
  const groups = {added: [], modified: [], deleted: [], type_changed: []};
  const paths = [...new Set([...before.keys(), ...after.keys()])].sort(
    seoCompareUtf8,
  );
  for (const path of paths) {
    const oldEntry = before.get(path);
    const newEntry = after.get(path);
    if (oldEntry === undefined) {
      groups.added.push({after: newEntry});
    } else if (newEntry === undefined) {
      groups.deleted.push({before: oldEntry});
    } else if (canonicalString(oldEntry) !== canonicalString(newEntry)) {
      const group = oldEntry.type === newEntry.type ? "modified" : "type_changed";
      groups[group].push({before: oldEntry, after: newEntry});
    }
  }
  return groups;
}

export async function verifyChangeSetContext(
  changeSet, originalSourceTree, finalSourceTree, patch,
) {
  const checked = await verifyDocumentDigest("change-set/v1", changeSet);
  const original = await verifyDocumentDigest(
    "source-tree-manifest/v1", originalSourceTree,
  );
  const final = await verifyDocumentDigest(
    "source-tree-manifest/v1", finalSourceTree,
  );
  const patchValue = await verifyDocumentDigest("change-set-patch/v1", patch);
  seoRequire(
    checked.original_source_state_id === original.source_state_id,
    "CHANGE_SET_CONTEXT_INVALID",
    "$.original_source_state_id",
  );
  seoRequire(
    checked.final_source_state_id === final.source_state_id,
    "CHANGE_SET_CONTEXT_INVALID",
    "$.final_source_state_id",
  );
  const expected = seoExpectedChangeGroups(original, final);
  for (const group of ["added", "modified", "deleted", "type_changed"]) {
    seoRequire(
      canonicalString(checked[group]) === canonicalString(expected[group]),
      "CHANGE_SET_CONTEXT_INVALID",
      "$." + group,
    );
  }
  seoRequire(
    seoRefMatchesDocument(
      checked.patch_ref, "change-set-patch/v1", patchValue,
    ),
    "CAS_CORRUPT",
    "$.patch_ref",
  );
  return checked;
}

function seoOrderedUnique(values, key) {
  const keys = values.map(key);
  const encoded = keys.map((item) => canonicalString(item));
  return new Set(encoded).size === encoded.length &&
    JSON.stringify(keys) === JSON.stringify([...keys].sort((left, right) => {
      const a = Array.isArray(left) ? left : [left];
      const b = Array.isArray(right) ? right : [right];
      for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
        if (a[index] < b[index]) return -1;
        if (a[index] > b[index]) return 1;
      }
      return a.length - b.length;
    }));
}

function seoContentRefKey(value) {
  return [value.content_schema_id, value.artifact_id, value.sha256];
}

function ruleExecutionStateManifest(value) {
  seoRequire(
    seoOrderedUnique(value.toolchain, (item) => item.tool_id),
    "EXECUTION_TOOLCHAIN_ORDER_INVALID", "$.toolchain",
  );
  seoRequire(
    seoOrderedUnique(value.config_and_fixtures, seoContentRefKey),
    "EXECUTION_CONFIG_ORDER_INVALID", "$.config_and_fixtures",
  );
  seoRequire(
    seoOrderedUnique(value.services, (item) => item.service_id),
    "EXECUTION_SERVICE_ORDER_INVALID", "$.services",
  );
  seoRequire(
    seoOrderedUnique(value.environment, (item) => item.key),
    "EXECUTION_ENVIRONMENT_ORDER_INVALID", "$.environment",
  );
  value.environment.forEach((item, index) => {
    const expected = item.kind === "value"
      ? ["key", "kind", "value"]
      : ["key", "kind", "secret_key_id", "secret_version"];
    seoRequire(
      JSON.stringify(Object.keys(item).sort()) === JSON.stringify(expected),
      "EXECUTION_ENVIRONMENT_SHAPE_INVALID",
      "$.environment[" + index + "]",
    );
  });
  const unsigned = Object.fromEntries(Object.entries(value).filter(
    ([key]) => key !== "execution_state_id" && key !== "manifest_digest",
  ));
  seoRequire(
    value.execution_state_id === sha256Sync(canonicalDocumentBytes(unsigned)),
    "EXECUTION_STATE_ID_INVALID", "$.execution_state_id",
  );
  seoVerifyEmbeddedDigest("execution-state-manifest/v1", value);
}

export async function verifyExecutionStateContext(
  manifest, sourceTree, executionProfile,
) {
  const checked = await verifyDocumentDigest(
    "execution-state-manifest/v1", manifest,
  );
  const source = await verifyDocumentDigest(
    "source-tree-manifest/v1", sourceTree,
  );
  const profile = await verifyDocumentDigest(
    "execution-profile/v1", executionProfile,
  );
  seoRequire(
    checked.source_state_id === source.source_state_id,
    "EXECUTION_STATE_CONTEXT_INVALID", "$.source_state_id",
  );
  seoRequire(
    checked.execution_profile_digest === profile.profile_digest,
    "EXECUTION_STATE_CONTEXT_INVALID", "$.execution_profile_digest",
  );
  seoRequire(
    seoRefMatchesDocument(
      checked.execution_profile_ref, "execution-profile/v1", profile,
    ),
    "CAS_CORRUPT", "$.execution_profile_ref",
  );
  return checked;
}
'''


__all__ = ["NPM_SOURCE_EXECUTION_OBSERVATION"]
