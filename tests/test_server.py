"""The chat API enforces sign-in and browser intent before state-machine dispatch."""
import hashlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO

from app.server import handler


class FakeEngine:
    def __init__(self):self.calls=[]
    def create(self,user):self.calls.append(('create',user));return {'id':'new','stage':'clarify'}
    def list(self,user):return []
    def get(self,cid,user):self.calls.append(('get',cid,user));return {'id':cid,'stage':'review'}
    def start(self,cid,user,action,body):
        self.calls.append(('start',cid,user,action,body));return {'id':cid,'stage':'applying' if action=='apply' else 'review'}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.engine=FakeEngine()
        self.httpd=ThreadingHTTPServer(('127.0.0.1',0),handler(self.engine,{'compartment_id':'ocid1.compartment.managed','region':'me-dubai-1','public_host':'chat.example'}))
        self.thread=threading.Thread(target=self.httpd.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.httpd.shutdown();self.httpd.server_close();self.thread.join()
    def request(self,method,path,body=None,**headers):
        conn=HTTPConnection('127.0.0.1',self.httpd.server_address[1],timeout=3)
        if body is not None:
            body=json.dumps(body).encode();headers.setdefault('Content-Type','application/json')
        with redirect_stdout(StringIO()):
            conn.request(method,path,body=body,headers=headers);response=conn.getresponse();raw=response.read()
        status=response.status;conn.close();return status,json.loads(raw)
    def signed_config(self):
        status,value=self.request('GET','/api/config',**{'X-Codegen-Subject':'manager'})
        self.assertEqual(status,200)
        return value
    def test_api_config_exposes_customer_configured_name_and_region(self):
        self.httpd.RequestHandlerClass = handler(self.engine, {'compartment_id': 'ocid1.compartment.example',
            'region': 'us-ashburn-1', 'scope_name': 'Example / managed', 'public_host': 'chat.example'})
        value = self.signed_config()
        self.assertEqual(value['scope'], {'name': 'Example / managed',
            'id': 'ocid1.compartment.example', 'region': 'us-ashburn-1'})
    def test_api_requires_verified_gateway_subject(self):
        status,value=self.request('GET','/api/config')
        self.assertEqual(status,403)
        self.assertEqual(value['code'],'SIGN_IN_REQUIRED')
        self.assertIn('Sign in',value['error'])
        self.assertEqual(self.engine.calls,[])
    def test_mutations_require_matching_csrf_and_chat_origin(self):
        value=self.signed_config()
        common={'X-Codegen-Subject':'manager','Origin':'https://chat.example'}
        status,rejected=self.request('POST','/api/conversations',{},**common)
        self.assertEqual(status,403)
        self.assertEqual(rejected['code'],'CSRF_MISMATCH')
        common['X-CSRF-Token']=value['csrf_token']
        status,rejected=self.request('POST','/api/conversations',{},**common)
        self.assertEqual(status,403)
        self.assertEqual(rejected['code'],'CSRF_MISMATCH')
        common.pop('X-CSRF-Token')
        common['X-Chat-CSRF']=value['csrf_token'];common['Origin']='https://unrelated.example'
        status,rejected=self.request('POST','/api/conversations',{},**common)
        self.assertEqual(status,403)
        self.assertEqual(rejected['code'],'ORIGIN_MISMATCH')
        self.assertEqual(self.engine.calls,[])
        common['Origin']='https://chat.example'
        status,_=self.request('POST','/api/conversations',{},**common)
        self.assertEqual(status,201)
        self.assertEqual(self.engine.calls,[('create',hashlib.sha256(b'manager').hexdigest())])
    def test_apply_is_an_explicit_post_with_the_exact_plan_id(self):
        value=self.signed_config();headers={'X-Codegen-Subject':'manager','X-Chat-CSRF':value['csrf_token'],'Origin':'https://chat.example'}
        status,_=self.request('POST','/api/conversations/example/apply',{'plan_id':'saved-plan','destructive_confirmation':'APPLY 12345678'},**headers)
        self.assertEqual(status,202)
        call=self.engine.calls[-1]
        self.assertEqual(call[0],'start');self.assertEqual(call[3],'apply')
        self.assertEqual(call[4],{'plan_id':'saved-plan','destructive_confirmation':'APPLY 12345678'})
    def test_json_content_type_and_object_are_required_before_dispatch(self):
        value=self.signed_config();headers={'X-Codegen-Subject':'manager','X-Chat-CSRF':value['csrf_token'],'Origin':'https://chat.example'}
        status,_=self.request('POST','/api/conversations',[],**headers)
        self.assertEqual(status,400);self.assertEqual(self.engine.calls,[])


if __name__=='__main__':unittest.main()
