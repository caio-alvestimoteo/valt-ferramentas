#!/usr/bin/env python3
"""Hooks que obrigam a consulta da valt-ponte nos momentos de risco — núcleo das regras e adaptador do Cursor.

Uso (em ~/.cursor/hooks.json): python3 ponte_hook.py <evento>, com a entrada JSON do Cursor no stdin.
O Antigravity usa as mesmas regras por ponte_hook_antigravity.py, que traduz a entrada e a resposta.

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
IDE = 'cursor'  # o adaptador do Antigravity troca ao carregar

SENSIVEL = re.compile(r'auth\.|\bgrant\b|\bpolicy\b|security\s+definer|e-?mail|telefone|phone|avatar|foto|'
                      r'\bnome\b|full_name|first_name|last_name|\bcpf\b', re.I)
GIT_OPCOES = r'(?:\s+(?:-C|-c|--git-dir|--work-tree)\s+(?:"[^"]*"|\'[^\']*\'|\S+)|\s+--?[\w-]+(?:=\S+)?)*'
COMMIT = re.compile(r'(?:^|[\s;&|({])git'+GIT_OPCOES+r'\s+commit\b')
ADD = re.compile(r'(?:^|[\s;&|({])git'+GIT_OPCOES+r'\s+add\b(.*)')
COMMIT_TUDO = re.compile(r'\s-[a-zA-Z]*a[a-zA-Z]*\b|\s--all\b')
DB_PUSH = re.compile(r'\bsupabase\s+db\s+push\b|\bpsql\b.*(postgres(ql)?://|\s-h\s)')
TESTE = re.compile(r'\b(tsc|jest|vitest|pg_prove|pgtap)\b|supabase\s+test\b|\bnpm\s+(run\s+)?test\b|\bnpx\s+(tsc|jest|vitest)\b')
ERRO = re.compile(r'error TS\d+[^\n]*|^\s*●[^\n]+|^FAIL\b[^\n]*|^not ok\b[^\n]*|^\s*(Error|AssertionError|TypeError):[^\n]*', re.M)
ANUNCIO = re.compile(r'(abrir|iniciar|chamar|disparar|pedir|fazer)\b[^.\n]{0,50}(consulta|parecer)\b[^.\n]{0,50}(claude|codex|consultor|ponte|ptyxis)'
                     r'|consultar (o )?(claude|codex)\b|segunda opini[aã]o (do|ao|com o|pelo) (claude|codex)'
                     r'|(abrir|iniciar|chamar)\b[^.\n]{0,30}(consulta_iniciar|valt-ponte|o consultor)', re.I)
# Configuração da ponte nas duas IDEs: o agente não edita (o instalador de cada IDE é a fonte).
ARQUIVO_CONFIG = r'(?:\.cursor/(?:mcp|hooks)|\.gemini/config/(?:mcp_config|hooks)|\.agents/(?:mcp_config|hooks))\.json'
CONFIG = re.compile(ARQUIVO_CONFIG)
INSTALADORES = {'cursor': 'bootstrap/cursor/ponte-hooks.sh', 'antigravity': 'bootstrap/antigravity/ponte-hooks.sh'}
AVISO_CONFIG = ('~/.cursor/mcp.json e hooks.json (Cursor) e ~/.gemini/config/mcp_config.json e hooks.json (Antigravity) '
                'são mantidos pelos instaladores em ~/Valt/bootstrap/<ide>/ponte-hooks.sh')
MATA_PONTE = re.compile(r'\b(p?kill|killall)\b[^;&|]*(ponte|\b\d+\b)')
FERRAMENTA = re.compile(r'(consulta_iniciar|consulta_dupla|consulta_rodada|consulta_status|consulta_cancelar|contexto_valt)$')
# O nome da ferramenta de plano muda por família de modelo (CreatePlan/create_plan/mcp_create_plan).
CRIA_PLANO = re.compile(r'^(create_?plan|mcp_create_plan)$', re.I)
TROCA_MODO = re.compile(r'^switch_?mode$', re.I)
# Em Plan mode o Cursor só deixa editar markdown; escrita fora disso é a implementação começando.
MARKDOWN = re.compile(r'\.(md|markdown|mdx)$', re.I)
LIMITE_PLANO_HOOK = 40000

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
    linha = {'hora': datetime.now().astimezone().isoformat(timespec='seconds'), 'ferramenta': evento, 'origem': 'hook', 'ide': IDE}
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

_CACHE_RAIZ = {}

def raiz_git(pasta):
    """Raiz do repositório, memorizada por processo.

    `alvo_do_plano` testa várias candidatas em toda escrita não-markdown; sem cache cada uma
    custava um subprocess `git rev-parse` com timeout de 3 s, dentro do alarme de LIMITE_S."""
    if not pasta:
        return None
    chave = str(pasta)
    if chave not in _CACHE_RAIZ:
        saida = git(pasta, 'rev-parse', '--show-toplevel')
        _CACHE_RAIZ[chave] = Path(saida.strip()).resolve() if saida else None
    return _CACHE_RAIZ[chave]

_CACHE_INDICE = {}

def indice():
    """{repositorio relativo a Sites: projeto do Valt} a partir de indices/repositorios.md.

    Memorizado por processo: o hook é um processo curto e relia o arquivo por candidata."""
    if 'mapa' in _CACHE_INDICE:
        return _CACHE_INDICE['mapa']
    mapa = {}
    try:
        texto = (VAULT/'indices/repositorios.md').read_text(encoding='utf-8')
    except OSError:
        return mapa  # sem índice o espelhamento ainda resolve; não memoriza a falha
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
    _CACHE_INDICE['mapa'] = mapa
    return mapa

def projeto_espelhado(rel):
    """As quatro árvores compartilham o caminho relativo: ~/Sites/Seara/ricca ↔ ~/Valt/Seara/ricca.

    O índice é um atalho e vive desatualizado (ricca e gradina não estavam lá em 17/09,
    embora tivessem documentação). O espelhamento é a regra do ambiente e não caduca.

    Exige o caminho COMPLETO: subir para o ancestral faria todo repositório desconhecido de
    ~/Sites/Seara virar projeto "Seara" (existe README lá), montando o dossiê sobre a
    documentação errada e fazendo a trava por projeto colidir entre repositórios diferentes.
    A comparação ignora maiúsculas porque ~/Sites/Seara/food espelha ~/Valt/Seara/Food."""
    alvo = Path(rel)
    if (VAULT/alvo/'README.md').is_file():
        return alvo.as_posix()
    pasta = VAULT.resolve()
    partes = []
    for parte in alvo.parts:
        achado = next((f.name for f in pasta.iterdir() if f.is_dir() and f.name.lower() == parte.lower()), None) \
            if pasta.is_dir() else None
        if not achado:
            return None
        partes.append(achado)
        pasta = pasta/achado
    return '/'.join(partes) if (pasta/'README.md').is_file() else None

def projeto_do_caminho_valt(rel):
    """Projeto dono de um caminho JÁ dentro do Valt: sobe até a pasta com README.

    Diferente de `projeto_espelhado`, aqui subir é correto — o plano cita um arquivo de
    documentação (`~/Valt/Seara/ricca/Operacao/nota.md`) e queremos o projeto dele, não uma
    correspondência com repositório.
    """
    pasta = (VAULT/rel).resolve()
    raiz = VAULT.resolve()
    if not pasta.is_relative_to(raiz):
        return None
    while pasta != raiz:
        if (pasta/'README.md').is_file():
            return pasta.relative_to(raiz).as_posix()
        pasta = pasta.parent
    return None

def repo_mapeado(pasta):
    """(raiz, repositorio relativo, projeto) se a pasta é de um repositório conhecido; senão None."""
    raiz = raiz_git(pasta)
    if not raiz or not raiz.is_relative_to(SITES):
        return None
    rel = raiz.relative_to(SITES).as_posix()
    projeto = indice().get(rel) or projeto_espelhado(rel)
    return (raiz, rel, projeto) if projeto else None

JANELA_JOBS = 30*24*3600

def jobs():
    limite = agora() - JANELA_JOBS
    for arquivo in STATE.glob('*/job.json'):
        try:
            if arquivo.stat().st_mtime < limite:
                continue  # consultas com mais de 30 dias não cobrem nada e só deixariam o hook lento
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

TETO_RODADAS_HORA = 6
MAX_TENTATIVAS_RODADA = 2
PLANOS_CURSOR = Path.home()/'.cursor/plans'

LIMITE_TRANSCRICAO = 2_000_000

def linhas_da_transcricao(caminho):
    """Linhas da transcrição, lidas uma vez por evento e com teto.

    Sem o teto, uma conversa longa (transcrições do Cursor chegam a dezenas de MB) estoura o
    alarme de LIMITE_S e derruba TODAS as regras do stop em silêncio — inclusive a rodada
    automática. Só o fim do arquivo interessa: o plano e o #sem-consulta são recentes."""
    if not caminho:
        return []
    arquivo = Path(caminho)
    try:
        if not arquivo.is_file():
            return []
        with arquivo.open('rb') as fonte:
            tamanho = arquivo.stat().st_size
            if tamanho > LIMITE_TRANSCRICAO:
                fonte.seek(tamanho - LIMITE_TRANSCRICAO)
                fonte.readline()  # descarta a linha partida ao meio
            bruto = fonte.read()
    except OSError:
        return []
    return bruto.decode('utf-8', errors='replace').splitlines()

