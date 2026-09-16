import hashlib
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import ponte_hook as hook

MIGRACAO = 'create policy dono on agenda.aulas using (auth.uid() = dono);\ngrant select on agenda.aulas to authenticated;\n'

class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raiz = Path(self.tmp.name)
        self.v, self.s, self.state = raiz/'Valt', raiz/'Sites', raiz/'state'
        self.repo = self.s/'Pessoais/lab'
        (self.repo/'supabase/migrations').mkdir(parents=True)
        self.put(self.v/'indices/repositorios.md',
                 '| lab | `~/Sites/Pessoais/lab` | [`../Pessoais/lab/README.md`](../Pessoais/lab/README.md) | teste |\n')
        self.put(self.v/'Pessoais/lab/README.md', 'lab')
        self.git('init', '-q'); self.git('config', 'user.email', 't@t'); self.git('config', 'user.name', 't')
        self.put(self.repo/'README.md', 'x'); self.git('add', '.'); self.git('commit', '-qm', 'base')
        self.patches = [patch.object(hook, 'VAULT', self.v), patch.object(hook, 'SITES', self.s), patch.object(hook, 'STATE', self.state)]
        for p in self.patches:
            p.start()
        self.state.mkdir()
    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()
    def put(self, path, texto):
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(texto)
    def git(self, *args):
        subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True)
    def evento(self, nome, **entrada):
        entrada.setdefault('conversation_id', 'c1')
        return hook.processar(nome, json.dumps(entrada))
    def shell(self, comando):
        return self.evento('beforeShellExecution', command=comando, cwd=str(self.repo))
    def stage_migracao(self, texto=MIGRACAO, nome='supabase/migrations/001_aulas.sql'):
        self.put(self.repo/nome, texto); self.git('add', nome)
        return nome, hashlib.sha256(texto.encode()).hexdigest()
    def job(self, estado='concluida', fontes=(), repositorio='Pessoais/lab', criada=None):
        jid = hashlib.md5(str(time.time_ns()).encode()).hexdigest()
        data = {'id': jid, 'estado': estado, 'criada': criada or time.time(),
                'pacote': {'projeto': 'Pessoais/lab', 'repositorio': repositorio, 'fontes': list(fontes)}}
        self.put(self.state/jid/'job.json', json.dumps(data))
        return jid
    def chamadas(self):
        arquivo = self.state/'chamadas.jsonl'
        return [json.loads(l) for l in arquivo.read_text().splitlines()] if arquivo.exists() else []

    # --- migração
    def test_commit_com_migracao_sensivel_e_negado(self):
        self.stage_migracao()
        r = self.shell('git commit -m "aulas"')
        self.assertEqual(r['permission'], 'deny')
        self.assertIn('consulta_iniciar', r['agent_message'])
        chamada = json.loads(r['agent_message'].split('argumentos:\n')[1].split('\n')[0])
        self.assertEqual(chamada['arquivos'], ['supabase/migrations/001_aulas.sql'])
        self.assertEqual(chamada['projeto'], 'Pessoais/lab'); self.assertEqual(chamada['repositorio'], 'Pessoais/lab')
        self.assertEqual(chamada['modo'], 'investigador'); self.assertFalse(chamada['interativo'])
        self.assertEqual(self.chamadas()[-1]['ferramenta'], 'hook_negou')
    def test_commit_liberado_com_consulta_do_mesmo_conteudo(self):
        nome, sha = self.stage_migracao()
        self.job(fontes=[{'arquivo': 'Sites/Pessoais/lab/'+nome, 'sha256': sha}])
        self.assertEqual(self.shell('git commit -m "aulas"'), {})
    def test_consulta_de_versao_antiga_nao_cobre(self):
        nome, sha = self.stage_migracao()
        self.job(fontes=[{'arquivo': 'Sites/Pessoais/lab/'+nome, 'sha256': sha}])
        self.stage_migracao(MIGRACAO+'grant all on agenda.aulas to anon;\n')
        self.assertEqual(self.shell('git commit -m "aulas"')['permission'], 'deny')
    def test_consulta_nao_concluida_nao_cobre(self):
        nome, sha = self.stage_migracao()
        self.job(estado='executando', fontes=[{'arquivo': 'Sites/Pessoais/lab/'+nome, 'sha256': sha}])
        self.assertEqual(self.shell('git commit -m "aulas"')['permission'], 'deny')
    def test_migracao_inocente_passa(self):
        self.stage_migracao('create table agenda.salas (id int);\n')
        self.assertEqual(self.shell('git commit -m "salas"'), {})
    def test_migracao_longa_e_sensivel(self):
        self.stage_migracao('select 1;\n'*301)
        self.assertEqual(self.shell('git commit -m "grande"')['permission'], 'deny')
    def test_commit_sem_migracao_passa(self):
        self.put(self.repo/'app.ts', 'export const x = 1'); self.git('add', 'app.ts')
        self.assertEqual(self.shell('git commit -m "app"'), {})
    def test_commit_all_ve_arquivo_modificado_fora_do_stage(self):
        nome, _ = self.stage_migracao('create table agenda.salas (id int);\n')
        self.git('commit', '-qm', 'salas')
        self.put(self.repo/nome, MIGRACAO)
        self.assertEqual(self.shell('git commit -am "muda"')['permission'], 'deny')
    def test_db_push_com_migracao_sensivel(self):
        self.stage_migracao(); self.git('commit', '-qm', 'aulas')
        self.assertEqual(self.shell('supabase db push')['permission'], 'deny')
    def test_repositorio_fora_do_indice_passa(self):
        outro = self.s/'Pessoais/fora'; outro.mkdir(parents=True)
        subprocess.run(['git', '-C', str(outro), 'init', '-q'], check=True)
        self.put(outro/'x.sql', MIGRACAO); subprocess.run(['git', '-C', str(outro), 'add', '.'], check=True)
        self.assertEqual(self.evento('beforeShellExecution', command='git commit -m x', cwd=str(outro)), {})
    def test_indice_com_link_para_mapa_do_repositorio(self):
        self.put(self.v/'indices/repositorios.md',
                 '| lab | `~/Sites/Pessoais/lab` | [`../Pessoais/lab/Repositorio/mapa-repositorio.md`](../Pessoais/lab/Repositorio/mapa-repositorio.md) | x |\n')
        self.put(self.v/'Pessoais/lab/Repositorio/mapa-repositorio.md', 'mapa')
        self.assertEqual(hook.indice(), {'Pessoais/lab': 'Pessoais/lab'})
        self.stage_migracao()
        self.assertEqual(self.shell('git commit -m x')['permission'], 'deny')
    def test_comando_comum_passa(self):
        self.stage_migracao()
        self.assertEqual(self.shell('git status --short'), {})
        self.assertEqual(self.shell('ls -la'), {})

    # --- erro repetido
    def falha_tsc(self, erro='src/x.ts(3,5): error TS2322: Type string is not assignable to number.'):
        self.uso = getattr(self, 'uso', 0) + 1
        return self.evento('postToolUseFailure', tool_name='Shell', tool_use_id=f'u{self.uso}', cwd=str(self.repo),
                           tool_input={'command': 'npx tsc --noEmit', 'cwd': str(self.repo)}, error_message=erro+'\nFound 1 error in 0.8s')
    def test_terceira_tentativa_com_mesmo_erro_e_negada(self):
        self.assertEqual(self.shell('npx tsc --noEmit'), {})
        self.falha_tsc(); self.assertEqual(self.shell('npx tsc --noEmit'), {})
        self.falha_tsc()
        r = self.shell('npx tsc --noEmit')
        self.assertEqual(r['permission'], 'deny')
        self.assertIn('error TS2322', r['agent_message']); self.assertIn('src/x.ts', r['agent_message'])
    def test_erros_diferentes_nao_contam_juntos(self):
        self.falha_tsc('a.ts(1,1): error TS2322: um'); self.falha_tsc('b.ts(1,1): error TS2554: outro')
        self.assertEqual(self.shell('npx tsc --noEmit'), {})
    def test_consulta_depois_do_segundo_erro_libera(self):
        self.falha_tsc(); self.falha_tsc()
        self.job(criada=time.time()+1)
        self.assertEqual(self.shell('npx tsc --noEmit'), {})
    def test_erro_repetido_em_outra_conversa_nao_conta(self):
        self.falha_tsc(); self.falha_tsc()
        self.assertEqual(self.evento('beforeShellExecution', conversation_id='c2', command='npx tsc', cwd=str(self.repo)), {})
    def test_post_tool_use_tambem_conta(self):
        for _ in range(2):
            self.uso = getattr(self, 'uso', 0) + 1
            self.evento('postToolUse', tool_name='Shell', tool_use_id=f'j{self.uso}', tool_input={'command': 'npx jest', 'cwd': str(self.repo)},
                        cwd=str(self.repo), tool_output=json.dumps({'output': '● soma › retorna 3\n  expect(received).toBe(expected)', 'exitCode': 1}))
        self.assertEqual(self.shell('npx jest')['permission'], 'deny')

    # --- stop
    def test_anunciou_e_parou_retoma(self):
        self.evento('beforeSubmitPrompt', prompt='quero segunda opinião')
        self.evento('afterAgentResponse', text='Vou montar o contexto e, em seguida, abrir a consulta não interativa ao Claude.')
        r = self.evento('stop', status='completed', loop_count=0)
        self.assertIn('consulta_iniciar', r['followup_message'])
    def test_anunciou_e_chamou_nao_retoma(self):
        self.evento('afterAgentResponse', text='Vou abrir a consulta ao Claude.')
        self.evento('afterMCPExecution', tool_name='valt-ponte-consulta_iniciar', result_json=json.dumps({'id': 'b'*32, 'estado': 'abrindo'}))
        self.put(self.state/('b'*32)/'job.json', json.dumps({'id': 'b'*32, 'estado': 'concluida'}))
        self.assertEqual(self.evento('stop', status='completed', loop_count=0), {})
    def test_consulta_ativa_sem_acompanhar_retoma(self):
        self.evento('afterMCPExecution', tool_name='consulta_iniciar', result_json=json.dumps({'id': 'c'*32, 'estado': 'abrindo'}))
        self.put(self.state/('c'*32)/'job.json', json.dumps({'id': 'c'*32, 'estado': 'executando'}))
        r = self.evento('stop', status='completed', loop_count=0)
        self.assertIn('c'*32, r['followup_message']); self.assertIn('consulta_status', r['followup_message'])
    def test_limite_de_retomadas(self):
        self.evento('afterAgentResponse', text='Vou abrir a consulta ao Claude.')
        self.assertIn('followup_message', self.evento('stop', status='completed', loop_count=0))
        self.assertIn('followup_message', self.evento('stop', status='completed', loop_count=1))
        self.assertEqual(self.evento('stop', status='completed', loop_count=2), {})
    def test_nova_mensagem_zera_anuncio(self):
        self.evento('afterAgentResponse', text='Vou abrir a consulta ao Claude.')
        self.evento('beforeSubmitPrompt', prompt='esquece, faz outra coisa')
        self.assertEqual(self.evento('stop', status='completed', loop_count=0), {})
    def test_stop_abortado_nao_retoma(self):
        self.evento('afterAgentResponse', text='Vou abrir a consulta ao Claude.')
        self.assertEqual(self.evento('stop', status='aborted', loop_count=0), {})
    def test_mais_de_20_arquivos_pede_revisao_final(self):
        for i in range(21):
            self.put(self.repo/f'src/f{i}.ts', 'x')
            self.evento('afterFileEdit', file_path=str(self.repo/f'src/f{i}.ts'))
        r = self.evento('stop', status='completed', loop_count=0)
        self.assertIn('Revisão final', r['followup_message']); self.assertIn('src/f0.ts', r['followup_message'])
        self.job(criada=time.time()+1)
        self.evento('beforeSubmitPrompt', prompt='segue')
        self.assertEqual(self.evento('stop', status='completed', loop_count=0), {})
    def test_20_arquivos_nao_pede(self):
        for i in range(20):
            self.put(self.repo/f'src/f{i}.ts', 'x')
            self.evento('afterFileEdit', file_path=str(self.repo/f'src/f{i}.ts'))
        self.assertEqual(self.evento('stop', status='completed', loop_count=0), {})

    # --- saídas de emergência e segurança
    def test_sem_consulta_libera_na_conversa(self):
        self.stage_migracao()
        self.assertEqual(self.evento('beforeSubmitPrompt', prompt='commita #sem-consulta'), {'continue': True})
        self.assertEqual(self.shell('git commit -m x'), {})
        self.assertEqual(self.evento('beforeShellExecution', conversation_id='c2', command='git commit -m x', cwd=str(self.repo))['permission'], 'deny')
    def test_arquivo_desligado_libera_tudo(self):
        self.stage_migracao(); (self.state/'desligado').touch()
        self.assertEqual(self.shell('git commit -m x'), {})
    def rodar_main(self, evento, entrada):
        saida = io.StringIO()
        with patch.object(sys, 'argv', ['ponte_hook.py', evento]), patch.object(sys, 'stdin', io.StringIO(entrada)), patch.object(sys, 'stdout', saida):
            hook.main()
        return json.loads(saida.getvalue())
    def test_entrada_quebrada_libera_e_registra(self):
        self.assertEqual(self.rodar_main('beforeShellExecution', '{isso não é json'), {})
        self.assertEqual(self.rodar_main('beforeSubmitPrompt', '[1,2]'), {'continue': True})
        self.assertIn('liberado por exceção', (self.state/'hooks.log').read_text())
    def test_excecao_interna_libera(self):
        self.stage_migracao()
        with patch.object(hook, 'regra_migracao', side_effect=RuntimeError('boom')):
            self.assertEqual(self.rodar_main('beforeShellExecution', json.dumps({'conversation_id': 'c1', 'command': 'git commit', 'cwd': str(self.repo)})), {})
    def test_evento_desconhecido_neutro(self):
        self.assertEqual(self.rodar_main('sessionStart', '{}'), {})

    # --- contrato real (entradas capturadas do Cursor 3.18.25)
    def fixtures(self, evento):
        texto = (Path(__file__).parent/'test_ponte_hook_fixtures.json').read_text()
        texto = texto.replace('{HOME}/Sites/Trabalho/valt-ferramentas', str(self.repo)).replace('{HOME}', self.tmp.name)
        return json.loads(texto)[evento]
    def test_fixture_before_shell_campos(self):
        for entrada in self.fixtures('beforeShellExecution'):
            self.assertIn('command', entrada); self.assertIn('cwd', entrada); self.assertIn('conversation_id', entrada)
            self.assertEqual(hook.processar('beforeShellExecution', json.dumps(entrada)), {})
    def test_fixture_mesmo_uso_nao_conta_duas_vezes(self):
        falha = self.fixtures('postToolUseFailure')[0]
        falha['tool_input']['command'] = 'npx tsc --noEmit'
        for _ in range(3):
            hook.processar('postToolUseFailure', json.dumps(falha))
        cid = falha['conversation_id']
        self.assertEqual(hook.processar('beforeShellExecution', json.dumps({'conversation_id': cid, 'command': 'npx tsc --noEmit', 'cwd': str(self.repo)})), {})
    def test_fixture_after_shell_nao_conta(self):
        for entrada in self.fixtures('afterShellExecution'):
            entrada['command'] = 'npx tsc --noEmit'
            hook.processar('afterShellExecution', json.dumps(entrada)); hook.processar('afterShellExecution', json.dumps(entrada))
        cid = entrada['conversation_id']
        self.assertEqual(hook.processar('beforeShellExecution', json.dumps({'conversation_id': cid, 'command': 'npx tsc --noEmit', 'cwd': str(self.repo)})), {})
    def test_fixture_post_tool_use_mcp_ignorado(self):
        for entrada in self.fixtures('postToolUse'):
            self.assertEqual(hook.processar('postToolUse', json.dumps(entrada)), {})
    def test_fixture_mcp_iniciar_registra_consulta(self):
        entrada = self.fixtures('afterMCPExecution')[0]
        entrada['tool_name'] = 'consulta_iniciar'
        entrada['result_json'] = json.dumps({'content': [{'type': 'text', 'text': json.dumps({'id': 'd'*32, 'estado': 'abrindo'})}]})
        hook.processar('afterMCPExecution', json.dumps(entrada))
        self.put(self.state/('d'*32)/'job.json', json.dumps({'id': 'd'*32, 'estado': 'executando'}))
        parada = self.fixtures('stop')[0]
        r = hook.processar('stop', json.dumps(parada))
        self.assertIn('d'*32, r['followup_message'])
    def test_fixture_resposta_e_parada(self):
        resposta = self.fixtures('afterAgentResponse')[0]
        resposta['text'] = 'Vou montar o contexto e, em seguida, abrir a consulta não interativa ao Claude.'
        hook.processar('afterAgentResponse', json.dumps(resposta))
        self.assertIn('followup_message', hook.processar('stop', json.dumps(self.fixtures('stop')[0])))
    def test_fixture_prompt_continua(self):
        self.assertEqual(hook.processar('beforeSubmitPrompt', json.dumps(self.fixtures('beforeSubmitPrompt')[0])), {'continue': True})

if __name__ == '__main__':
    unittest.main()
