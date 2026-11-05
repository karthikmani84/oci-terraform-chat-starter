"""Malformed model envelopes produce a clear message rather than Python type errors."""
import json
from types import SimpleNamespace as NS
import unittest

from oci.generative_ai_inference import models as m
from app.model import Model, SYSTEM


class ModelShapeTests(unittest.TestCase):
    def test_invalid_response_envelopes_are_rejected_without_type_errors(self):
        for value in ([],None,7,{'kind':{'draft':True},'message':'Delete NLB.'},
                      {'kind':['draft'],'message':'Delete NLB.'},
                      {'kind':'draft','message':{}}):
            with self.subTest(value=value):
                calls=[]
                class Client:
                    def chat(self,details):
                        calls.append(details)
                        msg=m.AssistantMessage(content=[m.TextContent(text=json.dumps(value))])
                        return NS(data=m.ChatResult(chat_response=m.GenericChatResponse(
                            choices=[m.ChatChoice(message=msg,finish_reason='STOP')])) )
                with self.assertRaisesRegex(ValueError,'model returned an incomplete response'):
                    Model(Client(),{'compartment_id':'ocid1.compartment.managed','region':'me-dubai-1'},None).respond({'messages':[]})
                self.assertEqual(len(calls),1)

    def test_prompt_specifies_removal_addresses_and_creation_metadata(self):
        self.assertIn('Every remove entry must be an address STRING',SYSTEM)
        self.assertIn('exact address',SYSTEM)
        self.assertIn('time_created',SYSTEM)
        self.assertIn('timestamps are missing or tied',SYSTEM)


if __name__=='__main__':unittest.main()