def itens_da_transcricao(caminho):
    for linha in linhas_da_transcricao(caminho):
        try:
            yield json.loads(linha)
        except ValueError:
            continue

def blocos_da_transcricao(caminho, itens=None):
    """Blocos tool_use da transcrição, na ordem em que aconteceram."""
    for item in (itens if itens is not None else itens_da_transcricao(caminho)):
        conteudo = (item.get('message') or {}).get('content')
        if not isinstance(conteudo, list):
            continue
        for bloco in conteudo:
            if isinstance(bloco, dict) and bloco.get('type') == 'tool_use':
                yield bloco

def plano_da_transcricao(entrada, itens=None):
    """O plano mais recente da conversa, lido da transcrição.

    O Cursor 3.20 não dispara hook para CreatePlan (só Read, Grep e Shell chegam ao
    preToolUse), mas a transcrição registra a chamada com o plano inteiro."""
    achado = ''
    for bloco in blocos_da_transcricao(entrada.get('transcript_path'), itens):
        nome = str(bloco.get('name') or '')
        argumentos = bloco.get('input') if isinstance(bloco.get('input'), dict) else {}
        if not CRIA_PLANO.match(nome):
            # Ferramenta dinâmica: o nome real vai em input.toolName.
            if not CRIA_PLANO.match(str(argumentos.get('toolName') or '')):
                continue
            argumentos = argumentos.get('arguments') if isinstance(argumentos.get('arguments'), dict) else argumentos
        for campo in ('plan', 'plano', 'content'):
            texto = argumentos.get(campo)
            if isinstance(texto, str) and texto.strip():
                achado = texto.strip()[:LIMITE_PLANO_HOOK]
    return achado

def plano_do_disco(desde):
    """Rede de segurança: o Cursor materializa cada plano em ~/.cursor/plans/*.plan.md.

    Exige uma janela de tempo: sem o início do turno, o mais recente poderia ser de outra
    conversa — ou de outra janela do Cursor — e a rodada sairia sobre o plano errado."""
    if not desde:
        return ''
    try:
        recentes = [p for p in PLANOS_CURSOR.glob('*.plan.md') if p.stat().st_mtime >= desde]
    except OSError:
        return ''
    if not recentes:
        return ''
    mais_novo = max(recentes, key=lambda p: p.stat().st_mtime)
    try:
        return mais_novo.read_text(encoding='utf-8', errors='replace')[:LIMITE_PLANO_HOOK].strip()
    except OSError:
        return ''

