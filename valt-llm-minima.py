#!/usr/bin/env python3
"""LLM minima para conversar com o Valt via Ollama local.

Fluxo: pergunta -> busca simples em .md -> contexto curto -> Ollama /api/chat.
Sem dependencias externas. Esta POC e intencionalmente menor que Jaime/JaimeChatBrowser.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


DEFAULT_VAULT = Path(os.environ.get("VALT_ROOT", str(Path.home() / "Valt")))
DEFAULT_MODEL = os.environ.get("VALT_LLM_MODEL", "qwen2.5:0.5b")
DEFAULT_BASE_URL = os.environ.get("VALT_LLM_BASE_URL", "http://127.0.0.1:11434")
DEFAULT_MAX_FILES = int(os.environ.get("VALT_LLM_MAX_FILES", "3"))
DEFAULT_MAX_CHARS = int(os.environ.get("VALT_LLM_MAX_CHARS", "8000"))

STOPWORDS = {
    "a",
    "as",
    "ao",
    "aos",
    "com",
    "como",
    "da",
    "das",
    "de",
    "do",
    "dos",
    "e",
    "em",
    "na",
    "nas",
    "no",
    "nos",
    "o",
    "os",
    "para",
    "por",
    "qual",
    "quais",
    "que",
    "se",
    "um",
    "uma",
}


@dataclass(frozen=True)
class Match:
    relpath: str
    score: int


def normalize_terms(question: str) -> list[str]:
    text = question.lower()
    text = re.sub(r"[^a-z0-9áàâãéêíóôõúüç_./ -]+", " ", text)
    terms = []
    for term in text.split():
        clean = term.strip("./-_")
        if len(clean) < 3 or clean in STOPWORDS:
            continue
        terms.append(clean)
    return terms or [part for part in text.split() if part]


def iter_markdown(vault: Path) -> list[Path]:
    ignored = {".git", ".agents", ".codex", "node_modules", "__pycache__"}
    files = []
    for path in vault.rglob("*.md"):
        if any(part in ignored for part in path.parts):
            continue
        files.append(path)
    return files


def score_docs(vault: Path, question: str, max_files: int) -> list[Match]:
    terms = normalize_terms(question)
    scored: dict[str, int] = {}
    for path in iter_markdown(vault):
        rel = path.relative_to(vault).as_posix()
        rel_l = rel.lower()
        try:
            content_l = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue

        score = 0
        for term in terms:
            if term in rel_l:
                score += 6
            count = content_l.count(term)
            if count:
                score += min(count, 6)

        if rel in {"AGENTS.md", "indice-geral.md", "mapa-ecossistema.md"}:
            score += 2
        if rel.startswith("indices/") or rel.endswith("/README.md"):
            score += 1
        if score > 0:
            scored[rel] = score

    matches = sorted(scored.items(), key=lambda item: (-item[1], item[0]))
    return [Match(rel, score) for rel, score in matches[:max_files]]


def build_context(vault: Path, matches: list[Match], max_chars: int) -> str:
    blocks = []
    for match in matches:
        path = vault / match.relpath
        content = path.read_text(encoding="utf-8", errors="ignore")
        if len(content) > max_chars:
            content = (
                content[:max_chars]
                + f"\n\n[... documento truncado em {max_chars} caracteres ...]"
            )
        blocks.append(f"---\nArquivo: {match.relpath}\n---\n{content}")
    return "\n\n".join(blocks)


def ollama_chat(
    base_url: str,
    model: str,
    question: str,
    context: str,
    stream: bool,
) -> int:
    system = (
        "Voce conversa com o Valt, um vault Markdown. Responda somente com base "
        "nos documentos fornecidos. Se nao encontrar a resposta nos documentos, "
        "diga que nao encontrou no Valt. Cite arquivos quando ajudar. Responda "
        "em portugues, direto e sem inventar caminhos."
    )
    payload: dict[str, object] = {
        "model": model,
        "stream": stream,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"Pergunta:\n{question}\n\nDocumentos do Valt:\n{context}",
            },
        ],
        "options": {"temperature": 0.2, "num_predict": 700},
    }
    think = os.environ.get("VALT_LLM_THINK")
    if think:
        payload["think"] = think.lower() == "true"

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if not stream:
                parsed = json.loads(response.read().decode("utf-8"))
                print(parsed.get("message", {}).get("content", "").strip())
                return 0

            for raw in response:
                if not raw.strip():
                    continue
                event = json.loads(raw.decode("utf-8"))
                if "error" in event:
                    print(f"\nErro Ollama: {event['error']}", file=sys.stderr)
                    return 1
                token = event.get("message", {}).get("content", "")
                if token:
                    print(token, end="", flush=True)
                if event.get("done"):
                    print()
                    return 0
    except urllib.error.URLError as exc:
        print(f"Ollama indisponivel em {base_url}: {exc}", file=sys.stderr)
        print("Suba o Ollama e baixe um modelo pequeno, ex.: ollama pull qwen2.5:0.5b", file=sys.stderr)
        return 1
    except TimeoutError:
        print("Timeout conversando com Ollama.", file=sys.stderr)
        return 1
    return 0


def run_question(args: argparse.Namespace, question: str) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        print(f"Vault nao encontrado: {vault}", file=sys.stderr)
        return 1

    matches = score_docs(vault, question, args.max_files)
    if not matches:
        print("Nenhum documento encontrado no Valt para esta pergunta.", file=sys.stderr)
        return 2

    print("==> Documentos selecionados", file=sys.stderr)
    for match in matches:
        print(f"  - {match.relpath} (score {match.score})", file=sys.stderr)

    if args.list:
        return 0

    context = build_context(vault, matches, args.max_chars)
    return ollama_chat(args.base_url, args.model, question, context, stream=not args.no_stream)


def repl(args: argparse.Namespace) -> int:
    print("valt-llm-minima. Pergunte sobre o Valt. Use /sair para encerrar.")
    while True:
        try:
            question = input("\nValt> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not question:
            continue
        if question in {"/sair", "/exit", "/quit"}:
            return 0
        code = run_question(args, question)
        if code not in {0, 2}:
            return code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LLM minima para conversar com o Valt usando Ollama local."
    )
    parser.add_argument("question", nargs="*", help="Pergunta sobre o Valt")
    parser.add_argument("--list", action="store_true", help="Mostra documentos encontrados e sai")
    parser.add_argument("--no-stream", action="store_true", help="Imprime resposta so no final")
    parser.add_argument("--vault", default=str(DEFAULT_VAULT), help="Raiz do Valt")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Modelo Ollama")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="URL do Ollama")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    question = " ".join(args.question).strip()
    if question:
        return run_question(args, question)
    if args.list:
        print("--list precisa de uma pergunta.", file=sys.stderr)
        return 1
    return repl(args)


if __name__ == "__main__":
    raise SystemExit(main())
