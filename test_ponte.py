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
    def tearDown(self):
        for p in self.patches:p.stop()
        self.tmp.cleanup()
    def put(self,path,text):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    def build(self,q='login',**kw):
        return ctx.build(self.v,self.s,'Seara/Food',q,**kw)
    def create_job(self,**updates):
        jid='a'*32
        data={'id':jid,'estado':'executando','provedor':'claude','pacote':self.build(),'prazo':time.time()+60,'criada':time.time(),'interativo':False}
        data.update(updates);ponte.write(ponte.job_path(jid)/'job.json',data)
        return jid
    def chamadas(self):
        return [json.loads(l) for l in (self.root/'state/chamadas.jsonl').read_text().splitlines()]

    # --- contexto
    def test_contexto_isolado_por_projeto(self):
        p=self.build(); self.assertNotIn('Outro',p['texto']);self.assertIn('login.md',p['texto'])
    def test_lembrete_consultor_sempre_presente(self):
        p=self.build()
        self.assertEqual(p['lembrete_consultor'], ctx.LEMBRETE_CONSULTOR)
        self.assertIn('consulta_iniciar', p['lembrete_consultor'])
        self.assertIn('Ptyxis', p['lembrete_consultor'])
        self.assertIn('Task', p['lembrete_consultor'])
        self.assertIn('Editor', p['lembrete_consultor'])
    def test_sem_correspondencia_so_obrigatorios_e_diario_do_projeto(self):
        p=self.build('zzzinexistente');self.assertNotIn('login.md',p['texto']);self.assertNotIn('Primor errado',p['texto'])
    def test_pergunta_vazia(self):
        with self.assertRaises(ValueError):self.build('')
    def test_acentos(self):
        self.assertEqual(ctx.terms('autorização'),ctx.terms('autorizacao'))
    def test_travessia(self):
        for path in ['../fora','/etc/passwd','Seara/../../fora']:
            with self.assertRaises(ValueError):ctx.inside(self.v,path)
    def test_link_simbolico(self):
        (self.project/'linked.md').symlink_to(self.v/'AGENTS.md')
        with self.assertRaises(ValueError):ctx.inside(self.v,'Seara/Food/linked.md')
    def test_segredo_excluido(self):
        self.put(self.project/'secret.md','-----BEGIN RSA PRIVATE KEY-----')
        self.assertNotIn('secret.md',self.build('secret')['texto'])
    def test_vpn_excluida(self):
        self.put(self.project/'VPN/test.md','login segredo')
        self.assertNotIn('VPN',self.build()['texto'])
    def test_consultas_nao_reingeridas(self):
        self.put(self.project/'consultas/past.md','login incorreto '*100)
        self.assertNotIn('past.md',self.build()['texto'])
    def test_repositorio_errado(self):
        (self.s/'Pessoais/Outro').mkdir(parents=True)
        with self.assertRaises(ValueError):self.build(repositorio='Pessoais/Outro')
    def test_codigo_explicito(self):
        self.put(self.s/'Seara/food/main.py','print(1)')
        p=self.build(repositorio='Seara/food',arquivos=['main.py'])
        self.assertIn('print(1)',p['texto'])
        self.assertEqual(p['fontes'][-1]['arquivo'],'Sites/Seara/food/main.py')
        self.assertIn('sujo',p['estado_git'])
    def test_codigo_nao_perde_espaco_para_notas(self):
        self.put(self.v/'AGENTS.md','Regras '*2000)
        self.put(self.v/'CLAUDE.md','Claude '*2000)
        self.put(self.project/'README.md','Food '*2000)
        self.put(self.project/'Repositorio/mapa-repositorio.md','mapa '*2000)
        self.put(self.s/'Seara/food/big.py','x'*25000)
        p=self.build(repositorio='Seara/food',arquivos=['big.py'])
        self.assertIn('Sites/Seara/food/big.py',[f['arquivo'] for f in p['fontes']])
        self.assertGreaterEqual(len(p['texto']),25000)
    def test_segredo_no_codigo(self):
        self.put(self.s/'Seara/food/config.py','API_KEY="sk-'+('x'*30)+'"')
        with self.assertRaises(ValueError):self.build(repositorio='Seara/food',arquivos=['config.py'])
    def test_arquivo_ausente_explica_o_argumento(self):
        with self.assertRaises(ValueError) as cm:self.build(repositorio='Seara/food',arquivos=['estado-da-aplicacao.md'])
        self.assertIn('estado-da-aplicacao.md',str(cm.exception))
        self.assertIn('relativos ao repositório',str(cm.exception))
    def test_fontes_alteradas(self):
        p=self.build();self.put(self.project/'login.md','alterado')
        self.assertIn('Valt/Seara/Food/login.md',ctx.stale(p,self.v,self.s))
    def test_fonte_removida_conta_como_alterada(self):
        p=self.build();(self.project/'login.md').unlink()
        self.assertTrue(ctx.stale(p,self.v,self.s))

    # --- ciclo da consulta
    def test_id_consulta_invalido(self):
        with self.assertRaises(ValueError):ponte.job_path('../bad')
    def test_expiracao(self):
        jid=self.create_job(prazo=time.time()-1)
        self.assertEqual(ponte.status(jid)['estado'],'expirada')
    def test_estado_final_imutavel(self):
        jid=self.create_job(estado='cancelada');ponte.mark(jid,estado='concluida')
        self.assertEqual(ponte.status(jid)['estado'],'cancelada')
    def test_registro_no_valt_e_retorno(self):
        jid=self.create_job();ponte.finish(jid,'Veredito: ok')
        response=ponte.status(jid)
        self.assertEqual(response['parecer'],'Veredito: ok')
        self.assertEqual(response['registro'],'~/Valt/Seara/Food/consultas/'+jid+'.md')
        self.assertTrue((self.project/'consultas'/f'{jid}.md').is_file())
        self.assertEqual(response['fontes_alteradas'],[])
        self.assertNotIn('pacote',response)
    def test_cancelamento_impede_registro(self):
        jid=self.create_job(estado='cancelada');ponte.finish(jid,'ok')
        self.assertFalse((self.project/'consultas').exists())
    def test_comando_sem_interpolacao_de_shell(self):
        cmd=ponte.provider_command('claude')
        self.assertEqual(cmd[cmd.index('--tools')+1],'')
        self.assertIn('--ignore-user-config',ponte.provider_command('codex'))
    def test_argumentos_invalidos(self):
        with self.assertRaises(ValueError):ponte.dispatch('consulta_status',{'id_consulta':'a'*32,'extra':True})
        with self.assertRaises(ValueError):ponte.dispatch('consulta_status',{'job_id':'a'*32})
    def test_protocolo(self):
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05'}},
                  {'jsonrpc':'2.0','method':'notifications/initialized'},
                  {'jsonrpc':'2.0','id':2,'method':'tools/list'},
                  {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'contexto_valt','arguments':{'projeto':'Seara/Food','pergunta':'login'}}}]
        output=io.StringIO()
        with patch.object(sys,'stdin',io.StringIO('\n'.join(map(json.dumps,messages)))),patch.object(sys,'stdout',output):ponte.serve()
        responses=list(map(json.loads,output.getvalue().splitlines()))
        self.assertEqual(len(responses),3)
        tools=responses[1]['result']['tools']
        self.assertEqual(len(tools),4)
        self.assertEqual(set(tools[1]['inputSchema']['required']),{'projeto','pergunta','provedor','id_pedido'})
        self.assertIn('Ptyxis', tools[1]['description'])
        self.assertIn('Task', tools[1]['description'])
        self.assertIn('não contam', tools[1]['description'])
        self.assertNotIn('isError',responses[2]['result'])
        self.assertIn('"fontes"',responses[2]['result']['content'][0]['text'])
        self.assertIn('lembrete_consultor', responses[2]['result']['content'][0]['text'])
    def test_abertura_e_idempotencia(self):
        with patch('ponte.shutil.which',return_value='/fake'),patch.dict(os.environ,{'DISPLAY':':0'}),patch('ponte.subprocess.Popen') as popen:
            first=ponte.start('Seara/Food','login','claude',id_pedido='r1')
            again=ponte.start('Seara/Food','login','claude',id_pedido='r1')
            self.assertEqual(first['id'],again['id']);self.assertEqual(popen.call_count,1)
            self.assertEqual(first['estado'],'abrindo')
            self.assertIn('--',popen.call_args.args[0])
    def test_falha_na_abertura(self):
        with patch('ponte.shutil.which',return_value='/fake'),patch.dict(os.environ,{'DISPLAY':':0'}),patch('ponte.subprocess.Popen',side_effect=OSError('no display')):
            self.assertEqual(ponte.start('Seara/Food','login','claude',id_pedido='r1')['estado'],'falhou')
    def test_dono_morto_cancela_sem_deadlock(self):
        jid=self.create_job(pid_dono=999999999)
        self.assertEqual(ponte.mark(jid,estado='executando')['estado'],'cancelada')
        self.assertEqual(ponte.status(jid)['estado'],'cancelada')
    def test_dono_inacessivel_nao_cancela(self):
        jid=self.create_job(pid_dono=1)
        with patch('ponte.os.kill',side_effect=PermissionError):
            self.assertEqual(ponte.mark(jid,estado='executando')['estado'],'executando')
    def test_abertura_sem_dono_ate_o_worker(self):
        with patch('ponte.shutil.which',return_value='/fake'),patch.dict(os.environ,{'DISPLAY':':0'}),patch('ponte.subprocess.Popen'):
            data=ponte.start('Seara/Food','login','claude',id_pedido='r-worker')
            job=ponte.read_job(data['id'])
            self.assertIsNone(job['pid_dono'])
            self.assertEqual(job['estado'],'abrindo')
    def test_provedor_falso_ponta_a_ponta(self):
        jid=self.create_job()
        with patch('ponte.provider_command',return_value=[sys.executable,'-c','import sys; p=sys.stdin.read(); print("Veredito: contexto recebido" if p else "")']):
            ponte.worker(jid) # só consultas em 'abrindo' rodam
            ponte.mark(jid,estado='abrindo')
            ponte.worker(jid)
        self.assertEqual(ponte.status(jid)['estado'],'concluida')
    def test_provedor_falso_com_erro(self):
        jid=self.create_job(estado='abrindo')
        with patch('ponte.provider_command',return_value=[sys.executable,'-c','raise SystemExit(2)']):ponte.worker(jid)
        self.assertEqual(ponte.status(jid)['estado'],'falhou')
    def test_erro_de_cota_traduzido(self):
        jid=self.create_job(estado='abrindo')
        with patch('ponte.provider_command',return_value=[sys.executable,'-c','import sys; sys.stderr.write("Error: usage_limit_exceeded\\n"); raise SystemExit(1)']):ponte.worker(jid)
        self.assertEqual(ponte.status(jid)['erro'],'Cota do provedor esgotada; aguarde ou troque o provedor')

    # --- novidades: tradução de erros, registro de chamadas, janela
    def test_traduzir_erro(self):
        self.assertIn('Cota',ponte.traduzir_erro('You have hit your usage limit'))
        self.assertIn('login',ponte.traduzir_erro('Not logged in. Please run /login'))
        self.assertIn('Flag',ponte.traduzir_erro("error: unknown option '--restricted'"))
        self.assertIn('consulte o terminal',ponte.traduzir_erro('segmentation fault'))
    def test_registro_de_chamadas(self):
        ponte.dispatch('contexto_valt',{'projeto':'Seara/Food','pergunta':'login'})
        with self.assertRaises(ValueError):ponte.dispatch('contexto_valt',{'projeto':'Seara/Food','pergunta':'login','arquivos':['x.md']})
        linhas=self.chamadas()
        self.assertEqual([l['ok'] for l in linhas],[True,False])
        self.assertEqual(linhas[0]['ferramenta'],'contexto_valt');self.assertEqual(linhas[0]['projeto'],'Seara/Food')
        self.assertIn('repositório explícito',linhas[1]['mensagem'])
    def test_janela_nao_trava_sem_tty(self):
        with patch.object(sys,'stdin',io.StringIO('')):
            ponte.segurar_janela()  # sem tty: retorna na hora
        class Tty(io.StringIO):
            def isatty(self): return True
        with patch.object(sys,'stdin',Tty('')),patch('builtins.input',side_effect=EOFError):
            ponte.segurar_janela()  # tty fechado: não explode

    # --- F1: robustez
    def abrir(self,projeto='Seara/Food',pedido='r1',**kw):
        with patch('ponte.shutil.which',return_value='/fake'),patch.dict(os.environ,{'DISPLAY':':0'}),patch.object(ponte,'subprocess'):
            return ponte.start(projeto,'login','claude',id_pedido=pedido,**kw)
    def test_saida_do_mcp_nao_cancela_consulta(self):
        self.assertFalse(hasattr(ponte,'OWNED'))
        codigo=Path(ponte.__file__).read_text()
        self.assertNotIn("estado='cancelada',erro='Sessão MCP encerrada'",codigo)
    def test_abrindo_sem_executor_falha_em_60s(self):
        jid=self.create_job(estado='abrindo',criada=time.time()-61)
        resposta=ponte.status(jid)
        self.assertEqual(resposta['estado'],'falhou');self.assertIn('60 s',resposta['erro'])
    def test_abrindo_preso_nao_trava_nova_consulta(self):
        self.create_job(estado='abrindo',criada=time.time()-61)
        self.assertEqual(self.abrir(pedido='r2')['estado'],'abrindo')
    def test_trava_por_projeto(self):
        self.put(self.v/'Jaiminho/README.md','Jaiminho')
        self.abrir()
        self.assertEqual(self.abrir('Jaiminho','r-outro')['estado'],'abrindo')
        with self.assertRaises(ValueError) as cm:self.abrir(pedido='r3')
        self.assertIn('Seara/Food',str(cm.exception))
    def test_interativo_e_modo_padrao(self):
        job=ponte.read_job(self.abrir()['id'])
        self.assertFalse(job['interativo']);self.assertEqual(job['modo'],'investigador')
        props=ponte.TOOLS[1]['inputSchema']['properties']
        self.assertFalse(props['interativo']['default']);self.assertEqual(props['modo']['default'],'investigador')
    def test_log_guarda_id_da_consulta_iniciada(self):
        with patch('ponte.shutil.which',return_value='/fake'),patch.dict(os.environ,{'DISPLAY':':0'}),patch('ponte.subprocess.Popen'):
            data=ponte.dispatch('consulta_iniciar',{'projeto':'Seara/Food','pergunta':'login','provedor':'claude','id_pedido':'r9'})
        linha=self.chamadas()[-1]
        self.assertEqual(linha['id_consulta'],data['id'][:8]);self.assertEqual(linha['estado'],'abrindo')
        self.assertEqual(linha['provedor'],'claude')
    def test_executor_registra_inicio_e_fim(self):
        jid=self.create_job(estado='abrindo')
        with patch('ponte.provider_command',return_value=[sys.executable,'-c','print("Veredito: ok")']):ponte.worker(jid)
        eventos=[l['ferramenta'] for l in self.chamadas()]
        self.assertEqual(eventos,['executor_inicio','executor_fim'])
        self.assertEqual(self.chamadas()[-1]['estado'],'concluida')
    def test_erro_de_projeto_sugere_pasta_pai(self):
        (self.project/'sem-readme').mkdir()
        with self.assertRaises(ValueError) as cm:ctx.build(self.v,self.s,'Seara/Food/sem-readme','login')
        self.assertIn("use projeto 'Seara/Food'",str(cm.exception))
    def test_initialize_tem_instrucoes(self):
        output=io.StringIO()
        with patch.object(sys,'stdin',io.StringIO(json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{}}))),patch.object(sys,'stdout',output):ponte.serve()
        self.assertIn('Anunciar a consulta não conta',json.loads(output.getvalue())['result']['instructions'])
    def test_contexto_resumido_por_padrao(self):
        resumo=ponte.dispatch('contexto_valt',{'projeto':'Seara/Food','pergunta':'login'})
        self.assertNotIn('texto',resumo);self.assertIn('texto_omitido',resumo);self.assertTrue(resumo['fontes'])
        completo=ponte.dispatch('contexto_valt',{'projeto':'Seara/Food','pergunta':'login','completo':True})
        self.assertIn('login.md',completo['texto'])

    # --- F2: investigador
    def test_comando_investigador_claude_so_leitura(self):
        cmd=ponte.provider_command('claude','investigador',Path('/r'),Path('/v/p'),Path('/s'))
        self.assertEqual(cmd[cmd.index('--tools')+1],'Read,Grep,Glob')
        self.assertIn('--restricted',cmd);self.assertIn('--strict-mcp-config',cmd)
        self.assertEqual(cmd[cmd.index('--add-dir')+1],'/v/p')
        self.assertNotIn('Bash',' '.join(cmd))
    def test_comando_investigador_codex_sandbox(self):
        cmd=ponte.provider_command('codex','investigador',Path('/r'),Path('/v/p'),Path('/s/parecer.md'))
        self.assertEqual(cmd[cmd.index('--sandbox')+1],'read-only')
        self.assertEqual(cmd[cmd.index('-C')+1],'/r');self.assertEqual(cmd[cmd.index('-o')+1],'/s/parecer.md')
        self.assertNotIn('danger',' '.join(cmd))
    def test_leitor_claude(self):
        leitor=ponte.Leitor('claude','investigador',time.monotonic())
        eventos=[{'type':'assistant','message':{'content':[{'type':'tool_use','name':'Read','input':{'file_path':'/r/a.sql'}}]}},
                 {'type':'assistant','message':{'content':[{'type':'tool_use','name':'Grep','input':{'pattern':'auth.users'}}]}},
                 {'type':'result','result':'Veredito: ok','is_error':False}]
        with patch('builtins.print'):
            for e in eventos:leitor.linha((json.dumps(e)+'\n').encode())
        self.assertEqual(leitor.resultado,'Veredito: ok');self.assertEqual(leitor.lidos,['/r/a.sql'])
    def test_leitor_codex(self):
        leitor=ponte.Leitor('codex','investigador',time.monotonic())
        with patch('builtins.print') as saida:
            leitor.linha(json.dumps({'type':'item.started','item':{'type':'command_execution','command':"/bin/bash -lc 'rg auth'"}}).encode())
        self.assertIn('rodando rg auth',saida.call_args.args[0]);self.assertEqual(len(leitor.lidos),1)
    def test_prompt_investigador(self):
        self.put(self.s/'Seara/food/main.py','print(1)')
        jid=self.create_job(modo='investigador',arquivos=['main.py'],pacote=self.build(repositorio='Seara/food',arquivos=['main.py']))
        prompt=ponte.prompt_inicial(ponte.read_job(jid))
        self.assertIn('SÓ LENDO',prompt);self.assertIn('- main.py',prompt)
        self.assertIn(str(self.project/'login.md'),prompt);self.assertIn('arquivo:linha',prompt)
        self.assertNotIn('Login exige email',prompt)  # notas vão por caminho, não por texto
    def test_investigador_ponta_a_ponta(self):
        jid=self.create_job(estado='abrindo',modo='investigador')
        fluxo=[{'type':'assistant','message':{'content':[{'type':'tool_use','name':'Read','input':{'file_path':'login.md'}}]}},
               {'type':'result','result':'Veredito: investigado','is_error':False}]
        script='import sys,json; sys.stdin.read(); ['+','.join(f'print({json.dumps(json.dumps(e))})' for e in fluxo)+']'
        with patch('ponte.provider_command',return_value=[sys.executable,'-c',script]),patch('builtins.print'):ponte.worker(jid)
        resposta=ponte.status(jid)
        self.assertEqual(resposta['estado'],'concluida');self.assertEqual(resposta['parecer'],'Veredito: investigado')
        registro=(self.project/'consultas'/f'{jid}.md').read_text()
        self.assertIn('Arquivos lidos pelo consultor',registro);self.assertIn('login.md',registro)
    def test_registro_sem_nome_de_usuario(self):
        jid=self.create_job(modo='investigador')
        ponte.finish(jid,'ok em '+str(Path.home()/'Sites/x/b.py:3'),[str(Path.home()/'Sites/x/a.py')])
        registro=(self.project/'consultas'/f'{jid}.md').read_text()
        self.assertIn('~/Sites/x/a.py',registro);self.assertIn('~/Sites/x/b.py:3',registro);self.assertNotIn(str(Path.home()),registro)
    def test_investigador_resultado_com_erro(self):
        jid=self.create_job(estado='abrindo',modo='investigador')
        script='import sys,json; sys.stdin.read(); print(json.dumps({"type":"result","result":"usage limit reached","is_error":True}))'
        with patch('ponte.provider_command',return_value=[sys.executable,'-c',script]),patch('builtins.print'):ponte.worker(jid)
        self.assertIn('Cota',ponte.status(jid)['erro'])

    def test_investigador_aceita_arquivo_grande_com_hash(self):
        self.put(self.s/'Seara/food/grande.sql','select 1;\n'*8000)  # 80 KB: estoura o orçamento do modo parecer
        with self.assertRaises(ValueError):self.abrir(pedido='p-grande',modo='parecer',repositorio='Seara/food',arquivos=['grande.sql'])
        job=ponte.read_job(self.abrir(pedido='i-grande',repositorio='Seara/food',arquivos=['grande.sql'])['id'])
        fonte=[f for f in job['pacote']['fontes'] if f['arquivo']=='Sites/Seara/food/grande.sql'][0]
        self.assertEqual(fonte['sha256'],__import__('hashlib').sha256((self.s/'Seara/food/grande.sql').read_bytes()).hexdigest())
        self.assertNotIn('select 1;',job['pacote']['texto'])
    def test_investigador_ainda_barra_segredo_e_travessia(self):
        self.put(self.s/'Seara/food/config.py','API_KEY="sk-'+('x'*30)+'"')
        with self.assertRaises(ValueError):self.abrir(pedido='i-seg',repositorio='Seara/food',arquivos=['config.py'])
        with self.assertRaises(ValueError):self.abrir(pedido='i-trav',repositorio='Seara/food',arquivos=['../../fora.py'])

if __name__=='__main__':unittest.main()