def rodadas_na_ultima_hora():
    limite = agora() - 3600
    total = 0
    # O arquivo é append-only e nunca rotacionado: ler só a cauda, não os meses todos.
    try:
        with (STATE/'chamadas.jsonl').open('rb') as fonte:
            tamanho = fonte.seek(0, os.SEEK_END)
            fonte.seek(max(0, tamanho - 200_000))
            linhas = fonte.read().decode('utf-8', errors='replace').splitlines()
    except OSError:
        return 0
    for linha in reversed(linhas[-400:]):
        try:
            item = json.loads(linha)
        except ValueError:
            continue
        if item.get('ferramenta') != 'hook_abriu_rodada' or item.get('ok') is False:
            continue  # rodada que falhou não consumiu cota nem janela
        try:
            quando = datetime.fromisoformat(item['hora']).timestamp()
        except (KeyError, ValueError):
            continue
        if quando >= limite:
            total += 1
    return total

def abrir_rodada_em_segundo_plano(projeto, repo_rel, plano, pergunta, prefixo='plano'):
    """Dispara a ponte fora do processo do hook: aqui só há orçamento de milissegundos."""
    pedido = {'projeto': projeto, 'repositorio': repo_rel, 'pergunta': pergunta,
              'plano': plano[:LIMITE_PLANO_HOOK], 'id_pedido': id_da_rodada(plano, prefixo)}
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent/'ponte.py'),
                          'rodada-automatica', json.dumps(pedido, ensure_ascii=False)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False

def trocar_plano(conversa, plano):
    """Plano novo recomeça o ciclo: crítica, entrega dos pareceres e conferência final.

    Sem zerar as três marcas, o segundo plano de uma conversa não recebe pareceres
    (`pareceres_entregues`) e o commit dele passa sem conferência (`plano_conferido`)."""
    if conversa.d.get('plano') == plano:
        return False
    conversa.d['plano'] = plano
    for marca in ('plano_criticado', 'pareceres_entregues', 'plano_conferido'):
        conversa.d.pop(marca, None)
    return True

def id_da_rodada(plano, prefixo='plano'):
    return prefixo+'-'+hashlib.sha256(plano.encode()).hexdigest()[:16]

FINAIS_CONSULTA = {'concluida', 'falhou', 'cancelada', 'expirada'}

def rodada_situacao(id_pedido):
    """(aberta, encerrada): há consultas dessa rodada? nenhuma ainda está em andamento?

    `encerrada` é True mesmo quando todos os provedores falharam: sem parecer nenhum a
    conversa segue em frente. Falha de provedor (cota, login) nunca pode travar o trabalho —
    é a mesma filosofia do resto do hook, que libera em qualquer erro."""
    encontrados = [j for j in jobs() if j.get('id_pedido') == id_pedido]
    if not encontrados:
        return False, False
    return True, all(j.get('estado') in FINAIS_CONSULTA for j in encontrados)

def rodada_teve_parecer(id_pedido):
    return any(j.get('id_pedido') == id_pedido and j.get('estado') == 'concluida' for j in jobs())

def chamada_dupla_pronta(projeto, repo_rel, plano, pergunta, prefixo='plano'):
    """A rodada de entrada e a de saída têm ids distintos: senão a crítica já concluída
    passaria por conferência e a 2ª rodada nunca aconteceria."""
    args = {'projeto': projeto, 'repositorio': repo_rel, 'pergunta': pergunta,
            'plano': plano[:LIMITE_PLANO_HOOK], 'id_pedido': id_da_rodada(plano, prefixo)}
    fecho = ('compare os dois pareceres com o seu plano e só então implemente'
             if prefixo == 'plano' else
             'avalie os pareceres e só então repita o commit')
    return ('Chame a ferramenta MCP consulta_dupla do servidor valt-ponte com exatamente estes argumentos:\n'
            + json.dumps(args, ensure_ascii=False)
            + '\nEla abre Claude e Codex em duas janelas do Ptyxis (ou dentro da IDE, se ela consome a fila). '
              'Depois chame consulta_rodada (id_pedido igual, espera_segundos 25) até estado final, '
            + fecho + '. Não substitua por Task/subagente.')

def entrada_da_ferramenta(entrada):
    dados = entrada.get('tool_input')
    if isinstance(dados, str):
        try:
            dados = json.loads(dados)
        except ValueError:
            dados = {}
    return dados if isinstance(dados, dict) else {}

def nome_interno(entrada):
    """Ferramenta dinâmica da IDE: o nome real vem em tool_input.toolName."""
    return str(entrada_da_ferramenta(entrada).get('toolName') or '')

def argumentos_da_ferramenta(entrada):
    """Argumentos reais: a ferramenta dinâmica os embrulha em tool_input.arguments."""
    dados = entrada_da_ferramenta(entrada)
    return dados['arguments'] if isinstance(dados.get('arguments'), dict) else dados

def e_ferramenta(entrada, padrao):
    return bool(padrao.match(str(entrada.get('tool_name') or '')) or padrao.match(nome_interno(entrada)))

def plano_da_ferramenta(entrada):
    """Devolve o texto do plano quando o evento é a ferramenta de plano da IDE."""
    if not e_ferramenta(entrada, CRIA_PLANO):
        return ''
    argumentos = argumentos_da_ferramenta(entrada)
    for campo in ('plan', 'plano', 'overview', 'content'):
        texto = argumentos.get(campo)
        if isinstance(texto, str) and texto.strip():
            return texto.strip()[:LIMITE_PLANO_HOOK]
    return ''

def saindo_do_plano(entrada):
    """SwitchMode para agent: o agente vai parar de planejar e começar a mexer."""
    if not e_ferramenta(entrada, TROCA_MODO):
        return False
    return str(argumentos_da_ferramenta(entrada).get('target_mode_id') or '').lower() not in {'plan', ''}

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
    # O Cursor 3.18 só repassa user_message ao agente (visto no cursor-agent); a instrução vai nos dois.
    return {'permission': 'deny', 'user_message': 'valt-ponte: '+mensagem_agente,
            'agent_message': mensagem_agente}

