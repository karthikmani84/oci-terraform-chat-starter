# Deploy one chat application on OCI

This guide sets up the app in your own environment. The included preparation command renders files for review; it does not call OCI or provision anything automatically.

## 1. Prepare the environment

Use an OCI account with permission to create the trial resources and configure their IAM access. Before deploying, have:

- Two compartments and their OCIDs:
  - a **platform compartment** for the chat's own container instance and state bucket;
  - a dedicated **managed compartment**, initially empty, which is the only scope Terraform may change.

  Keeping them separate means the chat can never modify, replace or delete its own runtime, state or permissions, and a container created through the chat does not inherit the chat's role.
- An Object Storage namespace and a unique name for a fresh private, versioned bucket.
- An existing private application subnet, an NSG and outbound access to OCI APIs, the model API and public dependency/provider download locations. Permit TCP 8080 from the API Gateway subnet to the app VNIC.
- An availability domain and enough capacity for `CI.Standard.E4.Flex`, 1 OCPU and 4 GB RAM.
- A working browser-authenticated OCI API Gateway with an Identity Domains/OpenID Connect sign-in flow, a dedicated sign-in application and an HTTPS hostname.
- On-demand access to the chosen model in `model_region`. The example calls `openai.gpt-oss-120b` directly in Frankfurt.
- Python 3.11 and a configured OCI CLI on your preparation computer.

Use networks and dependencies appropriate to the managed trial scope. The app does not create its hosting subnet or automatically configure identity providers. See Oracle's [Container Instances setup](https://docs.oracle.com/en-us/iaas/Content/container-instances/creating-a-container-instance.htm) and [Gateway authentication guide](https://docs.oracle.com/en-us/iaas/Content/APIGateway/Tasks/apigatewayusingjwttokens.htm) for those prerequisites.

## 2. Fill the configuration

From the repository root:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp examples/runtime-config.example.json runtime-config.json
cp examples/deployment-config.example.json deployment-config.json
```

Edit both copied files. Replace every placeholder with your own values. Do not edit the examples to contain private environment data.

The runtime file selects the managed compartment, state bucket, infrastructure region, model region and intended administrator. Set `public_host` to the exact browser hostname, without a scheme or path. Follow [configuration](CONFIGURATION.md) to obtain the hash of the actual verified administrator subject. Keep `auth` as `ResourcePrincipal` and `local_demo` as `false` for this deployment.

The deployment file selects the platform compartment, availability domain, existing private subnet/NSGs, identity-domain name and dynamic-group name. The default `image_mode` is `inline`: it uses a pinned public Python image and a checksum-verified inline archive of this repository's application code.

Use the same configured infrastructure region in the OCI CLI commands below. Replace `YOUR_INFRASTRUCTURE_REGION` with that region; the model region may differ.

## 3. Render reviewable files

```bash
python deploy/package.py --output build/app.zip
python deploy/prepare.py \
  --runtime-config runtime-config.json \
  --deployment-config deployment-config.json \
  --inline-archive build/app.zip \
  --output-dir build/deploy
```

Inspect the output:

| File | Purpose |
| --- | --- |
| `bucket.json` | CLI-ready private bucket with versioning enabled |
| `container-instance.json` | CLI-ready single private Container Instance |
| `dynamic-group-rule.txt` | Matching rule for container instances in the platform compartment |
| `IAM-policy.txt` | Four POC runtime permissions |
| `gateway-http-routes.template.json` | Eleven application routes with an explicit backend-IP placeholder |
| `summary.json` | Review summary |

The generated container payload includes your filled configuration and source archive. It contains no API signing key. Treat `build/` as local deployment output; it is excluded from Git.

## 4. Create the bucket and runtime role

Create the state bucket with the rendered file:

```bash
oci os bucket create \
  --region YOUR_INFRASTRUCTURE_REGION \
  --from-json file://build/deploy/bucket.json
