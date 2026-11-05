# Troubleshooting and maintenance

## The container is active but the page is unavailable

Inspect the container's logs for its `ready` event. The app initializes the remote backend and provider schema before opening its HTTP listener. Confirm its private IP, Gateway backend address and TCP 8080 path from the Gateway subnet. The container also needs outbound access to OCI APIs and the model endpoint.

Do not publish the container port directly as a substitute for the authenticated Gateway.

## Sign-in succeeds but actions return 403

Refresh the page after a process restart. Confirm the Gateway overwrites `X-Codegen-Subject` from the verified subject claim, and that the configured administrator hash matches that exact claim.

The two request-token systems are separate:

| Purpose | Browser response/input | Mutation request |
| --- | --- | --- |
| Gateway session protection | `X-Codegen-Csrf-Token` on `/api/config`, populated from the Gateway context | `X-CSRF-TOKEN` |
| Application action protection | `csrf_token` in the `/api/config` JSON body | `X-Chat-CSRF` |

Do not swap these headers. The web app handles them automatically. A `431 Request Header Fields Too Large` can indicate an oversized browser session; try a fresh private browser session to distinguish that from a container failure.

## GenAI returns an error

Compare a small direct request in the chosen model region with the app's configured path. Confirm on-demand availability, `model_region`, `model_id`, model-compartment access and any explicit routing profile. An Active routing profile alone does not establish that inference is currently routable.

Use the returned HTTP status, error code, timestamp and OCI request ID for diagnosis. Do not publish tokens or raw authentication headers in an issue. See [Oracle's model routing troubleshooting](https://docs.oracle.com/en-us/iaas/Content/generative-ai/routing-profile-tutorial.htm).

## A draft fails validation

The app returns validation feedback for at most two corrections. If it still cannot complete the draft, refine the request in the same conversation; your answers remain saved.

Deletion instructions must identify existing managed Terraform addresses. The app rejects malformed removal entries before lookups, removes matching import entries, and prunes empty resource mappings before validation. It does not silently import and delete an unmanaged resource.

If a new resource type is not exposed by the installed discovery/schema tools, adding IAM grants alone does not add application support. Check the capability table in the README.

## A Plan expires or drift appears

Create a fresh Plan after expiry. If live settings changed, choose whether to keep the live value or restore the code, then review the resulting Plan. Do not reuse a saved Plan ID from another request or edit a binary Plan.

If the process stopped during Apply, inspect the saved result and actual Terraform state before retrying. Restart does not automatically resume an uncertain Apply.

## Retention, recovery and updates

- Keep versioning enabled on the private bucket. The native OCI backend provides Terraform state locking. [Backend reference](https://developer.hashicorp.com/terraform/language/backend/oci).
- Plan expiry is not object deletion. The starter does not include scheduled retention cleanup. Establish your own retention process that preserves active state and any records you need.
- Back up state and workspace records together before recovery. Restoring an old state object alone does not roll back real infrastructure or its code history.
- For an application update, stop accepting workspace work, preserve the same configuration and bucket, start one replacement, verify it, and retire the old runtime. Refresh browser sessions after restart. Do not operate multiple replicas against the same workspace.
- Retain the tested Terraform/provider versions for a repeatable trial. Test version changes in a separate workspace before applying them to an existing one.
