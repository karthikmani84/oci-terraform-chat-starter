"""Application-owned review -> Plan -> exact saved Plan Apply state machine."""
from __future__ import annotations
import copy
import difflib
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import uuid

from .storage import Missing
from .terraform import TerraformError


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def text_files(path):
    return {p.name: p.read_text() for p in Path(path).glob('*.tf*')
            if p.name.endswith(('.tf', '.tf.json')) and p.name not in {'provider.tf', 'backend.tf', 'app.tf.json'}}


def resources(files):
    result = {}
    for filename, source in files.items():
        if filename.endswith('.tf.json'):
            for typ, blocks in json.loads(source).get('resource', {}).items():
                for name, attrs in blocks.items():
                    address = typ + '.' + name
                    if address in result: raise ValueError('Duplicate Terraform resource: ' + address)
                    result[address] = (filename, attrs)
    return result


def live_digest(refresh):
    return digest(refresh.get('plan', {}).get('planned_values', {}))


def has_drift(refresh):
    return any(x.get('change', {}).get('actions') != ['no-op']
               for x in refresh.get('plan', {}).get('resource_drift', []))


def code_diff(before, after):
    chunks = []
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) != after.get(name):
            chunks.extend(difflib.unified_diff(before.get(name, '').splitlines(),
                after.get(name, '').splitlines(), fromfile='Current/' + name,
                tofile='Proposed/' + name, lineterm=''))
    return '\n'.join(chunks)