# --- regras ---------------------------------------------------------------------------

def alterados_no_disco(raiz, prefixo=''):
    modificados = (git(raiz, 'diff', '--name-only', 'HEAD', '--', prefixo or '.') or git(raiz, 'diff', '--name-only', '--', prefixo or '.') or '')
    novos = git(raiz, 'ls-files', '--others', '--exclude-standard', '--', prefixo or '.') or ''
    return [n for n in (modificados+'\n'+novos).split('\n') if n]

def trechos(comando):
    """Quebra o comando em trechos simples: desembrulha `bash -c "…"`, subshell `( )` e bloco `{ }`."""
    comando = str(comando)
    for _ in range(3):
        interno = re.search(r'\b(?:ba|z)?sh\s+-l?c\s+("(?:[^"\\]|\\.)*"|\'[^\']*\')', comando)
        if not interno:
            break
        corpo = interno.group(1)[1:-1].replace('\\"', '"')
        comando = comando[:interno.start()] + ' ; ' + corpo + ' ; ' + comando[interno.end():]
    for trecho in re.split(r'&&|\|\||;|\||\n', comando):
        trecho = trecho.strip().lstrip('({').rstrip(')}').strip()
        trecho = re.sub(r'^(?:\w+=(?:"[^"]*"|\'[^\']*\'|\S+)\s+)+', '', trecho)  # VAR=x git …
        if trecho:
            yield trecho

def caminho_do_git(trecho, atual):
    via_c = re.search(r'\bgit\b[^;&|]*?\s-C\s+("[^"]+"|\'[^\']+\'|\S+)', trecho)
    if not via_c:
        return atual
    destino = Path(os.path.expanduser(via_c.group(1).strip('"\'')))
    return destino if destino.is_absolute() or atual is None else (atual/destino)

def adicionados_no_comando(raiz, comando, pasta):
    """Arquivos que um `git add` no mesmo comando vai colocar no stage antes do commit."""
    nomes = []
    for trecho in trechos(comando):
        m = ADD.search(trecho)
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
    """Pasta onde roda o trecho do comando que casa com `alvo`, seguindo `cd X`, `git -C X` e subshells."""
    atual = Path(pasta).expanduser() if pasta else None
    for trecho in trechos(comando):
        mudanca = re.match(r'^(?:cd|pushd)\s+("[^"]+"|\'[^\']+\'|\S+)\s*$', trecho)
        if mudanca:
            destino = Path(os.path.expanduser(mudanca.group(1).strip('"\'')))
            atual = destino if destino.is_absolute() or atual is None else (atual/destino)
            continue
        if alvo.search(' '+trecho):
            return caminho_do_git(trecho, atual)
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

def processos_da_ponte():
    saida = subprocess.run(['pgrep', '-f', 'ponte.py (mcp|worker)'], capture_output=True, text=True, timeout=2).stdout
    return set(saida.split())

def escreve_config(comando):
    """Só quando o destino da escrita é a configuração da ponte (ler ou copiar dela para outro lugar é livre)."""
    alvo = r'[^;&|]*?' + ARQUIVO_CONFIG
    for trecho in trechos(comando):
        if not CONFIG.search(trecho):
            continue
        if re.search(r'>{1,2}\s*["\']?[^\s;&|]*' + ARQUIVO_CONFIG, trecho):
            return True
        if re.search(r'^(?:sudo\s+)?(?:sed\s+-\S*i|perl\s+-\S*i|truncate|chmod|chown|rm|ln|unlink)\b' + alvo, trecho):
            return True
        if re.search(r'^(?:sudo\s+)?(?:tee)\b' + alvo, trecho) or re.search(r'\btee\b' + alvo, trecho):
            return True
        destino = re.match(r'^(?:sudo\s+)?(?:cp|mv|install|rsync)\b.*\s(\S+)\s*$', trecho)
        if destino and CONFIG.search(destino.group(1)):
            return True
        if re.search(r'\b(?:python3?|node|ruby|jq)\b', trecho) and re.search(r"open\([^)]*,\s*['\"][wa]|write_text|writeFile|>\s*\S*\.(?:cursor|gemini|agents)/|-i\b", trecho):
            return True
    return False

def regra_protecao(comando):
    """A configuração da ponte e seus processos não são do agente: nega escrita e kill."""
    if escreve_config(comando):
        return negar('alteração da configuração da ponte barrada',
                     'O hook da valt-ponte barrou este comando: ' + AVISO_CONFIG + ' e não devem ser editados pelo agente. '
                     'Se a ponte não funciona, relate o erro ao usuário em vez de alterar a configuração.', regra='protecao')
    alvo = MATA_PONTE.search(comando)
    if alvo and ('ponte' in alvo.group(0) or set(re.findall(r'\b\d+\b', alvo.group(0))) & processos_da_ponte()):
        return negar('encerrar processo da ponte barrado',
                     'O hook da valt-ponte barrou este comando: não encerre o servidor MCP nem o executor da ponte. '
                     'Se a ponte não responde, relate ao usuário.', regra='protecao')
    return None

FERRAMENTAS_LEITURA = {'read', 'read_file', 'grep', 'glob', 'ls', 'list_dir', 'codebase_search', 'semanticsearch', 'fetch',
                       # Antigravity
                       'view_file', 'view_file_outline', 'view_code_item', 'grep_search', 'find_by_name', 'read_url_content',
                       'search_web', 'read_terminal', 'command_status'}

def caminhos(valor, chave=''):
    """Só valores de campos de caminho (path, file_path, target_file…): o conteúdo escrito não conta."""
    if isinstance(valor, str):
        if re.search(r'path|file|target|uri|dest', chave, re.I):
            yield valor
    elif isinstance(valor, dict):
        for nome, item in valor.items():
            yield from caminhos(item, str(nome))
    elif isinstance(valor, list):
        for item in valor:
            yield from caminhos(item, chave)