```

Confirm in Console that the bucket is private and versioning is enabled. The [native OCI Terraform backend](https://developer.hashicorp.com/terraform/language/backend/oci) stores state and provides locking in this bucket.

In your identity domain, create the named dynamic group using `dynamic-group-rule.txt`. It matches `resource.type='computecontainerinstance'` in the **platform** compartment. The rule covers every Container Instance in that compartment, so keep the platform compartment for the chat only.

Create an IAM policy containing the rendered four rules. Place the policy in a common ancestor of the platform, managed and model-request compartments, such as their parent compartment or the tenancy root. A policy attached only inside the managed child cannot grant access to a model compartment outside that child. See [policy attachment and inheritance](https://docs.oracle.com/en-us/iaas/Content/Identity/Concepts/policies.htm).

1. Manage all resources in the managed compartment.
2. Use `generative-ai-family` in the model-request compartment.
3. Read the state bucket in the platform compartment.
4. Manage objects in the state bucket only.

The runtime has no write access to anything else in the platform compartment.

These are the app's runtime permissions. Your setup operator also needs rights to create the hosting resources and use the existing subnet/NSG. IAM changes may take time to become effective; verify the app's actual startup result rather than assuming that saving a policy proves access.

No additional queue, DevOps, Resource Manager, broker or worker roles are needed.

## 5. Create the container

```bash
oci container-instances container-instance create \
  --region YOUR_INFRASTRUCTURE_REGION \
  --from-json file://build/deploy/container-instance.json
```

Open **Developer Services → Containers & Artifacts → Container Instances** in the same region and compartment. Wait for the instance and child container to be Active. Inspect the container logs for its `ready` event and record its private IPv4 address.

The default bootstrap installs the pinned dependencies, verifies its source archive and Terraform download, initializes the backend and loads the provider schema before opening port 8080. A first startup can take several minutes. Active container status alone does not prove the HTTP application is ready.

Run one instance for this workspace. For a failed or uncertain create request, inspect the existing instance and work request before repeating the create command.

## 6. Connect the authenticated Gateway

Regenerate the routes with the actual private IP:

```bash
python deploy/prepare.py \
  --runtime-config runtime-config.json \
  --deployment-config deployment-config.json \
  --inline-archive build/app.zip \
  --backend-ip YOUR_CONTAINER_PRIVATE_IP \
  --output-dir build/deploy
```

This produces `gateway-http-routes.json`. It is an **array of application routes**, not a complete API deployment specification. Merge it into your already-authenticated Gateway deployment using Console or your normal deployment workflow. Do not submit it as a standalone unauthenticated deployment or overwrite global policies.

Preserve the global OpenID Connect authentication policy and the native OAuth login callback route, commonly `GET /auth/callback`. If you use a different static login path, preserve the exact route referenced by your authentication configuration.

Each generated application route requires authentication and overwrites this backend request header:

```text
X-Codegen-Subject: ${request.auth[id_token_claims][sub]}
```

The application compares the SHA256 of this verified value with `admin_subject_sha256`. The Gateway must overwrite caller-supplied subject values. An arbitrary browser header is not authentication, so keep the container VNIC private and reachable through the intended Gateway path.

For `/api/config`, the generated response policy returns:

```text
X-Codegen-Csrf-Token: ${request.auth[apigw_csrf_token]}
```

The browser returns that value in `X-CSRF-TOKEN` on mutation requests. Separately, the app supplies its own token in the config JSON and expects it in `X-Chat-CSRF`. The supplied frontend handles both. See Oracle's [authentication and CSRF documentation](https://docs.oracle.com/en-us/iaas/Content/APIGateway/Tasks/apigatewayusingjwttokens.htm) and [context-variable reference](https://docs.oracle.com/en-us/iaas/Content/APIGateway/Tasks/apigatewaycontextvariables.htm).

Host this starter at the Gateway root path. Preserve all generated route methods and path substitutions. Do not expose the private `/health` endpoint as an anonymous public application route.

## 7. Verify the deployment

1. Open the Gateway HTTPS URL and sign in as the configured administrator.
2. Confirm the page's managed-scope name, infrastructure region and **Ready** status.
3. Ask the model to list VCN names without proposing changes. Confirm live discovery works.
4. Use the [demo runbook](../DEMO_RUNBOOK.md) to create a small VCN through the reviewed Plan/Apply flow.
5. Stop and restart the app, refresh the browser and confirm saved history/workspace are retained before further changes.

Do not treat the repository's local test results as live verification of your environment. Each customer's sign-in, resource-principal access, model requests and native Plans must work in that deployment.

## Optional: build a prebuilt image

The default inline path does not require a registry upload. To bake dependencies and Terraform into your own image instead:

```bash
docker build --platform linux/amd64 -t YOUR_REGISTRY/oci-terraform-chat:0.1.0 .
docker push YOUR_REGISTRY/oci-terraform-chat:0.1.0
```

Set `image_mode` to `prebuilt` and `container_image` to that image in the deployment configuration, then run the renderer without `--inline-archive`. Use the existing Container Instances image-pull configuration for a private registry; credentials are not included in the example files.

The Docker build targets Linux AMD64, matching the E4 example shape. It verifies the Terraform archive against the bundled official checksum. Keep the same runtime configuration, private networking and authenticated Gateway for either image mode.
