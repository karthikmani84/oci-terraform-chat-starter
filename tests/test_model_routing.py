"""Direct model access must not move Terraform/discovery/state to its region."""
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import oci
from oci.generative_ai_inference import models as m

from app.cloud import clients
from app.model import Model


class ModelRegionTests(unittest.TestCase):
    def client_configs(self, config):
        factories = [
            (oci.object_storage, 'ObjectStorageClient'),
            (oci.core, 'VirtualNetworkClient'),
            (oci.network_load_balancer, 'NetworkLoadBalancerClient'),
            (oci.core, 'ComputeClient'),
            (oci.generative_ai_inference, 'GenerativeAiInferenceClient'),
        ]
        patches = [patch.object(module, name, side_effect=lambda sdk, **kw:
                                 NS(config=dict(sdk), kwargs=dict(kw)))
                   for module, name in factories]
        for item in patches: item.start()
        try:
            return clients(config)
        finally:
            for item in reversed(patches): item.stop()

    def test_resource_principal_model_region_does_not_change_other_clients(self):
        signer = object()
        with patch.object(oci.auth.signers, 'get_resource_principals_signer', return_value=signer):
            result = self.client_configs({'region': 'me-dubai-1', 'model_region': 'eu-frankfurt-1'})
        self.assertEqual(result['model'].config['region'], 'eu-frankfurt-1')
        for name in ('storage', 'network', 'nlb', 'compute'):
            self.assertEqual(result[name].config['region'], 'me-dubai-1')
        self.assertTrue(all(x.kwargs['signer'] is signer for x in result.values()))

    def test_config_file_model_region_keeps_credentials_and_infrastructure_region(self):
        native = {'region': 'us-chicago-1', 'tenancy': 'fixture-tenancy', 'user': 'fixture-user'}
        with patch.object(oci.config, 'from_file', return_value=dict(native)):
            result = self.client_configs({'auth': 'APIKey', 'config_file': 'fixture-config',
                                          'region': 'me-dubai-1', 'model_region': 'eu-frankfurt-1'})
        self.assertEqual(result['model'].config['region'], 'eu-frankfurt-1')
        for name in ('storage', 'network', 'nlb', 'compute'):
            self.assertEqual(result[name].config['region'], 'me-dubai-1')
        self.assertTrue(all(x.config['user'] == 'fixture-user' for x in result.values()))

    def test_legacy_configuration_keeps_all_clients_in_infrastructure_region(self):
        with patch.object(oci.auth.signers, 'get_resource_principals_signer', return_value=object()):
            result = self.client_configs({'region': 'me-dubai-1'})
        self.assertTrue(all(x.config['region'] == 'me-dubai-1' for x in result.values()))


class ServingTargetTests(unittest.TestCase):
    def test_direct_model_and_legacy_routing_profile_targets(self):
        for settings, expected in [
            ({'routing_profile_id': 'ocid1.generativeairoutingprofile.fixture',
              'model_id': 'openai.gpt-oss-120b'}, 'ocid1.generativeairoutingprofile.fixture'),
            ({'model_id': 'openai.gpt-oss-120b'}, 'openai.gpt-oss-120b'),
            ({'routing_profile_id': '', 'model_id': 'fixture-direct-model'}, 'fixture-direct-model'),
            ({}, 'openai.gpt-oss-120b'),
        ]:
            with self.subTest(settings=settings):
                captured = []
                class Client:
                    def chat(self, details):
                        captured.append(oci.util.to_dict(details))
                        message = m.AssistantMessage(content=[m.TextContent(text=json.dumps(
                            {'kind': 'clarify', 'message': 'Which VCN name would you like?'}))])
                        return NS(data=m.ChatResult(chat_response=m.GenericChatResponse(choices=[
                            m.ChatChoice(message=message, finish_reason='STOP')])))
                config = {'compartment_id': 'managed-compartment', 'region': 'me-dubai-1',
                          'scope_name': 'Example / managed',
                          'model_compartment_id': 'model-compartment',
                          'model_region': 'eu-frankfurt-1', **settings}
                result = Model(Client(), config, NS()).respond({'messages': [
                    {'role': 'user', 'content': 'Create a VCN'}]})
                self.assertEqual(result['kind'], 'clarify')
                self.assertEqual(captured[0]['serving_mode']['model_id'], expected)
                self.assertEqual(captured[0]['serving_mode']['serving_type'], 'ON_DEMAND')
                self.assertEqual(captured[0]['compartment_id'], 'model-compartment')
                system = captured[0]['chat_request']['messages'][0]['content'][0]['text']
                self.assertIn('"region": "me-dubai-1"', system)
                self.assertIn('"name": "Example / managed"', system)
                self.assertNotIn('eu-frankfurt-1', system)


if __name__ == '__main__': unittest.main()


class DedicatedEndpointTests(unittest.TestCase):
    ENDPOINT = 'ocid1.generativeaiendpoint.oc1.me-dubai-1.dedicatedfixture'

    def test_dedicated_endpoint_uses_dedicated_serving_mode(self):
        from app.model import serving_mode
        mode = serving_mode({'model_endpoint_id': self.ENDPOINT, 'model_id': 'openai.gpt-oss-120b'})
        self.assertIsInstance(mode, m.DedicatedServingMode)
        self.assertEqual(mode.endpoint_id, self.ENDPOINT)
        self.assertIsInstance(serving_mode({'model_id': 'openai.gpt-oss-120b'}), m.OnDemandServingMode)

    def test_config_accepts_endpoint_and_rejects_conflicts(self):
        from app.config import validate_config
        base = {'region': 'me-dubai-1', 'compartment_id': 'ocid1.compartment.oc1..examplemanaged',
                'namespace': 'ns', 'bucket': 'b', 'local_demo': True}
        self.assertEqual(validate_config({**base, 'model_endpoint_id': self.ENDPOINT})['model_endpoint_id'], self.ENDPOINT)
        with self.assertRaises(ValueError):
            validate_config({**base, 'model_endpoint_id': 'openai.gpt-oss-120b'})
        with self.assertRaises(ValueError):
            validate_config({**base, 'model_endpoint_id': self.ENDPOINT,
                             'routing_profile_id': 'ocid1.generativeairoutingprofile.fixture'})
