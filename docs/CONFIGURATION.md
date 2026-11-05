# Configuration

The app reads `SINGLE_CHAT_CONFIG_FILE` when set. Otherwise it reads JSON from `SINGLE_CHAT_CONFIG_JSON`. Keep filled configuration files outside Git; the repository contains examples only.

| Key | Meaning |
| --- | --- |
| `region` | OCI region for infrastructure, storage and the Terraform provider. Required. |
| `compartment_id` | The dedicated compartment the app may manage. Required. |
| `scope_name` | Friendly name displayed in chat; defaults to `Managed compartment`. |
| `namespace` | Object Storage namespace. Required. |
| `bucket` | Existing private, versioned state/artifact bucket. Required. |
| `state_key` | Terraform state object key; defaults to `terraform/managed.tfstate`. |
| `auth` | `ResourcePrincipal` in OCI; `APIKey` for local development. |
| `config_file` | OCI SDK configuration path when using `APIKey`; use the standard `~/.oci/config` for the local walkthrough. |
| `profile` | API-key profile name; defaults to `DEFAULT`. |
| `model_region` | Model API endpoint region; defaults to `region`. |
| `model_compartment_id` | Compartment used for model requests; defaults to `compartment_id`. |
| `model_id` | On-demand model identifier; defaults to `openai.gpt-oss-120b`. |
| `routing_profile_id` | Optional explicit OCI routing profile. Omit for direct model calls. When present, this takes precedence over `model_id`. |
| `provider_version` | OCI provider version, pinned to `9.11.0` in the example. |
| `terraform_bin` | Terraform executable path or name; defaults to `terraform`. |
| `workdir` | Private local working directory; the container uses `/data/workspace`. |
| `public_host` | Exact browser-facing hostname, without `https://`, a path or port. Required for hosted mode. |
| `admin_subject_sha256` | SHA256 digest of the intended administrator's exact verified ID-token `sub`. Required for hosted mode. |
| `local_demo` | Local developer mode only. When true, the listener is restricted to loopback. |
| `bind` | Hosted default is `0.0.0.0`; local mode uses `127.0.0.1`. |

`PORT` is a separate environment variable; the default is `8080`.

## Choose the administrator

Use the exact subject claim your identity provider issues to this sign-in application. It may differ from the user's email address or OCI user OCID. Ask the identity administrator or use the identity provider's supported user-info tools to establish the value.

Hash the subject locally. The helper accepts the subject through a hidden prompt and prints only its digest:

```bash
python3 tools/hash_subject.py
```

Put that digest in `admin_subject_sha256`. The helper is not a token decoder or verifier: establish the exact verified subject through your identity provider first. Do not put an ID token, access token or client secret in the runtime configuration. See Oracle's [Identity Domains token reference](https://docs.oracle.com/en-us/iaas/Content/Identity/api-getstarted/SupportedTokens.htm).

## Choose the model region

Direct access requires a model available for on-demand use in `model_region`. A routing profile is optional; it is not needed to call a model's regional API directly. The example uses `eu-frankfurt-1` for `openai.gpt-oss-120b` and leaves `routing_profile_id` absent.

OCI's [model availability table](https://docs.oracle.com/en-us/iaas/Content/generative-ai/model-endpoint-regions.htm) distinguishes on-demand and dedicated-only availability. Processing in `model_region` includes the prompts, code and filtered metadata supplied to the model.

## One workspace per configuration

Use one bucket per independent deployment. The app uses fixed workspace/conversation prefixes, so changing `state_key` alone does not isolate another application. Reusing an existing bucket intentionally loads its saved code and history. For an independent trial, create a fresh bucket and use one application instance.
