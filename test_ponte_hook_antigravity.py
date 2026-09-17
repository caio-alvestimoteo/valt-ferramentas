import hashlib
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import ponte_hook as nucleo
import ponte_hook_antigravity as ag
import test_ponte_hook as base  # módulo, não a classe: não repete os testes do Cursor

MIGRACAO = base.MIGRACAO

class AntigravityTest(unittest.TestCase):
    setUp = base.HookTest.setUp
    tearDown = base.HookTest.tearDown
    put = base.HookTest.put
    git = base.HookTest.git
    stage_migracao = base.HookTest.stage_migracao
    job = base.HookTest.job
    chamadas = base.HookTest.chamadas

    def evento(self, nome, **entrada):
        entrada.setdefault('conversationId', 'c1')
        entrada.setdefault('workspacePaths', [str(self.repo)])
        return ag.processar(nome, json.dumps(entrada))
    def passo(self):
        self.idx = getattr(self, 'idx', 0) + 1
        return self.idx
    def ferramenta(self, evento, nome, args, **extra):
        return self.evento(evento, toolCall={'name': nome, 'args': args}, stepIdx=self.passo(), **extra)
    def comando(self, linha, cwd=None, **extra):
        return self.ferramenta('PreToolUse', 'run_command', {'CommandLine': linha, 'Cwd': cwd or str(self.repo)}, **extra)
    def transcricao(self, *mensagens):
        caminho = self.state/'transcript.jsonl'
        self.put(caminho, ''.join(json.dumps({'role': papel, 'content': texto})+'\n' for papel, texto in mensagens))
        return str(caminho)

    # --- terminal
    def test_commit_com_migracao_sensivel_e_negado(self):
        self.stage_migracao()
        r = self.comando('git commit -m "aulas"')
        self.assertEqual(r['decision'], 'deny')
        self.assertIn('consulta_iniciar', r['reason'])
        self.assertEqual(self.chamadas()[-1]['ferramenta'], 'hook_negou')
    def test_commit_liberado_com_consulta_do_mesmo_conteudo(self):
        nome, sha = self.stage_migracao()
        self.job(fontes=[{'arquivo': 'Sites/Pessoais/lab/'+nome, 'sha256': sha}])
        self.assertEqual(self.comando('git commit -m "aulas"'), {})
    def test_comando_comum_passa(self):
        self.assertEqual(self.comando('git status'), {})
    def test_cwd_ausente_usa_raiz_do_workspace(self):
        self.stage_migracao()
        r = self.ferramenta('PreToolUse', 'run_command', {'CommandLine': 'git commit -m x'})
        self.assertEqual(r['decision'], 'deny')

    # --- erro repetido
    def falha(self, erro='src/x.ts(3,5): error TS2322: Type string is not assignable to number.'):
        return self.ferramenta('PostToolUse', 'run_command', {'CommandLine': 'npx tsc --noEmit', 'Cwd': str(self.repo)}, error=erro)
    def test_terceira_tentativa_negada_e_aviso_no_segundo_erro(self):
        self.falha(); self.falha()
        r = self.evento('PreInvocation', invocationNum=3)
        textos = [p['ephemeralMessage'] for p in r['injectSteps']]
        self.assertTrue(any('A próxima execução será barrada' in t for t in textos))
        self.assertEqual(self.evento('PreInvocation', invocationNum=4), {})  # aviso sai uma vez só
        self.assertEqual(self.comando('npx tsc --noEmit')['decision'], 'deny')
    def test_mesmo_passo_nao_conta_duas_vezes(self):
        args = {'CommandLine': 'npx tsc --noEmit', 'Cwd': str(self.repo)}
        for _ in range(3):
            self.evento('PostToolUse', toolCall={'name': 'run_command', 'args': args}, stepIdx=7, error='a.ts(1,1): error TS2322: x')
        self.assertEqual(self.comando('npx tsc --noEmit'), {})

    # --- turno, idioma e #sem-consulta
    def test_pedido_novo_injeta_idioma_uma_vez(self):
        t = self.transcricao(('user', 'oi'))
        r = self.evento('PreInvocation', invocationNum=0, transcriptPath=t)
        self.assertIn('português do Brasil', r['injectSteps'][0]['ephemeralMessage'])
        self.assertEqual(self.evento('PreInvocation', invocationNum=1, transcriptPath=t), {})
    def test_sem_consulta_pela_transcricao(self):
        self.stage_migracao()
        t = self.transcricao(('user', 'commita #sem-consulta'))
        self.evento('PreInvocation', invocationNum=0, transcriptPath=t)
        self.assertEqual(self.comando('git commit -m x', transcriptPath=t), {})
    def test_sem_transcricao_primeira_invocacao_e_turno(self):
        r = self.evento('PreInvocation', invocationNum=0)
        self.assertIn('consulta_iniciar', r['injectSteps'][0]['ephemeralMessage'])

    # --- Stop
    def test_anunciou_e_parou_continua(self):
        t = self.transcricao(('user', 'quero segunda opinião'), ('assistant', 'Vou abrir a consulta ao Claude pela valt-ponte.'))
        self.evento('PreInvocation', invocationNum=0, transcriptPath=t)
        r = self.evento('Stop', terminationReason='model_stop', fullyIdle=True, transcriptPath=t)
        self.assertEqual(r['decision'], 'continue'); self.assertIn('consulta_iniciar', r['reason'])
    def test_limite_de_retomadas(self):
        t = self.transcricao(('user', 'pede parecer'), ('assistant', 'Vou abrir a consulta ao Claude.'))
        self.evento('PreInvocation', invocationNum=0, transcriptPath=t)
        self.assertEqual(self.evento('Stop', terminationReason='model_stop', transcriptPath=t)['decision'], 'continue')
        self.evento('PreInvocation', invocationNum=1, transcriptPath=t)  # retomada: mesmo pedido, não zera
        self.assertEqual(self.evento('Stop', terminationReason='model_stop', transcriptPath=t)['decision'], 'continue')
        self.evento('PreInvocation', invocationNum=2, transcriptPath=t)
        self.assertEqual(self.evento('Stop', terminationReason='model_stop', transcriptPath=t), {})
    def test_stop_cancelado_nao_continua(self):
        t = self.transcricao(('user', 'x'), ('assistant', 'Vou abrir a consulta ao Claude.'))
        self.assertEqual(self.evento('Stop', terminationReason='user_cancelled', transcriptPath=t), {})
    def iniciar(self, estado):
        jid = 'e'*32
        self.put(self.state/jid/'job.json', json.dumps({'id': jid, 'id_pedido': 'p1', 'estado': estado, 'criada': time.time(),
                                                         'pacote': {'projeto': 'Pessoais/lab', 'repositorio': 'Pessoais/lab', 'fontes': []}}))
        self.ferramenta('PostToolUse', 'mcp_valt-ponte_consulta_iniciar', {'projeto': 'Pessoais/lab', 'id_pedido': 'p1'})
        return jid
    def test_anunciou_e_chamou_nao_continua(self):
        t = self.transcricao(('user', 'parecer'), ('assistant', 'Vou abrir a consulta ao Claude e o parecer está abaixo.'))
        self.evento('PreInvocation', invocationNum=0, transcriptPath=t)
        time.sleep(0.01)
        self.iniciar('concluida')
        self.assertEqual(self.evento('Stop', terminationReason='model_stop', transcriptPath=t), {})
    def test_consulta_ativa_sem_acompanhar_continua(self):
        jid = self.iniciar('executando')
        r = self.evento('Stop', terminationReason='model_stop')
        self.assertIn(jid, r['reason']); self.assertIn('consulta_status', r['reason'])
    def test_chamada_sem_consulta_criada_nao_conta(self):
        t = self.transcricao(('user', 'parecer'), ('assistant', 'Vou abrir a consulta ao Claude.'))
        self.evento('PreInvocation', invocationNum=0, transcriptPath=t)
        self.ferramenta('PostToolUse', 'mcp_valt-ponte_consulta_iniciar', {'id_pedido': 'inexistente'}, error='MCP server error')
        self.assertEqual(self.evento('Stop', terminationReason='model_stop', transcriptPath=t)['decision'], 'continue')
    def test_mais_de_20_arquivos_pede_revisao(self):
        for i in range(21):
            self.put(self.repo/f'src/f{i}.ts', 'x')
            self.ferramenta('PostToolUse', 'write_to_file', {'TargetFile': str(self.repo/f'src/f{i}.ts'), 'CodeContent': 'x'})
        r = self.evento('Stop', terminationReason='model_stop')
        self.assertIn('Revisão final', r['reason'])
        self.assertEqual(self.comando('git add -A && git commit -m muitos')['decision'], 'deny')

    # --- proteção da configuração
    def test_pre_tool_barra_edicao_da_config(self):
        for nome, args in [('write_to_file', {'TargetFile': str(Path.home()/'.gemini/config/hooks.json')}),
                           ('replace_file_content', {'TargetFile': '~/.gemini/config/mcp_config.json'}),
                           ('write_to_file', {'TargetFile': str(self.repo/'.agents/hooks.json')})]:
            r = self.ferramenta('PreToolUse', nome, args)
            self.assertEqual(r['decision'], 'deny', args); self.assertIn('instalador', r['reason'])
    def test_pre_tool_libera_leitura_e_outros_arquivos(self):
        self.assertEqual(self.ferramenta('PreToolUse', 'view_file', {'AbsolutePath': str(Path.home()/'.gemini/config/hooks.json')}), {})
        self.assertEqual(self.ferramenta('PreToolUse', 'write_to_file', {'TargetFile': '/tmp/x.md', 'CodeContent': '~/.gemini/config/hooks.json'}), {})
    def test_terminal_nao_edita_config(self):
        for c in ['sed -i x ~/.gemini/config/mcp_config.json', 'echo {} > ~/.gemini/config/hooks.json', 'rm .agents/hooks.json']:
            self.assertEqual(self.comando(c)['decision'], 'deny', c)
        self.assertEqual(self.comando('cat ~/.gemini/config/hooks.json'), {})
    def test_edicao_da_config_restaura_com_instalador_do_antigravity(self):
        with patch.object(nucleo.subprocess, 'run') as roda:
            self.put(self.v/'bootstrap/antigravity/ponte-hooks.sh', '')
            self.ferramenta('PostToolUse', 'write_to_file', {'TargetFile': str(Path.home()/'.gemini/config/hooks.json')})
            self.assertIn('bootstrap/antigravity/ponte-hooks.sh', ' '.join(roda.call_args.args[0]))
        self.assertIn('restaurou', self.evento('Stop', terminationReason='model_stop')['reason'])

    # --- saídas de emergência
    def test_desligado_libera_tudo(self):
        self.stage_migracao(); (self.state/'desligado').touch()
        self.assertEqual(self.comando('git commit -m x'), {})
        self.assertEqual(self.evento('PreInvocation', invocationNum=0), {})
    def rodar_main(self, evento, entrada):
        saida = io.StringIO()
        with patch.object(sys, 'argv', ['ponte_hook_antigravity.py', evento]), patch.object(sys, 'stdin', io.StringIO(entrada)), \
             patch.object(sys, 'stdout', saida), patch.object(nucleo, 'IDE', 'cursor'):
            ag.main()
        return json.loads(saida.getvalue())
    def test_entrada_quebrada_libera(self):
        self.assertEqual(self.rodar_main('PreToolUse', '{isso não é json'), {})
        self.assertIn('liberado por exceção', (self.state/'hooks.log').read_text())
    def test_evento_desconhecido_neutro(self):
        self.assertEqual(self.rodar_main('PostInvocation', '{}'), {})
    def test_log_marca_a_ide(self):
        self.stage_migracao()
        entrada = {'conversationId': 'c9', 'workspacePaths': [str(self.repo)],
                   'toolCall': {'name': 'run_command', 'args': {'CommandLine': 'git commit -m x', 'Cwd': str(self.repo)}}}
        self.assertEqual(self.rodar_main('PreToolUse', json.dumps(entrada))['decision'], 'deny')
        self.assertEqual(self.chamadas()[-1]['ide'], 'antigravity')

