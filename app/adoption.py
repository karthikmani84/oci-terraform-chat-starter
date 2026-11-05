"""First-change import and deterministic normalization of provider-generated HCL.

Import is only planned here. The application owns the eventual Apply button.
A successful baseline is precisely import-only: no create, update or delete.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any
import uuid

from .terraform import TerraformError, TerraformRunner, plan_summary

SUPPORTED = {'oci_core_vcn', 'oci_network_load_balancer_network_load_balancer'}
TAGS = {'defined_tags', 'freeform_tags'}
_ADDRESS = re.compile(r'(oci_[a-z0-9_]+)\.([a-zA-Z_][a-zA-Z0-9_]*)\Z')
_OCID = re.compile(r'ocid1\.[A-Za-z0-9._-]{8,255}\Z')


class AdoptionError(TerraformError):
    pass


def _clean_block(raw: dict, schema: dict, *, defaults: dict | None = None) -> dict:
    """Drop computed outputs, absent values and proven defaults; never guess.

Terraform's public schema omits SDK `Default` values. Callers can supply a
pinned provider-source default map; absent such evidence a meaningful value
is retained. Empty optional+computed values are omitted, because omission
retains the provider's current computed value on import. The baseline Plan
still must prove that normalization makes no live changes.
"""
    result = {}
    defaults = defaults or {}
    attributes = schema.get('attributes', {})
    blocks = schema.get('block_types', {})
    for key, value in raw.items():
        if key in TAGS or key in ('lifecycle', 'timeouts'):
            continue
        attr = attributes.get(key)
        if attr is not None:
            if attr.get('computed') and not (attr.get('optional') or attr.get('required')):
                continue
            if value is None and not attr.get('required'):
                continue
            if key in defaults and value == defaults[key] and not attr.get('required'):
                continue
            if attr.get('optional') and attr.get('computed') and value in (None, '', [], {}):
                continue
            result[key] = deepcopy(value)
        elif key in blocks:
            block = blocks[key]
            nested = block.get('block', {})
            if value is None:
                continue
            if isinstance(value, list):
                cleaned = [entry for entry in (_clean_block(item, nested) for item in value if isinstance(item, dict)) if entry]
                if cleaned:
                    result[key] = cleaned if block.get('nesting_mode') != 'single' else cleaned[0]
            elif isinstance(value, dict):
                cleaned = _clean_block(value, nested)
                if cleaned:
                    result[key] = cleaned
        else:
            # Unknown generated fields indicate schema/version mismatch.
            raise AdoptionError('IMPORT_SCHEMA_MISMATCH', f'Unknown generated attribute: {key}')
    return result


def normalize_generated(raw: str, *, resource_type: str, name: str, schema: dict,
                        compartment_id: str, defaults: dict | None = None) -> dict:
    try:
        import hcl2
    except ImportError as exc:
        raise AdoptionError('IMPORT_PARSER_MISSING', 'The HCL parser is not installed.') from exc
    try:
        parsed = hcl2.loads(raw)
    except Exception as exc:
        raise AdoptionError('IMPORT_HCL_INVALID', 'Terraform generated HCL could not be parsed.') from exc
    resources = parsed.get('resource', [])
    if len(resources) != 1 or set(resources[0]) != {resource_type} or set(resources[0][resource_type]) != {name}:
        raise AdoptionError('IMPORT_ADDRESS_MISMATCH')
    block = resources[0][resource_type][name]
    if block.get('compartment_id') != compartment_id:
        raise AdoptionError('IMPORT_OUTSIDE_MANAGED_SCOPE', 'The resource is outside the configured managed compartment.')
    normalized = _clean_block(block, schema, defaults=defaults)
    # Existing parent OCIDs remain literal; no new ownership graph is inferred.
    for key, value in normalized.items():
        if key.endswith('_id') and isinstance(value, str) and '${' in value:
            raise AdoptionError('IMPORT_PARENT_EXPRESSION', f'Imported parent {key} must remain a literal.')
    normalized['lifecycle'] = {'ignore_changes': ['defined_tags', 'freeform_tags']}
    return {'resource': {resource_type: {name: normalized}}}


def require_import_only(plan: dict, address: str, resource_id: str) -> None:
    summary = plan_summary(plan)
    counts = summary['counts']
    if any(counts[key] for key in ('add', 'change', 'destroy')):
        raise AdoptionError('IMPORT_BASELINE_NOT_ZERO',
            'The imported configuration would change live infrastructure. Import was not applied.',
            details={'summary': summary})
    exact = [item for item in plan.get('resource_changes', []) if item.get('address') == address]
    if len(exact) != 1 or exact[0].get('change', {}).get('importing', {}).get('id') != resource_id:
        raise AdoptionError('IMPORT_BASELINE_UNPROVEN', 'The baseline Plan does not prove the requested import.')
    if exact[0].get('change', {}).get('actions') not in (['no-op'], ['read']):
        raise AdoptionError('IMPORT_BASELINE_NOT_ZERO')


class AdoptionService:
    def __init__(self, runner: TerraformRunner):
        self.runner = runner

    def prepare(self, resource_type: str, address: str, resource_id: str) -> dict:
        match = _ADDRESS.fullmatch(address)
        if resource_type not in SUPPORTED or not match or match.group(1) != resource_type:
            raise AdoptionError('IMPORT_TYPE_UNSUPPORTED', 'First-change import currently supports VCNs and network load balancers.')
        if not _OCID.fullmatch(resource_id):
            raise AdoptionError('IMPORT_ID_INVALID')
        name = match.group(2)
        key = 'adopt_' + name
        import_path = self.runner.workdir / (key + '_import.tf.json')
        generated_path = self.runner.workdir / (key + '_generated.tf')
        cleaned_path = self.runner.workdir / (key + '.tf.json')
        if any(path.exists() for path in (import_path, generated_path, cleaned_path)):
            raise AdoptionError('IMPORT_ALREADY_PREPARED', 'This resource already has an import draft.')
        # Detect existing managed-resource drift before introducing the new import.
        prior = self.runner.refresh()
        if prior['summary']['has_drift']:
            raise AdoptionError('DRIFT_REQUIRES_DECISION', 'Resolve existing managed-resource drift before importing another resource.',
                                details={'summary': prior['summary'], 'refresh': prior})
        import_text = json.dumps({'import': [{'to': address, 'id': resource_id}]}, indent=2) + '\n'
        self.runner._write_private(import_path, import_text)
        token = uuid.uuid4().hex
        try:
            try:
                self.runner.generate_config('import-generate-' + token, generated_path)
            except TerraformError:
                # Terraform can emit a useful generated file and return a
                # conflicting/default argument validation error. It must still
                # pass the normalized baseline below; no error is accepted as
                # proof of a successful import.
                if not generated_path.is_file():
                    raise
            raw = generated_path.read_text()
            schema = self.runner.schema()['resource_schemas'][resource_type]['block']
            defaults = self.runner.config.get('provider_defaults', {}).get(resource_type, {})
            normalized = normalize_generated(raw, resource_type=resource_type, name=name,
                schema=schema, compartment_id=self.runner.config['compartment_id'], defaults=defaults)
            # Retain raw generated configuration privately for diagnostics; do
            # not leave it alongside the cleaned resource (duplicate address).
            archive = self.runner.plans_dir / (key + '-' + token + '.generated.txt')
            self.runner._write_private(archive, raw)
            generated_path.unlink()
            text = json.dumps(normalized, indent=2, sort_keys=True) + '\n'
            self.runner._write_private(cleaned_path, text)
            self.runner.validate()
            # Refresh-only does not import; it checks the already-managed set.
            # The application must handle any returned drift before proceeding.
            refresh = self.runner.refresh()
            if refresh['summary']['has_drift']:
                raise AdoptionError('DRIFT_REQUIRES_DECISION', details={'summary': refresh['summary'], 'refresh': refresh})
            baseline = self.runner.plan('import-baseline-' + token)
            require_import_only(baseline['plan'], address, resource_id)
            return {'files': {import_path.name: import_text, cleaned_path.name: text},
                    'baseline_plan': baseline, 'address': address, 'resource_id': resource_id,
                    'message': 'Import baseline verified: 0 to add, 0 to change, 0 to destroy. Import has not been applied.'}
        except Exception:
            # Preserve the prior workspace exactly; failed adoption drafts are
            # available only under private diagnostics, never active .tf files.
            for path in (import_path, generated_path, cleaned_path):
                if path.exists():
                    path.unlink()
            raise

    def adopt_live(self, drift: dict) -> dict[str, str]:
        """Keep live values for explicit direct attributes in managed JSON code.

