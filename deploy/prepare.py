"""Render review-only OCI CLI payloads. This command never calls OCI."""
from __future__ import annotations
import argparse
import base64
import copy
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import validate_config

IMAGE = 'docker.io/library/python@sha256:061c190f3d5815b341a18dfe8a45771f8ba68c15a607ea56997fde3009ac4eec'


def _ocid(value, kind):
    if not isinstance(value, str) or not re.fullmatch(r'ocid1\.' + re.escape(kind) + r'\.[a-z0-9]+\.[a-z0-9-]*\.[a-z0-9]+', value):
        raise ValueError(f'Replace the {kind} OCID placeholder with the intended resource OCID.')
    return value


def _name(value, description):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_. -]{0,99}', value):
        raise ValueError(f'{description} must contain letters, digits, spaces, periods, underscores or hyphens.')
    return value


def gateway_routes(backend_ip):
    if backend_ip is not None:
        address = ipaddress.ip_address(backend_ip)
        if address.version != 4 or not address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError('Use the container private IPv4 address as --backend-ip.')
        backend_ip = str(address)
    else:
        backend_ip = 'REPLACE_WITH_CONTAINER_PRIVATE_IP'
    subject = {'authorization': {'type': 'AUTHENTICATION_ONLY'}, 'headerTransformations': {
        'setHeaders': {'items': [{'name': 'X-Codegen-Subject',
            'values': ['${request.auth[id_token_claims][sub]}'], 'ifExists': 'OVERWRITE'}]}}}
    definitions = [('/', ['GET']), ('/app.js', ['GET']), ('/style.css', ['GET']),
        ('/api/config', ['GET']), ('/api/conversations', ['GET', 'POST']),
        ('/api/conversations/{conversationId}', ['GET'])]
    definitions += [('/api/conversations/{conversationId}/' + action, ['POST'])
                    for action in ('messages', 'review', 'plan', 'apply', 'drift')]
    routes = []
    for path, methods in definitions:
        route = {'path': path, 'methods': methods, 'requestPolicies': copy.deepcopy(subject),
            'backend': {'type': 'HTTP_BACKEND', 'url': 'http://' + backend_ip + ':8080' +
                path.replace('{conversationId}', '${request.path[conversationId]}'),
                'connectTimeoutInSeconds': 10, 'readTimeoutInSeconds': 30, 'sendTimeoutInSeconds': 30}}
        if path == '/api/config':
            route['responsePolicies'] = {'headerTransformations': {'setHeaders': {'items': [
                {'name': 'X-Codegen-Csrf-Token', 'values': ['${request.auth[apigw_csrf_token]}'], 'ifExists': 'OVERWRITE'}]}}}
        routes.append(route)
    return routes