class RodadaDuplaTest(unittest.TestCase):
    """consulta_dupla devolve dois ids; sem isso o agente é retomado por engano."""
    setUp = base.HookTest.setUp
    tearDown = base.HookTest.tearDown
    put = base.HookTest.put
    git = base.HookTest.git

    def test_resultado_mcp_devolve_os_dois_ids(self):
        pedido = 'plano-abc'
        for provedor in ('claude', 'codex'):
            jid = hashlib.md5(provedor.encode()).hexdigest()
            self.put(self.state/jid/'job.json', json.dumps({
                'id': jid, 'id_pedido': pedido, 'provedor': provedor, 'estado': 'executando',
                'criada': time.time(), 'pacote': {'projeto': 'Pessoais/lab', 'repositorio': 'Pessoais/lab', 'fontes': []}}))
        bruto = ag.resultado_mcp('mcp_valt-ponte_consulta_dupla', {'id_pedido': pedido})
        dados = json.loads(bruto)
        self.assertEqual(len(dados['consultas']), 2)
        self.assertEqual(sorted(c['provedor'] for c in dados['consultas']), ['claude', 'codex'])
    def test_pedido_desconhecido_segue_sinalizando_erro(self):
        self.assertIn('error', json.loads(ag.resultado_mcp('mcp_valt-ponte_consulta_dupla', {'id_pedido': 'nada'})))


if __name__ == '__main__':
    unittest.main()
