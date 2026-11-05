"""Load customer-supplied configuration without embedding deployment identities."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError('The application configuration must be a JSON object.')
    config = dict(value)
    for name in ('region', 'compartment_id', 'namespace', 'bucket'):
        if not isinstance(config.get(name), str) or not config[name].strip():
            raise ValueError('Configure a non-empty ' + name + '.')
        config[name] = config[name].strip()
    if not re.fullmatch(r'[a-z][a-z0-9-]*-[0-9]+', config['region']):
        raise ValueError('Configure an OCI infrastructure region identifier.')
    if not re.fullmatch(r'ocid1\.compartment\.[a-z0-9]+\.\.[a-z0-9]+', config['compartment_id']):
        raise ValueError('Configure the full managed compartment OCID.')
    config.setdefault('scope_name', 'Managed compartment')
    if not isinstance(config['scope_name'], str) or not config['scope_name'].strip():
        raise ValueError('scope_name must be a non-empty display name.')
    config['scope_name'] = config['scope_name'].strip()
    config.setdefault('auth', 'ResourcePrincipal')
    if config['auth'] not in ('ResourcePrincipal', 'APIKey'):
        raise ValueError('auth must be ResourcePrincipal or APIKey.')
    if config['auth'] == 'APIKey' and (not isinstance(config.get('config_file'), str) or not config['config_file'].strip()):
        raise ValueError('APIKey authentication requires a local config_file path.')
    if config['auth'] == 'APIKey':
        config['config_file'] = str(Path(config['config_file']).expanduser().resolve())
    config.setdefault('model_region', config['region'])
    if not isinstance(config['model_region'], str) or not re.fullmatch(r'[a-z][a-z0-9-]*-[0-9]+', config['model_region']):
        raise ValueError('Configure an OCI model_region identifier.')
    config.setdefault('model_compartment_id', config['compartment_id'])
    if not isinstance(config['model_compartment_id'], str) or not re.fullmatch(r'ocid1\.compartment\.[a-z0-9]+\.\.[a-z0-9]+', config['model_compartment_id']):
        raise ValueError('Configure the full model_compartment_id OCID.')
    config.setdefault('model_id', 'openai.gpt-oss-120b')
    if not isinstance(config['model_id'], str) or not config['model_id'].strip():
        raise ValueError('model_id must be a non-empty on-demand model identifier.')
    if 'routing_profile_id' in config and not isinstance(config['routing_profile_id'], str):
        raise ValueError('routing_profile_id must be a string when supplied.')
    endpoint = config.get('model_endpoint_id')
    if endpoint is not None and endpoint != '':
        # Dedicated AI cluster endpoint: inference stays on capacity reserved for this tenancy.
        if not isinstance(endpoint, str) or not re.fullmatch(r'ocid1\.generativeaiendpoint\.[a-z0-9]+\.[a-z0-9-]*\.[a-z0-9]+', endpoint):
            raise ValueError('model_endpoint_id must be a dedicated AI cluster endpoint OCID.')
        if config.get('routing_profile_id'):
            raise ValueError('Use either model_endpoint_id (dedicated) or routing_profile_id (on-demand), not both.')
    if 'local_demo' in config and not isinstance(config['local_demo'], bool):
        raise ValueError('local_demo must be true or false.')
    config.setdefault('local_demo', False)
    if config['local_demo']:
        # This option bypasses Gateway sign-in only for a loopback HTTP peer.
        # Keep it on IPv4 loopback, matching ThreadingHTTPServer's socket family.
        if config.get('bind', '127.0.0.1') not in ('127.0.0.1', 'localhost'):
            raise ValueError('local_demo must bind to 127.0.0.1.')
        config['bind'] = '127.0.0.1'
    else:
        host = config.get('public_host')
        if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host):
            raise ValueError('Configure public_host as the authenticated Gateway hostname, without a scheme, path or port.')
        config['public_host'] = host.lower()
        subject = config.get('admin_subject_sha256')
        if not isinstance(subject, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', subject):
            raise ValueError('Configure admin_subject_sha256 for the intended signed-in administrator.')
        config['admin_subject_sha256'] = subject.lower()
        config.setdefault('bind', '0.0.0.0')
    config.setdefault('workdir', 'local-data/workspace' if config['local_demo'] else '/data/workspace')
    if not isinstance(config['workdir'], str) or not config['workdir'].strip():
        raise ValueError('workdir must be a local directory path.')
    config['workdir'] = str(Path(config['workdir']).expanduser().resolve())
    return config


def load_config(environ=None):
    env = os.environ if environ is None else environ
    filename = env.get('SINGLE_CHAT_CONFIG_FILE')
    if filename:
        raw = Path(filename).read_text()
    else:
        raw = env.get('SINGLE_CHAT_CONFIG_JSON')
        if not raw:
            raise ValueError('Set SINGLE_CHAT_CONFIG_FILE or SINGLE_CHAT_CONFIG_JSON.')
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError('The application configuration is not valid JSON.') from exc
    return validate_config(value)
