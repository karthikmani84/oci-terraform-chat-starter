# Architecture and execution flow

## Runtime

One OCI Container Instance runs the Python HTTP/chat process and Terraform CLI. A private VNIC accepts HTTP from the authenticated API Gateway. The Gateway handles browser sign-in and forwards the verified user subject. The app accepts the configured administrator's subject hash.

The model receives the conversation, managed Terraform code when requested, and filtered resource metadata. It has three read-only tools. It does not receive OCI credentials or wholesale Terraform state/Plan files, and it has no execution tool.

The model region and infrastructure region are separate settings. For example, the app can run and manage resources in one OCI region while calling an on-demand model directly in another. Select `model_region` according to the customer's processing-location requirements.

## Who performs each step

| Step | Owner | Evidence or output |
| --- | --- | --- |
| Clarify the request | Model and administrator | Saved messages and answers |
| Discover a resource | App's read-only OCI tool | Filtered metadata in the configured compartment |
| Write a proposal | Model | Resource JSON changes and removals |
| Admit and validate code | Application | Validated code diff |
| Confirm code review | Administrator | Current code revision |
| Check drift and create Plan | Application | Saved binary Plan, summary, digest and expiry |
| Decide how to handle drift | Administrator | Keep live or restore code |
| Confirm Apply | Administrator | Exact saved Plan ID and, for destructive changes, a second confirmation |
| Execute Apply and record result | Application | Native Terraform output and retained remote state |

## Existing resources

First-change adoption supports VCNs and NLBs. The app prepares an import block, asks Terraform to generate configuration, and normalizes the result against the installed provider schema. It removes tag management, ignores tag changes, removes computed/null values and proven defaults, and keeps existing parent OCIDs as literals.

An import-only baseline must show zero additions, changes and deletions before the requested change can proceed. There is no hidden import Apply. The eventual saved-Plan Apply commits the reviewed import and requested change. Direct deletion of an unmanaged resource is rejected; it must first come under management.

## State, memory and concurrency

The private versioned bucket holds Terraform state, workspace code, conversations and saved Plan artifacts. Terraform's native OCI backend provides state locking. The application also serializes its workspace operations.

Each conversation is owner-scoped. A process restart retains its saved records but does not automatically resume an uncertain Apply. The administrator must inspect the current outcome and create a fresh Plan.

One app process is supported per workspace. Native state locking alone does not coordinate multiple applications' code revisions, conversation state or approval decisions.

## Apply gates

Apply requires the exact saved Plan ID, current reviewed code revision, unchanged workspace revision, matching code and binary digests, a fresh live-resource check, and a Plan younger than 15 minutes. Deletes and replacements additionally require the displayed confirmation phrase.

The API and state machine drive these checks. Text such as “apply now” in a model response cannot execute Terraform.

## Deployment boundaries

The downloadable starter contains no customer's live deployment or state. Its renderer creates reviewable JSON files. It does not create OCI resources, IAM policies, identity applications or an API Gateway authentication configuration.

Network, identity and model availability must be verified in each new environment. The service coverage in the README describes installed capabilities, not a guarantee that every OCI service can be provisioned.
