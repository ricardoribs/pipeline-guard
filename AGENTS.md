# Pipeline Guard Instructions

## Standalone Publication Boundary

`pipeline-guard` must run unattended with only its own plugin directory installed.

The root architecture permits one narrow exception for this plugin:

- `publish-draft-pr` may own private Bitbucket and GitHub HTTP adapters used only to locate,
  create, reuse, or safely close the draft pull request produced by Pipeline Guard.
- The adapters must receive a policy-approved neutral request and must not decide whether
  publication is allowed.
- Provider credentials must come from environment variables and must never appear in request
  payloads, command arguments, prompts, logs, or result contracts.
- The capability must not expose general pull-request management, review, approval, merge,
  deployment, issue, reviewer, branch-administration, or repository-administration operations.
- Adding another provider or expanding these operations requires an explicit architecture review.
- Bitbucket and GitHub list, create, conflict-reconciliation, and close behavior must remain
  covered by tests using initialized clients and mocked HTTP.

All other Git hosting capabilities are outside this plugin.
