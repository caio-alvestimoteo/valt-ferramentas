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
        self.planos = raiz/'planos-cursor'
        self.planos.mkdir()
        self.patches = [patch.object(hook, 'VAULT', self.v), patch.object(hook, 'SITES', self.s), patch.object(hook, 'STATE', self.state),
                        # sem isto os testes leriam os planos reais do Cursor em ~/.cursor/plans
                        patch.object(hook, 'PLANOS_CURSOR', self.planos),
                        # nenhum teste pode abrir janela de verdade
                        patch.object(hook, 'abrir_rodada_em_segundo_plano', return_value=True)]
        for p in self.patches:
            p.start()
        hook._CACHE_INDICE.clear(); hook._CACHE_RAIZ.clear()
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
    def job_rodada(self, id_pedido, estado='concluida', provedor='claude'):
        jid = hashlib.md5((id_pedido+provedor+str(time.time_ns())).encode()).hexdigest()
        data = {'id': jid, 'id_pedido': id_pedido, 'provedor': provedor, 'estado': estado,
                'criada': time.time(),
                'pacote': {'projeto': 'Pessoais/lab', 'repositorio': 'Pessoais/lab', 'fontes': []}}
        self.put(self.state/jid/'job.json', json.dumps(data))
        return jid
    def criar_plano(self, texto='# Plano\nTrocar o header do site.'):
        return self.evento('preToolUse', tool_name='CreatePlan', cwd=str(self.repo),
                           tool_input={'name': 'p', 'plan': texto})
    def editar(self, alvo='src/app.tsx'):
        return self.evento('preToolUse', tool_name='Write', cwd=str(self.repo),
                           tool_input={'file_path': str(self.repo/alvo), 'contents': 'x'})
    def chamadas(self):
        arquivo = self.state/'chamadas.jsonl'
        return [json.loads(l) for l in arquivo.read_text().splitlines()] if arquivo.exists() else []

    # --- migração
    def test_commit_com_migracao_sensivel_e_negado(self):
        self.stage_migracao()
        r = self.shell('git commit -m "aulas"')
        self.assertEqual(r['permission'], 'deny')
        self.assertIn('consulta_iniciar', r['agent_message'])
        self.assertIn('consulta_iniciar', r['user_message'])  # o Cursor só repassa user_message ao agente
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
        self.assertTrue(self.evento('beforeSubmitPrompt', prompt='commita #sem-consulta')['continue'])
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
        self.assertEqual(self.rodar_main('preCompact', '{}'), {})

    # --- plano (gatilho de entrada)
    def test_criar_plano_nunca_e_barrado(self):
        """É o plano que será criticado: barrá-lo impediria o próprio gatilho."""
        self.assertEqual(self.criar_plano(), {})
    def test_plano_capturado_barra_a_primeira_edicao(self):
        self.criar_plano()
        r = self.editar()
        self.assertEqual(r['permission'], 'deny')
        self.assertIn('consulta_dupla', r['agent_message'])
        self.assertIn('Trocar o header', r['agent_message'])
    def test_markdown_ainda_e_planejamento(self):
        self.criar_plano()
        self.assertEqual(self.editar('notas.md'), {})
    def test_sem_plano_a_edicao_passa(self):
        self.assertEqual(self.editar(), {})
    def test_saida_do_plan_mode_barrada(self):
        self.criar_plano()
        r = self.evento('preToolUse', tool_name='SwitchMode', cwd=str(self.repo),
                        tool_input={'target_mode_id': 'agent'})
        self.assertEqual(r['permission'], 'deny')
    def test_saida_do_plan_mode_por_ferramenta_dinamica(self):
        """O Cursor pode entregar SwitchMode embrulhado em CallDynamicTool."""
        self.criar_plano()
        r = self.evento('preToolUse', tool_name='CallDynamicTool', cwd=str(self.repo),
                        tool_input={'namespace': 'cursor', 'toolName': 'SwitchMode',
                                    'arguments': {'target_mode_id': 'agent'}})
        self.assertEqual(r['permission'], 'deny')
    def test_voltar_para_plan_mode_nao_e_barrado(self):
        self.criar_plano()
        r = self.evento('preToolUse', tool_name='SwitchMode', cwd=str(self.repo),
                        tool_input={'target_mode_id': 'plan'})
        self.assertEqual(r, {})
    def test_plano_por_ferramenta_dinamica_e_capturado(self):
        self.evento('preToolUse', tool_name='CallDynamicTool', cwd=str(self.repo),
                    tool_input={'namespace': 'cursor', 'toolName': 'create_plan',
                                'arguments': {'plan': '# Plano dinamico'}})
        self.assertIn('Plano dinamico', self.editar()['agent_message'])
    def test_rodada_em_andamento_manda_acompanhar(self):
        self.criar_plano()
        pedido = hook.id_da_rodada('# Plano\nTrocar o header do site.')
        self.job_rodada(pedido, estado='abrindo')
        r = self.editar()
        self.assertEqual(r['permission'], 'deny')
        self.assertIn('consulta_rodada', r['agent_message'])
        self.assertIn(pedido, r['agent_message'])
    def test_rodada_concluida_libera_a_implementacao(self):
        self.criar_plano()
        pedido = hook.id_da_rodada('# Plano\nTrocar o header do site.')
        self.job_rodada(pedido, provedor='claude')
        self.job_rodada(pedido, provedor='codex')
        self.assertEqual(self.editar(), {})
    def test_rodada_toda_falha_nao_trava_o_trabalho(self):
        """Provedor fora do ar (cota, login) não pode deixar a conversa presa para sempre."""
        self.criar_plano()
        pedido = hook.id_da_rodada('# Plano\nTrocar o header do site.')
        self.job_rodada(pedido, estado='falhou', provedor='claude')
        self.job_rodada(pedido, estado='falhou', provedor='codex')
        self.assertEqual(self.editar(), {})
        self.assertTrue(any('sem nenhum parecer' in c.get('mensagem', '') for c in self.chamadas()))
    def test_commit_liberado_se_a_conferencia_toda_falhar(self):
        texto = self.liberar_plano()
        pedido = hook.id_da_rodada(texto, 'confere')
        self.job_rodada(pedido, estado='falhou', provedor='claude')
        self.job_rodada(pedido, estado='expirada', provedor='codex')
        self.assertEqual(self.shell('git commit -m "header"'), {})
    def test_rodada_em_andamento_ainda_barra(self):
        """Liberar em falha não pode virar liberar sempre."""
        self.criar_plano()
        self.job_rodada(hook.id_da_rodada('# Plano\nTrocar o header do site.'), estado='executando')
        self.assertEqual(self.editar()['permission'], 'deny')
    def test_plano_novo_pede_critica_de_novo(self):
        self.criar_plano()
        pedido = hook.id_da_rodada('# Plano\nTrocar o header do site.')
        self.job_rodada(pedido); self.job_rodada(pedido, provedor='codex')
        self.assertEqual(self.editar(), {})
        self.criar_plano('# Plano B\nRefazer o rodape.')
        self.assertEqual(self.editar()['permission'], 'deny')
    def test_composer_mode_guardado(self):
        self.evento('sessionStart', composer_mode='plan')
        conversa = json.loads((self.state/'conversas/c1.json').read_text())
        self.assertEqual(conversa['composer_mode'], 'plan')
    def test_sem_consulta_libera_o_plano(self):
        self.criar_plano()
        self.evento('beforeSubmitPrompt', prompt='segue assim #sem-consulta')
        self.assertEqual(self.editar(), {})

    def test_repo_fora_do_indice_mapeia_por_espelhamento(self):
        """O índice vive desatualizado; o espelhamento Sites↔Valt é a regra do ambiente."""
        fora = self.s/'Seara/novo'
        (fora/'.git').mkdir(parents=True)
        subprocess.run(['git', '-C', str(fora), 'init', '-q'], check=True, capture_output=True)
        self.put(self.v/'Seara/novo/README.md', 'novo')
        mapeado = hook.repo_mapeado(fora)
        self.assertEqual(mapeado[1:], ('Seara/novo', 'Seara/novo'))
    def test_espelhamento_nao_sobe_para_o_guarda_chuva(self):
        """Repositório desconhecido não pode herdar a documentação do guarda-chuva."""
        fora = self.s/'Seara/app/frontend'
        fora.mkdir(parents=True)
        subprocess.run(['git', '-C', str(fora), 'init', '-q'], check=True, capture_output=True)
        self.put(self.v/'Seara/app/README.md', 'app')
        self.assertIsNone(hook.projeto_espelhado('Seara/app/frontend'))
        self.assertEqual(hook.projeto_espelhado('Seara/app'), 'Seara/app')
    def test_espelhamento_ignora_maiusculas(self):
        """~/Sites/Seara/food espelha ~/Valt/Seara/Food."""
        self.put(self.v/'Seara/Food/README.md', 'food')
        self.assertEqual(hook.projeto_espelhado('Seara/food'), 'Seara/Food')
    def test_caminho_de_documentacao_sobe_ate_o_projeto(self):
        """Aqui subir é certo: o plano cita um arquivo, não um repositório."""
        self.put(self.v/'Pessoais/lab/Operacao/nota.md', 'x')
        self.assertEqual(hook.projeto_do_caminho_valt('Pessoais/lab/Operacao/nota.md'), 'Pessoais/lab')
    def test_sem_documentacao_no_valt_nao_mapeia(self):
        fora = self.s/'Seara/orfao'
        fora.mkdir(parents=True)
        subprocess.run(['git', '-C', str(fora), 'init', '-q'], check=True, capture_output=True)
        self.assertIsNone(hook.repo_mapeado(fora))
    def test_rodada_que_falhou_nao_conta_no_teto(self):
        for _ in range(hook.TETO_RODADAS_HORA):
            hook.registrar('hook_abriu_rodada', ok=False, mensagem='falhou')
        self.assertEqual(hook.rodadas_na_ultima_hora(), 0)
        _, abriu = self.parar(self.transcricao_com_plano())
        self.assertTrue(abriu.chamou_agora)

    # --- o hook abre a rodada sozinho
    def transcricao_com_plano(self, texto='# Plano\nTrocar o header.', nome='CreatePlan'):
        arquivo = Path(self.tmp.name)/'transcript.jsonl'
        linhas = [json.dumps({'role': 'user', 'message': {'content': [{'type': 'text', 'text': 'planeja'}]}}),
                  json.dumps({'role': 'assistant', 'message': {'content': [
                      {'type': 'text', 'text': 'segue o plano'},
                      {'type': 'tool_use', 'name': nome, 'input': {'name': 'p', 'plan': texto}}]}})]
        arquivo.write_text('\n'.join(linhas))
        return str(arquivo)
    def parar(self, transcricao=None, **extra):
        abriu = hook.abrir_rodada_em_segundo_plano
        antes = abriu.call_count
        r = self.evento('stop', status='completed', transcript_path=transcricao,
                        workspace_roots=[str(self.repo)], **extra)
        abriu.chamou_agora = abriu.call_count > antes
        return r, abriu
    def test_alvo_vem_do_caminho_citado_no_plano(self):
        """Plano transversal não tem repositório aberto, mas nomeia os que toca."""
        class C: d = {'editados': {}}
        alvo = hook.alvo_do_plano({'workspace_roots': [str(self.v)]}, C(),
                                  'mexe em `~/Sites/Pessoais/lab` hoje')
        self.assertEqual(alvo, ('Pessoais/lab', 'Pessoais/lab'))
    def test_plano_so_de_documentacao_roda_sem_repositorio(self):
        class C: d = {'editados': {}}
        alvo = hook.alvo_do_plano({'workspace_roots': []}, C(), 'revisar ~/Valt/Pessoais/lab/README.md')
        self.assertEqual(alvo, ('Pessoais/lab', ''))
    def test_workspace_do_valt_serve_de_projeto(self):
        class C: d = {'editados': {}}
        alvo = hook.alvo_do_plano({'workspace_roots': [str(self.v/'Pessoais/lab')]}, C(), 'plano sem caminhos')
        self.assertEqual(alvo, ('Pessoais/lab', ''))
    def test_plano_sem_alvo_nenhum_nao_abre(self):
        class C: d = {'editados': {}}
        self.assertIsNone(hook.alvo_do_plano({'workspace_roots': [self.tmp.name]}, C(), 'plano solto'))
    def test_repositorio_aberto_vence_o_citado(self):
        class C: d = {'editados': {}}
        alvo = hook.alvo_do_plano({'workspace_roots': [str(self.repo)]}, C(),
                                  'cita `~/Sites/Outro/outro` mas o aberto e o lab')
        self.assertEqual(alvo[1], 'Pessoais/lab')
    def test_plano_novo_abre_a_rodada_sozinho(self):
        r, abriu = self.parar(self.transcricao_com_plano())
        self.assertTrue(abriu.called)
        projeto, repo_rel, plano, _pergunta = abriu.call_args.args
        self.assertEqual((projeto, repo_rel), ('Pessoais/lab', 'Pessoais/lab'))
        self.assertIn('Trocar o header.', plano)
        self.assertIn('abriu Claude e Codex', r['followup_message'])
    def test_plano_capturado_mesmo_sem_hook_de_createplan(self):
        """O Cursor 3.20 não dispara preToolUse para CreatePlan: o plano vem da transcrição."""
        entrada = {'transcript_path': self.transcricao_com_plano('# Plano B\nRefazer.')}
        self.assertIn('Refazer.', hook.plano_da_transcricao(entrada))
    def test_plano_por_ferramenta_dinamica_na_transcricao(self):
        arquivo = Path(self.tmp.name)/'t2.jsonl'
        arquivo.write_text(json.dumps({'role': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'name': 'CallDynamicTool',
             'input': {'toolName': 'create_plan', 'arguments': {'plan': '# Dinamico'}}}]}}))
        self.assertIn('Dinamico', hook.plano_da_transcricao({'transcript_path': str(arquivo)}))
    def test_teto_nao_silencia_o_plano_para_sempre(self):
        """Falha transitória não pode marcar o plano como visto."""
        t = self.transcricao_com_plano()
        for _ in range(hook.TETO_RODADAS_HORA):
            hook.registrar('hook_abriu_rodada', ok=True)
        _, abriu = self.parar(t)
        self.assertFalse(abriu.chamou_agora)
        (self.state/'chamadas.jsonl').write_text('')  # passou a hora
        _, abriu = self.parar(t)
        self.assertTrue(abriu.chamou_agora)
    def test_alvo_ausente_nao_silencia_o_plano(self):
        t = self.transcricao_com_plano()
        abriu = hook.abrir_rodada_em_segundo_plano
        self.evento('stop', status='completed', transcript_path=t, workspace_roots=[self.tmp.name])
        self.assertFalse(abriu.called)
        _, abriu = self.parar(t)  # agora com workspace bom
        self.assertTrue(abriu.called)
    def test_mesmo_plano_nao_reabre(self):
        """Quem impede a reabertura é a rodada existir no disco, não uma marca na conversa."""
        t = self.transcricao_com_plano()
        self.parar(t)
        self.job_rodada(hook.id_da_rodada('# Plano\nTrocar o header.'), estado='executando')
        _, abriu = self.parar(t)
        self.assertFalse(abriu.chamou_agora)
    def test_filho_que_falha_e_tentado_de_novo(self):
        """O Popen só garante o spawn: se o filho morre, nenhum job aparece e o turno seguinte tenta."""
        t = self.transcricao_com_plano()
        _, abriu = self.parar(t)
        self.assertTrue(abriu.chamou_agora)
        _, abriu = self.parar(t)
        self.assertTrue(abriu.chamou_agora)
    def test_tentativas_tem_limite(self):
        """Falha permanente não pode virar laço de spawn a cada turno."""
        t = self.transcricao_com_plano()
        for _ in range(hook.MAX_TENTATIVAS_RODADA):
            self.parar(t)
        _, abriu = self.parar(t)
        self.assertFalse(abriu.chamou_agora)
    def test_plano_diferente_abre_de_novo(self):
        self.parar(self.transcricao_com_plano())
        _, abriu = self.parar(self.transcricao_com_plano('# Plano B\nOutra coisa.'))
        self.assertTrue(abriu.chamou_agora)
    def test_rodada_ja_aberta_pelo_agente_nao_duplica(self):
        texto = '# Plano\nTrocar o header.'
        self.job_rodada(hook.id_da_rodada(texto), estado='abrindo')
        _, abriu = self.parar(self.transcricao_com_plano(texto))
        self.assertFalse(abriu.chamou_agora)
    def test_teto_por_hora_segura(self):
        for _ in range(hook.TETO_RODADAS_HORA):
            hook.registrar('hook_abriu_rodada')
        _, abriu = self.parar(self.transcricao_com_plano())
        self.assertFalse(abriu.chamou_agora)
        self.assertTrue(any('teto' in c.get('mensagem', '') for c in self.chamadas()))
    def test_rodada_velha_nao_conta_no_teto(self):
        antiga = {'hora': '2020-01-01T10:00:00-03:00', 'ferramenta': 'hook_abriu_rodada'}
        (self.state/'chamadas.jsonl').write_text(json.dumps(antiga)+'\n')
        self.assertEqual(hook.rodadas_na_ultima_hora(), 0)
    def test_sem_repositorio_mapeado_nao_abre(self):
        abriu = hook.abrir_rodada_em_segundo_plano
        self.evento('stop', status='completed', transcript_path=self.transcricao_com_plano(),
                    workspace_roots=[self.tmp.name])
        self.assertFalse(abriu.called)
    def test_turno_sem_plano_nao_abre(self):
        arquivo = Path(self.tmp.name)/'vazio.jsonl'
        arquivo.write_text(json.dumps({'role': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'oi'}]}}))
        _, abriu = self.parar(str(arquivo))
        self.assertFalse(abriu.chamou_agora)
    def test_sem_consulta_impede_a_abertura_automatica(self):
        self.evento('beforeSubmitPrompt', prompt='deixa quieto #sem-consulta')
        _, abriu = self.parar(self.transcricao_com_plano())
        self.assertFalse(abriu.chamou_agora)
    def test_plano_do_disco_exige_janela_de_tempo(self):
        (self.planos/'x.plan.md').write_text('# Plano de outra conversa')
        self.assertEqual(hook.plano_do_disco(0), '')
        self.assertIn('outra conversa', hook.plano_do_disco(time.time()-60))

    def job_com_parecer(self, id_pedido, provedor, parecer):
        jid = hashlib.md5((id_pedido+provedor).encode()).hexdigest()
        self.put(self.state/jid/'job.json', json.dumps({
            'id': jid, 'id_pedido': id_pedido, 'provedor': provedor, 'estado': 'concluida',
            'criada': time.time(), 'parecer': parecer, 'registro': f'~/Valt/x/{jid}.md',
            'pacote': {'projeto': 'Pessoais/lab', 'repositorio': 'Pessoais/lab', 'fontes': []}}))
    def test_pareceres_chegam_ao_chat_sem_o_agente_pedir(self):
        texto = '# Plano\nTrocar o header.'
        self.parar(self.transcricao_com_plano(texto))
        pedido = hook.id_da_rodada(texto)
        self.job_com_parecer(pedido, 'claude', 'o plano ignora o tema')
        self.job_com_parecer(pedido, 'codex', 'falta teste de contraste')
        r = self.evento('beforeSubmitPrompt', prompt='pode implementar')
        contexto = r['additional_context']
        self.assertIn('o plano ignora o tema', contexto)
        self.assertIn('falta teste de contraste', contexto)
        self.assertIn('Parecer do claude', contexto)
    def test_pareceres_entregues_uma_vez_so(self):
        texto = '# Plano\nTrocar o header.'
        self.parar(self.transcricao_com_plano(texto))
        self.job_com_parecer(hook.id_da_rodada(texto), 'claude', 'atencao ao tema')
        self.evento('beforeSubmitPrompt', prompt='vai')
        segundo = self.evento('beforeSubmitPrompt', prompt='vai de novo')
        self.assertNotIn('atencao ao tema', segundo['additional_context'])
    def test_entrega_libera_a_trava_da_implementacao(self):
        texto = '# Plano\nTrocar o header.'
        self.parar(self.transcricao_com_plano(texto))
        self.assertEqual(self.editar()['permission'], 'deny')
        self.job_com_parecer(hook.id_da_rodada(texto), 'claude', 'ok com ressalvas')
        self.evento('beforeSubmitPrompt', prompt='implementa')
        self.assertEqual(self.editar(), {})
    def test_rodada_incompleta_nao_entrega(self):
        texto = '# Plano\nTrocar o header.'
        self.parar(self.transcricao_com_plano(texto))
        self.job_rodada(hook.id_da_rodada(texto), estado='executando')
        r = self.evento('beforeSubmitPrompt', prompt='e ai')
        self.assertNotIn('Parecer do', r['additional_context'])

    # --- conferência final (gatilho de saída)
    def liberar_plano(self, texto='# Plano\nTrocar o header do site.'):
        """Plano criticado: a rodada de entrada terminou."""
        self.criar_plano(texto)
        pedido = hook.id_da_rodada(texto)
        self.job_rodada(pedido); self.job_rodada(pedido, provedor='codex')
        self.editar()  # marca plano_criticado
        return texto
    def test_commit_apos_plano_pede_conferencia(self):
        texto = self.liberar_plano()
        r = self.shell('git commit -m "header"')
        self.assertEqual(r['permission'], 'deny')
        self.assertIn('conferência final', r['user_message'])
        self.assertIn(hook.id_da_rodada(texto, 'confere'), r['agent_message'])
    def test_conferencia_abre_sozinha_no_commit(self):
        """Esperar o agente chamar é a premissa que falhou o dia inteiro."""
        texto = self.liberar_plano()
        abriu = hook.abrir_rodada_em_segundo_plano
        r = self.shell('git commit -m "header"')
        self.assertTrue(abriu.called)
        self.assertEqual(abriu.call_args.args[4] if len(abriu.call_args.args) > 4 else abriu.call_args.args[-1], 'confere')
        self.assertEqual(r['permission'], 'deny')
        self.assertIn('acabou de ser aberta', r['agent_message'])
        self.assertIn(hook.id_da_rodada(texto, 'confere'), r['agent_message'])
    def test_conferencia_nao_reabre_a_cada_commit(self):
        self.liberar_plano()
        abriu = hook.abrir_rodada_em_segundo_plano
        antes = abriu.call_count
        for _ in range(hook.MAX_TENTATIVAS_RODADA + 2):
            self.shell('git commit -m "x"')
        self.assertEqual(abriu.call_count - antes, hook.MAX_TENTATIVAS_RODADA)
    def test_conferencia_concluida_libera_o_commit(self):
        texto = self.liberar_plano()
        pedido = hook.id_da_rodada(texto, 'confere')
        self.job_rodada(pedido); self.job_rodada(pedido, provedor='codex')
        self.assertEqual(self.shell('git commit -m "header"'), {})
    def test_conferencia_em_andamento_manda_acompanhar(self):
        texto = self.liberar_plano()
        self.job_rodada(hook.id_da_rodada(texto, 'confere'), estado='abrindo')
        r = self.shell('git commit -m "header"')
        self.assertIn('consulta_rodada', r['agent_message'])
    def test_commit_sem_plano_nao_pede_conferencia(self):
        self.assertEqual(self.shell('git commit -m "x"'), {})
    def test_plano_nao_criticado_nao_chega_na_conferencia(self):
        """Antes da crítica o commit cai na regra de entrada, não na de saída."""
        self.criar_plano()
        r = self.shell('git commit -m "x"')
        self.assertEqual(r, {})  # a trava de entrada é na edição, não no commit
    def test_rodada_de_entrada_e_de_saida_sao_distintas(self):
        texto = '# Plano\nTrocar o header do site.'
        self.assertNotEqual(hook.id_da_rodada(texto), hook.id_da_rodada(texto, 'confere'))

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
        r = hook.processar('beforeSubmitPrompt', json.dumps(self.fixtures('beforeSubmitPrompt')[0]))
        self.assertTrue(r['continue']); self.assertIn('additional_context', r)

    # --- reavaliação: brechas encontradas
    def test_commit_com_cd_a_partir_de_outra_pasta(self):
        self.stage_migracao()
        r = self.evento('beforeShellExecution', command=f'cd {self.repo} && git commit -m x', cwd=str(self.v))
        self.assertEqual(r['permission'], 'deny')
    def test_commit_com_cd_relativo(self):
        self.stage_migracao()
        r = self.evento('beforeShellExecution', command='cd Pessoais/lab && git add -A && git commit -m x', cwd=str(self.s))
        self.assertEqual(r['permission'], 'deny')
    def test_commit_com_git_C(self):
        self.stage_migracao()
        r = self.evento('beforeShellExecution', command=f'git -C "{self.repo}" commit -m x', cwd=str(self.v))
        self.assertEqual(r['permission'], 'deny')
    def test_tsc_repetido_com_cd(self):
        for n in range(2):
            self.evento('postToolUseFailure', tool_name='Shell', tool_use_id=f'cd{n}', cwd=str(self.v),
                        tool_input={'command': f'cd {self.repo} && npx tsc --noEmit', 'cwd': str(self.v)},
                        error_message='src/a.ts(1,1): error TS2322: x')
        r = self.evento('beforeShellExecution', command=f'cd {self.repo} && npx tsc --noEmit', cwd=str(self.v))
        self.assertEqual(r['permission'], 'deny')
    def test_chamou_no_meio_e_anunciou_no_resumo_nao_retoma(self):
        self.evento('beforeSubmitPrompt', prompt='pede segunda opinião')
        time.sleep(0.01)
        self.evento('afterMCPExecution', tool_name='consulta_iniciar', result_json=json.dumps({'id': 'f'*32, 'estado': 'abrindo'}))
        self.put(self.state/('f'*32)/'job.json', json.dumps({'id': 'f'*32, 'estado': 'concluida'}))
        self.evento('afterAgentResponse', text='Vou abrir a consulta ao Claude e depois resumo; o parecer está abaixo.')
        self.assertEqual(self.evento('stop', status='completed', loop_count=0), {})
    def test_chamada_de_turno_anterior_nao_cobre_anuncio_novo(self):
        self.evento('beforeSubmitPrompt', prompt='primeiro')
        self.evento('afterMCPExecution', tool_name='consulta_iniciar', result_json=json.dumps({'id': 'a1'*16, 'estado': 'concluida'}))
        time.sleep(0.01)
        self.evento('beforeSubmitPrompt', prompt='agora outra coisa')
        self.evento('afterAgentResponse', text='Vou abrir a consulta ao Claude.')
        self.assertIn('followup_message', self.evento('stop', status='completed', loop_count=0))

    # --- git add no mesmo comando do commit (padrão real do Cursor)
    def test_add_e_commit_no_mesmo_comando(self):
        self.put(self.repo/'supabase/migrations/002_x.sql', MIGRACAO)
        r = self.shell('git add supabase/migrations/002_x.sql && git commit -m "x" && git status')
        self.assertEqual(r['permission'], 'deny')
    def test_add_ponto_e_commit(self):
        self.put(self.repo/'supabase/migrations/002_x.sql', MIGRACAO)
        self.assertEqual(self.shell('git add . && git commit -m x')['permission'], 'deny')
    def test_add_pasta_e_commit_a_partir_de_outra_pasta(self):
        self.put(self.repo/'supabase/migrations/002_x.sql', MIGRACAO)
        r = self.evento('beforeShellExecution', command=f'cd {self.repo} && git add supabase && git commit -m x', cwd=str(self.v))
        self.assertEqual(r['permission'], 'deny')
    def test_add_de_arquivo_inocente_e_commit_passa(self):
        self.put(self.repo/'app.ts', 'export const y = 2')
        self.assertEqual(self.shell('git add app.ts && git commit -m y'), {})
    def test_add_versao_consultada_passa(self):
        self.put(self.repo/'supabase/migrations/002_x.sql', MIGRACAO)
        sha = hashlib.sha256(MIGRACAO.encode()).hexdigest()
        self.job(fontes=[{'arquivo': 'Sites/Pessoais/lab/supabase/migrations/002_x.sql', 'sha256': sha}])
        self.assertEqual(self.shell('git add supabase/migrations/002_x.sql && git commit -m x'), {})
    def test_add_de_arquivo_ja_commitado_modificado(self):
        nome, _ = self.stage_migracao('create table agenda.salas (id int);\n'); self.git('commit', '-qm', 'base')
        self.put(self.repo/nome, MIGRACAO)
        self.assertEqual(self.shell(f'git add {nome} && git commit -m muda')['permission'], 'deny')

    def test_chamada_mcp_com_erro_nao_conta_como_chamada(self):
        self.evento('beforeSubmitPrompt', prompt='commita')
        self.evento('afterMCPExecution', tool_name='valt-ponte-consulta_iniciar', result_json=json.dumps({'error': 'MCP server does not exist: valt-ponte'}))
        self.evento('afterAgentResponse', text='O hook exige consulta. Vou abrir a consulta ao Claude pela valt-ponte.')
        self.assertIn('followup_message', self.evento('stop', status='completed', loop_count=0))

    # --- proteção da configuração (agente editou mcp.json e matou o MCP no teste real)
    def test_shell_nao_edita_config(self):
        for c in ['sed -i "s/a/b/" ~/.cursor/mcp.json', 'jq . x > ~/.cursor/hooks.json', 'cp /tmp/x ~/.cursor/mcp.json',
                  'python3 -c "open(\'/h/.cursor/mcp.json\',\'w\')"', 'echo {} | tee ~/.cursor/mcp.json', 'rm ~/.cursor/hooks.json',
                  'cd ~/.cursor && mv /tmp/y ~/.cursor/mcp.json']:
            self.assertEqual(self.shell(c)['permission'], 'deny', c)
    def test_shell_le_config_livre(self):
        for c in ['cat ~/.cursor/mcp.json', 'jq . ~/.cursor/hooks.json', 'grep valt ~/.cursor/mcp.json',
                  'cat ~/.cursor/mcp.json > /tmp/copia.json', 'cp ~/.cursor/mcp.json /tmp/copia.json', 'jq . ~/.cursor/mcp.json | tee /tmp/x']:
            self.assertEqual(self.shell(c), {}, c)
    def test_nao_mata_processo_da_ponte(self):
        self.assertEqual(self.shell('pkill -f "ponte.py mcp"')['permission'], 'deny')
        with patch.object(hook, 'processos_da_ponte', return_value={'4242'}):
            self.assertEqual(self.shell('kill 4242 2>/dev/null; sleep 1')['permission'], 'deny')
            self.assertEqual(self.shell('kill 999'), {})
    def test_protecao_vale_com_sem_consulta(self):
        self.evento('beforeSubmitPrompt', prompt='#sem-consulta')
        self.assertEqual(self.shell('sed -i x ~/.cursor/mcp.json')['permission'], 'deny')
    def test_edicao_da_config_restaura_e_avisa(self):
        with patch.object(hook, 'restaurar_config') as restaura:
            self.evento('afterFileEdit', file_path=str(Path.home()/'.cursor/mcp.json'))
            restaura.assert_called_once()
        r = self.evento('stop', status='completed', loop_count=0)
        self.assertIn('restaurou', r['followup_message'])
        self.assertEqual(self.chamadas()[-2]['ferramenta'], 'hook_restaurou')

    # --- idioma e lembrete, preToolUse e aviso no 2º erro
    def test_prompt_injeta_idioma_e_lembrete(self):
        r = self.evento('beforeSubmitPrompt', prompt='oi')
        self.assertTrue(r['continue'])
        self.assertIn('português do Brasil', r['additional_context']); self.assertIn('consulta_iniciar', r['additional_context'])
    def test_desligado_nao_injeta(self):
        (self.state/'desligado').touch()
        self.assertEqual(self.evento('beforeSubmitPrompt', prompt='oi'), {'continue': True})
    def test_pre_tool_barra_edicao_da_config(self):
        for nome, entrada in [('Write', {'path': str(Path.home()/'.cursor/mcp.json'), 'contents': '{}'}),
                              ('StrReplace', {'file_path': '~/.cursor/hooks.json', 'old_string': 'a', 'new_string': 'b'}),
                              ('Edit', {'target_file': str(Path.home()/'.cursor/mcp.json')})]:
            r = self.evento('preToolUse', tool_name=nome, tool_input=entrada)
            self.assertEqual(r['permission'], 'deny', nome); self.assertIn('instalador', r['user_message'])
    def test_pre_tool_libera_leitura_e_outros_arquivos(self):
        self.assertEqual(self.evento('preToolUse', tool_name='Read', tool_input={'path': str(Path.home()/'.cursor/mcp.json')}), {})
        self.assertEqual(self.evento('preToolUse', tool_name='Write', tool_input={'path': '/tmp/x.md', 'contents': 'veja ~/.cursor/mcp.json'}), {})
        self.assertEqual(self.evento('preToolUse', tool_name='Shell', tool_input={'command': 'cat ~/.cursor/mcp.json'}), {})
    def test_segundo_erro_avisa_com_chamada_pronta(self):
        self.falha_tsc()
        r = self.falha_tsc()
        self.assertIn('A próxima execução será barrada', r['additional_context']); self.assertIn('consulta_iniciar', r['additional_context'])

    # --- formas alternativas de commit (revisão)
    def test_formas_alternativas_de_commit(self):
        self.stage_migracao()
        r = str(self.repo)
        for c in [f'(cd {r}; git commit -m x)', f'{{ cd {r} && git commit -m x; }}', f'bash -c "cd {r} && git commit -m x"',
                  f"sh -c 'cd {r} && git commit -m x'", f'cd {r} && git --no-pager commit -m x', f'cd {r} && git -c user.name=x commit -m x',
                  f'cd {r} && GIT_AUTHOR_NAME=x git commit -m x', f'git -C {r} -c core.pager=cat commit -m x', f'cd {r} && git status | cat && git commit -m x']:
            self.assertEqual(self.evento('beforeShellExecution', command=c, cwd=str(self.v))['permission'], 'deny', c)
    def test_commit_citado_em_texto_nao_confunde(self):
        self.stage_migracao()
        self.assertEqual(self.evento('beforeShellExecution', command=f'cd {self.repo} && git log --grep="git commit"', cwd=str(self.v)), {})

    # --- cursor-agent não dispara beforeSubmitPrompt: #sem-consulta pela transcrição
    def test_sem_consulta_pela_transcricao(self):
        self.stage_migracao()
        transcricao = self.state/'t.jsonl'
        self.put(transcricao, json.dumps({'role': 'user', 'message': {'content': [{'type': 'text', 'text': '<user_query>#sem-consulta commita</user_query>'}]}})+'\n')
        self.assertEqual(self.evento('beforeShellExecution', command='git commit -m x', cwd=str(self.repo), transcript_path=str(transcricao)), {})
    def test_sem_consulta_so_vale_do_usuario(self):
        self.stage_migracao()
        transcricao = self.state/'t2.jsonl'
        self.put(transcricao, json.dumps({'role': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'posso usar #sem-consulta'}]}})+'\n')
        self.assertEqual(self.evento('beforeShellExecution', command='git commit -m x', cwd=str(self.repo), transcript_path=str(transcricao))['permission'], 'deny')
    def test_estado_da_consulta_no_resultado_real(self):
        interno = json.dumps({'id': 'a'*32, 'estado': 'abrindo'})
        self.evento('afterMCPExecution', tool_name='consulta_iniciar', result_json=json.dumps({'content': [{'type': 'text', 'text': interno}]}))
        d = json.loads(hook.caminho_conversa({'conversation_id': 'c1'}).read_text())
        self.assertEqual(d['consultas']['a'*32]['estado'], 'abrindo')

    def test_erro_conta_com_cwd_vazio_pela_raiz_do_workspace(self):
        for n in range(2):
            self.evento('postToolUseFailure', tool_name='Shell', tool_use_id=f'w{n}', cwd='', workspace_roots=[str(self.repo)],
                        tool_input={'command': 'npx tsc --noEmit', 'cwd': ''}, error_message='src/a.ts(1,1): error TS2322: x')
        self.assertEqual(self.evento('beforeShellExecution', command='npx tsc --noEmit', cwd='', workspace_roots=[str(self.repo)])['permission'], 'deny')

    def test_commit_barrado_com_mais_de_20_arquivos_sem_revisao(self):
        for i in range(21):
            self.put(self.repo/f'src/g{i}.ts', 'x'); self.evento('afterFileEdit', file_path=str(self.repo/f'src/g{i}.ts'))
        r = self.shell('git add -A && git commit -m muitos')
        self.assertEqual(r['permission'], 'deny'); self.assertIn('Revisão final', r['user_message'])
        self.job(criada=time.time()+1)
        self.assertEqual(self.shell('git add -A && git commit -m muitos'), {})
    def test_revisao_em_outro_repositorio_nao_barra(self):
        for i in range(21):
            self.put(self.repo/f'src/g{i}.ts', 'x'); self.evento('afterFileEdit', file_path=str(self.repo/f'src/g{i}.ts'))
        self.assertEqual(self.evento('beforeShellExecution', command='git commit -m x', cwd=str(self.v)), {})

    def test_consulta_antiga_nao_cobre(self):
        nome, sha = self.stage_migracao()
        jid = self.job(fontes=[{'arquivo': 'Sites/Pessoais/lab/'+nome, 'sha256': sha}])
        antigo = time.time() - 31*24*3600
        import os as _os; _os.utime(self.state/jid/'job.json', (antigo, antigo))
        self.assertEqual(self.shell('git commit -m x')['permission'], 'deny')

if __name__ == '__main__':
    unittest.main()
