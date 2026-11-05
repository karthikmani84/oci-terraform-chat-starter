import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from app.terraform import (TerraformRunner, TerraformError, live_snapshot,
                           plan_summary, safe_diagnostic, state_metadata)


class PlanEvidenceTests(unittest.TestCase):
    def test_replacement_and_sensitive_nested_diff(self):
        plan = {'resource_changes': [{'address':'oci_core_vcn.demo','type':'oci_core_vcn',
            'change': {'actions':['delete','create'],'before':{'display_name':'old','password':'a','nested':{'value':'s'}},
                'after':{'display_name':'new','password':'b','nested':{'value':'t'}},
                'before_sensitive':{'nested':{'value':True}},'after_sensitive':{'nested':{'value':True}}}}]}
        result=plan_summary(plan)
        self.assertEqual(result['counts'], {'add':1,'change':0,'destroy':1,'replace':1,'import':0})
        self.assertTrue(result['destructive'])
        self.assertEqual(result['resources'][0]['fields'], [{'field':'display_name','before':'old','after':'new'}])
        self.assertNotIn('"a"', json.dumps(result))

    def test_refresh_uses_resource_drift_and_does_not_report_normal_plan(self):
        p={'resource_changes':[{'address':'x','change':{'actions':['no-op']}}],
           'resource_drift':[{'address':'x','change':{'actions':['update'],'before':{'display_name':'before'},'after':{'display_name':'manual'}}}]}
        self.assertTrue(plan_summary(p,refresh=True)['has_drift'])
        self.assertFalse(plan_summary(p)['has_changes'])

    def test_snapshot_uses_canonical_values_and_rejects_missing(self):
        self.assertEqual(live_snapshot({'planned_values':{'a':1,'b':2}}),live_snapshot({'planned_values':{'b':2,'a':1}}))
        with self.assertRaises(TerraformError):live_snapshot({})

    def test_apply_result_metadata_omits_raw_outputs_and_attributes(self):
        raw={'serial':3,'lineage':'example','outputs':{'password':{'value':'SECRET'}},
             'resources':[{'mode':'managed','type':'oci_core_vcn','name':'demo','instances':[{'attributes':{'id':'ocid1.vcn.example','password':'SECRET'}}]}]}
        public=state_metadata(raw)
        self.assertEqual(public['serial'],3)
        self.assertEqual(public['resources'][0]['id'],'ocid1.vcn.example')
        self.assertNotIn('SECRET',json.dumps(public))
        self.assertNotIn('outputs',public)

    def test_diagnostics_never_echo_credentials(self):
        raw='password = "SECRETVALUE"\nAuthorization: Bearer abcdefgh\neyJabc.def.ghi\n-----BEGIN PRIVATE KEY-----\naaaa\n-----END PRIVATE KEY-----'
        result=safe_diagnostic(raw)
        for token in ['SECRETVALUE','abcdefgh','eyJabc.def.ghi','aaaa']:
            self.assertNotIn(token,result)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.runner=TerraformRunner(Path(self.tmp.name),{'terraform_bin':'/trusted/terraform','region':'us-ashburn-1','compartment_id':'managed','namespace':'ns','bucket':'private'})
    def tearDown(self):self.tmp.cleanup()

    def test_init_sets_native_backend_and_never_embeds_key_material(self):
        with patch('app.terraform.subprocess.run',return_value=subprocess.CompletedProcess([],0,'initialized','')) as call:
            self.runner.init()
        base=json.loads((self.runner.workdir/'app.tf.json').read_text())
        backend=json.loads((self.runner.workdir/'.backend.json').read_text())
        self.assertEqual(base['terraform']['backend'],{'oci':{}})
        self.assertEqual(base['provider']['oci']['auth'],'ResourcePrincipal')
        self.assertEqual(backend['key'],'terraform/managed.tfstate')
        self.assertFalse(call.call_args.kwargs['shell'])
        self.assertIn('-reconfigure',call.call_args.args[0])
        self.assertEqual((self.runner.workdir/'app.tf.json').stat().st_mode&0o777,0o600)

    def test_saved_plan_is_binary_evidence_not_recreated_on_apply(self):
        def fake_run(argv,**kwargs):
            if argv[1]=='plan':
                Path(next(x[5:] for x in argv if x.startswith('-out='))).write_bytes(b'EXACT-SAVED-PLAN')
                return subprocess.CompletedProcess(argv,2,'Plan: 1 to add.','')
            if argv[1]=='show':
                return subprocess.CompletedProcess(argv,0,json.dumps({'planned_values':{},'resource_changes':[{'address':'x','change':{'actions':['create'],'after':{'name':'demo'}}}]}),'')
            if argv[1]=='state':return subprocess.CompletedProcess(argv,0,'{"serial":1}','')
            return subprocess.CompletedProcess(argv,0,'Apply complete.','')
        with patch('app.terraform.subprocess.run',side_effect=fake_run) as call:
            plan=self.runner.plan('saved123')
            result=self.runner.apply(plan['path'])
        self.assertTrue(result['applied'])
        applies=[x.args[0] for x in call.call_args_list if x.args[0][1]=='apply']
        self.assertEqual(len(applies),1)
        self.assertEqual(applies[0][-1],plan['path'])
        self.assertNotIn('-auto-approve',applies[0])
        with self.assertRaises(TerraformError):self.runner.plan('saved123')

    def test_foreign_paths_and_invalid_ids_cannot_be_applied(self):
        with self.assertRaises(TerraformError):self.runner.plan('../escape')
        path=Path(self.tmp.name).parent/'foreign.tfplan'
        path.write_bytes(b'bad')
        try:
            with self.assertRaises(TerraformError):self.runner.apply(path)
        finally:path.unlink()

    def test_refresh_cli_flag_and_drift_diff(self):
        def fake_run(argv,**kwargs):
            if argv[1]=='plan':
                Path(next(x[5:] for x in argv if x.startswith('-out='))).write_bytes(b'refresh')
                self.assertIn('-refresh-only',argv)
                return subprocess.CompletedProcess(argv,2,'','')
            return subprocess.CompletedProcess(argv,0,json.dumps({'planned_values':{},'resource_drift':[{'address':'x','change':{'actions':['update'],'before':{'name':'a'},'after':{'name':'b'}}}]}),'')
        with patch('app.terraform.subprocess.run',side_effect=fake_run):
            result=self.runner.refresh()
        self.assertTrue(result['summary']['has_drift'])


if __name__=='__main__':unittest.main()
