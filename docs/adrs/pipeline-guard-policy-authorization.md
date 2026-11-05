# Pipeline Guard: Policy authorization for unattended execution

<!-- Architecture Decision Record.
     File: docs/adrs/pipeline-guard-policy-authorization.md -->

## Scope

**Component: Pipeline Guard at the repository root**. This decision applies to Pipeline Guard's unattended
mode. The interactive flow continues to use a person's local confirmation.

## Status

**accepted**

## Date

2026-10-27

## Context

The unattended mode can start a repair when a person is not available to approve each execution,
for example during a host's overnight run. A repair must not occur merely because a failure
exists: it needs verifiable authorization tied to the diagnosis and base commit to be repaired.

The documented architecture separates trust boundaries: orchestrated approvals must be
authenticated by the host, while local approvals belong exclusively to an interactive session.
The unattended contract defines a versioned project policy as the authorization channel for
unattended mode.

## Decision

**Pipeline Guard will authorize unattended repair only through a versioned project policy, using
`channel_type=policy`.**

When `unattended.remediation_enabled` is `true` and the diagnosis decision is
`ELIGIBLE_FOR_REMEDIATION`, the unattended Pipeline Guard entrypoint (`unattended_cli.py`, through
`policy_authorization.py`) will emit an `approval_input_v1`. The host provides the execution
context, service identity, and reference to the authorization secret; the entrypoint derives
`external_reference` and the other execution data from that context. The approval conforms to
`approval_input_v1` and its policy profile, and contains:

- `schema_version=1.0`;
- the failure context's `incident_id`;
- the approved diagnosis's `diagnosis_hash`;
- `decision=APPROVED`;
- `actor` equal to `unattended.service_identity`;
- `decided_at` at policy authorization issuance;
- `channel_type=policy`;
- `external_reference` equal to `host_run.run_url` or, when it is unavailable,
  `host_run.run_reference`;
- `base_commit` equal to `code_version.commit`; and
- an HMAC-SHA256 signature calculated over the canonical representation of the approval, excluding
  the `signature` field itself, using the secret identified by
  `unattended.authorization_secret_ref`.

Before creating a workspace or allowing writes, the signature is authenticated and the contract,
decision, incident, diagnosis, base commit, and validity period are validated. In unattended mode,
`channel_type=local` is rejected. Human review of the draft PR remains required before any merge.

Before any workspace or repository write, an invocation with remediation enabled must receive
`--authorization-state-dir`, a host-managed directory that persists across isolated executions and
is outside the consumer repository and workspace. The entrypoint passes this path as
`plugin_data_dir` to `resolve_artifact_store` with the `local` backend. The default store rooted at
the ephemeral `CLAUDE_PLUGIN_DATA` remains appropriate for ordinary run artifacts, but does not
satisfy this authorization gate. The state directory is never exposed to the authoring agent.

At the gate immediately before the first write, the entrypoint uses the durable store's
create-once `put` to persist a consumed-authorization marker under the fixed run ID
`authorization-consumption`. The marker reference is the canonical representation of the pair
containing the host execution's `external_reference` and the SHA-256 hash of the canonical signed
authorization. A successful create atomically records consumption before the write; an existing
marker blocks the execution. The backing filesystem must preserve the store's atomic create-once
semantics; preflight rejects a missing, non-writable, or unsupported state directory before any
workspace is created.

A consumed authorization is never made valid again, so the same signed authorization cannot be
presented again to authorize another write. A retry can issue a new authorization for its
execution; repeated failures and draft PR reuse are instead deduplicated by the failure signature
and the private publisher's deterministic draft-PR reconciliation.

### Alternatives Considered

#### Option A: Subsequent human approval through an external channel

- **Pros:** there is explicit confirmation from a person for every execution.
- **Cons:** it does not allow a PR to be prepared when nobody is available, such as during an
  overnight failure.
- **Rejected because:** it does not meet the goal of unattended mode.

#### Option B: No authorization beyond the draft PR

- **Pros:** simpler implementation while retaining human review before merge.
- **Cons:** it would allow a repair to be created without verifiable authorization before writing
  to the repository.
- **Rejected because:** it breaks the core safety invariant that every remediation must have approval
  tied to the diagnosis and base commit.

## Consequences

### Positive

- The policy can be reviewed through a pull request in the consumer project before enabling repair.
- Authorization is tied to the execution, diagnosis, and base commit, and can be audited.
- A document forged by the model or injected into the workspace cannot pass without the host's
  secret.
- Local approvals cannot silently be promoted to unattended mode.

### Negative

- HMAC-SHA256 proves possession of the shared secret; it does not independently identify the
  person declared in `actor`.
- The entrypoint and verifier use the same host-managed secret, so security depends on protecting
  that secret and the environment running it.
- Secret rotation and protection of the authorization secret remain the host's responsibility.
- The host must provide durable storage with atomic create-once semantics for authorization
  consumption markers whenever unattended remediation is enabled.
- The one-time-use gate rejects re-presentation of a consumed signed authorization. A retry uses a
  newly issued authorization and is deduplicated separately by the failure signature and draft-PR
  reconciliation.

### Neutral

- The secret must never be stored literally in the repository, approval contract, or logs; the
  host provides it through a secure mechanism.
- Interactive mode remains available, but its local approval can be consumed only by the
  interactive flow.

## Related

- [Approval contract](../../contracts/schema/approval_input_v1.schema.json) and [policy profile](../../contracts/schema/approval_input_v1.policy_profile.schema.json).

- [Unattended execution guide](../unattended/README.md#local-and-unattended-flows).

