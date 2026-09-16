#!/usr/bin/env python3
"""Hooks do Cursor que obrigam a consulta da valt-ponte nos momentos de risco.

Uso (em ~/.cursor/hooks.json): python3 ponte_hook.py <evento>, com a entrada JSON do Cursor no stdin.

Regras:
- beforeShellExecution nega `git commit` com migração sensível no stage e `supabase db push`
  sem consulta concluída sobre aquele arquivo (mesmo hash); nega a 3ª execução de tsc/Jest/
  Vitest/pgTAP depois de o mesmo erro aparecer duas vezes sem consulta.
- stop retoma o agente quando ele anunciou a consulta e não chamou, quando parou de acompanhar
  uma consulta ativa ou quando editou mais de 20 arquivos sem revisão final (máx. 2 por turno).

Nunca trava o Cursor: qualquer erro, entrada estranha ou demora libera e vai para hooks.log.
Saídas de emergência: `#sem-consulta` na mensagem ou o arquivo <estado>/desligado.
"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

VAULT = Path(os.environ.get('VALT', str(Path.home()/'Valt'))).expanduser().resolve()
SITES = Path(os.environ.get('SITES', str(Path.home()/'Sites'))).expanduser().resolve()
STATE = Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state')))/'valt-ponte'
LIMITE_S = 4
MAX_RETOMADAS = 2
LIMITE_ARQUIVOS = 20

SENSIVEL = re.compile(r'auth\.|\bgrant\b|\bpolicy\b|security\s+definer|e-?mail|telefone|phone|avatar|foto|'
                      r'\bnome\b|full_name|first_name|last_name|\bcpf\b', re.I)
COMMIT = re.compile(r'(^|[;&|]\s*|\s)git(\s+-C\s+\S+)?\s+commit\b')
COMMIT_TUDO = re.compile(r'\s-[a-zA-Z]*a[a-zA-Z]*\b|\s--all\b')
DB_PUSH = re.compile(r'\bsupabase\s+db\s+push\b|\bpsql\b.*(postgres(ql)?://|\s-h\s)')
TESTE = re.compile(r'\b(tsc|jest|vitest|pg_prove|pgtap)\b|supabase\s+test\b|\bnpm\s+(run\s+)?test\b|\bnpx\s+(tsc|jest|vitest)\b')
ERRO = re.compile(r'error TS\d+[^\n]*|^\s*●[^\n]+|^FAIL\b[^\n]*|^not ok\b[^\n]*|^\s*(Error|AssertionError|TypeError):[^\n]*', re.M)
ANUNCIO = re.compile(r'(abrir|iniciar|chamar|disparar|pedir|fazer)\b[^.\n]{0,50}(consulta|parecer)\b[^.\n]{0,50}(claude|codex|consultor|ponte|ptyxis)'
                     r'|consultar (o )?(claude|codex)\b|segunda opini[aã]o (do|ao|com o|pelo) (claude|codex)'
                     r'|(abrir|iniciar|chamar)\b[^.\n]{0,30}(consulta_iniciar|valt-ponte|o consultor)', re.I)
FERRAMENTA = re.compile(r'(consulta_iniciar|consulta_status|consulta_cancelar|contexto_valt)$')

def agora():
    return time.time()

def log(texto):
    try:
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (STATE/'hooks.log').open('a', encoding='utf-8') as out:
            out.write(datetime.now().astimezone().isoformat(timespec='seconds')+' '+texto.rstrip()+'\n')
    except OSError:
        pass

def registrar(evento, **campos):
    linha = {'hora': datetime.now().astimezone().isoformat(timespec='seconds'), 'ferramenta': evento, 'origem': 'hook'}
    linha.update({k: v for k, v in campos.items() if v not in (None, '', [])})
    try:
        with (STATE/'chamadas.jsonl').open('a', encoding='utf-8') as out:
            out.write(json.dumps(linha, ensure_ascii=False)+'\n')
    except OSError:
        pass

def neutro(evento):
    return {'continue': True} if evento == 'beforeSubmitPrompt' else {}

# --- estado por conversa --------------------------------------------------------------

def caminho_conversa(entrada):
    cid = str(entrada.get('conversation_id') or entrada.get('conversationId') or 'sem-conversa')
    return STATE/'conversas'/(re.sub(r'[^A-Za-z0-9_-]', '_', cid)[:100]+'.json')

class Conversa:
    def __init__(self, entrada):
        self.path = caminho_conversa(entrada)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (self.path.with_suffix('.lock')).open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            self.d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.d = {}
        self.d.setdefault('consultas', {})
        self.d.setdefault('erros', {})
        self.d.setdefault('editados', {})

    def salvar(self):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.d, ensure_ascii=False, indent=1))
        os.replace(temp, self.path)
        self.lock.close()

# --- repositórios e consultas ---------------------------------------------------------

def git(repo, *args, binario=False):
    r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, timeout=3)
    if r.returncode:
        return None
    return r.stdout if binario else r.stdout.decode('utf-8', errors='replace')

def raiz_git(pasta):
    if not pasta:
        return None
    saida = git(pasta, 'rev-parse', '--show-toplevel')
    return Path(saida.strip()).resolve() if saida else None

def indice():
    """{repositorio relativo a Sites: projeto do Valt} a partir de indices/repositorios.md."""
    mapa = {}
    try:
        texto = (VAULT/'indices/repositorios.md').read_text(encoding='utf-8')
    except OSError:
        return mapa
    raiz = VAULT.resolve()
    for linha in texto.splitlines():
        repo = re.search(r'`~/Sites/([^`{}]+)`', linha)
        if not repo:
            continue
        for link in re.findall(r'\]\(\.\./([^)#]+)\)', linha):
            # O link pode apontar para README.md ou Repositorio/mapa-repositorio.md: sobe até a pasta com README.
            pasta = (raiz/link).parent
            while pasta != raiz and pasta.is_relative_to(raiz) and not (pasta/'README.md').is_file():
                pasta = pasta.parent
            if pasta != raiz and pasta.is_relative_to(raiz):
                mapa.setdefault(repo.group(1).rstrip('/'), pasta.relative_to(raiz).as_posix())
                break
    return mapa

def repo_mapeado(pasta):
    """(raiz, repositorio relativo, projeto) se a pasta é de um repositório do índice; senão None."""
    raiz = raiz_git(pasta)
    if not raiz or not raiz.is_relative_to(SITES):
        return None
    rel = raiz.relative_to(SITES).as_posix()
    projeto = indice().get(rel)
    return (raiz, rel, projeto) if projeto else None

def jobs():
    for arquivo in STATE.glob('*/job.json'):
        try:
            yield json.loads(arquivo.read_text())
        except (OSError, ValueError):
            continue

def coberto(repo_rel, caminho, sha):
    """Existe consulta concluída sobre este arquivo, com o mesmo conteúdo?"""
    alvo = 'Sites/'+repo_rel+'/'+caminho
    for job in jobs():
        if job.get('estado') != 'concluida':
            continue
        for fonte in job.get('pacote', {}).get('fontes', []):
            if fonte.get('arquivo') == alvo and fonte.get('sha256') == sha:
                return job['id']
    return None

def consulta_depois(repo_rel, desde):
    for job in jobs():
        if job.get('estado') == 'concluida' and job.get('pacote', {}).get('repositorio') == repo_rel and job.get('criada', 0) >= desde:
            return job['id']
    return None

def sensivel(conteudo):
    texto = conteudo.decode('utf-8', errors='replace')
    return bool(SENSIVEL.search(texto)) or texto.count('\n') > 300

def eh_migracao(caminho):
    return caminho.endswith('.sql')

def chamada_pronta(projeto, repo_rel, arquivos, pergunta, semente):
    args = {'projeto': projeto, 'repositorio': repo_rel, 'arquivos': arquivos[:6], 'provedor': 'claude',
            'modo': 'investigador', 'interativo': False, 'pergunta': pergunta,
            'id_pedido': 'hook-'+hashlib.sha256(semente.encode()).hexdigest()[:16]}
    return ('Chame a ferramenta MCP consulta_iniciar do servidor valt-ponte com exatamente estes argumentos:\n'
            + json.dumps(args, ensure_ascii=False)
            + '\nDepois chame consulta_status (espera_segundos 25) até estado final, avalie o parecer e só então repita o comando. '
              'Não substitua por Task/subagente.')

def negar(motivo_usuario, mensagem_agente, **log_campos):
    registrar('hook_negou', mensagem=motivo_usuario[:200], **log_campos)
    return {'permission': 'deny', 'user_message': 'valt-ponte: '+motivo_usuario,
            'agent_message': mensagem_agente}

# --- regras ---------------------------------------------------------------------------

def alterados_no_disco(raiz, prefixo=''):
    modificados = (git(raiz, 'diff', '--name-only', 'HEAD', '--', prefixo or '.') or git(raiz, 'diff', '--name-only', '--', prefixo or '.') or '')
    novos = git(raiz, 'ls-files', '--others', '--exclude-standard', '--', prefixo or '.') or ''
    return [n for n in (modificados+'\n'+novos).split('\n') if n]

def adicionados_no_comando(raiz, comando, pasta):
    """Arquivos que um `git add` no mesmo comando vai colocar no stage antes do commit."""
    nomes = []
    for trecho in re.split(r'&&|\|\||;|\n', comando):
        m = re.search(r'\bgit(?:\s+-C\s+\S+)?\s+add\b(.*)', trecho)
        if not m:
            continue
        args = [a.strip('"\'') for a in m.group(1).split()]
        if not args or any(a in {'-A', '--all', '.', '-u', '--update', ':/'} for a in args):
            nomes += alterados_no_disco(raiz)
            continue
        base = Path(pasta) if pasta else raiz
        for arg in args:
            if arg.startswith('-'):
                continue
            alvo = (base/os.path.expanduser(arg)).resolve() if not Path(os.path.expanduser(arg)).is_absolute() else Path(os.path.expanduser(arg)).resolve()
            try:
                rel = alvo.relative_to(raiz).as_posix()
            except ValueError:
                continue
            nomes += alterados_no_disco(raiz, rel) if alvo.is_dir() else [rel]
    return list(dict.fromkeys(nomes))

def arquivos_do_commit(raiz, comando, pasta=None):
    """(nomes, lidos_do_disco): o que o commit vai levar, incluindo `git add` no mesmo comando."""
    nomes = (git(raiz, 'diff', '--cached', '--name-only') or '').split('\n')
    do_disco = set(adicionados_no_comando(raiz, comando, pasta))
    if COMMIT_TUDO.search(comando):
        do_disco |= set(n for n in (git(raiz, 'diff', '--name-only') or '').split('\n') if n)
    return [n for n in dict.fromkeys(nomes+sorted(do_disco)) if n], do_disco

def conteudo_stage(raiz, caminho):
    blob = git(raiz, 'show', ':'+caminho, binario=True)
    if blob is None and (raiz/caminho).is_file():
        blob = (raiz/caminho).read_bytes()
    return blob

def diretorio_efetivo(comando, pasta, alvo):
    """Pasta onde roda o trecho do comando que casa com `alvo`, seguindo `cd X &&` e `git -C X`."""
    atual = Path(pasta).expanduser() if pasta else None
    for trecho in re.split(r'&&|\|\||;|\n', comando):
        trecho = trecho.strip()
        mudanca = re.match(r'^(?:cd|pushd)\s+("[^"]+"|\'[^\']+\'|\S+)\s*$', trecho)
        if mudanca:
            destino = Path(os.path.expanduser(mudanca.group(1).strip('"\'')))
            atual = destino if destino.is_absolute() or atual is None else (atual/destino)
            continue
        if alvo.search(' '+trecho):
            via_c = re.search(r'\bgit\s+-C\s+("[^"]+"|\'[^\']+\'|\S+)', trecho)
            if via_c:
                destino = Path(os.path.expanduser(via_c.group(1).strip('"\'')))
                return destino if destino.is_absolute() or atual is None else (atual/destino)
            return atual
    return atual

def regra_migracao(entrada, comando, pasta):
    alvo = COMMIT if COMMIT.search(comando) else DB_PUSH
    pasta = diretorio_efetivo(comando, pasta, alvo)
    mapeado = repo_mapeado(pasta)
    if not mapeado:
        return None
    raiz, repo_rel, projeto = mapeado
    if COMMIT.search(comando):
        candidatos, do_disco = arquivos_do_commit(raiz, comando, pasta)
        acao = 'commit'
    elif DB_PUSH.search(comando):
        candidatos = [n for n in (git(raiz, 'diff', '--name-only', 'HEAD') or '').split('\n') if n]
        upstream = git(raiz, 'diff', '--name-only', '@{u}..HEAD')
        candidatos += [n for n in (upstream or '').split('\n') if n]
        do_disco = set(candidatos)
        migracoes = sorted((raiz/'supabase/migrations').glob('*.sql')) if (raiz/'supabase/migrations').is_dir() else []
        if migracoes:
            candidatos.append(migracoes[-1].relative_to(raiz).as_posix())
        acao = 'db push'
    else:
        return None
    pendentes = []
    for caminho in dict.fromkeys(candidatos):
        if not eh_migracao(caminho):
            continue
        no_disco = (raiz/caminho).read_bytes() if (raiz/caminho).is_file() else None
        # commit sem -a leva o stage; commit -a e db push levam o que está no disco.
        conteudo = no_disco if caminho in do_disco else conteudo_stage(raiz, caminho)
        if conteudo is None or not sensivel(conteudo):
            continue
        sha = hashlib.sha256(conteudo).hexdigest()
        if not coberto(repo_rel, caminho, sha):
            pendentes.append((caminho, sha))
    if not pendentes:
        return None
    arquivos = [c for c, _ in pendentes]
    semente = repo_rel+'|'+'|'.join(s for _, s in pendentes)
    return negar(f'{acao} barrado: migração sensível sem consulta ({", ".join(arquivos[:3])})',
                 f'O {acao} foi barrado pelo hook da valt-ponte: {", ".join(arquivos)} toca auth/grant/policy/dados pessoais '
                 'ou passa de 300 linhas e ainda não teve consulta concluída sobre esta versão do arquivo. '
                 + chamada_pronta(projeto, repo_rel, arquivos, 'Revisão de segurança e privacidade desta migração antes do '+acao, semente),
                 projeto=projeto, repositorio=repo_rel, regra='migracao')

def assinatura_erro(saida):
    achado = ERRO.search(saida or '')
    if not achado:
        return None
    primeira = achado.group(0)
    return re.sub(r'\d+(\.\d+)?\s*(ms|s)\b|\(\d+,\d+\)|:\d+:\d+', '', primeira).strip()[:200]

def tipo_teste(comando):
    m = TESTE.search(comando or '')
    return m.group(0).split()[-1] if m else None

def regra_erro_repetido(conversa, comando, pasta):
    tipo = tipo_teste(comando)
    if not tipo:
        return None
    mapeado = repo_mapeado(diretorio_efetivo(comando, pasta, TESTE))
    if not mapeado:
        return None
    raiz, repo_rel, projeto = mapeado
    for assinatura, info in conversa.d['erros'].items():
        if info.get('tipo') != tipo or info.get('repositorio') != repo_rel or info.get('vezes', 0) < 2:
            continue
        if consulta_depois(repo_rel, info['segunda_em']):
            continue
        return negar(f'3ª tentativa barrada: o mesmo erro de {tipo} apareceu duas vezes',
                     f'O hook da valt-ponte barrou a 3ª execução de {tipo}: o erro abaixo já apareceu duas vezes sem consulta.\n'
                     f'Erro: {assinatura}\n'
                     + chamada_pronta(projeto, repo_rel, info.get('arquivos', [])[:6] or [],
                                      f'O mesmo erro de {tipo} apareceu duas vezes: {assinatura}. Qual a causa raiz e a correção?',
                                      repo_rel+'|'+assinatura+'|'+str(info['segunda_em'])),
                     projeto=projeto, repositorio=repo_rel, regra='erro_repetido')
    return None

def pasta_da_entrada(entrada):
    cwd = entrada.get('cwd') or entrada.get('working_directory')
    if cwd:
        return Path(cwd).expanduser()
    for raiz in entrada.get('workspace_roots') or []:
        return Path(raiz).expanduser()
    return None

def comando_da_entrada(entrada):
    if entrada.get('command'):
        return str(entrada['command'])
    ferramenta = entrada.get('tool_input') or {}
    if isinstance(ferramenta, str):
        try:
            ferramenta = json.loads(ferramenta)
        except ValueError:
            return ferramenta
    return str(ferramenta.get('command') or ferramenta.get('cmd') or '')

def saida_da_entrada(entrada):
    partes = []
    for chave in ('output', 'stdout', 'stderr', 'result', 'error', 'error_message'):
        valor = entrada.get(chave)
        if isinstance(valor, (dict, list)):
            valor = json.dumps(valor, ensure_ascii=False)
        if valor:
            partes.append(str(valor))
    return '\n'.join(partes)

# --- eventos --------------------------------------------------------------------------

def before_shell(entrada, conversa):
    if conversa.d.get('sem_consulta'):
        return {}
    comando, pasta = comando_da_entrada(entrada), pasta_da_entrada(entrada)
    return regra_migracao(entrada, comando, pasta) or regra_erro_repetido(conversa, comando, pasta) or {}

def after_tool(entrada, conversa, falhou=False):
    """postToolUse/postToolUseFailure do Shell: conta a 1ª linha de erro de tsc/Jest/Vitest/pgTAP.

    afterShellExecution não serve: não traz cwd e dispara junto com postToolUse (contaria em dobro).
    """
    if str(entrada.get('tool_name', '')) != 'Shell':
        return {}
    id_uso = entrada.get('tool_use_id')
    vistos = conversa.d.setdefault('usos_contados', [])
    if id_uso and id_uso in vistos:
        return {}
    comando = comando_da_entrada(entrada)
    tipo = tipo_teste(comando)
    if not tipo:
        return {}
    saida = saida_da_entrada(entrada)
    codigo = None
    bruto = entrada.get('tool_output')
    if isinstance(bruto, str):
        try:
            bruto = json.loads(bruto)
        except ValueError:
            bruto = None
    if isinstance(bruto, dict):
        saida = str(bruto.get('output', '')) + '\n' + saida
        codigo = bruto.get('exitCode')
    if not falhou and codigo in (0, None) and not ERRO.search(saida):
        return {}
    ferramenta = entrada.get('tool_input') if isinstance(entrada.get('tool_input'), dict) else {}
    base = entrada.get('cwd') or ferramenta.get('cwd')
    mapeado = repo_mapeado(diretorio_efetivo(comando, base, TESTE))
    assinatura = assinatura_erro(saida)
    if not mapeado or not assinatura:
        return {}
    if id_uso:
        vistos.append(id_uso)
        del vistos[:-200]
    info = conversa.d['erros'].setdefault(assinatura, {'tipo': tipo, 'repositorio': mapeado[1], 'vezes': 0})
    info['vezes'] += 1
    if info['vezes'] == 2:
        info['segunda_em'] = agora()
    arquivos = re.findall(r'([\w./-]+\.(?:ts|tsx|js|jsx|sql|py))[:(]', saida)
    info['arquivos'] = list(dict.fromkeys(info.get('arquivos', []) + arquivos))[:6]
    return {}

def nome_ferramenta(entrada):
    nome = str(entrada.get('tool_name') or entrada.get('toolName') or entrada.get('name') or '')
    m = FERRAMENTA.search(nome)
    return m.group(1) if m else None

def after_mcp(entrada, conversa):
    nome = nome_ferramenta(entrada)
    if nome not in {'consulta_iniciar', 'consulta_status', 'consulta_cancelar'}:
        return {}
    resultado = entrada.get('result_json') or entrada.get('result') or entrada.get('tool_output') or ''
    texto = resultado if isinstance(resultado, str) else json.dumps(resultado, ensure_ascii=False)
    for id_consulta in set(re.findall(r'\b[a-f0-9]{32}\b', texto)):
        estado = re.search(r'"estado"\s*:\s*\\?"(\w+)', texto)
        conversa.d['consultas'][id_consulta] = {'estado': estado.group(1) if estado else '?', 'em': agora()}
    if nome == 'consulta_iniciar':
        conversa.d['chamou_em'] = agora()
    return {}

def after_file_edit(entrada, conversa):
    caminho = entrada.get('file_path') or entrada.get('path')
    if caminho:
        raiz = raiz_git(Path(caminho).expanduser().parent)
        if raiz:
            lista = conversa.d['editados'].setdefault(str(raiz), [])
            if caminho not in lista:
                lista.append(caminho)
            conversa.d.setdefault('primeira_edicao', {}).setdefault(str(raiz), agora())
    return {}

def after_response(entrada, conversa):
    texto = str(entrada.get('text') or entrada.get('response') or entrada.get('content') or '')
    if ANUNCIO.search(texto):
        conversa.d['anunciou_em'] = agora()
    return {}

def before_prompt(entrada, conversa):
    texto = str(entrada.get('prompt') or entrada.get('text') or '')
    if '#sem-consulta' in texto:
        conversa.d['sem_consulta'] = True
        registrar('hook_sem_consulta')
    conversa.d.pop('anunciou_em', None)
    conversa.d['turno_em'] = agora()
    conversa.d['retomadas'] = 0
    return {'continue': True}

def consulta_ativa(conversa):
    for id_consulta in conversa.d['consultas']:
        try:
            job = json.loads((STATE/id_consulta/'job.json').read_text())
        except (OSError, ValueError):
            continue
        if job.get('estado') not in {'concluida', 'falhou', 'cancelada', 'expirada'}:
            return id_consulta
    return None

def retomar(conversa, mensagem, regra):
    conversa.d['retomadas'] = conversa.d.get('retomadas', 0)+1
    registrar('hook_retomou', regra=regra, mensagem=mensagem[:200])
    return {'followup_message': mensagem}

def stop(entrada, conversa):
    if conversa.d.get('sem_consulta'):
        return {}
    voltas = max(int(entrada.get('loop_count') or 0), conversa.d.get('retomadas', 0))
    if voltas >= MAX_RETOMADAS or str(entrada.get('status', 'completed')) not in {'completed', 'complete', 'success'}:
        return {}
    ativa = consulta_ativa(conversa)
    if ativa:
        return retomar(conversa, f'A consulta {ativa} da valt-ponte ainda está ativa. Chame consulta_status '
                       f'(id_consulta "{ativa}", espera_segundos 25) até o estado final e avalie o parecer antes de encerrar.', 'consulta_ativa')
    anunciou = conversa.d.get('anunciou_em')
    # afterAgentResponse chega no fim do turno: basta ter chamado consulta_iniciar em qualquer ponto do turno.
    chamou = conversa.d.get('chamou_em')
    chamou_no_turno = chamou is not None and chamou >= conversa.d.get('turno_em', 0)
    if anunciou and not chamou_no_turno:
        return retomar(conversa, 'Você anunciou uma consulta ao consultor mas encerrou sem chamar a ferramenta. Chame agora '
                       'consulta_iniciar do servidor MCP valt-ponte (provedor claude, modo investigador, interativo false, '
                       'id_pedido único) e depois consulta_status até o estado final, no mesmo turno. Task/subagente não substitui.', 'anunciou')
    for raiz, arquivos in conversa.d['editados'].items():
        if len(arquivos) <= LIMITE_ARQUIVOS:
            continue
        mapeado = repo_mapeado(Path(raiz))
        if not mapeado:
            continue
        _, repo_rel, projeto = mapeado
        if consulta_depois(repo_rel, conversa.d.get('primeira_edicao', {}).get(raiz, 0)):
            continue
        relativos = [Path(a).resolve().relative_to(Path(raiz)).as_posix() for a in arquivos if Path(a).resolve().is_relative_to(Path(raiz))]
        return retomar(conversa, f'Você alterou {len(arquivos)} arquivos em {repo_rel} nesta conversa. Antes de declarar pronto, faça a revisão final. '
                       + chamada_pronta(projeto, repo_rel, relativos[:6], 'Revisão final: riscos e o que faltou nesta implementação',
                                        repo_rel+'|'+str(len(arquivos))), 'revisao_final')
    return {}

EVENTOS = {'beforeShellExecution': before_shell, 'postToolUse': after_tool,
           'postToolUseFailure': lambda entrada, conversa: after_tool(entrada, conversa, falhou=True), 'afterMCPExecution': after_mcp, 'afterFileEdit': after_file_edit,
           'afterAgentResponse': after_response, 'beforeSubmitPrompt': before_prompt, 'stop': stop}

def processar(evento, bruto):
    if (STATE/'desligado').exists():
        return neutro(evento)
    entrada = json.loads(bruto) if bruto.strip() else {}
    if not isinstance(entrada, dict):
        raise ValueError('entrada não é objeto JSON')
    funcao = EVENTOS.get(evento)
    if not funcao:
        return neutro(evento)
    if evento in {'postToolUse', 'postToolUseFailure'} and str(entrada.get('tool_name', '')) != 'Shell':
        return {}
    conversa = Conversa(entrada)
    try:
        resposta = funcao(entrada, conversa)
    finally:
        conversa.salvar()
    return resposta if resposta else neutro(evento)

def main():
    evento = sys.argv[1] if len(sys.argv) > 1 else ''
    def estourou(signum, frame):
        raise TimeoutError(f'hook passou de {LIMITE_S} s')
    signal.signal(signal.SIGALRM, estourou)
    signal.alarm(LIMITE_S)
    try:
        resposta = processar(evento, sys.stdin.read())
    except Exception:
        log(f'{evento}: liberado por exceção\n{traceback.format_exc()}')
        resposta = neutro(evento)
    finally:
        signal.alarm(0)
    print(json.dumps(resposta, ensure_ascii=False))

if __name__ == '__main__':
    main()
