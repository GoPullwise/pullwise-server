# Model Gateway and Worker Profile architecture

Date: 2026-09-02
Status: accepted for incremental implementation

## Decision

Pullwise will centralize upstream model access behind a dedicated Model Gateway
process. Pullwise Server remains the authority for non-secret Provider Connection
metadata, immutable Profile Set revisions, Worker Pool desired state, and
Worker-scoped Gateway grants. The Gateway alone resolves provider secret values
and dispatches allowlisted upstream requests. Workers receive a shared logical
manifest but unique short-lived Gateway authorization and materialize only a
managed Pi `pullwise-gateway` profile.

The first deployable Gateway lives in this repository for coordinated testing and
release, but runs through a separate entry point and trust boundary. It must not
import or open the Server business database. It receives a public token trust set,
an internal broker credential, and a Gateway-owned encrypted secret directory.

## Drivers

- Upstream API keys must never enter Server business storage, Worker state,
  catalog/heartbeat payloads, install commands, logs, audit bodies, or Admin state
  after submission.
- Every Worker needs independent authorization even when the fleet shares one
  logical model manifest.
- Route/model selection must stay exact and fail closed; the Gateway cannot be a
  generic URL/header/HTTP proxy.
- Upstream key rotation must not require Worker redeployment or Pi auth changes.
- Pullwise's existing one-slot Worker, typed desired-state, no-remote-shell, and
  clean-break constraints remain in force.

## Responsibility boundaries

### Pullwise Server

- Stores only Provider Connection metadata and opaque `secret_ref`/version facts.
- Owns versioned Profile Sets, stable route ids, plan-policy intersection, Worker
  Pools, desired revision, observed reconciliation state, and dependency checks.
- Authenticates an existing Worker control-plane identity before issuing a
  distinct Ed25519-signed Gateway token for that exact Worker and revision.
- Stores token id/hash/scope/generation/expiry/revocation, never usable token text.
- Proxies initial secret writes only through a narrow write-only broker client and
  records de-secreted audit facts. It has no secret-read operation.

### Model Gateway

- Runs as a separate process with no business-DB access.
- Owns the encrypted secret store (or later Secret Manager adapter), public token
  verification keys, exact route registry, upstream adapters, limits, cancellation,
  and de-secreted audit emission.
- Accepts only implemented methods and paths. A model alias resolves to an exact
  provider connection, adapter, endpoint origin/path, and upstream model.
- Injects upstream credentials only inside the selected adapter call and never
  logs request/response bodies, prompts, repository content, or secret-bearing
  headers.

### Worker

- Uses its existing control-plane token only with Server.
- Pulls a signed, non-secret manifest plus its own short-lived Gateway token.
- Atomically writes a private Pi `models.json` and `auth.json`; the latter contains
  only the Gateway token, never an upstream credential.
- Reports desired/applied revision, manifest/catalog digest, apply outcome, and
  readiness. It stops claiming affected work while desired and observed state
  differ.

### Admin

- Creates write-only Provider Connections and sees only safe metadata/health.
- Authors and publishes immutable Profile Set revisions, binds them to Worker
  Pools, selects rollout waves, observes convergence, and invokes staged
  rotation/removal/emergency revoke.
- Continues to configure plan-level provider/model/thinking policy. Effective
  routability is that tuple intersected with a Pool's published Profile Set and
  Gateway-validated availability; there is no fallback.

## Token and request flow

1. Worker authenticates to Server with its control-plane bearer token and requests
   its bound model profile.
2. Server resolves the enabled Worker, Pool, published Profile Set revision, and
   exact enabled routes; it signs a five-minute token with Worker subject, Gateway
   audience, revision, route scopes, generation, expiry, and unique token id.
3. Server persists only the grant metadata and token SHA-256, then returns the
   manifest and token once over TLS.
4. Worker validates the manifest digest and atomically reconciles Pi files.
5. Pi calls the fixed Gateway base URL with model alias and Worker-specific token.
6. Gateway validates signature, audience, time, revocation/current generation,
   Worker/revision binding, exact route, limits, and request shape before it asks
   SecretStore for the active version.
7. The selected adapter injects that value into one configured upstream request.
   Cancellation and timeout propagate; audit records contain ids/outcome/usage but
   no bodies.

## Provider secret lifecycle

Initial setup writes a candidate secret version, validates it through the
Gateway, and promotes only safe metadata. Rotation writes a new version, validates,
canaries, atomically promotes, observes, revokes at the provider, and retires the
old encrypted value. Worker files do not change. Removal first proves there are no
live Profile Set or plan-policy dependencies, stops new leases, drains/aborts by
policy, publishes and converges a replacement revision, removes the route, revokes
the provider key, and retains a non-secret tombstone. Emergency revoke skips drain
and immediately disables resolution.

## Alternatives considered

### Store encrypted provider keys in Server SQLite

Rejected. Encryption reduces disclosure from a copied database but leaves key
custody and secret-read authority in the business process/database boundary, and
contradicts the approved storage boundary.

### Copy one shared gateway key or `auth.json` to all Workers

Rejected. A single compromise becomes fleet-wide, individual revocation is
impossible, and access is not attributable to one Worker.

### Let each Worker keep upstream provider credentials

Rejected as the old target. It makes batch deployment, rotation, removal,
least-privilege routing, and centralized audit materially weaker.

### Put Gateway forwarding directly inside the Server HTTP process

Rejected. Co-location may be operationally convenient, but in-process forwarding
would collapse the business-data, signing, secret decryption, and upstream request
trust boundaries.

## Consequences

- Deployment gains a separately configured process and internal broker/trust
  channels.
- Server/Gateway/Worker clocks and signing-key rotation need explicit operational
  controls.
- Admin secret submission still traverses Server in the first slice; this residual
  exposure is bounded to request memory and a write-only broker call, and must be
  revisited when direct broker authentication is available.
- Short-lived bearer tokens are replayable if stolen until expiry/revocation.
  Per-Worker subject/path/generation checks are required now; mTLS or proof-of-
  possession remains a production-hardening decision.
- Profile revision convergence becomes a scheduler prerequisite, so rollout and
  failure status are product-visible rather than best-effort configuration.

## Local verification boundary

`python -m pytest -q tests/test_model_gateway_end_to_end.py` starts temporary
TLS loopback Server and Gateway listeners, runs the real Worker `sync` command,
routes one completion through live introspection and an encrypted temporary
SecretStore, and verifies cross-Worker denial plus secret/body non-disclosure.
Its upstream validator and completion adapter are deterministic fakes. It proves
the complete local plumbing and trust-boundary flow without requiring or
embedding a provider key; it does not claim real-provider or full-review
readiness. The broader `ops/local_debug_loop.py` report continues to label its
empty-profile smoke as `local-plumbing` and reports `fullReviewReady: false`.

## Follow-ups

- Implement the phased tracer-bullet plan in
  `/.omx/plans/2026-09-02-model-gateway-profile-sets.md`.
- Evaluate mTLS/workload identity after the bearer-token slice proves the control
  and data paths.
- Add a distinct provider-secret-manager permission when the Admin role model can
  express it.
- Add production Secret Manager backends behind the same narrow interface only
  with explicit dependency/deployment approval.
- Write provider-specific streaming/cancellation conformance suites before adding
  OpenAI, DeepSeek, and MiniMax production adapters.
