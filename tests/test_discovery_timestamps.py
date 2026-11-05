"""Creation timestamps let the model resolve 'newest' using OCI metadata."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace as NS
import unittest

import oci
from app.cloud import ReadTools

C = 'ocid1.compartment.oc1..managedmanaged'


class DiscoveryTimestampTests(unittest.TestCase):
    def test_list_and_get_preserve_native_created_times_without_extra_content(self):
        older = oci.network_load_balancer.models.NetworkLoadBalancerSummary(
            id='ocid1.networkloadbalancer.oc1.me-dubai-1.older', compartment_id=C,
            display_name='chat-managed-nlb', lifecycle_state='ACTIVE',
            time_created=datetime(2026, 11, 3, 12, tzinfo=timezone.utc))
        newer = oci.network_load_balancer.models.NetworkLoadBalancerSummary(
            id='ocid1.networkloadbalancer.oc1.me-dubai-1.newer', compartment_id=C,
            display_name='spoke-nlb', lifecycle_state='ACTIVE',
            time_created=datetime(2026, 11, 4, 12, tzinfo=timezone.utc),
            freeform_tags={'private': 'exclude-tag-content'})
        class Client:
            def list_network_load_balancers(self, **kwargs):
                if kwargs['compartment_id'] != C: raise AssertionError('Scope changed')
                return NS(data=NS(items=[older, newer]), headers={})
            def get_network_load_balancer(self, resource_id):
                if resource_id != newer.id: raise AssertionError('Wrong resource')
                return NS(data=newer, headers={})
        tools = ReadTools({'nlb': Client()}, C, lambda: {})
        with redirect_stdout(StringIO()):
            result = tools.execute('list_resources', {'resource_type': 'nlb'})
            selected = tools.execute('get_resource', {'resource_type': 'nlb', 'resource_id': newer.id})
        values = result['resources']
        self.assertLess(values[0]['time_created'], values[1]['time_created'])
        self.assertEqual(selected['time_created'], values[1]['time_created'])
        self.assertEqual(selected['display_name'], 'spoke-nlb')
        self.assertNotIn('freeform_tags', selected)
        self.assertNotIn('exclude-tag-content', str(result))


if __name__ == '__main__': unittest.main()