class Engine:
    def __init__(self, config, store, runner, adoption, model, tools, *, clock=time.time):
        self.config, self.store, self.runner, self.adoption = config, store, runner, adoption
        self.model, self.tools, self.clock = model, tools, clock
        self.lock = threading.RLock()
        self.tasks = set()
        self.workspace_lock = threading.RLock()
        self.current = store.json('workspace/current.json', {'revision': 0, 'files': {}})
        self._write(self.current['files'])

    def _write(self, files):
        root = self.runner.workdir
        root.mkdir(parents=True, exist_ok=True)
        for p in root.glob('*.tf*'):
            if p.name not in {'provider.tf', 'backend.tf', 'app.tf.json'} and p.name.endswith(('.tf', '.tf.json')):
                p.unlink()
        for name, source in files.items():
            if not re.fullmatch(r'[a-zA-Z0-9_-]+\.tf(?:\.json)?', name) or name in {'provider.tf', 'backend.tf', 'app.tf.json'}:
                raise ValueError('Generated code cannot replace application configuration.')
            (root / name).write_text(source)

    def _key(self, cid):
        if not re.fullmatch(r'[0-9a-f-]{36}', str(cid)): raise ValueError('Unknown conversation.')
        return 'conversations/' + cid + '.json'

    def _get(self, cid, owner):
        conv = self.store.json(self._key(cid))
        if not conv or conv['owner'] != owner: raise Missing(cid)
        return conv

    def _save(self, conv):
        conv['updated_at'] = self.clock()
        self.store.save(self._key(conv['id']), conv)

    def public(self, conv):
        result = {k: copy.deepcopy(v) for k, v in conv.items() if k in {
            'id', 'revision', 'reviewed_revision', 'stage', 'messages', 'diff', 'plan',
            'drift', 'result', 'error', 'busy', 'updated_at', 'title', 'baseline'}}
        if result.get('plan') and self.clock() >= result['plan']['expires_at'] and result['stage'] == 'planned':
            result['stage'], result['error'] = 'review', 'This Plan expired. Create a fresh Plan.'
        return result

    def create(self, owner):
        with self.lock:
            conv = {'id': str(uuid.uuid4()), 'owner': owner, 'revision': 0, 'stage': 'clarify',
                    'messages': [], 'busy': False, 'title': 'New idea', 'files': {},
                    'workspace_revision': self.current['revision']}
            self._save(conv)
            return self.public(conv)

    def get(self, cid, owner):
        with self.lock:
            conv = self._get(cid, owner)
            if conv.get('busy') and cid not in self.tasks:
                # A process restart cannot resume an uncertain Apply automatically.
                conv.update(busy=False, stage='error', error='The service restarted during this operation. Your request and remote Terraform state are retained. Review and create a new Plan before continuing.')
                self._save(conv)
            return self.public(conv)

    def list(self, owner):
        items = []
        with self.lock:
            for key in self.store.keys('conversations/'):
                value = self.store.json(key)
                if value and value.get('owner') == owner:
                    items.append({'id': value['id'], 'title': value.get('title', 'Request'),
                                  'stage': value['stage'], 'updated_at': value.get('updated_at', 0)})
        return sorted(items, key=lambda v: v['updated_at'], reverse=True)

    def start(self, cid, owner, action, body):
        with self.lock:
            conv = self._get(cid, owner)
            if conv.get('busy'): raise ValueError('This conversation is still processing.')
            if action == 'review':
                if conv['stage'] != 'review' or body.get('revision') != conv['revision']:
                    raise ValueError('Review the latest code revision.')
                conv['reviewed_revision'] = conv['revision']
                conv['messages'].append({'role': 'assistant', 'content': 'Diff reviewed. Create a Plan to preview the live changes.'})
                self._save(conv)
                return self.public(conv)
            if action == 'messages':
                message = body.get('message')
                if not isinstance(message, str) or not 1 <= len(message.strip()) <= 20000:
                    raise ValueError('Enter a request or answer.')
                conv['messages'].append({'role': 'user', 'content': message.strip()})
                if conv['title'] == 'New idea': conv['title'] = message.strip()[:72]
                conv.pop('plan', None)
                conv.pop('saved_plan', None)
                conv['stage'] = 'clarify'
            elif action == 'plan':
                if body.get('revision') != conv['revision'] or conv.get('reviewed_revision') != conv['revision']:
                    raise ValueError('Review the latest diff before creating a Plan.')
                if conv['stage'] not in {'review', 'planned', 'error'}:
                    raise ValueError('This request is not ready for a Plan.')
                conv['stage'] = 'planning'
            elif action == 'apply':
                self._check_apply(conv, body)
                conv['stage'] = 'applying'
            elif action == 'drift':
                if conv['stage'] != 'drift' or body.get('choice') not in {'keep_live', 'restore_code'}:
                    raise ValueError('Choose whether to keep the live value or restore the code value.')
            else: raise ValueError('Unknown action.')
            conv.update(busy=True, error=None)
            self.tasks.add(cid)
            self._save(conv)
            threading.Thread(target=self._work, args=(cid, owner, action, copy.deepcopy(body)), daemon=True).start()
            return self.public(conv)

    def _check_apply(self, conv, body):
        plan = conv.get('saved_plan') or {}
        if conv['stage'] != 'planned' or body.get('plan_id') != plan.get('id'):
            raise ValueError('Apply requires the exact saved Plan shown in this conversation.')
        if self.clock() >= plan['expires_at']: raise ValueError('This Plan expired. Create a new Plan.')
        if plan['code_digest'] != digest(conv['files']) or plan['revision'] != conv['revision']:
            raise ValueError('The code changed. Review it and create a new Plan.')
        if plan['workspace_revision'] != self.current['revision']:
            raise ValueError('Another request changed managed state. Create a fresh Plan.')
        if plan['destructive'] and body.get('destructive_confirmation') != conv['plan']['confirmation_phrase']:
            raise ValueError('Deletes or replacements require the second confirmation shown with this Plan.')

    def _work(self, cid, owner, action, body):
        try:
            with self.workspace_lock:
                with self.lock: conv = self._get(cid, owner)
                if action == 'messages': self._draft(conv)
                elif action == 'plan': self._plan(conv)
                elif action == 'apply': self._apply(conv, body)
                elif action == 'drift': self._resolve_drift(conv, body['choice'])
                with self.lock: self._save(conv)
        except Exception as exc:
            with self.lock:
                conv = self._get(cid, owner)
                # Terraform diagnostics are already sanitized by the runner.
                message = str(exc)[:3500]
                conv.update(stage='error', error=message)
                conv['messages'].append({'role': 'assistant', 'content': message + '\nYour request and answers remain saved.'})
                self._save(conv)
        finally:
            with self.lock:
                conv = self._get(cid, owner)
                conv['busy'] = False
                self.tasks.discard(cid)
                self._save(conv)

    def _draft(self, conv):
        self._write(self.current['files'])
        self.runner.init()
        feedback = None
        for attempt in range(3):
            output = self.model.respond(conv, feedback)
            if output['kind'] == 'clarify':
                conv['messages'].append({'role': 'assistant', 'content': output['message']})
                conv.update(stage='clarify', workspace_revision=self.current['revision'])
                return
            try:
                files = copy.deepcopy(self.current['files'])
                adoptions = []
                changes = output.get('changes', [])
                removals = output.get('remove', [])
                if not isinstance(changes, list) or not isinstance(removals, list) or not (changes or removals):
                    raise ValueError('A draft needs at least one resource change or removal.')
                # Validate the entire draft before resource lookups or import work.
                # Format errors belong in the existing two-correction loop.
                for item in changes:
                    if not isinstance(item, dict):
                        raise ValueError('Each change must be an object with address and values.')
                    address, values = item.get('address', ''), item.get('values')
                    if not isinstance(address, str) or not re.fullmatch(r'oci_[a-z0-9_]+\.[a-zA-Z_][a-zA-Z0-9_]*', address) or not isinstance(values, dict):
                        raise ValueError('Return one Terraform resource address and attributes per change.')
                    if 'existing_id' in item and (not isinstance(item['existing_id'], str) or not item['existing_id'].startswith('ocid1.')):
                        raise ValueError('existing_id must be the resource OCID string returned by get_resource.')
                for address in removals:
                    if not isinstance(address, str) or not re.fullmatch(r'oci_[a-z0-9_]+\.[a-zA-Z_][a-zA-Z0-9_]*', address):
                        raise ValueError('Each removal must be a Terraform resource address string. Use remove: ["oci_resource_type.resource_name"], not objects or resource names.')
                    if address not in resources(self.current['files']):
                        raise ValueError('Cannot remove an unmanaged resource. Select its existing managed Terraform address; do not import it as part of a deletion draft.')
                self._write(files)
                for item in changes:
                    address, values = item['address'], item['values']
                    if 'existing_id' in item and address not in resources(files):
                        live = self.tools.execute('get_resource', {'resource_type': address.split('.')[0], 'resource_id': item['existing_id']})
                        if live.get('compartment_id') != self.config['compartment_id']: raise ValueError('Existing resource is outside managed scope.')
                        baseline = self.adoption.prepare(address.split('.')[0], address, item['existing_id'])
                        files.update(baseline['files'])
                        adoptions.append({'address': address, 'resource_type': address.split('.')[0], 'resource_id': item['existing_id']})
                        conv['baseline'] = {'message': 'Import baseline checked: 0 to add, 0 to change, 0 to destroy.', 'address': address}
                    existing = resources(files)
                    filename, before = existing.get(address, ('resources.tf.json', {}))
                    if not filename.endswith('.tf.json'): raise ValueError('Adopted code must be normalized Terraform JSON.')
                    document = json.loads(files.get(filename, '{}'))
                    typ, name = address.split('.')
                    attrs = copy.deepcopy(before)
                    attrs.update(values)
                    if attrs.get('compartment_id', self.config['compartment_id']) != self.config['compartment_id']:
                        raise ValueError('All managed resources must stay in the configured managed compartment.')
                    attrs.pop('defined_tags', None); attrs.pop('freeform_tags', None)
                    attrs.setdefault('lifecycle', {})['ignore_changes'] = ['defined_tags', 'freeform_tags']
                    document.setdefault('resource', {}).setdefault(typ, {})[name] = attrs
                    files[filename] = json.dumps(document, indent=2, sort_keys=True) + '\n'
                for address in removals:
                    existing = resources(files)
                    if address not in existing: raise ValueError('Cannot remove an unmanaged resource.')
                    filename, _ = existing[address]
                    document = json.loads(files[filename]); typ, name = address.split('.')
                    del document['resource'][typ][name]
                    if not document['resource'][typ]: del document['resource'][typ]
                    if not document['resource']: document.pop('resource')
                    files[filename] = json.dumps(document, indent=2, sort_keys=True) + '\n'
                    # Applied imports remain in saved configuration. Remove only
                    # this target's import records along with its resource block.
                    for import_file, source in list(files.items()):
                        document = json.loads(source)
                        imports = document.get('import', [])
                        retained = [item for item in imports if item.get('to') != address]
                        if len(retained) != len(imports):
                            if retained: document['import'] = retained
                            else: document.pop('import')
                            files[import_file] = json.dumps(document, indent=2, sort_keys=True) + '\n'
                self._admit(files)
                self._write(files)
                self.runner.validate()
                conv.update(files=files, revision=conv['revision'] + 1, reviewed_revision=None,
                            workspace_revision=self.current['revision'], stage='review', adoptions=adoptions)
                conv['diff'] = code_diff(self.current['files'], files)
                conv['messages'].append({'role': 'assistant', 'content': output['message'] + '\nReview the diff, then create a Plan.'})
                return
            except (ValueError, TerraformError) as exc:
                if getattr(exc, 'code', '') == 'DRIFT_REQUIRES_DECISION' and getattr(exc, 'details', {}).get('refresh'):
                    refresh = exc.details['refresh']
                    conv.update(stage='drift', drift={'summary': refresh.get('summary', {})},
                                drift_raw=refresh['plan'], drift_digest=live_digest(refresh), pending_draft=True)
                    conv['messages'].append({'role': 'assistant', 'content': 'A managed resource changed outside Terraform. Keep the live value or restore its code value before importing this resource.'})
                    return
                if attempt == 2: raise
                feedback = str(exc)[:3000]

    def _admit(self, files):
        known = set(resources(files))
        def expressions(value):
            if isinstance(value, str) and ('${' in value or '%{' in value):
                match = re.fullmatch(r'\$\{(oci_[a-z0-9_]+\.[A-Za-z_][A-Za-z0-9_]*)\.id\}', value)
                if not match or match.group(1) not in known:
                    raise ValueError('Use literal values or a direct ID reference to a managed resource. Filesystem, command and external expressions are unavailable.')
            elif isinstance(value, dict):
                for item in value.values(): expressions(item)
            elif isinstance(value, list):
                for item in value: expressions(item)
        for name, source in files.items():
            if not name.endswith('.tf.json'): raise ValueError('Only application-normalized Terraform JSON is accepted.')
            doc = json.loads(source)
            if set(doc) - {'resource', 'import'}: raise ValueError('The app owns providers, backend and execution settings.')
            for typ, blocks in doc.get('resource', {}).items():
                if not typ.startswith('oci_'): raise ValueError('Only OCI resources are supported.')
                for attrs in blocks.values():
                    if any(k in attrs for k in ('provisioner', 'connection', 'provider', 'count', 'for_each')):
                        raise ValueError('Executable hooks and alternate providers are unavailable.')
                    if 'compartment_id' in attrs and attrs['compartment_id'] != self.config['compartment_id']:
                        raise ValueError('Resource compartment must be the literal managed compartment OCID.')
                    expressions(attrs)

    def _refresh(self, files):
        self._write(files)
        self.runner.init()
        return self.runner.refresh()

    def _plan(self, conv):
        if conv['workspace_revision'] != self.current['revision']:
            raise ValueError('Another request changed the workspace. Refine this request to review an updated diff.')
        refresh = self._refresh(self.current['files'])
        snapshot = live_digest(refresh)
        if has_drift(refresh) and snapshot not in {conv.get('resolved_drift_digest'), self.current.get('accepted_live_digest')}:
            conv.update(stage='drift', drift={'summary': refresh.get('summary', refresh.get('changes', []))},
                        drift_raw=refresh['plan'], drift_digest=snapshot)
            conv['messages'].append({'role': 'assistant', 'content': 'Live settings changed outside Terraform. Keep the live values in code, or restore the code values? No infrastructure has been changed.'})
            return
        self._write(conv['files'])
        imports_before = self._import_fingerprints(conv)
        pid = str(uuid.uuid4())
        result = self.runner.plan(pid)
        self._plan_scope(result['plan'])
        if self._import_fingerprints(conv) != imports_before:
            raise ValueError('The existing resource changed while its Plan was being prepared. Create a fresh Plan.')
        plan_bytes = Path(result['path']).read_bytes()
        self.store.put('plans/' + pid + '/saved.tfplan', plan_bytes)
        self.store.save('plans/' + pid + '/raw.json', result['plan'])
        expires = self.clock() + 15 * 60
        destructive = bool(result.get('destructive')) or any('delete' in x.get('change', {}).get('actions', []) for x in result['plan'].get('resource_changes', []))
        saved = {'id': pid, 'expires_at': expires, 'created_at': self.clock(), 'revision': conv['revision'],
                 'code_digest': digest(conv['files']), 'plan_digest': hashlib.sha256(plan_bytes).hexdigest(),
                 'live_digest': snapshot, 'workspace_revision': self.current['revision'], 'destructive': destructive}
        saved['import_fingerprints'] = imports_before
        conv.update(stage='planned', saved_plan=saved, drift=None,
                    plan={'id': pid, 'expires_at': expires, 'summary': result.get('summary', result.get('changes', [])),
                          'destructive': destructive, 'confirmation_phrase': 'APPLY ' + pid[-8:] if destructive else ''})
        self.store.save('plans/' + pid + '/metadata.json', saved)
        conv['messages'].append({'role': 'assistant', 'content': 'Plan ready. Review the changes below. Apply uses only this saved Plan and expires in 15 minutes.'})

    def _resolve_drift(self, conv, choice):
        fresh = self._refresh(self.current['files'])
        if live_digest(fresh) != conv['drift_digest']:
            conv.update(drift={'summary': fresh.get('summary', fresh.get('changes', []))}, drift_raw=fresh['plan'], drift_digest=live_digest(fresh))
            conv['messages'].append({'role': 'assistant', 'content': 'Live settings changed again. Review the latest drift before choosing.'})
            return
        if choice == 'keep_live':
            # Regenerate only observed changed attributes, never ask the model to
            # decide which live values win over a user's explicit drift choice.
            updated = self.adoption.adopt_live(fresh['plan'])
            files = copy.deepcopy(self.current['files'])
            files.update(updated.get('files', updated))
            self._admit(files)
            self.current = {'revision': self.current['revision'] + 1, 'files': files,
                            'accepted_live_digest': live_digest(fresh)}
            self.store.save('workspace/current.json', self.current)
            conv['messages'].append({'role': 'assistant', 'content': 'Kept the live values in managed code. Send the requested change again to review its diff against that baseline.'})
            conv.update(stage='clarify', files=files, workspace_revision=self.current['revision'], plan=None, reviewed_revision=None)
            conv['resolved_drift_digest'] = live_digest(fresh)
            conv['pending_draft'] = False
            self._draft(conv)
        else:
            conv.update(resolved_drift_digest=conv['drift_digest'], stage='review', plan=None)
            conv['messages'].append({'role': 'assistant', 'content': 'The next Plan will restore the code values along with your reviewed change. Create a fresh Plan to inspect the result.'})

    def _apply(self, conv, body):
        # Repeat every check while holding the single workspace operation lock.
        conv['stage'] = 'planned'
        self._check_apply(conv, body)
        saved = conv['saved_plan']
        fresh = self._refresh(self.current['files'])
        if live_digest(fresh) != saved['live_digest'] or self._import_fingerprints(conv) != saved.get('import_fingerprints', {}):
            conv.pop('saved_plan', None); conv.pop('plan', None)
            conv.update(stage='drift', drift={'summary': fresh.get('summary', fresh.get('changes', []))},
                        drift_raw=fresh['plan'], drift_digest=live_digest(fresh))
            conv['messages'].append({'role': 'assistant', 'content': 'This Plan was invalidated by a live change. Resolve the drift and create a new Plan.'})
            return
        # A long refresh can cross the expiry deadline.
        self._check_apply(conv, body)
        raw = self.store.get('plans/' + saved['id'] + '/saved.tfplan')
        if hashlib.sha256(raw).hexdigest() != saved['plan_digest']:
            raise ValueError('The saved Plan changed. Create a new Plan.')
        self._write(conv['files'])
        path = self.runner.plans_dir / ('apply-' + saved['id'] + '.tfplan')
        path.write_bytes(raw)
        conv['stage'] = 'applying'
        with self.lock: self._save(conv)
        try:
            result = self.runner.apply(str(path))
        finally:
            # Even a partial provider failure can write a newer remote state.
            self.current = {'revision': self.current['revision'] + 1, 'files': copy.deepcopy(conv['files'])}
            self.store.save('workspace/current.json', self.current)
        conv.update(stage='result', result={'plan_id': saved['id'], 'message': 'Saved Plan applied. Terraform state is retained in the private versioned bucket.', 'details': result}, plan=None)
        conv.pop('saved_plan', None)
        conv['messages'].append({'role': 'assistant', 'content': conv['result']['message']})

    def _import_fingerprints(self, conv):
        return {item['address']: digest(self.tools.execute('get_resource',
                    {'resource_type': item['resource_type'], 'resource_id': item['resource_id']}))
                for item in conv.get('adoptions', [])}

    def _plan_scope(self, plan):
        """Every changed compartment-bearing resource must remain in fixed scope."""
        for item in plan.get('resource_changes', []):
            if item.get('mode') == 'data': continue
            if not str(item.get('type', '')).startswith('oci_'):
                raise ValueError('This Plan contains a non-OCI managed resource.')
            change = item.get('change', {})
            for side in ('before', 'after'):
                values = change.get(side)
                if values and 'compartment_id' in values and values['compartment_id'] != self.config['compartment_id']:
                    raise ValueError('The Plan would affect a resource outside the configured managed compartment.')
            if change.get('after_unknown', {}).get('compartment_id'):
                raise ValueError('The Plan must prove the managed compartment before Apply.')