CAMINHO_CITADO = re.compile(r'~/Sites/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)')
PROJETO_CITADO = re.compile(r'~/Valt/([A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)')

def alvo_do_plano(entrada, conversa, plano=''):
    """(projeto, repositório) a que o plano se refere, ou None.

    Procura em ordem: cwd, workspaces, o que já foi editado e **os caminhos citados no
    próprio plano** — um plano transversal costuma não ter repositório aberto, mas nomeia
    os que toca. Em último caso aceita projeto do Valt sem repositório: plano de
    documentação também merece crítica."""
    candidatas = []
    pasta = pasta_da_entrada(entrada)
    if pasta:
        candidatas.append(pasta)
    candidatas += [Path(r).expanduser() for r in (entrada.get('workspace_roots') or [])]
    candidatas += [Path(r) for r in conversa.d.get('editados', {})]
    candidatas += [SITES/rel for rel in CAMINHO_CITADO.findall(plano)]
    for candidata in candidatas:
        mapeado = repo_mapeado(candidata)
        if mapeado:
            return mapeado[2], mapeado[1]
    # Sem repositório: o consultor investiga a documentação do projeto no Valt.
    for rel in PROJETO_CITADO.findall(plano):
        projeto = projeto_do_caminho_valt(rel)
        if projeto:
            return projeto, ''
    for raiz in (entrada.get('workspace_roots') or []):
        caminho = Path(raiz).expanduser().resolve()
        if caminho.is_relative_to(VAULT.resolve()) and caminho != VAULT.resolve():
            projeto = projeto_do_caminho_valt(caminho.relative_to(VAULT.resolve()).as_posix())
            if projeto:
                return projeto, ''
    return None

def repo_do_plano(entrada, conversa):
    """Compatível com as regras que precisam de (raiz, repositório, projeto)."""
    alvo = alvo_do_plano(entrada, conversa, conversa.d.get('plano') or '')
    if not alvo or not alvo[1]:
        return None
    return repo_mapeado(SITES/alvo[1])

def regra_plano_sem_critica(entrada, conversa):
    """Há plano nesta conversa e ninguém o criticou: não começa a implementar ainda.

    O gatilho é o plano, não o volume de arquivos: é no momento de decidir a abordagem
    que a segunda opinião vale mais."""
    plano = conversa.d.get('plano') or ''
    if not plano or conversa.d.get('plano_criticado'):
        return None
    if pediu_sem_consulta(entrada, conversa):
        return None
    id_pedido = id_da_rodada(plano)
    aberta, encerrada = rodada_situacao(id_pedido)
    if encerrada:
        conversa.d['plano_criticado'] = True
        if not rodada_teve_parecer(id_pedido):
            registrar('hook_plano', ok=False, regra='plano_sem_critica',
                      mensagem='rodada terminou sem nenhum parecer; implementação liberada')
        return None
    mapeado = repo_do_plano(entrada, conversa)
    if not mapeado:
        return None  # fora de repositório mapeado a ponte não tem o que consultar
    _, repo_rel, projeto = mapeado
    if aberta:
        return negar('rodada de crítica do plano em andamento',
                     'A rodada de crítica deste plano já foi aberta e ainda não terminou. '
                     f'Chame consulta_rodada com id_pedido "{id_pedido}" e espera_segundos 25 até o estado final, '
                     'compare os pareceres com o seu plano e só então implemente.',
                     projeto=projeto, repositorio=repo_rel, regra='plano_sem_critica')
    return negar('plano ainda não criticado por Claude e Codex',
                 'Este plano ainda não passou pela crítica dos consultores. '
                 + chamada_dupla_pronta(projeto, repo_rel, plano,
                                        'Critique este plano: riscos, o que faltou, o que está errado na abordagem.'),
                 projeto=projeto, repositorio=repo_rel, regra='plano_sem_critica')

def pre_tool(entrada, conversa):
    nome = str(entrada.get('tool_name') or '')
    # O plano aparece aqui antes de existir em qualquer outro lugar; guardar não custa nada.
    plano = plano_da_ferramenta(entrada)
    if plano:
        if trocar_plano(conversa, plano):
            registrar('hook_plano', mensagem='plano capturado ('+str(len(plano))+' caracteres)')
        return {}  # nunca barrar a criação do plano: é ele que será criticado
    if e_ferramenta(entrada, TROCA_MODO):
        if saindo_do_plano(entrada):
            pendente = regra_plano_sem_critica(entrada, conversa)
            if pendente:
                return pendente
        return {}  # trocar de modo não escreve arquivo: não passa pela regra de caminhos
    if nome.lower() in FERRAMENTAS_LEITURA or nome.startswith('MCP:') or nome == 'Shell':
        return {}
    ferramenta = entrada.get('tool_input')
    if isinstance(ferramenta, str):
        try:
            ferramenta = json.loads(ferramenta)
        except ValueError:
            pass
    if any(CONFIG.search(texto) for texto in caminhos(ferramenta)):
        registrar('hook_negou', mensagem=f'edição da configuração da ponte barrada ({nome})', regra='protecao')
        aviso = ('valt-ponte: edição barrada — ' + AVISO_CONFIG + '. Não altere essa configuração; '
                 'se a ponte falhar, relate o erro ao usuário.')
        return {'permission': 'deny', 'user_message': aviso, 'agent_message': aviso}
    # Markdown ainda é planejamento; qualquer outro arquivo é a implementação começando.
    alvos = [texto for texto in caminhos(ferramenta) if texto]
    if alvos and not all(MARKDOWN.search(texto) for texto in alvos):
        pendente = regra_plano_sem_critica(entrada, conversa)
        if pendente:
            return pendente
    return {}

def restaurar_config(caminho=''):
    ide = 'antigravity' if re.search(r'\.(gemini|agents)/', str(caminho)) else 'cursor'
    instalador = VAULT/INSTALADORES[ide]
    if instalador.is_file():
        subprocess.run(['bash', str(instalador), 'instalar', '--aplicar'], capture_output=True, timeout=3)

