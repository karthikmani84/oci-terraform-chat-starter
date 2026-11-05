"""Read-only model tools; credentials and raw state never enter tool results."""
from __future__ import annotations
import copy
import json
import re


SAFE = {'id', 'display_name', 'name', 'compartment_id', 'lifecycle_state', 'time_created',
        'vcn_id', 'subnet_id', 'cidr_block', 'cidr_blocks', 'dns_label',
        'is_private', 'is_preserve_source_destination', 'is_symmetric_hash_enabled',
        'is_public_ip_assigned', 'prohibit_public_ip_on_vnic', 'shape',
        'availability_domain', 'network_security_group_ids', 'route_table_id',
        'security_list_ids', 'ip_version', 'ipv6cidr_block'}
REGISTRY = {
    'oci_core_vcn': ('network', 'list_vcns', 'get_vcn'),
    'oci_core_subnet': ('network', 'list_subnets', 'get_subnet'),
    'oci_core_route_table': ('network', 'list_route_tables', 'get_route_table'),
    'oci_core_security_list': ('network', 'list_security_lists', 'get_security_list'),
    'oci_core_network_security_group': ('network', 'list_network_security_groups', 'get_network_security_group'),
    'oci_network_load_balancer_network_load_balancer': ('nlb', 'list_network_load_balancers', 'get_network_load_balancer'),
    'oci_core_instance': ('compute', 'list_instances', 'get_instance'),
}


ALIASES = {
    'vcn': 'oci_core_vcn',
    'subnet': 'oci_core_subnet',
    'nlb': 'oci_network_load_balancer_network_load_balancer',
    'network_load_balancer': 'oci_network_load_balancer_network_load_balancer',
}
TOOLS = {'list_resources', 'get_resource', 'get_terraform_block'}


def canonical_type(value):
    if not isinstance(value, str):
        return ''
    value = value.strip().lower().replace('-', '_').replace(' ', '_')
    return ALIASES.get(value, value)


def error_metadata(exc):
    """Fixed/bounded service failure evidence, without raw exception payloads."""
    code = getattr(exc, 'code', None)
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', code):
        code = 'TOOL_ARGUMENT_INVALID' if isinstance(exc, ValueError) else 'TOOL_READ_FAILED'
    result = {'error_code': code}
    status = getattr(exc, 'status', None)
    if isinstance(status, int) and 100 <= status <= 599:
        result['http_status'] = status
    request_id = getattr(exc, 'request_id', None)
    if isinstance(request_id, str) and re.fullmatch(r'[A-Za-z0-9:/_.-]{1,512}', request_id):
        result['opc_request_id'] = request_id
    return result


