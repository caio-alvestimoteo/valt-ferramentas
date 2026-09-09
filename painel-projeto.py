#!/usr/bin/env python3
"""Gera e acompanha painéis HTML vivos a partir de um plano Markdown e estado JSON."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path


VAULT = Path(os.environ.get("VALT") or Path.home() / "Valt").expanduser()
PLANEJAMENTOS = Path.home() / "Planejamentos"
THEME = VAULT / "temas" / "painel-hud.css"


def resolve_plan(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (VAULT / path).resolve()


def load(plan: Path) -> tuple[dict, str, Path]:
    state_path = plan.with_name("estado-implementacao.json")
    if not plan.exists():
        raise ValueError(f"Plano não encontrado: {plan}")
    if not state_path.exists():
        raise ValueError(f"Estado não encontrado: {state_path}")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON inválido em {state_path}: {error}") from error
    if not isinstance(state.get("stages"), list) or not state["stages"]:
        raise ValueError("O estado precisa conter ao menos uma etapa.")
    return state, plan.read_text(encoding="utf-8"), state_path


def validate(state: dict) -> list[str]:
    errors: list[str] = []
    valid_statuses = {"not_started", "ready", "in_progress", "blocked", "needs_review", "completed", "skipped"}
    ids: set[str] = set()
    active: list[str] = []
    for stage in state["stages"]:
        if stage.get("status") not in valid_statuses:
            errors.append(f"Etapa {stage.get('id')}: status inválido.")
        for task in stage.get("tasks", []):
            task_id = task.get("id")
            if not task_id or task_id in ids:
                errors.append(f"ID de tarefa ausente ou duplicado: {task_id!r}.")
            ids.add(task_id)
            if task.get("status") not in valid_statuses:
                errors.append(f"Tarefa {task_id}: status inválido.")
            if task.get("status") == "in_progress":
                active.append(task_id)
            if task.get("status") == "completed" and (not task.get("test") or not task.get("evidence")):
                errors.append(f"Tarefa {task_id}: concluída sem teste ou evidência.")
    if len(active) > 1:
        errors.append("Somente uma tarefa pode ficar em andamento: " + ", ".join(active))
    if state.get("current_task") and state["current_task"] not in ids:
        errors.append("current_task não existe nas tarefas.")
    return errors


def esc(value: object) -> str:
    return html.escape(str(value))


def status_label(value: str) -> str:
    labels = {
        "not_started": "não iniciado", "ready": "pronto para iniciar", "in_progress": "em andamento",
        "blocked": "bloqueado", "needs_review": "aguarda revisão", "completed": "concluído", "skipped": "não aplicável",
    }
    return labels.get(value, value)


def status_glyph(value: str) -> str:
    """Segunda codificação do status, para que a cor nunca seja o único sinal.

    Verde e mauve ficam a ΔE 11 sob deuteranopia — legível, mas abaixo do piso
    categórico de 15. O glifo e o rótulo carregam o estado por conta própria.
    """
    glyphs = {
        "not_started": "·", "ready": "○", "in_progress": "◐", "blocked": "▲",
        "needs_review": "◆", "completed": "●", "skipped": "×",
    }
    return glyphs.get(value, "·")


def task_html(task: dict) -> str:
    evidence = task.get("evidence") or []
    evidence_html = "<em>Ainda sem evidência — não iniciar a próxima tarefa como concluída.</em>" if not evidence else "<ul>" + "".join(f"<li><code>{esc(item)}</code></li>" for item in evidence) + "</ul>"
    log = task.get("log") or [
        f"Status atual: {status_label(task['status'])}.",
        "Teste e evidência serão registrados aqui antes de liberar a próxima tarefa.",
    ]
    return f"""
      <details class=\"task status-{esc(task['status'])}\" data-task-id=\"{esc(task['id'])}\">
        <summary><span class=\"task-id\">{esc(task['id'])}</span><span class=\"task-title\">{esc(task['title'])}</span><span class=\"status task-status\" data-glyph=\"{esc(status_glyph(task['status']))}\">{esc(status_label(task['status']))}</span></summary>
        <div class=\"task-body\">
        <p><strong>Em palavras simples:</strong> {esc(task['why'])}</p>
        <details><summary>Como saber que deu certo</summary><p>{esc(task['test'])}</p></details>
        <details><summary>Evidências</summary>{evidence_html}</details>
        <div class=\"mini-log\"><b>Log da tarefa</b>{''.join(f'<p>{esc(item)}</p>' for item in log)}</div>
        </div>
      </details>"""


def stage_html(stage: dict) -> str:
    tasks = "".join(task_html(task) for task in stage.get("tasks", []))
    dependencies = ", ".join(stage.get("depends_on", [])) or "nenhuma"
    total = len(stage.get("tasks", []))
    done = sum(task.get("status") == "completed" for task in stage.get("tasks", []))
    stage_progress = round((done / total) * 100) if total else 0
    log = stage.get("log") or [
        f"Etapa {stage['id']} está {status_label(stage['status'])}.",
        f"{done}/{total} tarefas concluídas.",
        "Aguardando a confirmação do teste anterior antes de avançar.",
    ]
    return f"""
      <details class=\"stage status-{esc(stage['status'])}\" id=\"etapa-{esc(stage['id'])}\" data-stage-id=\"{esc(stage['id'])}\">
        <summary><span class=\"stage-topline\"><span class=\"stage-number\">{esc(stage['id'])}</span><span class=\"status stage-status\" data-glyph=\"{esc(status_glyph(stage['status']))}\">{esc(status_label(stage['status']))}</span></span><span class=\"stage-heading\"><b>{esc(stage['title'])}</b><small class=\"stage-progress-label\">{done}/{total} tarefas concluídas</small></span><span class=\"stage-meter\" style=\"--stage-progress:{stage_progress}%\"><i></i></span><span class=\"expand-label\">Ver detalhes</span></summary>
        <div class=\"stage-body\">
        <p class=\"plain\">{esc(stage['plain_language'])}</p>
        <div class=\"stage-meta\"><span><b>Depende de:</b> {esc(dependencies)}</span><span><b>Pronto quando:</b> {esc(stage['acceptance'])}</span></div>
        <div class=\"mini-log\"><b>Log da etapa</b>{''.join(f'<p>{esc(item)}</p>' for item in log)}</div>
        <div class=\"tasks\">{tasks}</div>
        <aside class=\"resume\"><b>Como retomar esta etapa:</b> {esc(stage['resume_from'])}</aside>
        </div>
      </details>"""


SEGMENTS = 24


def rail_segment(stage: dict) -> str:
    done = sum(task.get("status") == "completed" for task in stage.get("tasks", []))
    total = len(stage.get("tasks", []))
    return (f'<a class="rail-seg" href="#etapa-{esc(stage["id"])}" data-status="{esc(stage["status"])}" '
            f'data-rail-id="{esc(stage["id"])}" title="{esc(stage["title"])} — {esc(status_label(stage["status"]))}">'
            f'<b>{esc(stage["id"])}</b><i>{esc(status_glyph(stage["status"]))}</i>'
            f'<span class="sr">{esc(status_label(stage["status"]))} · {done}/{total}</span></a>')


def segmeter(progress: int) -> str:
    filled = round(progress / 100 * SEGMENTS)
    return "".join(f'<span class="{"on" if i < filled else ""}"></span>' for i in range(SEGMENTS))


def build_html(state: dict, markdown: str, plan: Path) -> str:
    theme = THEME.read_text(encoding="utf-8")
    tasks = [task for stage in state["stages"] for task in stage.get("tasks", [])]
    counts = {key: sum(task.get("status") == key for task in tasks) for key in ["completed", "in_progress", "blocked", "ready", "not_started"]}
    total = len(tasks)
    progress = round((counts["completed"] / total) * 100) if total else 0
    stages = "".join(stage_html(stage) for stage in state["stages"])
    rail = "".join(rail_segment(stage) for stage in state["stages"])
    blockers = state.get("blockers") or ["Nenhum bloqueio ativo."]
    sessions = state.get("sessions") or []
    backlog = state.get("future_backlog") or []
    activity_log = state.get("activity_log") or []
    updated = state.get("updated_at", datetime.now().astimezone().isoformat(timespec="seconds"))
    policy = state.get("implementation_model_policy", {})
    plan_rel = plan.relative_to(VAULT)
    stage_done = sum(stage.get("status") == "completed" for stage in state["stages"])
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>MidiaFlow — monitor de execução</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Anton&family=Archivo+Black&family=JetBrains+Mono:wght@400;500;600;700&family=Noto+Sans+JP:wght@700;900&display=swap" rel="stylesheet">
<style>{theme}
.sr{{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}}
</style></head><body>
<main class="wrap">

<header class="panel hud-header">
  <span class="jp-ghost" aria-hidden="true">監視</span>
  <div>
    <p class="eyebrow">MidiaFlow · execução local · monitor vivo</p>
    <h1>Pipeline MVP</h1>
  </div>
  <div class="live-state"><i class="live-dot" aria-hidden="true"></i> <span>Ao vivo</span></div>
</header>

<div class="telemetry">
  <div class="telemetry-item"><span class="telemetry-label">Etapa corrente</span><span class="telemetry-value" data-current-stage>{esc(state.get('current_stage','—'))}</span></div>
  <div class="telemetry-item"><span class="telemetry-label">Tarefa em foco</span><span class="telemetry-value" data-current-task-strip>{esc(state.get('current_task','nenhuma'))}</span></div>
  <div class="telemetry-item"><span class="telemetry-label">Etapas fechadas</span><span class="telemetry-value" data-stage-done>{stage_done}/{len(state['stages'])}</span></div>
  <div class="telemetry-item"><span class="telemetry-label">Atualizado</span><span class="telemetry-value" data-updated-at>{esc(updated)}</span></div>
</div>

<section class="readout">
  <div class="panel hero-metric">
    <p class="eyebrow">Progresso verificado por teste</p>
    <div>
      <div class="value"><span data-overall-progress-value>{progress}</span><small>%</small></div>
      <div class="segmeter" data-segmeter aria-hidden="true">{segmeter(progress)}</div>
    </div>
    <p class="eyebrow" style="margin-top:12px"><span data-count-total>{counts['completed']}</span> de {total} tarefas com evidência registrada</p>
  </div>
  <div class="panel tile tile--done"><div class="value" data-count-completed>{counts['completed']}</div><div class="label"><span class="glyph" aria-hidden="true">●</span>Concluído</div></div>
  <div class="panel tile tile--active"><div class="value" data-count-active>{counts['ready'] + counts['in_progress']}</div><div class="label"><span class="glyph" aria-hidden="true">◐</span>Em andamento</div></div>
  <div class="panel tile tile--blocked"><div class="value" data-count-blocked>{counts['blocked']}</div><div class="label"><span class="glyph" aria-hidden="true">▲</span>Bloqueado</div></div>
</section>

<section class="panel rail">
  <p class="eyebrow">Trilho de execução · {len(state['stages'])} etapas</p>
  <div class="rail-track" data-rail>{rail}</div>
  <div class="legend">
    <span><i style="color:var(--green)">●</i> concluído</span>
    <span><i style="color:var(--mauve)">◐</i> em andamento</span>
    <span><i style="color:var(--red)">▲</i> bloqueado</span>
    <span><i style="color:var(--muted)">·</i> não iniciado</span>
  </div>
</section>

<section class="focus-row">
  <div class="panel focus">
    <p class="eyebrow">Próxima ação segura</p>
    <h2 data-current-task>{esc(state.get('current_task','nenhuma'))}</h2>
    <p data-next-action>{esc(state.get('next_action','não definida'))}</p>
    <p class="eyebrow" style="margin-top:14px">Retomar com</p>
    <p><code>{esc(state.get('resume_command',''))}</code></p>
  </div>
  <div class="panel blockers">
    <p class="eyebrow">Atenção</p>
    <h2>Bloqueios</h2>
    <ul>{''.join(f'<li>{esc(item)}</li>' for item in blockers)}</ul>
  </div>
</section>

<details class="panel activity">
  <summary><span><span class="eyebrow">Log ao vivo</span><b>Últimos eventos</b></span><span class="expand-label">Expandir</span></summary>
  <div class="activity-body" data-activity-log>{''.join(f'<p><time>{esc(item.get("at", ""))}</time> · <strong>{esc(item.get("stage", "geral"))}</strong> · {esc(item.get("message", ""))}</p>' for item in activity_log) or '<p>Ainda não há eventos registrados.</p>'}</div>
</details>

<section id="pipeline" class="section">
  <div class="section-head">
    <div><p class="eyebrow">Grafo de execução</p><h2>Estágios</h2></div>
    <p>Abra um estágio para inspecionar tarefas, critério de teste,<br>evidências e log de decisões.</p>
  </div>
  <div class="stages">{stages}</div>
</section>

<section class="section">
  <div class="section-head"><div><p class="eyebrow">Sessões preservadas</p><h2>Histórico</h2></div></div>
  <div class="panel" style="padding:6px 22px 18px">{''.join(f'<div class="session"><strong>{esc(item.get("at",""))}</strong> · {esc(item.get("by",""))}<br>{esc(item.get("summary",""))}<br><em>Próximo: {esc(item.get("next", ""))}</em></div>' for item in sessions)}</div>
</section>

<section class="section">
  <div class="section-head"><div><p class="eyebrow">Não iniciar antes do piloto</p><h2>Depois do MVP</h2></div></div>
  <div class="panel" style="padding:20px 24px">
    <p style="margin-top:0">Bloqueado até um cliente real operar por duas semanas e gerar receita.</p>
    <ul style="font-size:12.5px">{''.join(f'<li>{esc(item)}</li>' for item in backlog)}</ul>
  </div>
</section>

<p class="footer-note">Modelo de construção: {esc(policy.get('default','—'))} · escalar para {esc(policy.get('escalation','—'))} · mecânico com {esc(policy.get('mechanical','—'))}.<br>
Painel gerado de fontes versionáveis; não armazena segredos. Atualizar com <code>python3 scripts/painel-projeto.py build {esc(plan_rel)}</code>.</p>
</main>
<script>
(() => {{
  const labels = {{not_started: 'não iniciado', ready: 'pronto para iniciar', in_progress: 'em andamento', blocked: 'bloqueado', needs_review: 'aguarda revisão', completed: 'concluído', skipped: 'não aplicável'}};
  const glyphs = {{not_started: '·', ready: '○', in_progress: '◐', blocked: '▲', needs_review: '◆', completed: '●', skipped: '×'}};
  const SEGMENTS = {SEGMENTS};
  const text = (selector, value) => {{ const node = document.querySelector(selector); if (node) node.textContent = value; }};
  const badge = (selector, status) => {{
    const node = document.querySelector(selector);
    if (!node) return;
    node.textContent = labels[status] || status;
    node.setAttribute('data-glyph', glyphs[status] || '·');
  }};
  const taskList = (state) => state.stages.flatMap((stage) => stage.tasks || []);
  const refresh = (state) => {{
    const tasks = taskList(state);
    const count = (status) => tasks.filter((task) => task.status === status).length;
    const progress = tasks.length ? Math.round((count('completed') / tasks.length) * 100) : 0;
    text('[data-current-stage]', state.current_stage || '—');
    text('[data-current-task-strip]', state.current_task || 'nenhuma');
    text('[data-current-task]', state.current_task || 'nenhuma');
    text('[data-next-action]', state.next_action || 'não definida');
    text('[data-updated-at]', state.updated_at || '');
    text('[data-overall-progress-value]', progress);
    text('[data-count-completed]', count('completed'));
    text('[data-count-total]', count('completed'));
    text('[data-count-active]', count('ready') + count('in_progress'));
    text('[data-count-blocked]', count('blocked'));
    text('[data-stage-done]', `${{state.stages.filter((stage) => stage.status === 'completed').length}}/${{state.stages.length}}`);
    const meter = document.querySelector('[data-segmeter]');
    if (meter) {{
      const filled = Math.round((progress / 100) * SEGMENTS);
      [...meter.children].forEach((cell, index) => cell.classList.toggle('on', index < filled));
    }}
    state.stages.forEach((stage) => {{
      const node = document.querySelector(`[data-stage-id="${{stage.id}}"]`);
      if (!node) return;
      node.className = `stage status-${{stage.status}}`;
      const done = (stage.tasks || []).filter((task) => task.status === 'completed').length;
      const totalTasks = (stage.tasks || []).length;
      badge(`[data-stage-id="${{stage.id}}"] .stage-status`, stage.status);
      text(`[data-stage-id="${{stage.id}}"] .stage-progress-label`, `${{done}}/${{totalTasks}} tarefas concluídas`);
      const bar = node.querySelector('.stage-meter');
      if (bar) bar.style.setProperty('--stage-progress', `${{totalTasks ? Math.round((done / totalTasks) * 100) : 0}}%`);
      const seg = document.querySelector(`[data-rail-id="${{stage.id}}"]`);
      if (seg) {{
        seg.setAttribute('data-status', stage.status);
        const mark = seg.querySelector('i');
        if (mark) mark.textContent = glyphs[stage.status] || '·';
      }}
      (stage.tasks || []).forEach((task) => {{
        const taskNode = document.querySelector(`[data-task-id="${{task.id}}"]`);
        if (!taskNode) return;
        taskNode.className = `task status-${{task.status}}`;
        badge(`[data-task-id="${{task.id}}"] .task-status`, task.status);
      }});
    }});
    const log = document.querySelector('[data-activity-log]');
    if (log) {{
      log.replaceChildren();
      (state.activity_log || []).forEach((item) => {{
        const row = document.createElement('p');
        const at = document.createElement('time'); at.textContent = item.at || '';
        const stage = document.createElement('strong'); stage.textContent = item.stage || 'geral';
        row.append(at, ' · ', stage, ` · ${{item.message || ''}}`);
        log.append(row);
      }});
      if (!log.childElementCount) log.textContent = 'Ainda não há eventos registrados.';
    }}
  }};
  const refreshLive = () => fetch('./estado-implementacao.json', {{cache: 'no-store'}}).then((response) => response.ok ? response.json() : null).then((state) => state && refresh(state)).catch(() => {{}});
  if (location.protocol.startsWith('http')) {{
    const events = new EventSource('/__live_reload/events');
    events.addEventListener('reload', refreshLive);
    setInterval(refreshLive, 30000);
  }}
}})();
</script></body></html>"""


def output_path(plan: Path) -> Path:
    return PLANEJAMENTOS / plan.relative_to(VAULT).with_suffix(".html")


def build(plan: Path) -> Path:
    state, markdown, _ = load(plan)
    errors = validate(state)
    if errors:
        raise ValueError("\n".join(errors))
    destination = output_path(plan)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.with_name("estado-implementacao.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    destination.write_text(build_html(state, markdown, plan), encoding="utf-8")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build", "validate", "watch"])
    parser.add_argument("plan")
    args = parser.parse_args()
    plan = resolve_plan(args.plan)
    if args.command == "validate":
        state, _, _ = load(plan)
        errors = validate(state)
        if errors:
            raise SystemExit("\n".join(errors))
        print("Estado válido.")
        return
    if args.command == "build":
        print(build(plan))
        return
    watched = [plan, plan.with_name("estado-implementacao.json"), THEME]
    previous = None
    print("Painel vivo iniciado. Ctrl+C para encerrar.")
    while True:
        stamps = tuple(path.stat().st_mtime_ns for path in watched)
        if stamps != previous:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {build(plan)}")
            previous = stamps
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"Erro: {error}", file=sys.stderr)
        raise SystemExit(2)
