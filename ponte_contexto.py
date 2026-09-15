"""Contexto explícito, rastreável e limitado por projeto. Apenas biblioteca padrão."""
from __future__ import annotations
import hashlib
import re
import subprocess
import unicodedata
from pathlib import Path

STOP = {'como', 'para', 'com', 'uma', 'que', 'dos', 'das', 'por', 'esse', 'essa'}
DENIED = {'vpn', 'secrets', '.git', '.ssh', '.cursor', '.codex', '.claude',
          'node_modules', 'vendor', 'consultas', 'contextos', 'handoffs'}
EXT = {'.md', '.py', '.js', '.ts', '.tsx', '.jsx', '.css', '.html', '.sh', '.sql'}
SECRET = re.compile(r'-----BEGIN .*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})|(?im:^\s*(?:[\w-]*(?:secret|password|token|api_key)[\w-]*)\s*[:=]\s*[\'\"]?[^\s\'\"$<{]{12,})')
LEMBRETE_CONSULTOR = (
    'Gatilhos: migração/SQL sensível antes do commit; mesmo erro tsc/Jest/pgTAP duas vezes; '
    'revisão final com mais de 20 arquivos. Chame consulta_iniciar (provedor: claude, '
    'interativo: false) e aguarde consulta_status. Task e agents internos do Cursor não '
    'substituem: o consultor só abre numa janela nova do Ptyxis (título Valt · claude · …). '
    'Layout Editor não é a ponte.'
)

def terms(value):
    value = ''.join(c for c in unicodedata.normalize('NFKD', value.lower()) if not unicodedata.combining(c))
    return set(re.findall(r'[a-z0-9]{3,}', value)) - STOP

def inside(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise ValueError('Use caminho relativo sem ..')
    path = root / rel
    if any((root / Path(*rel.parts[:i])).is_symlink() for i in range(1, len(rel.parts)+1)):
        raise ValueError('Links simbólicos não entram no contexto')
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError('Caminho fora da raiz permitida')
    return resolved

def safe_text(path: Path, relative: str):
    parts = {p.lower() for p in Path(relative).parts}
    if parts & DENIED or path.suffix.lower() not in EXT or path.name.startswith('.env'):
        raise ValueError('Arquivo excluído da consulta: '+relative)
    if not path.is_file() or path.stat().st_size > 300_000:
        raise ValueError('Arquivo ausente ou maior que 300 KB: '+relative)
    text = path.read_text(encoding='utf-8')
    if '\x00' in text or SECRET.search(text):
        raise ValueError('Conteúdo potencialmente sensível ou binário; revisão manual necessária: '+relative)
    return text

def git_state(repo: Path):
    def run(*args):
        result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else None
    return {'head': run('rev-parse', 'HEAD'), 'branch': run('branch', '--show-current'),
            'sujo': bool(run('status', '--porcelain'))}

def build(vault: Path, sites: Path, projeto: str, pergunta: str, repositorio: str = '', arquivos=None):
    if not pergunta.strip() or len(pergunta) > 12000:
        raise ValueError('Pergunta obrigatória, até 12000 caracteres')
    scope = inside(vault, projeto)
    if not scope.is_dir() or scope == vault.resolve() or not (scope / 'README.md').is_file():
        raise ValueError('Projeto deve ser uma pasta do Valt com README.md')
    if set(Path(projeto).parts) & DENIED:
        raise ValueError('Projeto excluído')
    repo_path = inside(sites, repositorio) if repositorio else None
    if repo_path and (not repo_path.is_dir() or len(Path(repositorio).parts) < 2):
        raise ValueError('Repositório deve ser específico: guarda-chuva/repo')
    if repo_path:
        index = (vault/'indices/repositorios.md').read_text(encoding='utf-8')
        related = [line for line in index.splitlines() if '`~/Sites/'+repositorio+'`' in line]
        if not any('../'+projeto+'/' in line for line in related):
            raise ValueError('Projeto e repositório não correspondem no índice do Valt')
    candidates, avisos = [], []
    mandatory = [vault/'AGENTS.md', vault/'CLAUDE.md', scope/'README.md', scope/'Repositorio/mapa-repositorio.md']
    # Diário da raiz é filtrado por tarefa/projeto; não escolher simplesmente a primeira entrada.
    diary = vault/Path(projeto).parts[0]/'continuidade/dataehoradaultimaatualizacao.md'
    paths = list(dict.fromkeys(mandatory + sorted(scope.rglob('*.md')) + [diary]))
    query = terms(pergunta)
    for path in paths:
        rel = path.relative_to(vault).as_posix()
        try:
            content = safe_text(inside(vault, rel), rel)
        except (ValueError, OSError, UnicodeError):
            continue
        if path == diary:
            entries = re.split(r'(?m)(?=^## )', content)
            selected = [s for s in entries if terms(Path(projeto).name) & terms(s)]
            content = '\n'.join(selected[:2])
            if not content:
                continue
        score = len(query & terms(content)) + 3 * len(query & terms(rel))
        if path in mandatory or score:
            candidates.append((path not in mandatory, -score, rel, content, path))
    arquivos = arquivos or []
    if len(arquivos) > 6 or (arquivos and not repo_path):
        raise ValueError('Até 6 arquivos de código e repositório explícito obrigatório')
    code_items = []
    for rel in arquivos:
        try:
            path = inside(repo_path, rel)
            content = safe_text(path, rel)
        except ValueError as exc:
            raise ValueError(f'{exc} — `arquivos` recebe caminhos relativos ao repositório {repositorio}; '
                             'notas do Valt entram automaticamente, não as passe aqui') from exc
        code_items.append((rel, content, path))
    if sum(len(content) for _, content, _ in code_items) > 60000:
        raise ValueError('Arquivos excedem o orçamento; reduza o pacote')
    candidates.sort(key=lambda item: item[:3])
    fontes, blocks, budget = [], [], 30000
    def add(rel, content, path, limit):
        nonlocal budget
        excerpt = content[:min(limit, budget)]
        if not excerpt:
            return
        truncado = len(excerpt) < len(content)
        blocks.append(f'### {rel}\n{excerpt}' + ('\n[TRUNCADO — solicite contexto adicional]' if truncado else ''))
        fontes.append({'arquivo': rel, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'truncado': truncado})
        budget -= len(excerpt)
    for _, _, rel, content, path in candidates[:8]:
        add('Valt/'+rel, content, path, 9000 if path == vault/'AGENTS.md' else 5000)
    budget += 50000
    for rel, content, path in code_items:
        add('Sites/'+repositorio+'/'+rel, content, path, len(content))
    if not fontes:
        raise ValueError('Nenhuma fonte utilizável')
    return {'projeto': projeto, 'repositorio': repositorio, 'pergunta': pergunta, 'fontes': fontes,
            'estado_git': git_state(repo_path) if repo_path else None,
            'avisos': avisos, 'texto': '\n\n'.join(blocks),
            'lembrete_consultor': LEMBRETE_CONSULTOR}

def stale(pacote, vault, sites):
    alteradas = []
    for fonte in pacote['fontes']:
        root, rel = fonte['arquivo'].split('/', 1)
        try:
            path = inside(vault if root == 'Valt' else sites, rel)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except (ValueError, OSError):
            digest = None
        if digest != fonte['sha256']:
            alteradas.append(fonte['arquivo'])
    if pacote['repositorio'] and git_state(inside(sites, pacote['repositorio'])) != pacote['estado_git']:
        alteradas.append('estado Git do repositório')
    return alteradas