Reject missing/deleted objects, nested blocks and computed-only properties.
Those require an explicit follow-up design rather than a guessed conversion.
"""
        changes = drift.get('plan', drift).get('resource_drift', [])
        if not changes:
            return {}
        schema = self.runner.schema().get('resource_schemas', {})
        updates: dict[Path, dict] = {}
        for item in changes:
            address = item.get('address', '')
            match = _ADDRESS.fullmatch(address)
            change = item.get('change', {})
            if not match or change.get('actions') != ['update']:
                raise AdoptionError('DRIFT_KEEP_LIVE_UNSUPPORTED', 'This live change needs a manual configuration decision.')
            resource_type, name = match.groups()
            before, after = change.get('before') or {}, change.get('after') or {}
            candidates = []
            for path in self.runner.workdir.glob('*.tf.json'):
                data = updates.get(path)
                if data is None:
                    try:
                        data = json.loads(path.read_text())
                    except (OSError, ValueError):
                        continue
                block = data.get('resource', {}).get(resource_type, {}).get(name)
                if isinstance(block, dict):
                    candidates.append((path, data, block))
            if len(candidates) != 1:
                raise AdoptionError('DRIFT_CODE_NOT_EDITABLE', 'Keep live currently requires the application’s JSON Terraform configuration.')
            path, data, block = candidates[0]
            attributes = schema.get(resource_type, {}).get('block', {}).get('attributes', {})
            for field in sorted(before.keys() | after.keys()):
                if before.get(field) == after.get(field) or field in TAGS:
                    continue
                attribute = attributes.get(field, {})
                if attribute.get('computed') and not (attribute.get('optional') or attribute.get('required')):
                    continue
                if field not in block:
                    # An omitted optional+computed property is already delegated
                    # to the provider. No code edit is required to keep it.
                    if attribute.get('optional') and attribute.get('computed'):
                        continue
                    raise AdoptionError('DRIFT_KEEP_LIVE_UNSUPPORTED', f'Cannot reconcile omitted field {field}.')
                if field.endswith('_id') or field == 'compartment_id':
                    if after.get(field) != before.get(field):
                        raise AdoptionError('DRIFT_PARENT_CHANGED', 'A changed parent requires a new reviewed proposal.')
                if isinstance(before.get(field), (dict, list)) or isinstance(after.get(field), (dict, list)):
                    raise AdoptionError('DRIFT_KEEP_LIVE_UNSUPPORTED', f'Nested live change in {field} needs a new proposal.')
                if '${' in str(block[field]):
                    raise AdoptionError('DRIFT_EXPRESSION_UNSUPPORTED', f'Cannot rewrite an expression in {field}.')
                if after.get(field) is None and not attribute.get('required'):
                    block.pop(field, None)
                else:
                    block[field] = deepcopy(after.get(field))
            updates[path] = data
        return {path.name: json.dumps(data, indent=2, sort_keys=True) + '\n' for path, data in updates.items()}
