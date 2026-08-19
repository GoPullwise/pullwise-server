# pullwise-canonical-json/v1

All contract JSON is decoded as strict UTF-8. Reject a BOM, invalid UTF-8, duplicate object keys, non-NFC keys or strings, floating-point numbers, negative zero, integers outside JavaScript safe integer range, and a document that fails its selected JSON Schema definition.

Canonical bytes are RFC 8785 JSON Canonicalization Scheme bytes with these Pullwise restrictions: integers only; strings and keys must already be NFC; object keys sort by Unicode code point; no insignificant whitespace; UTF-8 output without BOM or trailing newline.

A JSON digest is lowercase SHA-256 over canonical bytes, rendered as `sha256:<64 lowercase hex>`. Raw artifact digests are lowercase SHA-256 over exact artifact bytes and are not JSON-canonicalized.

Self-referential projections:
- `ResultCandidate.candidate_digest`: omit only `candidate_digest`, canonicalize the remaining complete object, then hash.
- `ReviewEvent.event_digest`: omit only `event_digest`, canonicalize the remaining complete object, then hash.
- contract root: canonicalize `{schema_id, contract_version, canonicalization, files}` from manifest.json; omit `manifest_digest`.
- every idempotency request digest: canonicalize the authenticated, server-derived command after removing transport-only headers; tenant and repository scope are inserted by Server before hashing.

Validation order is decode → duplicate/NFC/number checks → schema → cross-field invariants → canonicalization → digest comparison. Fail closed; no parser repair.

Test vectors:

- empty-object: canonical `{}`; sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
- sorted-keys: canonical `{"a":1,"b":"x"}`; sha256:ecf9e98ec0641e23113ff3ce8bdc78d0ddd249886517fd4a7f68cc83d4e65667
- unicode: canonical `{"text":"审查"}`; sha256:22fb58b4ca59907c273ff22aed489c7c9a43087d64d245bea2e50d035fb663ce
