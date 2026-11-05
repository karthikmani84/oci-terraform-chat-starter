# Run locally with your OCI account

This route avoids container and Gateway setup while you try the chat flow. It uses real OCI APIs and remote state. Clicking Apply can provision or change real resources in your configured compartment.

## Prerequisites

- Python 3.11 and Terraform CLI 1.13.5 on `PATH`.
- An OCI API-key profile with access to a dedicated trial compartment, the state bucket and the selected GenAI model.
- A fresh private Object Storage bucket with versioning enabled.
- Access from your computer to OCI APIs, HashiCorp's provider registry and the selected model region.

Follow Oracle's [API signing-key setup](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm) if you do not already have a working local profile.

## Start

From the repository root:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp examples/local-config.example.json local-config.json
```

Edit `local-config.json` with your compartment, Object Storage namespace/bucket, model region and OCI profile. Use the standard `~/.oci/config` location for this walkthrough and select the same profile for the app and Terraform. Keep `auth` as `APIKey`, `local_demo` as `true`, and `bind` as `127.0.0.1`. Use an absolute `workdir` or a private directory such as `local-data/workspace`.

If you need the pinned Terraform binary on macOS ARM64 or Linux AMD64, the bundled helper downloads it from HashiCorp and verifies its checksum:

```bash
python scripts/install_terraform.py --output .local/bin/terraform
export PATH="$PWD/.local/bin:$PATH"
```

For another local platform, install the same Terraform version from [HashiCorp's official release](https://releases.hashicorp.com/terraform/1.13.5/). The OCI container example targets Linux AMD64.

Start the application:

```bash
export SINGLE_CHAT_CONFIG_FILE="$PWD/local-config.json"
python -m app.server
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080). Startup initializes the native OCI backend and loads the provider schema before the page becomes available. The first startup can take longer because Terraform downloads the pinned provider.

The local page does not require Gateway sign-in. Its loopback-only mode is intended for the person operating that computer. For a shared hosted page, use the [OCI deployment guide](OCI_SETUP.md).

Do not use Docker bridge port forwarding for this local mode: the server deliberately binds inside the container's loopback interface. Use direct Python as above, or deploy the hosted mode with its authenticated private Gateway.

## First request

Ask:

> List the VCNs in the managed compartment. Do not propose any changes.

Then try the [demo runbook](../DEMO_RUNBOOK.md). A successful read-only response establishes connectivity, not that a future Terraform Plan or Apply will succeed. Review each real Plan before deciding to apply it.

## Stop and resume

Use Ctrl+C to stop the local process. Restart with the same configuration to load the saved bucket-backed history and workspace. Refresh the browser after a restart so it obtains a fresh application request token.

Keep the OCI profile, private key and filled configuration out of this repository. The provided `.gitignore` covers the documented local filenames. Use a separate bucket for this local trial; a different state key in a bucket shared with another deployment does not isolate the app's code or conversations.
