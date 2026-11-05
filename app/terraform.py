"""Application-owned Terraform CLI. No model can dispatch commands here.

The native OCI backend owns state locking; the chat service additionally
serializes the entire refresh / plan / apply sequence for its one workspace.
Plans and state are private artifacts, never supplied wholesale to the model.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any
import uuid


class TerraformError(RuntimeError):
    def __init__(self, code: str, message: str = '', *, details: dict | None = None):
        self.code, self.message, self.details = code, message or code, details or {}
        super().__init__(self.message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _sensitive(value: Any, mask: Any) -> Any:
    if mask is True:
        return '[sensitive]'
    if isinstance(value, dict):
        masks = mask if isinstance(mask, dict) else {}
        return {key: _sensitive(item, masks.get(key, False)) for key, item in value.items()}
    if isinstance(value, list):
        masks = mask if isinstance(mask, list) else []
        return [_sensitive(item, masks[i] if i < len(masks) else False) for i, item in enumerate(value)]
    return value


_SECRET_KEY = re.compile(r'password|private.?key|secret|credential|token|user_data|connection_string|content', re.I)


def safe_value(value: Any) -> Any:
    """Default deny known credential/content fields in Plan and diagnostic views."""
    if isinstance(value, dict):
        return {key: '[sensitive]' if _SECRET_KEY.search(key) else safe_value(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [safe_value(item) for item in value]
    return value


def safe_diagnostic(raw: str, maximum: int = 6000) -> str:
    raw = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[private key redacted]', raw, flags=re.S)
    raw = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[token redacted]', raw)
    raw = re.sub(r'(?im)authorization\s*[:=]\s*[^\n]+', '[authorization redacted]', raw)
    raw = re.sub(r'(?i)bearer\s+[^\s,]+', '[authorization redacted]', raw)
    raw = re.sub(r'(?im)((?:password|client_secret|private_key|access_token|security_token)\s*[:=]\s*)[^\n]+', r'\1[sensitive]', raw)
    return raw[:maximum]


def _field_changes(before: Any, after: Any, prefix: str = '') -> list[dict[str, Any]]:
    """Produce a bounded, readable diff with sensitive masks already applied."""
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        result: list[dict] = []
        for key in sorted(before.keys() | after.keys()):
            result.extend(_field_changes(before.get(key), after.get(key), f'{prefix}.{key}'.lstrip('.')))
        return result[:200]
    return [{'field': prefix or 'value', 'before': before, 'after': after}]


def plan_summary(plan: dict, *, refresh: bool = False) -> dict:
    source = plan.get('resource_drift', []) if refresh else plan.get('resource_changes', [])
    changes, counts = [], {'add': 0, 'change': 0, 'destroy': 0, 'replace': 0, 'import': 0}
    for item in source:
        change = item.get('change', {})
        actions = change.get('actions', [])
        importing = change.get('importing')
        if importing:
            counts['import'] += 1
        if actions in ([], ['no-op'], ['read']):
            if not importing:
                continue
        if 'create' in actions:
            counts['add'] += 1
        if 'delete' in actions:
            counts['destroy'] += 1
        if 'update' in actions:
            counts['change'] += 1
        if 'create' in actions and 'delete' in actions:
            counts['replace'] += 1
        before = safe_value(_sensitive(change.get('before'), change.get('before_sensitive')))
        after = safe_value(_sensitive(change.get('after'), change.get('after_sensitive')))
        changes.append({'address': item.get('address'), 'type': item.get('type'), 'actions': actions,
                        'importing': bool(importing), 'fields': _field_changes(before, after),
                        'unknown': safe_value(change.get('after_unknown', {}))})
    return {'counts': counts, 'resources': changes, 'destructive': counts['destroy'] > 0,
            'has_changes': any(counts[k] for k in ('add', 'change', 'destroy')),
            'has_drift': bool(changes) if refresh else bool(plan.get('resource_drift'))}


@dataclass(frozen=True)
class CLIResult:
    returncode: int
    stdout: str
    stderr: str


class TerraformRunner:
    def __init__(self, workdir: Path, config: dict):
        self.workdir = Path(workdir).resolve()
        self.config = dict(config)
        self.binary = str(config.get('terraform_bin', 'terraform'))
        self.workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.plans_dir = self.workdir / '.plans'
        self.plans_dir.mkdir(mode=0o700, exist_ok=True)
        self.timeout = int(config.get('terraform_timeout', 600))
        self.environment = os.environ.copy()
        self.environment.update(TF_IN_AUTOMATION='1', TF_INPUT='0', CHECKPOINT_DISABLE='1')
        # Authentication values remain in the process environment/config file;
        # private key material is never embedded in provider or backend HCL.
        if config.get('config_file'):
            self.environment['OCI_CONFIG_FILE'] = str(config['config_file'])
            self.environment['OCI_CONFIG_FILE_PROFILE'] = str(config.get('profile', 'DEFAULT'))
        self.environment['OCI_REGION'] = config['region']

    def run(self, args: list[str], *, allowed: tuple[int, ...] = (0,), cwd: Path | None = None) -> CLIResult:
        if not all(isinstance(arg, str) and '\x00' not in arg for arg in args):
            raise TerraformError('CLI_ARGUMENT_INVALID')
        try:
            completed = subprocess.run([self.binary, *args], cwd=cwd or self.workdir,
                env=self.environment, text=True, capture_output=True, timeout=self.timeout,
                stdin=subprocess.DEVNULL, shell=False)
        except subprocess.TimeoutExpired as exc:
            raise TerraformError('TERRAFORM_TIMEOUT', 'Terraform timed out; inspect the saved state before retrying.') from exc
        except OSError as exc:
            raise TerraformError('TERRAFORM_UNAVAILABLE', 'The Terraform executable is unavailable.') from exc
        if completed.returncode not in allowed:
            raise TerraformError('TERRAFORM_COMMAND_FAILED', safe_diagnostic(completed.stderr or completed.stdout),
                details={'operation': args[0] if args else '', 'exit_code': completed.returncode})
        return CLIResult(completed.returncode, completed.stdout, completed.stderr)

    def write_base(self) -> None:
        region = self.config['region']
        auth = self.config.get('auth', 'ResourcePrincipal')
        if auth not in ('ResourcePrincipal', 'APIKey'):
            raise TerraformError('AUTH_CONFIGURATION_INVALID')
        version = self.config.get('provider_version', '9.11.0')
        if not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise TerraformError('PROVIDER_VERSION_INVALID')
        provider = {'region': region, 'auth': auth}
        if auth == 'APIKey':
            provider['config_file_profile'] = self.config.get('profile', 'DEFAULT')
        # This JSON file is application-owned, never model-editable.
        content = {'terraform': {'required_version': '>= 1.13.0, < 2.0.0',
            'required_providers': {'oci': {'source': 'oracle/oci', 'version': '= ' + version}},
            'backend': {'oci': {}}}, 'provider': {'oci': provider}}
        self._write_private(self.workdir / 'app.tf.json', _json(content))

    def init(self) -> dict:
        self.write_base()
        backend = {key: self.config[key] for key in ('namespace', 'bucket')}
        backend.update(key=self.config.get('state_key', 'terraform/managed.tfstate'),
            region=self.config['region'], auth=self.config.get('auth', 'ResourcePrincipal'))
        if self.config.get('auth') == 'APIKey':
            backend['config_file_profile'] = self.config.get('profile', 'DEFAULT')
        target = self.workdir / '.backend.json'
        self._write_private(target, _json(backend))
        result = self.run(['init', '-input=false', '-no-color', '-reconfigure', '-backend-config=' + str(target)])
        return {'initialized': True, 'message': safe_diagnostic(result.stdout, 2000)}

    @staticmethod
    def _write_private(path: Path, content: str | bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content.encode() if isinstance(content, str) else content)

    def validate(self) -> dict:
        self.run(['fmt', '-no-color'])
        result = self.run(['validate', '-json', '-no-color'])
        data = json.loads(result.stdout)
        if not data.get('valid'):
            raise TerraformError('TERRAFORM_INVALID', safe_diagnostic(result.stdout))
        return {'valid': True, 'warnings': data.get('warning_count', 0)}

    def _path(self, plan_id: str) -> Path:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', plan_id):
            raise TerraformError('PLAN_ID_INVALID')
        path = self.plans_dir / (plan_id + '.tfplan')
        if path.is_symlink():
            raise TerraformError('PLAN_PATH_INVALID')
        return path

    def show(self, plan_path: str | Path) -> dict:
        path = Path(plan_path).resolve()
        if path.parent != self.plans_dir or not path.is_file():
            raise TerraformError('PLAN_NOT_FOUND')
        return json.loads(self.run(['show', '-json', str(path)]).stdout)

    def _plan(self, plan_id: str, *, refresh: bool = False, generate_config_out: Path | None = None) -> dict:
        path = self._path(plan_id)
        if path.exists():
            raise TerraformError('PLAN_ALREADY_EXISTS')
        args = ['plan', '-input=false', '-no-color', '-lock-timeout=60s', '-detailed-exitcode', '-out=' + str(path)]
        if refresh:
            args.append('-refresh-only')
        if generate_config_out is not None:
            output = Path(generate_config_out).resolve()
            if output.parent != self.workdir or output.exists() or output.suffix != '.tf':
                raise TerraformError('GENERATE_OUTPUT_INVALID')
            args.append('-generate-config-out=' + str(output))
        result = self.run(args, allowed=(0, 2))
        if not path.is_file():
            raise TerraformError('PLAN_ARTIFACT_MISSING')
        os.chmod(path, 0o600)
        plan = self.show(path)
        summary = plan_summary(plan, refresh=refresh)
        return {'id': plan_id, 'plan': plan, 'path': str(path), 'digest': sha256(path),
                'changes': summary['resources'], 'summary': summary,
                'destructive': summary['destructive'], 'exit_code': result.returncode}

    def refresh(self) -> dict:
        return self._plan('refresh-' + uuid.uuid4().hex, refresh=True)

    def plan(self, plan_id: str) -> dict:
        # The state machine must inspect this result for unresolved drift.
        # Refresh is separate so administrators can choose keep/restore first.
        return self._plan(plan_id)

    def generate_config(self, plan_id: str, output: Path) -> dict:
        return self._plan(plan_id, generate_config_out=output)

    def apply(self, plan_path: str | Path) -> dict:
        path = Path(plan_path).resolve()
        if path.parent != self.plans_dir or not path.is_file() or path.is_symlink():
            raise TerraformError('PLAN_NOT_FOUND')
        # No -auto-approve: passing the exact saved binary is the confirmation.
        result = self.run(['apply', '-input=false', '-no-color', '-lock-timeout=60s', str(path)])
        return {'applied': True, 'plan_digest': sha256(path),
                'message': safe_diagnostic(result.stdout, 4000), 'state': state_metadata(self.state())}

    def state(self) -> dict:
        result = self.run(['state', 'pull'])
        data = json.loads(result.stdout)
        return data

    def schema(self) -> dict:
        data = json.loads(self.run(['providers', 'schema', '-json']).stdout)
        try:
            return data['provider_schemas']['registry.terraform.io/oracle/oci']
        except KeyError as exc:
            raise TerraformError('PROVIDER_SCHEMA_MISSING') from exc


def live_snapshot(plan: dict) -> str:
    """Fingerprint refresh-only planned values for the app's drift/expiry gate.

This digest is private state evidence, not a model context payload. Callers
must compare two refresh-only snapshots, not a desired-change Plan to refresh.
"""
    values = plan.get('planned_values')
    if not isinstance(values, dict):
        raise TerraformError('REFRESH_VALUES_MISSING')
    return hashlib.sha256(_json(values).encode()).hexdigest()


def state_metadata(state: dict) -> dict:
    """Public result metadata; state values and outputs stay in the backend."""
    items = []
    for resource in state.get('resources', []):
        if resource.get('mode', 'managed') != 'managed':
            continue
        address = resource.get('type', '') + '.' + resource.get('name', '')
        if resource.get('module'):
            address = resource['module'] + '.' + address
        for instance in resource.get('instances', []):
            key = instance.get('index_key')
            indexed = address if key is None else address + '[' + json.dumps(key) + ']'
            items.append({'address': indexed, 'type': resource.get('type'),
                          'id': (instance.get('attributes') or {}).get('id')})
    return {'serial': state.get('serial'), 'lineage': state.get('lineage'), 'resources': items}