def prepare(runtime, deployment, *, inline_archive=None, backend_ip=None):
    config = validate_config(runtime)
    if config.get('local_demo') or config['auth'] != 'ResourcePrincipal':
        raise ValueError('OCI deployment requires auth=ResourcePrincipal and local_demo=false.')
    compartment = _ocid(config['compartment_id'], 'compartment')
    model_compartment = _ocid(config.get('model_compartment_id', compartment), 'compartment')
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,255}', config['bucket']) or 'REPLACE_' in config['bucket']:
        raise ValueError('Replace the bucket placeholder with your private bucket name.')
    if not isinstance(config['namespace'], str) or 'REPLACE_' in config['namespace']:
        raise ValueError('Replace the Object Storage namespace placeholder.')
    availability_domain = deployment.get('availability_domain', '')
    if not isinstance(availability_domain, str) or not availability_domain or 'REPLACE_' in availability_domain:
        raise ValueError('Set the availability_domain for the runtime region.')
    subnet = _ocid(deployment.get('subnet_id'), 'subnet')
    nsgs = deployment.get('nsg_ids')
    if not isinstance(nsgs, list) or not nsgs:
        raise ValueError('Provide the application NSG OCID in nsg_ids.')
    nsgs = [_ocid(value, 'networksecuritygroup') for value in nsgs]
    domain = _name(deployment.get('identity_domain', 'Default'), 'Identity domain')
    group = _name(deployment.get('dynamic_group_name', 'TerraformChat'), 'Dynamic group')
    display_name = _name(deployment.get('container_name', 'oci-terraform-chat'), 'Container name')
    image = deployment.get('container_image', IMAGE)
    if not isinstance(image, str) or not image or any(x.isspace() for x in image):
        raise ValueError('container_image must be a container registry image reference.')
    mode = deployment.get('image_mode', 'inline')
    child = {'displayName': 'chat-and-terraform', 'imageUrl': image, 'isResourcePrincipalDisabled': False,
        'environmentVariables': {'SINGLE_CHAT_CONFIG_JSON': json.dumps(config, separators=(',', ':')), 'PORT': '8080'}}
    archive_sha = None
    if mode == 'inline':
        if inline_archive is None:
            raise ValueError('Inline mode requires --inline-archive; create it with deploy/package.py first.')
        archive = Path(inline_archive).read_bytes()
        archive_sha = hashlib.sha256(archive).hexdigest()
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as source:
                names = source.namelist()
                required = {'app/server.py', 'app/config.py', 'requirements.txt', 'LICENSE',
                            'deploy/terraform-release.json', 'scripts/install_terraform.py'}
                if not required.issubset(names) or len(names) != len(set(names)):
                    raise ValueError('Use the source archive produced by deploy/package.py.')
                for name in names:
                    allowed = name in required or name.startswith('app/') and Path(name).suffix in {'.py', '.html', '.css', '.js'}
                    if not allowed or '..' in Path(name).parts or name.startswith('/'):
                        raise ValueError('Inline archive contains an unexpected file. Rebuild it with deploy/package.py.')
        except zipfile.BadZipFile as exc:
            raise ValueError('Inline archive is not a source ZIP from deploy/package.py.') from exc
        encoded = base64.b64encode(archive).decode('ascii')
        if len(encoded) > 115000:
            raise ValueError('Inline archive exceeds the safe command-argument size. Use the optional prebuilt Docker image path.')
        child.update(command=['python3', '-c'], arguments=[(ROOT / 'deploy/bootstrap.py').read_text(), encoded])
        child['environmentVariables']['APP_SHA256'] = archive_sha
    elif mode == 'prebuilt':
        if inline_archive is not None:
            raise ValueError('Do not supply --inline-archive in prebuilt image mode.')
        if image == IMAGE:
            raise ValueError('Prebuilt mode requires container_image to name your built chat image, not the base Python image.')
    else:
        raise ValueError('image_mode must be inline or prebuilt.')
    container = {'displayName': display_name, 'compartmentId': compartment,
        'availabilityDomain': availability_domain, 'shape': 'CI.Standard.E4.Flex',
        'shapeConfig': {'ocpus': 1, 'memoryInGBs': 4}, 'containerRestartPolicy': 'ALWAYS',
        'gracefulShutdownTimeoutInSeconds': 60, 'containers': [child],
        'vnics': [{'subnetId': subnet, 'nsgIds': nsgs, 'isPublicIpAssigned': False}]}
    bucket = {'namespaceName': config['namespace'], 'compartmentId': compartment, 'name': config['bucket'],
        'publicAccessType': 'NoPublicAccess', 'versioning': 'Enabled', 'storageTier': 'Standard'}
    rule = f"ALL {{resource.type = 'computecontainerinstance', resource.compartment.id = '{compartment}'}}"
    statements = [f"Allow dynamic-group '{domain}'/'{group}' to manage all-resources in compartment id {compartment}",
        f"Allow dynamic-group '{domain}'/'{group}' to use generative-ai-family in compartment id {model_compartment}"]
    return {'bucket': bucket, 'container': container, 'matchingRule': rule, 'policyStatements': statements,
        'gatewayRoutes': gateway_routes(backend_ip), 'summary': {'mode': 'REVIEW_ONLY', 'cloudChangesMade': False,
            'region': config['region'], 'modelRegion': config.get('model_region', config['region']),
            'managedCompartmentId': compartment, 'stateBucket': config['bucket'], 'stateKey': config.get('state_key', 'terraform/managed.tfstate'),
            'imageMode': mode, 'archiveSha256': archive_sha, 'containerCount': 1, 'policyStatementCount': 2,
            'gatewayApplicationRouteCount': 11, 'requiresExistingAuthenticatedGateway': True,
            'gatewayBackendAddressKnown': backend_ip is not None}}


def write_outputs(result, output, *, backend_ip=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o700)
    route_file = 'gateway-http-routes.json' if backend_ip else 'gateway-http-routes.template.json'
    files = {'bucket.json': json.dumps(result['bucket'], indent=2),
        'container-instance.json': json.dumps(result['container'], indent=2),
        'dynamic-group-rule.txt': result['matchingRule'], 'IAM-policy.txt': '\n'.join(result['policyStatements']),
        route_file: json.dumps(result['gatewayRoutes'], indent=2),
        'summary.json': json.dumps(result['summary'], indent=2),
        'gateway-merge-notes.txt': 'ROUTES ONLY: merge these application routes into an already authenticated Gateway deployment.\n'
            'Preserve the global OAuth configuration, dedicated browser app/client binding, and native OAuth callback route.\n'
            'Do not submit this route array as a complete deployment specification.\n'
            'The container must remain private and accept TCP8080 only from the Gateway network.\n'}
    for name, content in files.items():
        path = output / name
        path.write_text(content + '\n')
        path.chmod(0o600)
    return {'outputDirectory': str(output), 'files': list(files), **result['summary']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-config', type=Path, required=True)
    parser.add_argument('--deployment-config', type=Path, required=True)
    parser.add_argument('--inline-archive', type=Path)
    parser.add_argument('--backend-ip')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'build/deploy')
    args = parser.parse_args()
    try:
        result = prepare(json.loads(args.runtime_config.read_text()), json.loads(args.deployment_config.read_text()),
            inline_archive=args.inline_archive, backend_ip=args.backend_ip)
        print(json.dumps(write_outputs(result, args.output_dir, backend_ip=args.backend_ip)))
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
