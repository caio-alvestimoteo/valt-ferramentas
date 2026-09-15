import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import ponte
import ponte_contexto as ctx

class PonteTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.v=self.root/'Valt'; self.s=self.root/'Sites'
        self.project=self.v/'Seara/Food'; self.project.mkdir(parents=True)
        (self.s/'Seara/food').mkdir(parents=True)
        self.put(self.project/'README.md','Food: decisões vigentes de login')
        self.put(self.project/'login.md','Login exige email e senha; validar erros.')
        self.put(self.v/'AGENTS.md','Regras globais do Valt')
        self.put(self.v/'indices/repositorios.md','| `~/Sites/Seara/food` | ../Seara/Food/README.md |')
        self.put(self.v/'Pessoais/Outro/README.md','login '*100)
        self.put(self.v/'Seara/continuidade/dataehoradaultimaatualizacao.md','## Hoje\nPrimor errado\n## Ontem\nFood login corrigido')
        self.patches=[patch.object(ponte,'VAULT',self.v),patch.object(ponte,'SITES',self.s),patch.object(ponte,'STATE',self.root/'state')]
        for p in self.patches:p.start()
        ponte.OWNED.clear()
    def tearDown(self):
        for p in self.patches:p.stop()
        self.tmp.cleanup()
    def put(self,path,text):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    def build(self,q='login',**kw):
        return ctx.build(self.v,self.s,'Seara/Food',q,**kw)
    def create_job(self,**updates):
        jid='a'*32
        data={'id':jid,'status':'running','provider':'claude','packet':self.build(),'deadline':time.time()+60,'interactive':False}
        data.update(updates);ponte.write(ponte.job_path(jid)/'job.json',data)
        return jid
    def test_context_project_isolated(self):
        p=self.build(); self.assertNotIn('Outro',p['text']);self.assertIn('login.md',p['text'])
    def test_no_match_adds_only_mandatory_and_scoped_diary(self):
        p=self.build('zzzinexistente');self.assertNotIn('login.md',p['text']);self.assertNotIn('Primor errado',p['text'])
    def test_empty_query(self):
        with self.assertRaises(ValueError):self.build('')
    def test_accents(self):
        self.assertEqual(ctx.terms('autorização'),ctx.terms('autorizacao'))
    def test_traversal(self):
        for path in ['../fora','/etc/passwd','Seara/../../fora']:
            with self.assertRaises(ValueError):ctx.inside(self.v,path)
    def test_symlink(self):
        (self.project/'linked.md').symlink_to(self.v/'AGENTS.md')
        with self.assertRaises(ValueError):ctx.inside(self.v,'Seara/Food/linked.md')
    def test_secret_excluded(self):
        self.put(self.project/'secret.md','-----BEGIN RSA PRIVATE KEY-----')
        self.assertNotIn('secret.md',self.build('secret')['text'])
    def test_vpn_excluded(self):
        self.put(self.project/'VPN/test.md','login segredo')
        self.assertNotIn('VPN',self.build()['text'])
    def test_consultations_not_reingested(self):
        self.put(self.project/'consultas/past.md','login incorreto '*100)
        self.assertNotIn('past.md',self.build()['text'])
    def test_wrong_repo(self):
        (self.s/'Pessoais/Outro').mkdir(parents=True)
        with self.assertRaises(ValueError):self.build(repo='Pessoais/Outro')
    def test_explicit_code(self):
        self.put(self.s/'Seara/food/main.py','print(1)')
        self.assertIn('print(1)',self.build(repo='Seara/food',files=['main.py'])['text'])
    def test_code_secret(self):
        self.put(self.s/'Seara/food/config.py','API_KEY="sk-'+('x'*30)+'"')
        with self.assertRaises(ValueError):self.build(repo='Seara/food',files=['config.py'])
    def test_context_staleness(self):
        p=self.build();self.put(self.project/'login.md','alterado')
        self.assertIn('Valt/Seara/Food/login.md',ctx.stale(p,self.v,self.s))
    def test_missing_source_staleness(self):
        p=self.build();(self.project/'login.md').unlink()
        self.assertTrue(ctx.stale(p,self.v,self.s))
    def test_invalid_job_id(self):
        with self.assertRaises(ValueError):ponte.job_path('../bad')
    def test_expiration(self):
        jid=self.create_job(deadline=time.time()-1)
        self.assertEqual(ponte.status(jid)['status'],'expired')
    def test_final_state_immutable(self):
        jid=self.create_job(status='cancelled');ponte.mark(jid,status='completed')
        self.assertEqual(ponte.status(jid)['status'],'cancelled')
    def test_handoff_in_vault_and_return(self):
        jid=self.create_job();ponte.finish(jid,'Veredito: ok')
        response=ponte.status(jid)
        self.assertEqual(response['answer'],'Veredito: ok')
        self.assertTrue((self.project/'consultas'/f'{jid}.md').is_file())
        self.assertEqual(response['changed_sources'],[])
    def test_cancel_prevents_handoff(self):
        jid=self.create_job(status='cancelled');ponte.finish(jid,'ok')
        self.assertFalse((self.project/'consultas').exists())
    def test_command_no_shell_interpolation(self):
        cmd=ponte.provider_command('claude')
        self.assertEqual(cmd[cmd.index('--tools')+1],'')
        self.assertIn('--ignore-user-config',ponte.provider_command('codex'))
    def test_bad_tool_args(self):
        with self.assertRaises(ValueError):ponte.dispatch('consulta_status',{'job_id':'a'*32,'extra':True})
    def test_protocol(self):
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05'}},
                  {'jsonrpc':'2.0','method':'notifications/initialized'},
                  {'jsonrpc':'2.0','id':2,'method':'tools/list'},
                  {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'contexto_valt','arguments':{'project':'Seara/Food','question':'login'}}}]
        output=io.StringIO()
        with patch.object(sys,'stdin',io.StringIO('\n'.join(map(json.dumps,messages)))),patch.object(sys,'stdout',output):ponte.serve()
        responses=list(map(json.loads,output.getvalue().splitlines()))
        self.assertEqual(len(responses),3)
        self.assertEqual(len(responses[1]['result']['tools']),4)
        self.assertNotIn('isError',responses[2]['result'])
    def test_launch_and_idempotence(self):
        with patch('ponte.shutil.which',return_value='/fake'),patch.dict(os.environ,{'DISPLAY':':0'}),patch('ponte.subprocess.Popen') as popen:
            first=ponte.start('Seara/Food','login','claude',request_id='r1')
            again=ponte.start('Seara/Food','login','claude',request_id='r1')
            self.assertEqual(first['id'],again['id']);self.assertEqual(popen.call_count,1)
            self.assertIn('--',popen.call_args.args[0])
    def test_launch_failure(self):
        with patch('ponte.shutil.which',return_value='/fake'),patch.dict(os.environ,{'DISPLAY':':0'}),patch('ponte.subprocess.Popen',side_effect=OSError('no display')):
            self.assertEqual(ponte.start('Seara/Food','login','claude',request_id='r1')['status'],'failed')
    def test_dead_owner_cancels_without_deadlock(self):
        jid=self.create_job(owner_pid=999999999)
        self.assertEqual(ponte.mark(jid,status='running')['status'],'cancelled')
        self.assertEqual(ponte.status(jid)['status'],'cancelled')
    def test_fake_provider_end_to_end(self):
        jid=self.create_job()
        with patch('ponte.provider_command',return_value=[sys.executable,'-c','import sys; p=sys.stdin.read(); print("Veredito: contexto recebido" if p else "")']):
            ponte.worker(jid) # only opening jobs are allowed
            ponte.mark(jid,status='opening')
            ponte.worker(jid)
        self.assertEqual(ponte.status(jid)['status'],'completed')
    def test_fake_provider_error(self):
        jid=self.create_job(status='opening')
        with patch('ponte.provider_command',return_value=[sys.executable,'-c','raise SystemExit(2)']):ponte.worker(jid)
        self.assertEqual(ponte.status(jid)['status'],'failed')

if __name__=='__main__':unittest.main()
