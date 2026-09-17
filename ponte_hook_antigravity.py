#!/usr/bin/env python3
"""Hooks da valt-ponte no Google Antigravity: traduz a entrada e a resposta e usa as regras de ponte_hook.

Uso (em ~/.gemini/config/hooks.json): python3 ponte_hook_antigravity.py <evento>, com a entrada JSON do
Antigravity no stdin. Eventos: PreToolUse, PostToolUse, PreInvocation, Stop.

Equivalências com o Cursor:
- PreToolUse de run_command  → beforeShellExecution; outras ferramentas → preToolUse. Negação vira
  {"decision": "deny", "reason": …}.
- PostToolUse de run_command → postToolUse(Failure) (erro repetido); das ferramentas MCP da ponte →
  afterMCPExecution, com o estado lido do job da consulta; das ferramentas de escrita → afterFileEdit.
- PreInvocation com mensagem nova do usuário na transcrição → beforeSubmitPrompt. O contexto que o
  Cursor devolve em additional_context (idioma, lembrete, aviso do 2º erro) entra aqui como
  injectSteps/ephemeralMessage, porque o PostToolUse do Antigravity não devolve nada ao agente.
- Stop → afterAgentResponse (última resposta da transcrição) + stop; retomar vira
  {"decision": "continue", "reason": …}.

O formato da transcrição (transcriptPath) é lido de forma tolerante; as travas de terminal, MCP e
edição não dependem dele. Nunca trava o Antigravity: qualquer erro libera e vai para hooks.log.
"""
from __future__ import annotations
import hashlib
import json
import re
import signal
import sys
import traceback
from pathlib import Path

import ponte_hook as nucleo

TERMINAL = {'run_command'}
ESCRITA = re.compile(r'write|edit|replace|create|delete|move|rename', re.I)
LIMITE_TRANSCRICAO = 2_000_000

# --- entrada --------------------------------------------------------------------------

def argumentos(entrada):
    chamada = entrada.get('toolCall') or {}
    args = chamada.get('args') or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return str(chamada.get('name') or ''), args if isinstance(args, dict) else {}

def pasta(entrada, args=None):
    cwd = (args or {}).get('Cwd') or (args or {}).get('cwd')
    if cwd:
        return str(Path(cwd).expanduser())
    raizes = entrada.get('workspacePaths') or []
    return str(Path(raizes[0]).expanduser()) if raizes else ''

def base(entrada):
    """Campos comuns já no formato que as regras do núcleo leem."""
    return {'conversation_id': entrada.get('conversationId') or 'sem-conversa',
            'workspace_roots': [str(Path(r).expanduser()) for r in entrada.get('workspacePaths') or []],
            'transcript_path': str(Path(entrada['transcriptPath']).expanduser()) if entrada.get('transcriptPath') else None}

def texto_de(valor):
    if isinstance(valor, str):
        return valor
    if isinstance(valor, list):
        return '\n'.join(filter(None, (texto_de(v) for v in valor)))
    if isinstance(valor, dict):
        for chave in ('text', 'content', 'message', 'userInput', 'items', 'parts'):
            if chave in valor:
                return texto_de(valor[chave])
    return ''

def mensagens(caminho):
    """[(papel, texto)] da transcrição, papel em {'usuario', 'agente'}; tolerante ao formato."""
    if not caminho:
        return []
    try:
        arquivo = Path(caminho).expanduser()
        if not arquivo.is_file() or arquivo.stat().st_size > LIMITE_TRANSCRICAO:
            return []
        linhas = arquivo.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    saida = []
    for linha in linhas:
        try:
            item = json.loads(linha)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        papel = str(item.get('role') or item.get('type') or item.get('source') or item.get('author') or '').lower()
        texto = texto_de({k: item[k] for k in ('content', 'text', 'message', 'userInput') if k in item})
        if not texto:
            continue
        if 'user' in papel:
            saida.append(('usuario', texto))
        elif re.search(r'assistant|model|planner|agent|response', papel):
            saida.append(('agente', texto))
    return saida

def ultima(caminho, papel):
    for quem, texto in reversed(mensagens(caminho)):
        if quem == papel:
            return texto
    return None

# --- estado próprio do adaptador (fora do lock do núcleo) -----------------------------

def com_conversa(entrada, funcao):
    conversa = nucleo.Conversa(base(entrada))
    try:
        return funcao(conversa.d)
    finally:
        conversa.salvar()

def guardar_contexto(entrada, texto):
    com_conversa(entrada, lambda d: d.setdefault('ag_contexto', []).append(texto))

def nucleo_processar(evento, entrada):
    return nucleo.processar(evento, json.dumps(entrada, ensure_ascii=False))

# --- eventos --------------------------------------------------------------------------

def pre_tool(entrada):
    nome, args = argumentos(entrada)
    if nome in TERMINAL:
        r = nucleo_processar('beforeShellExecution', {**base(entrada), 'command': str(args.get('CommandLine') or args.get('command') or ''),
                                                      'cwd': pasta(entrada, args)})
    else:
        r = nucleo_processar('preToolUse', {**base(entrada), 'tool_name': nome, 'tool_input': args})
    if r.get('permission') == 'deny':
        return {'decision': 'deny', 'reason': r.get('user_message') or r.get('agent_message') or 'valt-ponte: barrado'}
    return {}

