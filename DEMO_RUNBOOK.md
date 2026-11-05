# Demo runbook

The same signed-in administrator can request, review and apply changes in chat. No OCI Console action is needed for the normal request-to-Apply flow. Use a dedicated trial compartment and disposable resources.

## Before presenting

1. Start one configured application instance and open its chat page.
2. Confirm the displayed compartment name, region and **Ready** status.
3. Ask it to list VCN names without proposing changes. Confirm that the names belong to the intended compartment.
4. Choose CIDRs that do not conflict with your trial environment.

Use **New request** to give each scenario a clear starting point. Answers and results remain saved in the same workspace.

## 1. Create a VCN

Example request:

> Create an isolated VCN named chat-demo-vcn with CIDR 10.240.0.0/16 in the managed compartment. Do not add an internet gateway or public subnet.

Answer any necessary question, then inspect the proposed code. It should add the intended VCN and preserve unrelated managed resources.

Select **Confirm code review**, then **Create plan**. Check the resource address, compartment and counts. If the Plan matches the request, select **Apply saved plan** yourself. Check the result message.

Ask a follow-up that lists the VCNs. This tests both live discovery and persistence across requests.

## 2. Change a pre-existing NLB

Prepare a disposable private NLB outside Terraform in the managed compartment using your normal Console or CLI. It needs a suitable existing subnet. Record its display name; no application-specific OCID is required in the prompt.

Ask:

> Rename the existing NLB trial-existing-nlb to trial-managed-nlb. Keep its subnet and traffic settings unchanged.

The app discovers it, prepares an import and proves an import-only baseline with zero creates, changes and deletes. The review card records this baseline. It does not mean the import was already applied.

Review the normalized code and create the Plan. The result should show the expected import and display-name change. Apply the saved Plan yourself if correct. Inspect the result and verify that subsequent requests find the renamed resource.

Direct deletion of an unmanaged resource is not supported. Bring it under management first through the reviewed adoption flow.

## 3. Detect a manual change

After scenario 2 has completed, make a manual display-name change to the disposable NLB using your usual OCI Console or CLI. This is the deliberate out-of-band change for the demonstration.

In chat, request the desired NLB change and proceed to **Create plan**. The app runs refresh-only first and presents the observed drift.

- **Keep the live value:** adopt the observed value into managed code, then continue the requested change.
- **Restore the code's value:** prepare a Plan that restores the saved value, together with any reviewed request.

Choose deliberately, inspect the fresh Plan and apply it yourself if correct. The app performs another live check before Apply; another intervening change invalidates the saved Plan.

## 4. Review a deletion

Ask:

> Delete the managed NLB trial-managed-nlb. Prepare the proposal for review.

Inspect the code diff. Only the selected NLB and explicitly intended dependent changes should be present. Create a Plan and inspect its deletion/replacement counts.

For an actual deletion, the administrator must choose **Apply saved plan** and type the exact second confirmation phrase displayed for that Plan. Stop at code or Plan review if you only want to demonstrate the controls.

## What to explain

- The model reads metadata and writes a proposal; the app runs Terraform.
- Code review does not change infrastructure. A Plan previews changes; Apply performs them.
- State and conversation memory remain in the private versioned bucket.
- A Plan expires after 15 minutes and is invalidated by relevant changes.
- This starter currently adopts VCNs and NLBs; it is not a universal service importer or a general troubleshooting assistant.

After the demo, use explicit reviewed deletion requests for disposable resources. Stopping the application does not remove resources or delete its state bucket.
