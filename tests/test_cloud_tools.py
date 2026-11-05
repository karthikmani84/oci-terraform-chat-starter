"""Name-based discovery and managed-code reuse for ordinary chat requests."""
from contextlib import redirect_stdout
from io import StringIO
import json
from types import SimpleNamespace as NS
import unittest

import oci
from oci.generative_ai_inference import models as m
from app.cloud import ALIASES, REGISTRY, ReadTools, canonical_type
from app.model import Model

C='ocid1.compartment.oc1..managedmanaged'
NLB='oci_network_load_balancer_network_load_balancer'
RID='ocid1.networkloadbalancer.oc1.me-dubai-1.nativeexisting'


class NativeFailure(RuntimeError):
    code='NotAuthorizedOrNotFound'
    status=404
    request_id='safe-request-123'


class FakeNLB:
    def __init__(self):self.calls=[];self.failure=None
    def item(self):
        return oci.network_load_balancer.models.NetworkLoadBalancerSummary(
            id=RID,compartment_id=C,display_name='SECRET_RESOURCE_NAME',lifecycle_state='ACTIVE')
    def list_network_load_balancers(self,**kwargs):
        self.calls.append(('list',kwargs))
        if self.failure:raise self.failure
        return NS(data=NS(items=[self.item()]),headers={'opc-request-id':'safe-request-123'})
    def get_network_load_balancer(self,rid):
        self.calls.append(('get',rid))
        if self.failure:raise self.failure
        return NS(data=self.item(),headers={'opc-request-id':'safe-request-123'})


class ReadToolTests(unittest.TestCase):
    def setUp(self):
        self.nlb=FakeNLB()
        self.files={'resources.tf.json':json.dumps({'resource':{
            NLB:{'already_managed':{'compartment_id':C,'display_name':'managed-name','subnet_id':'ocid1.subnet.literal'}},
            'oci_core_vcn':{'first':{'compartment_id':C,'display_name':'first'}}}})}
        self.schema={NLB:{'block':{'attributes':{'display_name':{'required':True,'type':'string'},'secret':{'optional':True,'sensitive':True}},'block_types':{}}}}
        self.tools=ReadTools({'nlb':self.nlb},C,lambda:self.files,self.schema)
    def run_tool(self,name,args):
        output=StringIO()
        with redirect_stdout(output):result=self.tools.execute(name,args)
        return result,json.loads(output.getvalue())
    def test_aliases_find_unique_nlb_without_user_supplying_an_ocid(self):
        for alias in ('nlb','network_load_balancer','Network Load Balancer'):
            result,trace=self.run_tool('list_resources',{'resource_type':alias})
            self.assertEqual(result['resources'][0]['id'],RID)
            self.assertEqual(trace['resource_type'],NLB)
            self.assertEqual(trace['count'],1)
            self.assertTrue(trace['ok'])
        result,_=self.run_tool('get_resource',{'resource_type':'nlb','resource_id':RID})
        self.assertEqual(result['display_name'],'SECRET_RESOURCE_NAME')
        self.assertEqual(self.nlb.calls[0][1]['compartment_id'],C)
        self.assertEqual(canonical_type('VCN'),'oci_core_vcn')
        self.assertEqual(canonical_type('subnet'),'oci_core_subnet')
    def test_no_argument_code_lookup_returns_managed_addresses_without_schema_dump(self):
        result,_=self.run_tool('get_terraform_block',{})
        self.assertEqual(set(result),{'managed_blocks'})
        self.assertEqual({x['address'] for x in result['managed_blocks']},{NLB+'.already_managed','oci_core_vcn.first'})
        self.assertEqual(self.nlb.calls,[])
    def test_typed_schema_lookup_includes_existing_addresses_and_hides_sensitive_schema(self):
        result,_=self.run_tool('get_terraform_block',{'resource_type':'nlb'})
        self.assertEqual(result['resource_type'],NLB)
        self.assertEqual(result['managed_blocks'][0]['address'],NLB+'.already_managed')
        self.assertNotIn('secret',result['attributes'])
        self.assertEqual(result['managed_blocks'][0]['values']['subnet_id'],'ocid1.subnet.literal')
    def test_trace_contains_no_resource_names_ids_or_values(self):
        _,trace=self.run_tool('get_resource',{'resource_type':'nlb','resource_id':RID})
        self.assertEqual(trace,{'event':'read_only_tool','tool':'get_resource','resource_type':NLB,'count':1,'ok':True,'opc_request_id':'safe-request-123'})
        self.assertNotIn('SECRET_RESOURCE_NAME',json.dumps(trace));self.assertNotIn(RID,json.dumps(trace))
    def test_native_failure_trace_reports_bounded_cause_without_exception_text(self):
        self.nlb.failure=NativeFailure('SECRET_ERROR_VALUES')
        output=StringIO()
        with redirect_stdout(output):
            with self.assertRaises(NativeFailure):self.tools.execute('get_resource',{'resource_type':'nlb','resource_id':RID})
        trace=json.loads(output.getvalue())
        self.assertFalse(trace['ok']);self.assertEqual(trace['error_code'],'NotAuthorizedOrNotFound')
        self.assertEqual(trace['http_status'],404);self.assertEqual(trace['opc_request_id'],'safe-request-123')
        self.assertNotIn('SECRET_ERROR_VALUES',output.getvalue());self.assertNotIn(RID,output.getvalue())


class ModelToolContractTests(unittest.TestCase):
    def test_native_sdk_has_seven_exact_types_and_forwards_safe_lookup_failure(self):
        calls=[]
        class Tools:
            def execute(self,name,args):raise NativeFailure('SECRET_NATIVE_EXCEPTION')
        class Client:
            def chat(self,details):
                payload=oci.util.to_dict(details);calls.append(payload)
                for tool in payload['chat_request']['tools']:
                    self_types=tool['parameters']['properties']['resource_type']['enum']
                    if self_types!=sorted(REGISTRY):raise AssertionError(self_types)
                if len(calls)==1:
                    message=m.AssistantMessage(tool_calls=[m.FunctionCall(id='read1',name='get_resource',arguments=json.dumps({'resource_type':NLB,'resource_id':RID}))])
                else:
                    message=m.AssistantMessage(content=[m.TextContent(text=json.dumps({'kind':'clarify','message':'The lookup returned 404; check access to the selected resource.'}))])
                return NS(data=m.ChatResult(chat_response=m.GenericChatResponse(choices=[m.ChatChoice(message=message,finish_reason='STOP')])))
        result=Model(Client(),{'compartment_id':C,'region':'me-dubai-1','routing_profile_id':'route'},Tools()).respond({'messages':[{'role':'user','content':'Rename my uniquely named NLB'}]})
        self.assertEqual(result['kind'],'clarify');self.assertEqual(len(calls),2)
        history=calls[1]['chat_request']['messages']
        data=json.loads([x for x in history if x['role']=='TOOL'][0]['content'][0]['text'])['read_only_tool_data']
        self.assertEqual(data['error_code'],'NotAuthorizedOrNotFound');self.assertEqual(data['http_status'],404)
        self.assertNotIn('SECRET_NATIVE_EXCEPTION',json.dumps(data))
        system=calls[0]['chat_request']['messages'][0]['content'][0]['text']
        self.assertIn('Before asking for an OCID, discover',system)
        self.assertIn('reuse their',system)


if __name__=='__main__':unittest.main()
