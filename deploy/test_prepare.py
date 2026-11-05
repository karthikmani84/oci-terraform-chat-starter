"""Offline deployment contracts: fixed scope, private state, one runtime and auth routes."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare_module = load('starter_deployment_prepare', ROOT / 'deploy/prepare.py')
package_module = load('starter_deployment_package', ROOT / 'deploy/package.py')
installer = load('starter_terraform_installer', ROOT / 'scripts/install_terraform.py')


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.runtime = {'region': 'me-dubai-1', 'compartment_id': 'ocid1.compartment.oc1..manageddemo',
            'scope_name': 'demo/managed', 'namespace': 'examplenamespace', 'bucket': 'example-chat-state',
            'auth': 'ResourcePrincipal', 'model_region': 'eu-frankfurt-1',
            'model_compartment_id': 'ocid1.compartment.oc1..modeldemo',
            'public_host': 'chat.example.com', 'admin_subject_sha256': 'a' * 64}
        self.deployment = {'availability_domain': 'example:REGION-AD-1',
            'subnet_id': 'ocid1.subnet.oc1.example.subnetdemo',
            'nsg_ids': ['ocid1.networksecuritygroup.oc1.example.nsgdemo'],
            'identity_domain': 'Default', 'dynamic_group_name': 'TerraformChat'}
        raw, self.manifest = package_module.package(ROOT)
        self.archive = self.folder / 'source.zip'
        self.archive.write_bytes(raw)

    def tearDown(self):
        self.temp.cleanup()

    def render(self, **kwargs):
        return prepare_module.prepare(self.runtime, self.deployment, inline_archive=self.archive, **kwargs)

    def test_scope_private_state_and_one_runtime_are_bound_to_same_config(self):
        result = self.render()
        self.assertEqual(result['bucket']['compartmentId'], self.runtime['compartment_id'])
        self.assertEqual(result['bucket']['namespaceName'], self.runtime['namespace'])
        self.assertEqual(result['bucket']['publicAccessType'], 'NoPublicAccess')
        self.assertEqual(result['bucket']['versioning'], 'Enabled')
        instance = result['container']
        self.assertEqual(instance['compartmentId'], self.runtime['compartment_id'])
        self.assertEqual(len(instance['containers']), 1)
        self.assertFalse(instance['vnics'][0]['isPublicIpAssigned'])
        config = json.loads(instance['containers'][0]['environmentVariables']['SINGLE_CHAT_CONFIG_JSON'])
        self.assertEqual(config['region'], 'me-dubai-1')
        self.assertEqual(config['model_region'], 'eu-frankfurt-1')
        self.assertFalse(config.get('routing_profile_id'))
        self.assertEqual(instance['containers'][0]['environmentVariables']['APP_SHA256'], self.manifest['sha256'])
        self.assertEqual(len(result['policyStatements']), 2)
        self.assertIn(self.runtime['compartment_id'], result['matchingRule'])
        self.assertIn('manage all-resources in compartment id ' + self.runtime['compartment_id'], result['policyStatements'][0])
        self.assertIn('use generative-ai-family in compartment id ' + self.runtime['model_compartment_id'], result['policyStatements'][1])
        self.assertFalse(result['summary']['cloudChangesMade'])

    def test_routes_require_auth_overwrite_subject_and_preserve_dual_csrf_contract(self):
        result = self.render(backend_ip='10.20.1.10')
        routes = result['gatewayRoutes']
        self.assertEqual(len(routes), 11)
        self.assertNotIn('/auth/callback', [x['path'] for x in routes])
        for route in routes:
            self.assertEqual(route['requestPolicies']['authorization']['type'], 'AUTHENTICATION_ONLY')
            self.assertEqual(route['requestPolicies']['headerTransformations']['setHeaders']['items'][0], {
                'name': 'X-Codegen-Subject', 'values': ['${request.auth[id_token_claims][sub]}'], 'ifExists': 'OVERWRITE'})
            self.assertTrue(route['backend']['url'].startswith('http://10.20.1.10:8080/'))
        config = next(x for x in routes if x['path'] == '/api/config')
        self.assertEqual(config['responsePolicies']['headerTransformations']['setHeaders']['items'][0]['values'],
                         ['${request.auth[apigw_csrf_token]}'])

    def test_renderer_rejects_local_credentials_placeholders_and_public_backend(self):
        for changes in [{'local_demo': True, 'bind': '127.0.0.1'},
                        {'auth': 'APIKey', 'config_file': '~/.oci/config'},
                        {'compartment_id': 'ocid1.compartment.oc1..REPLACE_ME'}]:
            original = deepcopy(self.runtime)
            self.runtime.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.render()
            self.runtime = original
        for address in ['8.8.8.8', '127.0.0.1', '0.0.0.0', '::1']:
            with self.subTest(address=address), self.assertRaises(ValueError): self.render(backend_ip=address)

    def test_prebuilt_requires_an_actual_app_image_and_no_inline_archive(self):
        self.deployment['image_mode'] = 'prebuilt'
        with self.assertRaises(ValueError): self.render()
        with self.assertRaises(ValueError): prepare_module.prepare(self.runtime, self.deployment)
        self.deployment['container_image'] = 'registry.example.com/terraform-chat:0.1.0'
        result = prepare_module.prepare(self.runtime, self.deployment)
        child = result['container']['containers'][0]
        self.assertNotIn('arguments', child)
        self.assertNotIn('APP_SHA256', child['environmentVariables'])

    def test_package_is_deterministic_and_contains_bootstrap_installer_without_private_inputs(self):
        raw, manifest = package_module.package(ROOT)
        self.assertEqual(raw, self.archive.read_bytes())
        names = [item['name'] for item in manifest['members']]
        self.assertIn('scripts/install_terraform.py', names)
        self.assertIn('LICENSE', names)
        self.assertIn('app/config.py', names)
        self.assertNotIn('examples/runtime-config.example.json', names)
        self.assertFalse(any('state' in name and not name.startswith('app/') for name in names))
        self.assertFalse(any(name.startswith(('deployment-evidence/', '.git/', 'config/')) for name in names))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest['sha256'])

    def test_renderer_rejects_archive_with_extra_private_configuration(self):
        raw = self.archive.read_bytes()
        buffer = io.BytesIO(raw)
        with zipfile.ZipFile(buffer, 'a') as archive:
            archive.writestr('config/private.json', '{"do_not_ship": true}')
        self.archive.write_bytes(buffer.getvalue())
        with self.assertRaisesRegex(ValueError, 'unexpected file'): self.render()


class InstallerTests(unittest.TestCase):
    def test_verified_archive_preserves_vendor_license_and_bad_hash_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as archive:
                archive.writestr('terraform', b'fixture-binary')
                archive.writestr('LICENSE.txt', b'fixture-vendor-license')
            raw = buffer.getvalue()
            entry = {'url': 'https://releases.hashicorp.com/terraform/1.13.5/terraform_1.13.5_linux_amd64.zip',
                     'sha256': hashlib.sha256(raw).hexdigest()}
            metadata = p / 'release.json'
            metadata.write_text(json.dumps({'version': '1.13.5', 'archives': {'terraform_1.13.5_linux_amd64.zip': entry}}))
            result = installer.install(metadata, p / 'bin/terraform', system='Linux', machine='x86_64', download=lambda url: raw)
            self.assertEqual((p / 'bin/terraform').read_bytes(), b'fixture-binary')
            self.assertEqual(Path(result['license']).read_bytes(), b'fixture-vendor-license')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                installer.install(metadata, p / 'bad/terraform', system='Linux', machine='x86_64', download=lambda url: b'bad')
            self.assertFalse((p / 'bad/terraform').exists())


if __name__ == '__main__': unittest.main()
