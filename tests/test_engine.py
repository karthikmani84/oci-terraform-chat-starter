"""The single app, rather than the model, controls every live operation."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from app.engine import Engine, digest, resources
from app.storage import MemoryStore, Missing
from app.terraform import TerraformError, plan_summary
from app.adoption import AdoptionError

C='ocid1.compartment.oc1..managedmanaged'
OWNER='example-admin'
NLB='oci_network_load_balancer_network_load_balancer'
RID='ocid1.networkloadbalancer.oc1.me-dubai-1.existingexisting'


def file_for(typ,name,**attrs):
    return json.dumps({'resource':{typ:{name:{'compartment_id':C,**attrs}}}})


def refresh(name='baseline',drift=False):
    value={'plan':{'planned_values':{'root_module':{'resources':[{'address':'oci_core_vcn.first','values':{'display_name':name}}]}},'resource_drift':[]}}
    if drift:
        value['plan']['resource_drift']=[{'address':'oci_core_vcn.first','change':{'actions':['update'],'before':{'display_name':'baseline'},'after':{'display_name':name}}}]
    value['summary']=plan_summary(value['plan'],refresh=True)
    return value


class FakeRunner:
    def __init__(self,root):
        self.workdir=Path(root);self.plans_dir=self.workdir/'.plans';self.plans_dir.mkdir()
        self.calls=[];self.refreshes=[];self.validation_errors=[]
        self.plan_data={'planned_values':{},'resource_changes':[{'address':'oci_core_vcn.first','type':'oci_core_vcn','change':{'actions':['create'],'before':None,'after':{'display_name':'new','compartment_id':C}}}]}
    def init(self):self.calls.append('init')
    def validate(self):
        self.calls.append('validate')
        if self.validation_errors:raise self.validation_errors.pop(0)
        return {'valid':True}
    def refresh(self):
        self.calls.append('refresh')
        return deepcopy(self.refreshes.pop(0)) if self.refreshes else refresh()
    def plan(self,pid):
        self.calls.append('plan')
        path=self.plans_dir/(pid+'.tfplan');path.write_bytes(('SAVED:'+pid).encode())
        data=deepcopy(self.plan_data);summary=plan_summary(data)
        return {'plan':data,'path':str(path),'summary':summary,'destructive':summary['destructive']}
    def apply(self,path):
        self.calls.append(('apply',path))
        if Path(path).parent!=self.plans_dir:raise ValueError('The saved Plan must be restored under private .plans')
        return {'applied':True,'state':{'serial':1,'resources':[]}}


class FakeModel:
    def __init__(self):self.outputs=[];self.calls=[]
    def respond(self,conv,feedback):
        self.calls.append({'feedback':feedback,'messages':deepcopy(conv['messages'])})
        return deepcopy(self.outputs.pop(0))


class FakeTools:
    def __init__(self):self.calls=[];self.name="native-nlb"
    def execute(self,name,args):
        self.calls.append((name,args))
        return {'compartment_id':C,'id':args.get('resource_id'),'display_name':self.name}


class FakeAdoption:
    def __init__(self):self.calls=[];self.live_files={}
    def prepare(self,typ,address,rid):
        self.calls.append((typ,address,rid))
        _,name=address.split('.')
        return {'files':{'adopt_nlb.tf.json':file_for(typ,name,display_name='native-nlb',subnet_id='ocid1.subnet.native'),
            'adopt_import.tf.json':json.dumps({'import':[{'to':address,'id':rid}]})},
            'baseline_plan':{'plan':{'resource_changes':[{'address':address,'change':{'actions':['no-op'],'importing':{'id':rid}}}]}}}
    def adopt_live(self,drift):return deepcopy(self.live_files)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.clock=[1000.]
        self.store=MemoryStore();self.runner=FakeRunner(self.tmp.name);self.adoption=FakeAdoption();self.model=FakeModel();self.tools=FakeTools()
        self.engine=Engine({'compartment_id':C},self.store,self.runner,self.adoption,self.model,self.tools,clock=lambda:self.clock[0])
        public=self.engine.create(OWNER);self.cid=public['id'];self.conv=self.engine._get(self.cid,OWNER)
    def tearDown(self):self.tmp.cleanup()
    def draft(self):
        self.conv.update(files={'resources.tf.json':file_for('oci_core_vcn','first',display_name='new')},revision=1,reviewed_revision=1,stage='review',workspace_revision=self.engine.current['revision'])
        return self.conv
    def planned(self,destructive=False):
        self.draft()
        if destructive:
            self.runner.plan_data['resource_changes'][0]['change']['actions']=['delete','create']
        self.engine._plan(self.conv)
        return self.conv['saved_plan']
    def test_plan_runs_refresh_first_and_saves_15_minute_exact_binary(self):
        saved=self.planned()
        self.assertEqual(saved['expires_at'],1900)
        self.assertLess(self.runner.calls.index('refresh'),self.runner.calls.index('plan'))
        self.assertEqual(self.store.get('plans/'+saved['id']+'/saved.tfplan'),('SAVED:'+saved['id']).encode())
        self.assertNotIn('saved_plan',self.engine.public(self.conv))
    def test_apply_rejects_wrong_id_expiry_changed_code_revision_and_workspace(self):
        saved=deepcopy(self.planned());original=deepcopy(self.conv)
        cases=[({'plan_id':'different'},None),({'plan_id':saved['id']},'expired'),({'plan_id':saved['id']},'code'),({'plan_id':saved['id']},'revision'),({'plan_id':saved['id']},'workspace')]
        for body,mutation in cases:
            conv=deepcopy(original);self.clock[0]=1000
            if mutation=='expired':self.clock[0]=1900
            if mutation=='code':conv['files']['resources.tf.json']='{}'
            if mutation=='revision':conv['revision']+=1
            if mutation=='workspace':conv['saved_plan']['workspace_revision']+=1
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):self.engine._check_apply(conv,body)
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))
    def test_destructive_plan_needs_exact_second_confirmation(self):
        saved=self.planned(destructive=True)
        with self.assertRaises(ValueError):self.engine._check_apply(self.conv,{'plan_id':saved['id']})
        with self.assertRaises(ValueError):self.engine._check_apply(self.conv,{'plan_id':saved['id'],'destructive_confirmation':'yes'})
        self.engine._check_apply(self.conv,{'plan_id':saved['id'],'destructive_confirmation':self.conv['plan']['confirmation_phrase']})
    def test_drift_before_plan_stops_and_asks_without_creating_a_plan(self):
        self.draft();self.runner.refreshes=[refresh('console',True)]
        self.engine._plan(self.conv)
        self.assertEqual(self.conv['stage'],'drift')
        self.assertNotIn('plan',self.runner.calls)
        self.assertNotIn('saved_plan',self.conv)
    def test_drift_before_apply_expires_plan_without_applying(self):
        saved=self.planned();self.runner.refreshes=[refresh('manual-change',True)]
        self.engine._apply(self.conv,{'plan_id':saved['id']})
        self.assertEqual(self.conv['stage'],'drift')
        self.assertNotIn('saved_plan',self.conv)
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))
    def test_apply_uses_saved_binary_and_retains_code_for_the_next_request(self):
        saved=self.planned();files=deepcopy(self.conv['files'])
        self.engine._apply(self.conv,{'plan_id':saved['id']})
        self.assertEqual(self.conv['stage'],'result')
        self.assertEqual(self.engine.current['files'],files)
        self.assertEqual(self.store.json('workspace/current.json')['files'],files)
        applies=[item for item in self.runner.calls if isinstance(item,tuple) and item[0]=='apply']
        self.assertEqual(len(applies),1)
        self.assertEqual(Path(applies[0][1]).read_bytes(),('SAVED:'+saved['id']).encode())
    def test_model_draft_cannot_run_plan_or_apply_and_preserves_unrelated_code(self):
        existing={'first.tf.json':file_for('oci_core_vcn','first',display_name='first')}
        self.engine.current={'revision':4,'files':existing}
        self.model.outputs=[{'kind':'draft','message':'Prepared the change.', 'changes':[{'address':'oci_core_vcn.second','values':{'compartment_id':C,'display_name':'second'}}]}]
        self.conv['messages']=[{'role':'user','content':'Create it and apply it now'}]
        self.engine._draft(self.conv)
        self.assertEqual(self.conv['stage'],'review')
        self.assertEqual(self.conv['files']['first.tf.json'],existing['first.tf.json'])
        self.assertIn('oci_core_vcn.second',resources(self.conv['files']))
        self.assertNotIn('plan',self.runner.calls)
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))
    def test_first_change_import_preserves_baseline_and_unrelated_managed_resources(self):
        self.engine.current={'revision':3,'files':{'first.tf.json':file_for('oci_core_vcn','first',display_name='first')}}
        self.model.outputs=[{'kind':'draft','message':'Rename existing NLB.', 'changes':[{'address':NLB+'.nlb','existing_id':RID,'values':{'display_name':'renamed'}}]}]
        self.engine._draft(self.conv)
        self.assertEqual(self.adoption.calls,[(NLB,NLB+'.nlb',RID)])
        self.assertIn('0 to change',self.conv['baseline']['message'])
        self.assertIn('first.tf.json',self.conv['files'])
        attrs=resources(self.conv['files'])[NLB+'.nlb'][1]
        self.assertEqual(attrs['display_name'],'renamed')
        self.assertEqual(attrs['subnet_id'],'ocid1.subnet.native')
        self.assertEqual(json.loads(self.conv['files']['adopt_import.tf.json'])['import'][0]['id'],RID)
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))
    def test_keep_live_merges_with_other_managed_code_and_does_not_repeat_question(self):
        files={'first.tf.json':file_for('oci_core_vcn','first',display_name='baseline'),
               'second.tf.json':file_for('oci_core_vcn','second',display_name='unchanged')}
        self.engine.current={'revision':2,'files':files};self.draft()
        observed=refresh('console',True);self.runner.refreshes=[observed,observed,observed]
        self.engine._plan(self.conv)
        self.adoption.live_files={'first.tf.json':file_for('oci_core_vcn','first',display_name='console')}
        self.model.outputs=[{'kind':'draft','message':'Continue the requested change.', 'changes':[{'address':'oci_core_vcn.second','values':{'display_name':'unchanged'}}]}]
        self.engine._resolve_drift(self.conv,'keep_live')
        self.assertIn('second.tf.json',self.engine.current['files'])
        self.assertEqual(resources(self.engine.current['files'])['oci_core_vcn.first'][1]['display_name'],'console')
        self.conv.update(files=deepcopy(self.engine.current['files']),stage='review',reviewed_revision=self.conv['revision'])
        self.engine._plan(self.conv)
        self.assertEqual(self.conv['stage'],'planned','Choosing keep live must not ask the identical question forever')
    def test_restore_code_allows_only_the_reviewed_observed_drift(self):
        self.draft();observed=refresh('console',True);self.runner.refreshes=[observed,observed,observed]
        self.engine._plan(self.conv);self.engine._resolve_drift(self.conv,'restore_code');self.engine._plan(self.conv)
        self.assertEqual(self.conv['stage'],'planned')
        self.runner.refreshes=[refresh('changed-again',True)]
        pid=self.conv['saved_plan']['id'];self.engine._apply(self.conv,{'plan_id':pid})
        self.assertEqual(self.conv['stage'],'drift')
    def test_validation_failure_is_given_to_model_for_at_most_two_corrections(self):
        self.runner.validation_errors=[TerraformError('invalid','Incorrect attribute syntax')]
        draft={'kind':'draft','message':'VCN','changes':[{'address':'oci_core_vcn.first','values':{'compartment_id':C,'display_name':'first'}}]}
        self.model.outputs=[draft,draft]
        self.engine._draft(self.conv)
        self.assertEqual(len(self.model.calls),2)
        self.assertIn('Incorrect attribute syntax',self.model.calls[1]['feedback'])
        self.assertEqual(self.conv['stage'],'review')
    def test_pending_import_live_change_expires_saved_plan(self):
        self.draft()
        self.conv['adoptions']=[{'address':NLB+'.nlb','resource_type':NLB,'resource_id':RID}]
        self.engine._plan(self.conv)
        pid=self.conv['saved_plan']['id']
        self.tools.name='manual-after-plan'
        self.engine._apply(self.conv,{'plan_id':pid})
        self.assertEqual(self.conv['stage'],'drift')
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))
    def test_long_refresh_cannot_extend_the_plan_expiry(self):
        saved=self.planned()
        def slow_refresh():
            self.clock[0]=saved['expires_at']+1
            return refresh()
        self.runner.refresh=slow_refresh
        with self.assertRaises(ValueError):self.engine._apply(self.conv,{'plan_id':saved['id']})
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))
    def test_altered_saved_binary_is_never_executed(self):
        saved=self.planned();self.store.put('plans/'+saved['id']+'/saved.tfplan',b'changed')
        with self.assertRaises(ValueError):self.engine._apply(self.conv,{'plan_id':saved['id']})
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))
    def test_plan_scope_rejects_foreign_before_after_and_unknown_compartment(self):
        for change in [{'before':{'compartment_id':'foreign'},'after':{'compartment_id':C}},
                       {'before':None,'after':{'compartment_id':'foreign'}},
                       {'before':None,'after':{},'after_unknown':{'compartment_id':True}}]:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):self.engine._plan_scope({'resource_changes':[{'type':'oci_core_vcn','change':change}]})
    def test_deletion_only_draft_reaches_review_without_applying(self):
        self.engine.current={'revision':1,'files':{'first.tf.json':file_for('oci_core_vcn','first',display_name='old')}}
        self.model.outputs=[{'kind':'draft','message':'Remove VCN.', 'changes':[],'remove':['oci_core_vcn.first']}]
        self.engine._draft(self.conv)
        self.assertEqual(self.conv['stage'],'review')
        self.assertNotIn('oci_core_vcn.first',resources(self.conv['files']))
        self.assertEqual(json.loads(self.conv['files']['first.tf.json']),{})
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))
    def test_delete_last_resource_of_type_keeps_other_type_without_empty_labels(self):
        address=NLB+'.nlb'
        other={'compartment_id':C,'display_name':'keep-vcn'}
        original={'resource':{NLB:{'nlb':{'compartment_id':C,'display_name':'delete-nlb'}},
                              'oci_core_vcn':{'first':other}}}
        self.engine.current={'revision':1,'files':{'resources.tf.json':json.dumps(original)}}
        self.model.outputs=[{'kind':'draft','message':'Delete NLB.','changes':[],'remove':[address]}]
        self.engine._draft(self.conv)
        proposed=json.loads(self.conv['files']['resources.tf.json'])
        self.assertEqual(proposed,{'resource':{'oci_core_vcn':{'first':other}}})
        self.assertEqual(json.loads(self.engine.current['files']['resources.tf.json']),original)
        self.assertNotIn('plan',self.runner.calls)
    def test_delete_one_name_keeps_other_resource_of_same_type(self):
        address=NLB+'.delete_nlb'
        kept={'compartment_id':C,'display_name':'keep-nlb'}
        self.engine.current={'revision':1,'files':{'resources.tf.json':json.dumps({'resource':{
            NLB:{'delete_nlb':{'compartment_id':C},'keep_nlb':kept}}})}}
        self.model.outputs=[{'kind':'draft','message':'Delete selected NLB.','changes':[],'remove':[address]}]
        self.engine._draft(self.conv)
        self.assertEqual(json.loads(self.conv['files']['resources.tf.json']),
                         {'resource':{NLB:{'keep_nlb':kept}}})
    def test_delete_final_resource_and_its_import_produces_empty_document(self):
        address=NLB+'.nlb'
        self.engine.current={'revision':1,'files':{'resources.tf.json':json.dumps({
            'resource':{NLB:{'nlb':{'compartment_id':C,'display_name':'delete-nlb'}}},
            'import':[{'to':address,'id':RID}]})}}
        self.model.outputs=[{'kind':'draft','message':'Delete adopted NLB.','changes':[],'remove':[address]}]
        self.engine._draft(self.conv)
        self.assertEqual(json.loads(self.conv['files']['resources.tf.json']),{})
        self.assertEqual(self.conv['stage'],'review')
        self.assertNotIn('plan',self.runner.calls)
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))
    def test_deleted_final_resource_output_is_accepted_by_real_terraform_parser(self):
        available=os.environ.get('TERRAFORM_BIN') or shutil.which('terraform')
        if not available:self.skipTest('Terraform is not on PATH')
        binary=Path(available)
        address=NLB+'.nlb'
        self.engine.current={'revision':1,'files':{'resources.tf.json':json.dumps({
            'resource':{NLB:{'nlb':{'compartment_id':C}}},'import':[{'to':address,'id':RID}]})}}
        self.model.outputs=[{'kind':'draft','message':'Delete NLB.','changes':[],'remove':[address]}]
        self.engine._draft(self.conv)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name,source in self.conv['files'].items():(root/name).write_text(source)
            result=subprocess.run([str(binary),'validate','-json','-no-color'],cwd=root,capture_output=True,text=True)
        report=json.loads(result.stdout)
        self.assertEqual(result.returncode,0,report)
        self.assertTrue(report['valid'],report)
    def test_removal_object_is_corrected_instead_of_crashing(self):
        address=NLB+'.spoke_nlb'
        original={'nlb.tf.json':file_for(NLB,'spoke_nlb',display_name='spoke-nlb'),
                  'first.tf.json':file_for('oci_core_vcn','first',display_name='first')}
        self.engine.current={'revision':2,'files':deepcopy(original)}
        self.model.outputs=[{'kind':'draft','message':'Remove selected NLB.','changes':[],
                             'remove':[{'address':address}]},
                            {'kind':'draft','message':'Remove selected NLB.','changes':[],'remove':[address]}]
        self.engine._draft(self.conv)
        self.assertEqual(len(self.model.calls),2)
        self.assertIn('address string',self.model.calls[1]['feedback'])
        self.assertEqual(self.conv['stage'],'review')
        self.assertNotIn(address,resources(self.conv['files']))
        self.assertEqual(self.conv['files']['first.tf.json'],original['first.tf.json'])
        self.assertEqual(self.engine.current['files'],original)
        self.assertNotIn('plan',self.runner.calls)
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))
    def test_invalid_removals_stop_after_two_corrections_without_live_actions(self):
        address=NLB+'.spoke_nlb'
        self.engine.current={'revision':1,'files':{'nlb.tf.json':file_for(NLB,'spoke_nlb',display_name='spoke-nlb')}}
        for invalid in ({'address':address},[address],None,3,'spoke-nlb'):
            with self.subTest(invalid=invalid):
                self.model.calls=[]
                draft={'kind':'draft','message':'Remove NLB.','changes':[],'remove':[invalid]}
                self.model.outputs=[draft,draft,draft]
                with self.assertRaisesRegex(ValueError,'removal must be a Terraform resource address string'):
                    self.engine._draft(self.conv)
                self.assertEqual(len(self.model.calls),3)
                self.assertIsNone(self.model.calls[0]['feedback'])
                self.assertTrue(all(x['feedback'] for x in self.model.calls[1:]))
                self.assertIn(address,resources(self.engine.current['files']))
        self.assertEqual(self.adoption.calls,[])
        self.assertNotIn('plan',self.runner.calls)
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))
    def test_invalid_change_is_rejected_before_any_import_or_lookup(self):
        valid={'address':NLB+'.nlb','existing_id':RID,'values':{'display_name':'renamed'}}
        invalid_changes=['not an object',{'address':{'name':'bad'},'values':{}},
                         {'address':NLB+'.nlb','values':[]},
                         {'address':NLB+'.nlb','existing_id':{'id':RID},'values':{}}]
        for invalid in invalid_changes:
            with self.subTest(invalid=invalid):
                self.model.calls=[]
                draft={'kind':'draft','message':'Change NLB.','changes':[valid,invalid],'remove':[]}
                self.model.outputs=[draft,draft,draft]
                with self.assertRaises(ValueError):self.engine._draft(self.conv)
                self.assertEqual(len(self.model.calls),3)
                self.assertEqual(self.tools.calls,[])
                self.assertEqual(self.adoption.calls,[])
        self.assertNotIn('plan',self.runner.calls)
    def test_delete_adopted_resource_removes_only_its_import_targets(self):
        address=NLB+'.nlb';other='oci_core_vcn.first'
        original={'nlb.tf.json':file_for(NLB,'nlb',display_name='native-nlb'),
                  'first.tf.json':file_for('oci_core_vcn','first',display_name='first'),
                  'imports.tf.json':json.dumps({'import':[{'to':address,'id':RID},
                                                        {'to':other,'id':'ocid1.vcn.literal'}]}),
                  'last_import.tf.json':json.dumps({'import':[{'to':address,'id':RID}]})}
        self.engine.current={'revision':2,'files':deepcopy(original)}
        self.model.outputs=[{'kind':'draft','message':'Delete adopted NLB.','changes':[],'remove':[address]}]
        self.engine._draft(self.conv)
        self.assertEqual(self.conv['stage'],'review')
        self.assertNotIn(address,resources(self.conv['files']))
        self.assertEqual(json.loads(self.conv['files']['imports.tf.json'])['import'],
                         [{'to':other,'id':'ocid1.vcn.literal'}])
        self.assertNotIn('import',json.loads(self.conv['files']['last_import.tf.json']))
        self.assertEqual(self.conv['files']['first.tf.json'],original['first.tf.json'])
        self.assertEqual(self.engine.current['files'],original)
        self.assertEqual(self.adoption.calls,[])
        self.assertNotIn('plan',self.runner.calls)
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))
    def test_unmanaged_deletion_is_not_silently_imported_or_applied(self):
        draft={'kind':'draft','message':'Delete NLB.','changes':[],'remove':[NLB+'.unmanaged']}
        self.model.outputs=[draft,draft,draft]
        with self.assertRaisesRegex(ValueError,'Cannot remove an unmanaged resource'):
            self.engine._draft(self.conv)
        self.assertEqual(len(self.model.calls),3)
        self.assertEqual(self.adoption.calls,[])
        self.assertEqual(self.tools.calls,[])
        self.assertNotIn('plan',self.runner.calls)
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))
    def test_unmanaged_change_and_remove_is_rejected_before_adoption(self):
        address=NLB+'.unmanaged'
        for change in ({'address':address,'existing_id':RID,'values':{'display_name':'native-nlb'}},
                       {'address':address,'values':{'compartment_id':C,'display_name':'new-nlb'}}):
            with self.subTest(change=change):
                self.model.calls=[]
                draft={'kind':'draft','message':'Delete NLB.','changes':[change],'remove':[address]}
                self.model.outputs=[draft,draft,draft]
                with self.assertRaisesRegex(ValueError,'Cannot remove an unmanaged resource'):
                    self.engine._draft(self.conv)
                self.assertEqual(len(self.model.calls),3)
                self.assertEqual(self.adoption.calls,[])
                self.assertEqual(self.tools.calls,[])
        self.assertNotIn('plan',self.runner.calls)
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))
    def test_explicit_delete_plan_still_needs_the_second_confirmation(self):
        address=NLB+'.nlb'
        self.engine.current={'revision':1,'files':{'nlb.tf.json':file_for(NLB,'nlb',display_name='nlb')}}
        self.model.outputs=[{'kind':'draft','message':'Delete NLB.','changes':[],'remove':[address]}]
        self.engine._draft(self.conv)
        self.conv['reviewed_revision']=self.conv['revision']
        self.runner.plan_data={'resource_changes':[{'address':address,'type':NLB,'change':{
            'actions':['delete'],'before':{'compartment_id':C,'display_name':'nlb'},'after':None}}]}
        self.engine._plan(self.conv)
        saved=self.conv['saved_plan']
        self.assertTrue(self.conv['plan']['destructive'])
        with self.assertRaisesRegex(ValueError,'second confirmation'):
            self.engine._check_apply(self.conv,{'plan_id':saved['id']})
        self.engine._check_apply(self.conv,{'plan_id':saved['id'],
            'destructive_confirmation':self.conv['plan']['confirmation_phrase']})
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))
    def test_adoption_drift_prompts_admin_instead_of_repairing_the_model(self):
        drift=refresh('console',True)
        def prepare(*args):
            raise AdoptionError('DRIFT_REQUIRES_DECISION','Resolve live drift.',details={'refresh':drift,'summary':drift['summary']})
        self.adoption.prepare=prepare
        self.model.outputs=[{'kind':'draft','message':'Rename NLB.', 'changes':[{'address':NLB+'.nlb','existing_id':RID,'values':{'display_name':'renamed'}}]}]
        self.engine._draft(self.conv)
        self.assertEqual(self.conv['stage'],'drift')
        self.assertEqual(len(self.model.calls),1)
        self.assertNotIn('plan',self.runner.calls)

    def test_admission_allows_only_direct_managed_id_expressions(self):
        valid={'resources.tf.json':json.dumps({'resource':{
            'oci_core_vcn':{'first':{'compartment_id':C,'cidr_blocks':['10.42.0.0/16']}},
            'oci_core_subnet':{'subnet':{'compartment_id':C,'vcn_id':'${oci_core_vcn.first.id}'}}}})}
        self.engine._admit(valid)
        for expression in ['${file("/etc/passwd")}', '${templatefile("script.sh", {})}', '${oci_core_vcn.foreign.id}', '%{ for x in [1] }x%{ endfor }']:
            bad=deepcopy(valid);doc=json.loads(bad['resources.tf.json']);doc['resource']['oci_core_subnet']['subnet']['vcn_id']=expression;bad['resources.tf.json']=json.dumps(doc)
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError):self.engine._admit(bad)
    def test_admission_rejects_generated_execution_and_application_config(self):
        for document in [{'provider':{'oci':{}}},{'terraform':{'backend':{'local':{}}}},{'resource':{'oci_core_vcn':{'first':{'compartment_id':C,'provisioner':{'local-exec':{'command':'do something'}}}}}}]:
            with self.subTest(document=document):
                with self.assertRaises(ValueError):self.engine._admit({'resources.tf.json':json.dumps(document)})

    def test_conversation_owner_and_process_restart_do_not_resume_apply(self):
        with self.assertRaises(Missing):self.engine.get(self.cid,'another-user')
        self.conv.update(stage='applying',busy=True);self.engine._save(self.conv)
        result=self.engine.get(self.cid,OWNER)
        self.assertEqual(result['stage'],'error')
        self.assertFalse(result['busy'])
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='apply' for x in self.runner.calls))


if __name__=='__main__':unittest.main()