def resultado_mcp(nome, args):
    """Estado da consulta lido do job (o PostToolUse não traz o resultado da ferramenta)."""
    job = None
    if nome.endswith('consulta_iniciar') and args.get('id_pedido'):
        for candidato in nucleo.jobs():
            if candidato.get('id_pedido') == args['id_pedido']:
                job = candidato
                break
    elif re.fullmatch(r'[a-f0-9]{32}', str(args.get('id_consulta') or '')):
        try:
            job = json.loads((nucleo.STATE/args['id_consulta']/'job.json').read_text())
        except (OSError, ValueError):
            job = None
    return json.dumps({'id': job['id'], 'estado': job.get('estado')}) if job and job.get('id') else json.dumps({'error': 'consulta não encontrada'})

def post_tool(entrada):
    nome, args = argumentos(entrada)
    comum = base(entrada)
    if nome in TERMINAL:
        erro = str(entrada.get('error') or '')
        saida = erro or str(entrada.get('output') or entrada.get('result') or '')
        r = nucleo_processar('postToolUseFailure' if erro else 'postToolUse',
                             {**comum, 'tool_name': 'Shell', 'cwd': pasta(entrada, args),
                              'tool_use_id': f"{comum['conversation_id']}:{entrada.get('stepIdx')}",
                              'tool_input': {'command': str(args.get('CommandLine') or ''), 'cwd': pasta(entrada, args)},
                              'error_message': saida})
        if r.get('additional_context'):
            guardar_contexto(entrada, r['additional_context'])
        return {}
    if nucleo.FERRAMENTA.search(nome):
        nucleo_processar('afterMCPExecution', {**comum, 'tool_name': nome, 'result_json': resultado_mcp(nome, args)})
        return {}
    if nome.lower() not in nucleo.FERRAMENTAS_LEITURA and ESCRITA.search(nome) and not entrada.get('error'):
        for caminho in dict.fromkeys(nucleo.caminhos(args)):
            nucleo_processar('afterFileEdit', {**comum, 'file_path': str(Path(caminho).expanduser())})
    return {}

def pre_invocation(entrada):
    comum = base(entrada)
    pedido = ultima(comum['transcript_path'], 'usuario')
    if pedido is None and int(entrada.get('invocationNum') or 0) == 0:
        pedido = ''  # sem transcrição legível: a 1ª invocação da execução conta como turno novo
    contextos = []
    if pedido is not None:
        marca = hashlib.sha256(pedido.encode()).hexdigest()
        def pedido_novo(d):
            # A retomada do Stop gera outra invocação com o mesmo pedido: só mensagem nova zera o turno.
            if d.get('ag_ultimo_pedido') == marca:
                return False
            d['ag_ultimo_pedido'] = marca
            return True
        if com_conversa(entrada, pedido_novo):
            r = nucleo_processar('beforeSubmitPrompt', {**comum, 'prompt': pedido})
            if r.get('additional_context'):
                contextos.append(r['additional_context'])
    contextos += com_conversa(entrada, lambda d: d.pop('ag_contexto', []))
    if not contextos:
        return {}
    return {'injectSteps': [{'ephemeralMessage': texto} for texto in contextos]}

def stop(entrada):
    comum = base(entrada)
    resposta = ultima(comum['transcript_path'], 'agente')
    if resposta:
        nucleo_processar('afterAgentResponse', {**comum, 'text': resposta})
    motivo = str(entrada.get('terminationReason') or '')
    interrompido = entrada.get('error') or re.search(r'cancel|abort|interrupt|user|error|timeout', motivo, re.I)
    r = nucleo_processar('stop', {**comum, 'status': 'aborted' if interrompido else 'completed', 'loop_count': 0})
    if r.get('followup_message'):
        return {'decision': 'continue', 'reason': r['followup_message']}
    return {}

EVENTOS = {'PreToolUse': pre_tool, 'PostToolUse': post_tool, 'PreInvocation': pre_invocation, 'Stop': stop}

def processar(evento, bruto):
    if (nucleo.STATE/'desligado').exists():
        return {}
    entrada = json.loads(bruto) if bruto.strip() else {}
    if not isinstance(entrada, dict):
        raise ValueError('entrada não é objeto JSON')
    funcao = EVENTOS.get(evento)
    return (funcao(entrada) or {}) if funcao else {}

def main():
    evento = sys.argv[1] if len(sys.argv) > 1 else ''
    nucleo.IDE = 'antigravity'
    def estourou(signum, frame):
        raise TimeoutError(f'hook passou de {nucleo.LIMITE_S} s')
    signal.signal(signal.SIGALRM, estourou)
    signal.alarm(nucleo.LIMITE_S)
    try:
        resposta = processar(evento, sys.stdin.read())
    except Exception:
        nucleo.log(f'antigravity {evento}: liberado por exceção\n{traceback.format_exc()}')
        resposta = {}
    finally:
        signal.alarm(0)
    print(json.dumps(resposta, ensure_ascii=False))

if __name__ == '__main__':
    main()
