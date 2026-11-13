import json, unittest, time
from unittest.mock import patch, Mock
from contextlib import nullcontext
from psy_modules import psy_group_codex as m


class Incident(unittest.TestCase):
    def test_expired_reset_is_not_announced(self):
        self.assertNotIn("по Москве",m.ModelIssue("quota","http",time.time()-1).public())

    def test_model_decides_even_when_included_window_is_full(self):
        import sys
        from types import ModuleType
        response=Mock(status_code=200)
        response.iter_lines.return_value=[
            'data: {"type":"response.output_item.done","item":{"type":"message","content":[{"type":"output_text","text":"ok"}]}}',
            'data: {"type":"response.completed"}']
        client=Mock()
        client.get.return_value.json.return_value={"rate_limit":{"allowed":False,"secondary_window":{"used_percent":100}},
                                                  "credits":{"has_credits":True}}
        client.stream.return_value=nullcontext(response)
        from unittest.mock import MagicMock
        httpx=MagicMock()
        httpx.Client.return_value=nullcontext(client)
        headers=ModuleType("agent.codex_headers")
        headers.codex_cloudflare_headers=lambda token:{}
        bootstrap=ModuleType("agent.process_bootstrap")
        bootstrap.enable_happy_eyeballs_on_client=lambda c:None
        modules={"httpx":httpx,"agent.codex_headers":headers,"agent.process_bootstrap":bootstrap}
        with patch.dict(sys.modules,modules), patch.object(m,"_credentials",return_value={
                "provider":"openai-codex","base_url":m.ENDPOINT,"api_key":"test-only", "generation":"fixture-generation"}):
            self.assertEqual(m._respond([{"role":"user","content":"test"}],research=False,draw=False,model="test").text,"ok")
        client.get.assert_not_called()
        self.assertEqual(client.stream.call_args.args,("POST",m.ENDPOINT+"/responses"))
        self.assertEqual(client.stream.call_count,1)

    def test_stream_and_http_quota(self):
        for status in (200,429):
            client=Mock()
            err={"code":"usage_limit_reached","message":"sensitive"}
            resp=Mock(status_code=status)
            client.stream.return_value=nullcontext(resp)
            resp.iter_bytes.return_value=[json.dumps({"error":err}).encode()]
            resp.iter_lines.return_value=['data: '+json.dumps({"type":"response.failed","response":{"error":err}})]
            with self.assertRaises(m.ModelIssue) as caught:m._stream(client,{},[],False,"m")
            self.assertEqual(caught.exception.kind,"quota")
            self.assertNotIn("sensitive",caught.exception.public())
        self.assertEqual(m._failure(429,{},"http").kind,"busy")
        self.assertEqual(m._failure(401,{},"http").kind,"signin")
