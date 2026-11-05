from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from app.adoption import AdoptionError, AdoptionService, normalize_generated, require_import_only
from app.terraform import TerraformRunner, plan_summary

C='ocid1.compartment.oc1..managedmanaged'
NLB='oci_network_load_balancer_network_load_balancer'
RID='ocid1.networkloadbalancer.oc1.me-dubai-1.existingexisting'
SUBNET='ocid1.subnet.oc1.me-dubai-1.parentparent'
SCHEMA={'attributes':{
 'compartment_id':{'required':True},'subnet_id':{'required':True},'display_name':{'required':True},
 'defined_tags':{'optional':True,'computed':True},'freeform_tags':{'optional':True,'computed':True},
 'is_private':{'optional':True,'computed':True},'is_preserve_source_destination':{'optional':True,'computed':True},
 'is_symmetric_hash_enabled':{'optional':True,'computed':True},
 'nlb_ip_version':{'optional':True,'computed':True},'assigned_ipv6':{'optional':True},
 'network_security_group_ids':{'optional':True},'state':{'computed':True},
 'security_attributes':{'optional':True,'computed':True}},'block_types':{}}
RAW=f'''resource "{NLB}" "existing" {{
  compartment_id = "{C}"
  display_name = "existing-service"
  subnet_id = "{SUBNET}"
  defined_tags = {{ "org.CostCentre" = "POC" }}
  freeform_tags = {{ "owner" = "operator" }}
  is_private = true
  is_preserve_source_destination = false
  is_symmetric_hash_enabled = false
  nlb_ip_version = "IPV4"
  assigned_ipv6 = null
  network_security_group_ids = []
  security_attributes = {{}}
  state = "ACTIVE"
}}
'''


def baseline(extra=None):
    result={'resource_changes':[{'address':NLB+'.existing','type':NLB,'change':{
        'actions':['no-op'],'importing':{'id':RID},'before':None,'after':{'display_name':'existing-service'}}}]}
    if extra:result['resource_changes'].append(extra)
    return result


class NormalizationTests(unittest.TestCase):
    def test_generated_nlb_keeps_literal_parent_and_meaningful_live_settings(self):
        normalized=normalize_generated(RAW,resource_type=NLB,name='existing',schema=SCHEMA,compartment_id=C)
        block=normalized['resource'][NLB]['existing']
        self.assertEqual(block['subnet_id'],SUBNET)
        self.assertNotIn('defined_tags',block)
        self.assertNotIn('freeform_tags',block)
        self.assertNotIn('state',block)
        self.assertNotIn('assigned_ipv6',block)
        self.assertNotIn('security_attributes',block)
        self.assertFalse(block['is_preserve_source_destination'])
        self.assertEqual(block['lifecycle']['ignore_changes'],['defined_tags','freeform_tags'])
        self.assertEqual(block['network_security_group_ids'],[])

    def test_defaults_removed_only_from_pinned_evidence(self):
        default={'is_symmetric_hash_enabled':False}
        clean=normalize_generated(RAW,resource_type=NLB,name='existing',schema=SCHEMA,compartment_id=C,defaults=default)
        self.assertNotIn('is_symmetric_hash_enabled',clean['resource'][NLB]['existing'])
        self.assertIn('is_private',clean['resource'][NLB]['existing'])

    def test_scope_address_and_unknown_generated_field_rejected(self):
        for raw,c in [(RAW,'foreign'),(RAW.replace('"existing"','"other"'),'managed'),(RAW.replace('state = "ACTIVE"','unknown = 42'),C)]:
            with self.assertRaises(AdoptionError):
                normalize_generated(raw,resource_type=NLB,name='existing',schema=SCHEMA,compartment_id=c)

    def test_import_baseline_rejects_any_live_change_including_an_unrelated_delete(self):
        require_import_only(baseline(),NLB+'.existing',RID)
        for actions in [['update'],['delete'],['delete','create']]:
            p=baseline({'address':'oci_core_vcn.unrelated','change':{'actions':actions}})
            with self.assertRaises(AdoptionError):require_import_only(p,NLB+'.existing',RID)
        with self.assertRaises(AdoptionError):require_import_only({'resource_changes':[]},NLB+'.existing',RID)

    def test_json_ignore_changes_is_accepted_by_real_terraform_parser(self):
        available=shutil.which('terraform')
        if not available:self.skipTest('Terraform is not on PATH')
        binary=Path(available)
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)
            (p/'main.tf.json').write_text(json.dumps({'resource':{'terraform_data':{'example':{'input':'test','lifecycle':{'ignore_changes':['input']}}}}}))
            subprocess.run([str(binary),'init','-backend=false','-input=false','-no-color'],cwd=p,check=True,capture_output=True)
            result=subprocess.run([str(binary),'validate','-no-color'],cwd=p,check=True,capture_output=True,text=True)
            self.assertIn('valid',result.stdout)


