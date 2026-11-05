"""A recipient supplies their own region, scope, state and identity settings."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.config import load_config, validate_config
from app.server import build, handler, main


BASE = {'region': 'us-ashburn-1',
        'compartment_id': 'ocid1.compartment.oc1..examplemanaged',
        'namespace': 'example_namespace', 'bucket': 'example-chat-state',
        'public_host': 'chat.example.com', 'admin_subject_sha256': 'a' * 64}


class ConfigurationTests(unittest.TestCase):
    def test_custom_region_scope_and_model_settings_are_not_tied_to_original_demo(self):
        source = {**BASE, 'scope_name': 'Example / managed', 'model_region': 'us-chicago-1'}
        before = deepcopy(source)
        actual = validate_config(source)
        self.assertEqual(source, before)
        self.assertEqual(actual['region'], 'us-ashburn-1')
        self.assertEqual(actual['model_region'], 'us-chicago-1')
        self.assertEqual(actual['scope_name'], 'Example / managed')
        self.assertEqual(actual['model_compartment_id'], BASE['compartment_id'])
        self.assertEqual(actual['model_id'], 'openai.gpt-oss-120b')
        self.assertEqual(actual['bind'], '0.0.0.0')
        self.assertEqual(actual['auth'], 'ResourcePrincipal')

    def test_required_scope_and_deployed_identity_are_validated_before_cloud_clients(self):
        for key in ('region', 'compartment_id', 'namespace', 'bucket', 'public_host', 'admin_subject_sha256'):
            source = dict(BASE); source.pop(key)
            with self.subTest(missing=key), patch('app.server.clients') as cloud:
                with self.assertRaises(ValueError): build(source)
                cloud.assert_not_called()
        for fields in ({'public_host': 'https://chat.example.com/'},
                       {'public_host': 'chat.example.com:8443'},
                       {'admin_subject_sha256': 'not-a-sha'},
                       {'model_compartment_id': 'not-an-ocid'},
                       {'region': 'https://foreign.example'},
                       {'model_region': {}}, {'routing_profile_id': {}},
                       {'local_demo': 'false'}):
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError): validate_config({**BASE, **fields})

    def test_local_mode_defaults_to_loopback_and_rejects_a_public_bind(self):
        source = {key: BASE[key] for key in ('region', 'compartment_id', 'namespace', 'bucket')}
        source.update(local_demo=True, auth='APIKey', config_file='~/.oci/config')
        actual = validate_config(source)
        self.assertEqual(actual['bind'], '127.0.0.1')
        self.assertTrue(Path(actual['config_file']).is_absolute())
        self.assertTrue(actual['workdir'].endswith('local-data/workspace'))
        self.assertEqual(validate_config({**source, 'bind': 'localhost'})['bind'], '127.0.0.1')
        for bind in ('0.0.0.0', '::', '::1', '192.0.2.10'):
            with self.subTest(bind=bind):
                with self.assertRaisesRegex(ValueError, '127.0.0.1'):
                    validate_config({**source, 'bind': bind})
        with self.assertRaisesRegex(ValueError, 'config_file'):
            validate_config({**source, 'config_file': ''})

    def test_config_file_takes_precedence_and_json_env_is_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'; path.write_text(json.dumps(BASE))
            loaded = load_config({'SINGLE_CHAT_CONFIG_FILE': str(path), 'SINGLE_CHAT_CONFIG_JSON': 'invalid'})
        self.assertEqual(loaded['region'], BASE['region'])
        self.assertEqual(load_config({'SINGLE_CHAT_CONFIG_JSON': json.dumps(BASE)})['region'], BASE['region'])
        with self.assertRaisesRegex(ValueError, 'Set SINGLE_CHAT_CONFIG'):
            load_config({})
        with self.assertRaisesRegex(ValueError, 'valid JSON'):
            load_config({'SINGLE_CHAT_CONFIG_JSON': 'not-json'})
        with self.assertRaisesRegex(ValueError, 'JSON object'):
            load_config({'SINGLE_CHAT_CONFIG_JSON': '[]'})

    def test_local_header_does_not_allow_a_remote_peer_to_use_demo_bypass(self):
        klass = handler(None, {'local_demo': True})
        request = object.__new__(klass)
        request.client_address = ('192.0.2.10', 12345)
        request.headers = {'X-Codegen-Subject': 'forged-subject'}
        with self.assertRaisesRegex(PermissionError, 'loopback'): request._user()

    def test_local_process_starts_only_on_the_validated_loopback_bind(self):
        source = {key: BASE[key] for key in ('region', 'compartment_id', 'namespace', 'bucket')}
        source.update(local_demo=True, auth='APIKey', config_file='~/.oci/config')
        with patch.dict('os.environ', {'SINGLE_CHAT_CONFIG_JSON': json.dumps(source), 'PORT': '8765'}, clear=True), \
             patch('app.server.build') as construct, patch('app.server.ThreadingHTTPServer') as server:
            main()
        self.assertEqual(construct.call_args.args[0]['bind'], '127.0.0.1')
        self.assertEqual(server.call_args.args[0], ('127.0.0.1', 8765))
        server.return_value.serve_forever.assert_called_once()


if __name__ == '__main__': unittest.main()