def pediu_sem_consulta(entrada, conversa, itens=None):
    """#sem-consulta vem do beforeSubmitPrompt (IDE) ou das mensagens do usuário na transcrição (cursor-agent não dispara o evento)."""
    if conversa.d.get('sem_consulta'):
        return True
    for item in (itens if itens is not None else itens_da_transcricao(entrada.get('transcript_path'))):
        if item.get('role') != 'user':
            continue
        conteudo = item.get('message', {}).get('content', [])
        texto = ' '.join(p.get('text', '') for p in conteudo if isinstance(p, dict)) if isinstance(conteudo, list) else str(conteudo)
        if '#sem-consulta' in texto:
            conversa.d['sem_consulta'] = True
            registrar('hook_sem_consulta', origem='hook', mensagem='lido da transcrição')
            return True
    return False

def pendencia_revisao(conversa):
    """(repo_rel, projeto, relativos, raiz) do primeiro repositório com mais de 20 arquivos editados sem revisão final."""
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
        return repo_rel, projeto, relativos, raiz
    return None

def regra_conferencia_no_commit(comando, pasta, conversa):
    """Plano criticado e implementado: o commit espera a conferência entre o feito e o combinado.

    É a 2ª rodada — a 1ª critica a abordagem, esta confere o resultado."""
    if not COMMIT.search(comando):
        return None
    plano = conversa.d.get('plano') or ''
    if not plano or not conversa.d.get('plano_criticado') or conversa.d.get('plano_conferido'):
        return None
    id_pedido = id_da_rodada(plano, 'confere')
    aberta, encerrada = rodada_situacao(id_pedido)
    if encerrada:
        conversa.d['plano_conferido'] = True
        if not rodada_teve_parecer(id_pedido):
            registrar('hook_plano', ok=False, regra='conferencia_final',
                      mensagem='conferência terminou sem nenhum parecer; commit liberado')
        return None
    alvo = repo_mapeado(diretorio_efetivo(comando, pasta, COMMIT))
    if not alvo:
        return None
    raiz, repo_rel, projeto = alvo
    if aberta:
        return negar('conferência final do plano em andamento',
                     'A conferência final deste plano já foi aberta e ainda não terminou. '
                     f'Chame consulta_rodada com id_pedido "{id_pedido}" e espera_segundos 25 até o estado final, '
                     'avalie os pareceres e só então repita o commit.',
                     projeto=projeto, repositorio=repo_rel, regra='conferencia_final')
    editados = conversa.d.get('editados', {}).get(str(raiz), [])
    relativos = [Path(a).resolve().relative_to(raiz).as_posix() for a in editados
                 if Path(a).resolve().is_relative_to(raiz)]
    pergunta = ('Conferência final: o que foi implementado corresponde ao plano? '
                'Aponte desvios, riscos e o que ficou faltando. Arquivos tocados: '
                + (', '.join(relativos[:20]) or '(nenhum registrado)'))
    # A 2ª rodada abre sozinha, como a 1ª: esperar o agente chamar é a premissa que falhou o
    # dia inteiro — o commit ficaria barrado para sempre.
    tentativas = conversa.d.get('confere_tentativas') or {}
    chave = id_pedido
    if (tentativas.get(chave, 0) < MAX_TENTATIVAS_RODADA
            and rodadas_na_ultima_hora() < TETO_RODADAS_HORA
            and abrir_rodada_em_segundo_plano(projeto, repo_rel, plano, pergunta, 'confere')):
        conversa.d['confere_tentativas'] = {**tentativas, chave: tentativas.get(chave, 0)+1}
        registrar('hook_plano', projeto=projeto, repositorio=repo_rel, regra='conferencia_final',
                  mensagem='conferência final aberta pelo hook; abrindo Claude e Codex')
        return negar('commit barrado: conferência final aberta agora',
                     f'O commit foi barrado: esta conversa implementou um plano ({len(relativos)} arquivos) '
                     'e a conferência final acabou de ser aberta em duas janelas do Ptyxis (ou dentro da IDE, se ela consome a fila). '
                     f'Chame consulta_rodada (id_pedido "{id_pedido}", espera_segundos 25) até o estado final, '
                     'avalie os pareceres e só então repita o commit.',
                     projeto=projeto, repositorio=repo_rel, regra='conferencia_final')
    return negar('commit barrado: plano implementado sem conferência final',
                 f'O commit foi barrado: esta conversa implementou um plano ({len(relativos)} arquivos) '
                 'e ainda não houve a conferência final. '
                 + chamada_dupla_pronta(projeto, repo_rel, plano, pergunta, 'confere'),
                 projeto=projeto, repositorio=repo_rel, regra='conferencia_final')

def regra_revisao_no_commit(comando, pasta, conversa):
    if not COMMIT.search(comando):
        return None
    pendente = pendencia_revisao(conversa)
    alvo = repo_mapeado(diretorio_efetivo(comando, pasta, COMMIT))
    if not pendente or not alvo or alvo[1] != pendente[0]:
        return None
    repo_rel, projeto, relativos, raiz = pendente
    return negar(f'commit barrado: {len(conversa.d["editados"][raiz])} arquivos editados sem revisão final',
                 f'O commit foi barrado pelo hook da valt-ponte: esta conversa editou {len(conversa.d["editados"][raiz])} arquivos em {repo_rel} '
                 'e ainda não houve revisão final. '
                 + chamada_pronta(projeto, repo_rel, relativos[:6], 'Revisão final: riscos e o que faltou nesta implementação',
                                  repo_rel+'|revisao|'+str(len(relativos))), projeto=projeto, repositorio=repo_rel, regra='revisao_final')