class FakeRunner:
    _write_private=staticmethod(TerraformRunner._write_private)
    def __init__(self,path,fail=False):
        self.workdir=Path(path);self.plans_dir=self.workdir/'.plans';self.plans_dir.mkdir()
        self.config={'compartment_id':C};self.calls=[];self.fail=fail
    def refresh(self):
        self.calls.append('refresh')
        return {'plan':{'resource_drift':[]},'summary':{'has_drift':False}}
    def generate_config(self,plan_id,output):
        self.calls.append('generate');output.write_text(RAW)
    def schema(self):return {'resource_schemas':{NLB:{'block':SCHEMA}}}
    def validate(self):self.calls.append('validate');return {'valid':True}
    def plan(self,plan_id):
        self.calls.append('plan')
        p=baseline({'address':'x','change':{'actions':['update']}}) if self.fail else baseline()
        return {'plan':p,'summary':plan_summary(p)}


class PreparationAndDriftTests(unittest.TestCase):
    def test_prepare_generates_then_proves_zero_change_without_applying(self):
        with tempfile.TemporaryDirectory() as p:
            r=FakeRunner(p);result=AdoptionService(r).prepare(NLB,NLB+'.existing',RID)
            self.assertEqual(r.calls,['refresh','generate','validate','refresh','plan'])
            self.assertEqual(set(result['files']),{'adopt_existing_import.tf.json','adopt_existing.tf.json'})
            self.assertIn('0 to change',result['message'])
            self.assertTrue((Path(p)/'adopt_existing.tf.json').exists())
            self.assertFalse((Path(p)/'adopt_existing_generated.tf').exists())
    def test_failed_baseline_restores_prior_workspace(self):
        with tempfile.TemporaryDirectory() as p:
            r=FakeRunner(p,True);existing=Path(p)/'unrelated.tf.json';existing.write_text('{}')
            with self.assertRaises(AdoptionError):AdoptionService(r).prepare(NLB,NLB+'.existing',RID)
            self.assertEqual(existing.read_text(),'{}')
            self.assertEqual([x.name for x in Path(p).glob('adopt*.tf*')],[])
    def test_keep_live_direct_name_change_retains_parent_and_no_file_write(self):
        with tempfile.TemporaryDirectory() as p:
            r=FakeRunner(p)
            body=normalize_generated(RAW,resource_type=NLB,name='existing',schema=SCHEMA,compartment_id=C)
            path=Path(p)/'resource.tf.json';original=json.dumps(body);path.write_text(original)
            drift={'resource_drift':[{'address':NLB+'.existing','change':{'actions':['update'],
                'before':{'display_name':'old','subnet_id':SUBNET,'state':'CREATING'},
                'after':{'display_name':'console-change','subnet_id':SUBNET,'state':'ACTIVE'}}}]}
            files=AdoptionService(r).adopt_live(drift)
            result=json.loads(files['resource.tf.json'])['resource'][NLB]['existing']
            self.assertEqual(result['display_name'],'console-change')
            self.assertEqual(result['subnet_id'],SUBNET)
            self.assertEqual(path.read_text(),original)
    def test_keep_live_refuses_changed_parent(self):
        with tempfile.TemporaryDirectory() as p:
            r=FakeRunner(p)
            body=normalize_generated(RAW,resource_type=NLB,name='existing',schema=SCHEMA,compartment_id=C)
            (Path(p)/'resource.tf.json').write_text(json.dumps(body))
            drift={'resource_drift':[{'address':NLB+'.existing','change':{'actions':['update'],'before':{'subnet_id':SUBNET},'after':{'subnet_id':'foreign'}}}]}
            with self.assertRaises(AdoptionError):AdoptionService(r).adopt_live(drift)


if __name__=='__main__':unittest.main()
