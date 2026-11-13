import hashlib
import hmac
import json
import struct
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode
import access_broker as broker


class AccessBrokerTests(unittest.TestCase):
    token='12345:'+('x'*35)

    def signed(self):
        fields={'auth_date':str(broker.C.now()),'user':json.dumps({'id':81}), 'start_param':'psy_group'}
        key=hmac.new(b'WebAppData',self.token.encode(),hashlib.sha256).digest()
        fields['hash']=hmac.new(key,'\n'.join(k+'='+v for k,v in sorted(fields.items())).encode(),hashlib.sha256).hexdigest()
        return urlencode(fields)

    def test_signed_init_only_returns_minimal_context(self):
        result=broker.dispatch({'action':'init','init_data':self.signed()},self.token)
        self.assertEqual(result,{'status':200,'uid':81,'start_param':'psy_group'})
        self.assertEqual(broker.dispatch({'action':'init','init_data':self.signed()+'x'},self.token),{'status':401})

    def test_only_fixed_group_method_and_no_send_authority(self):
        https=MagicMock()
        response=https.return_value.getresponse.return_value
        response.status=200
        response.read.return_value=b'{"ok":true,"result":{"status":"member"}}'
        with patch.object(broker.http.client,'HTTPSConnection',https):
            self.assertEqual(broker.dispatch({'action':'member','uid':81},self.token),{'status':200,'member':True})
            for args in ({'action':'sendDocument','uid':81},{'action':'member','uid':81,'chat_id':123},
                         {'action':'member','uid':True}):
                self.assertEqual(broker.dispatch(args,self.token),{'status':400})
        https.assert_called_once_with('api.telegram.org',timeout=6)
        call=https.return_value.request.call_args
        self.assertEqual(call.args[0],'POST')
        self.assertTrue(call.args[1].endswith('/getChatMember'))
        self.assertEqual(json.loads(call.kwargs['body']),{'chat_id':broker.C.GROUP,'user_id':81})

    def test_peer_mismatch_reads_no_request_and_transient_membership_is_503(self):
        peer=MagicMock()
        peer.getsockopt.return_value=struct.pack('3i',111,98,5)
        with patch.object(broker.socket,'SO_PEERCRED',17,create=True):
            broker.serve(peer,99)
        peer.makefile.assert_not_called()
        self.assertEqual(broker.dispatch({'action':'member','uid':81},self.token,MagicMock(side_effect=TimeoutError())),
                         {'status':503})