def before_shell(entrada, conversa):
    comando, pasta = comando_da_entrada(entrada), pasta_da_entrada(entrada)
    protecao = regra_protecao(comando)
    if protecao:
        return protecao
    if pediu_sem_consulta(entrada, conversa):
        return {}
    return (regra_migracao(entrada, comando, pasta) or regra_erro_repetido(conversa, comando, pasta)
            or regra_conferencia_no_commit(comando, pasta, conversa)
            or regra_revisao_no_commit(comando, pasta, conversa) or {})

def after_tool(entrada, conversa, falhou=False):
    """postToolUse/postToolUseFailure do Shell: conta a 1ª linha de erro de tsc/Jest/Vitest/pgTAP.

    afterShellExecution não serve: não traz cwd e dispara junto com postToolUse (contaria em dobro).
    Não tente capturar plano aqui: `processar` descarta postToolUse que não seja de Shell.
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
    base = entrada.get('cwd') or ferramenta.get('cwd') or pasta_da_entrada(entrada)
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
    if info['vezes'] == 2:
        raiz, repo_rel, projeto = mapeado
        return {'additional_context': f'valt-ponte: o mesmo erro de {tipo} apareceu 2 vezes ({assinatura}). A próxima execução '
                'será barrada até haver consulta. Consulte agora. '
                + chamada_pronta(projeto, repo_rel, info['arquivos'],
                                 f'O mesmo erro de {tipo} apareceu duas vezes: {assinatura}. Qual a causa raiz e a correção?',
                                 repo_rel+'|'+assinatura+'|'+str(info['segunda_em']))}
    return {}

def nome_ferramenta(entrada):
    nome = str(entrada.get('tool_name') or entrada.get('toolName') or entrada.get('name') or '')
    m = FERRAMENTA.search(nome)
    return m.group(1) if m else None

def after_mcp(entrada, conversa):
    nome = nome_ferramenta(entrada)
    if nome not in {'consulta_iniciar', 'consulta_dupla', 'consulta_rodada', 'consulta_status', 'consulta_cancelar'}:
        return {}
    resultado = entrada.get('result_json') or entrada.get('result') or entrada.get('tool_output') or ''
    texto = resultado if isinstance(resultado, str) else json.dumps(resultado, ensure_ascii=False)
    ids = set(re.findall(r'\b[a-f0-9]{32}\b', texto))
    estado = re.search(r'estado\\*"\s*:\s*\\*"(\w+)', texto)
    for id_consulta in ids:
        conversa.d['consultas'][id_consulta] = {'estado': estado.group(1) if estado else '?', 'em': agora()}
    # Só conta como chamada se a consulta existiu de fato (erro de MCP não devolve id).
    if nome in {'consulta_iniciar', 'consulta_dupla'} and ids:
        conversa.d['chamou_em'] = agora()
    # A crítica do plano acabou? A rodada é que diz, não o anúncio do agente.
    plano = conversa.d.get('plano')
    if plano and not conversa.d.get('plano_criticado'):
        _, encerrada = rodada_situacao(id_da_rodada(plano))
        if encerrada:
            conversa.d['plano_criticado'] = True
            registrar('hook_plano_criticado', mensagem='rodada concluída; implementação liberada')
    return {}

def after_file_edit(entrada, conversa):
    caminho = entrada.get('file_path') or entrada.get('path')
    if caminho and CONFIG.search(str(Path(caminho).expanduser())):
        restaurar_config(caminho)
        conversa.d['config_restaurada_em'] = agora()
        registrar('hook_restaurou', mensagem=str(caminho).replace(str(Path.home()), '~'))
        return {}
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

IDIOMA = ('Responda, narre, comente e nomeie tarefas sempre em português do Brasil, inclusive títulos e prompts '
          'de subagentes, mensagens de commit e notas. Identificadores, comandos, flags, nomes de arquivo e saída de '
          'ferramentas ficam como estão.')
LEMBRETE = ('valt-ponte: feito um plano, chame consulta_dupla (com o plano na íntegra) e consulta_rodada até o estado '
            'final no mesmo turno, antes de sair do Plan mode ou editar arquivo que não seja markdown — anunciar não conta. '
            'Para um provedor só, consulta_iniciar + consulta_status. Se um comando for negado pelo hook da valt-ponte, '
            'faça a consulta indicada na mensagem e não altere a configuração mcp/hooks da IDE.')

LIMITE_PARECER_INJETADO = 3000

def pareceres_da_rodada(id_pedido):
    """Pareceres já concluídos de uma rodada, para entregar ao chat sem o agente pedir."""
    achados = []
    for job in jobs():
        if job.get('id_pedido') != id_pedido or job.get('estado') != 'concluida':
            continue
        achados.append((job.get('provedor', '?'), (job.get('parecer') or '')[:LIMITE_PARECER_INJETADO],
                        job.get('registro', '')))
    return sorted(achados)

def entregar_pareceres(conversa):
    """O hook abriu a rodada; agora devolve o resultado ao chat por conta própria.

    Sem isto o agente só saberia dos pareceres se chamasse consulta_rodada — e contar com
    isso é justamente o que nunca funcionou."""
    plano = conversa.d.get('plano')
    if not plano or conversa.d.get('pareceres_entregues'):
        return ''
    id_pedido = id_da_rodada(plano)
    _, encerrada = rodada_situacao(id_pedido)
    if not encerrada:
        return ''
    achados = pareceres_da_rodada(id_pedido)
    if not achados:
        return ''
    conversa.d['pareceres_entregues'] = True
    conversa.d['plano_criticado'] = True
    registrar('hook_entregou_pareceres', mensagem=f'{len(achados)} parecer(es) da rodada {id_pedido}')
    partes = [f'### Parecer do {provedor} ({registro})\n{texto}' for provedor, texto, registro in achados]
    return ('\n\nvalt-ponte: os consultores criticaram o seu plano. Compare com o que você propôs — '
            'onde concordam, trate como achado firme; onde divergem, decida e diga por quê.\n\n'
            + '\n\n'.join(partes))

def anotar_modo(entrada, conversa):
    """composer_mode só chega em sessionStart e beforeSubmitPrompt; 'plan' é valor válido
    (o enum da IDE tem plan/spec/debug etc., embora a doc liste só agent|ask|edit)."""
    modo = str(entrada.get('composer_mode') or '').lower()
    if modo:
        conversa.d['composer_mode'] = modo
    return modo

def session_start(entrada, conversa):
    anotar_modo(entrada, conversa)
    return {'continue': True, 'additional_context': IDIOMA+'\n'+LEMBRETE}

def before_prompt(entrada, conversa):
    anotar_modo(entrada, conversa)
    texto = str(entrada.get('prompt') or entrada.get('text') or '')
    if '#sem-consulta' in texto:
        conversa.d['sem_consulta'] = True
        registrar('hook_sem_consulta')
    conversa.d.pop('anunciou_em', None)
    conversa.d['turno_em'] = agora()
    conversa.d['retomadas'] = 0
    return {'continue': True, 'additional_context': IDIOMA+'\n'+LEMBRETE+entregar_pareceres(conversa)}

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

def rodada_do_plano_no_stop(entrada, conversa, itens=None):
    """Nasceu um plano neste turno: o hook abre Claude e Codex por conta própria.

    Não adianta pedir ao agente — ele não chama a ponte sozinho (medido em 15 e 16/09).
    Aqui o hook deixa de ser só porteiro e vira quem abre a janela."""
    plano = plano_da_transcricao(entrada, itens) or plano_do_disco(conversa.d.get('turno_em', 0))
    if not plano:
        return None
    assinatura = hashlib.sha256(plano.encode()).hexdigest()[:16]
    tentativas = conversa.d.get('plano_tentativas') or {}
    if tentativas.get(assinatura, 0) >= MAX_TENTATIVAS_RODADA:
        return None  # já tentamos e o filho não subiu: não insistir em laço
    trocar_plano(conversa, plano)
    id_pedido = id_da_rodada(plano)
    aberta, _ = rodada_situacao(id_pedido)
    if aberta:
        return None  # a rodada existe no disco (o hook ou o agente abriu): nada a fazer
    # A partir daqui, o que impedir a abertura é transitório (alvo, teto, spawn): não contar
    # tentativa, senão uma falha de um turno silenciaria o plano para sempre.
    alvo = alvo_do_plano(entrada, conversa, plano)
    if not alvo:
        registrar('hook_plano', ok=False, mensagem='plano sem projeto nem repositório mapeado; rodada não aberta')
        return None
    projeto, repo_rel = alvo
    if rodadas_na_ultima_hora() >= TETO_RODADAS_HORA:
        registrar('hook_plano', ok=False, repositorio=repo_rel,
                  mensagem=f'teto de {TETO_RODADAS_HORA} rodadas por hora atingido; rodada não aberta')
        return None
    registrar('hook_plano', projeto=projeto, repositorio=repo_rel,
              mensagem=f'plano novo ({len(plano)} caracteres); abrindo Claude e Codex')
    if not abrir_rodada_em_segundo_plano(projeto, repo_rel, plano,
                                         'Critique este plano: riscos, o que faltou, o que está errado na abordagem.'):
        return None
    # Conta a tentativa: o Popen subiu, mas o filho ainda pode falhar (projeto inválido, cota).
    # Se falhar, `rodada_situacao` não acha nada e o próximo turno tenta de novo — até o teto.
    conversa.d['plano_tentativas'] = {**tentativas, assinatura: tentativas.get(assinatura, 0)+1}
    return retomar(conversa,
                   'A valt-ponte abriu Claude e Codex em duas janelas do Ptyxis (ou dentro da IDE, se ela consome a fila) para criticarem este plano. '
                   f'Chame consulta_rodada (id_pedido "{id_pedido}", espera_segundos 25) até o estado final, '
                   'compare os dois pareceres com o seu plano e só então implemente.', 'plano_novo')

def stop(entrada, conversa):
    # Uma leitura da transcrição para o evento inteiro: o #sem-consulta e o plano vinham
    # fazendo duas varreduras completas dentro do mesmo orçamento de LIMITE_S.
    itens = list(itens_da_transcricao(entrada.get('transcript_path')))
    if pediu_sem_consulta(entrada, conversa, itens):
        return {}
    if conversa.d.pop('config_restaurada_em', None):
        return retomar(conversa, 'Você editou a configuração da valt-ponte (mcp/hooks da IDE); o hook restaurou a versão '
                       'do instalador. Não altere essa configuração: se a ponte falhar, relate o erro ao usuário.', 'config')
    voltas = max(int(entrada.get('loop_count') or 0), conversa.d.get('retomadas', 0))
    if voltas >= MAX_RETOMADAS or str(entrada.get('status', 'completed')) not in {'completed', 'complete', 'success'}:
        return {}
    nova = rodada_do_plano_no_stop(entrada, conversa, itens)
    if nova:
        return nova
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
    pendente = pendencia_revisao(conversa)
    if pendente:
        repo_rel, projeto, relativos, raiz = pendente
        arquivos = conversa.d['editados'][raiz]
        return retomar(conversa, f'Você alterou {len(arquivos)} arquivos em {repo_rel} nesta conversa. Antes de declarar pronto, faça a revisão final. '
                       + chamada_pronta(projeto, repo_rel, relativos[:6], 'Revisão final: riscos e o que faltou nesta implementação',
                                        repo_rel+'|revisao|'+str(len(relativos))), 'revisao_final')
    return {}

EVENTOS = {'preToolUse': pre_tool, 'beforeShellExecution': before_shell, 'postToolUse': after_tool,
           'postToolUseFailure': lambda entrada, conversa: after_tool(entrada, conversa, falhou=True), 'afterMCPExecution': after_mcp, 'afterFileEdit': after_file_edit,
           'afterAgentResponse': after_response, 'beforeSubmitPrompt': before_prompt,
           'sessionStart': session_start, 'stop': stop}

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
    vistos = conversa.d.setdefault('eventos_vistos', [])
    if evento not in vistos:
        vistos.append(evento)
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