class ReadTools:
    def __init__(self, clients, compartment_id, files, schema=None):
        self.clients, self.compartment_id = clients, compartment_id
        self.files, self.schema = files, schema or {}

    def _item(self, value):
        import oci
        data = oci.util.to_dict(value)
        if data.get('compartment_id') != self.compartment_id:
            raise ValueError('Resource is outside the configured managed compartment.')
        return {k: v for k, v in data.items() if k in SAFE}

    def execute(self, name, args):
        resource_type = canonical_type(args.get('resource_type', '')) if isinstance(args, dict) else ''
        trace = {'event': 'read_only_tool', 'tool': name if name in TOOLS else 'UNKNOWN',
                 'resource_type': resource_type if resource_type in REGISTRY else 'UNSPECIFIED',
                 'count': 0, 'ok': False}
        try:
            result = self._execute(name, args, resource_type, trace)
            trace['ok'] = not bool(result.get('error'))
            if 'resources' in result: trace['count'] = len(result['resources'])
            elif 'managed_blocks' in result: trace['count'] = len(result['managed_blocks'])
            elif result.get('id') or result.get('managed'): trace['count'] = 1
            if result.get('error'): trace['error_code'] = result.get('error_code', 'TOOL_READ_UNAVAILABLE')
            return result
        except Exception as exc:
            trace.update(error_metadata(exc))
            raise
        finally:
            # Only operation/type/count/outcome and a native request ID. Never
            # resource IDs/names, arguments, values, model reasoning or errors.
            print(json.dumps(trace, sort_keys=True), flush=True)

    @staticmethod
    def _response_trace(response, trace):
        request_id = getattr(response, 'headers', {}).get('opc-request-id')
        if isinstance(request_id, str) and re.fullmatch(r'[A-Za-z0-9:/_.-]{1,512}', request_id):
            trace['opc_request_id'] = request_id

    def _execute(self, name, args, resource_type, trace):
        if name not in TOOLS:
            raise ValueError('This model tool is not available.')
        if not isinstance(args, dict) or set(args) - {'resource_type', 'resource_id', 'address'}:
            raise ValueError('Unexpected tool argument.')
        if name == 'get_terraform_block':
            address = args.get('address', '')
            resources = {}
            for filename, source in self.files().items():
                if filename.endswith('.tf.json'):
                    for typ, blocks in json.loads(source).get('resource', {}).items():
                        for label, attrs in blocks.items(): resources[typ + '.' + label] = attrs
            if address:
                return {'address': address, 'values': copy.deepcopy(resources.get(address)),
                        'managed': address in resources}
            managed = [{'address': key, 'values': copy.deepcopy(value)}
                       for key, value in sorted(resources.items())
                       if not resource_type or key.startswith(resource_type + '.')]
            if not resource_type:
                return {'managed_blocks': managed}
            if resource_type not in self.schema:
                return {'resource_type': resource_type, 'available_types': sorted(REGISTRY),
                        'managed_blocks': managed, 'error': 'Unknown installed provider resource type.',
                        'error_code': 'PROVIDER_RESOURCE_UNAVAILABLE'}
            block = self.schema[resource_type].get('block', {})
            attrs = {k: {f: v[f] for f in ('type', 'required', 'optional', 'computed', 'description') if f in v}
                     for k, v in block.get('attributes', {}).items() if not v.get('sensitive')}
            return {'resource_type': resource_type, 'attributes': attrs,
                    'nested_blocks': block.get('block_types', {}), 'managed_blocks': managed,
                    'provider_version': '9.11.0'}
        if resource_type not in REGISTRY:
            return {'error': 'Discovery for this service is not installed.', 'error_code': 'DISCOVERY_UNAVAILABLE',
                    'available_types': sorted(REGISTRY)}
        client_name, list_name, get_name = REGISTRY[resource_type]
        client = self.clients[client_name]
        if name == 'get_resource':
            resource_id = args.get('resource_id', '')
            if not isinstance(resource_id, str) or not resource_id.startswith('ocid1.'):
                raise ValueError('Select a resource from list_resources before requesting its configuration.')
            response = getattr(client, get_name)(resource_id)
            self._response_trace(response, trace)
            return self._item(response.data)
        response, items = None, []
        while True:
            kw = {'compartment_id': self.compartment_id, 'limit': 100}
            if response and response.headers.get('opc-next-page'): kw['page'] = response.headers['opc-next-page']
            response = getattr(client, list_name)(**kw)
            self._response_trace(response, trace)
            data = response.data
            for item in getattr(data, 'items', data):
                if getattr(item, 'lifecycle_state', '') not in ('DELETED', 'TERMINATED'):
                    items.append(self._item(item))
            if not response.headers.get('opc-next-page') or len(items) >= 500: break
        return {'resources': items, 'compartment_id': self.compartment_id}


def clients(config):
    import oci
    if config.get('auth', 'ResourcePrincipal') == 'ResourcePrincipal':
        signer = oci.auth.signers.get_resource_principals_signer()
        sdk = {'region': config['region']}
        kw = {'signer': signer}
    else:
        sdk = oci.config.from_file(config['config_file'], config.get('profile', 'DEFAULT'))
        sdk['region'] = config['region']
        kw = {}
    # Model processing may use a different region without moving managed
    # infrastructure, Terraform state, or discovery out of its configured scope.
    model_sdk = dict(sdk)
    model_sdk['region'] = config.get('model_region') or config['region']
    return {
        'storage': oci.object_storage.ObjectStorageClient(sdk, **kw),
        'network': oci.core.VirtualNetworkClient(sdk, **kw),
        'nlb': oci.network_load_balancer.NetworkLoadBalancerClient(sdk, **kw),
        'compute': oci.core.ComputeClient(sdk, **kw),
        'model': oci.generative_ai_inference.GenerativeAiInferenceClient(model_sdk, **kw),
    }
