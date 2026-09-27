#!/usr/bin/env python3
"""Trava contra caminhos e apelidos que não sobrevivem a uma formatação.

Três usuários já passaram por esta máquina (``arch``, ``arquiteto``, ``caiox``) e cada troca
invalidou a documentação inteira — 184 ocorrências de caminho absoluto e 61 arquivos apontando
para um OneDrive montado via WSL. Corrigir à mão só reseta o relógio; este script o para.

Também pega os apelidos SSH do prefixo antigo ``github-``, aposentados porque apontavam para a
conta errada depois que o papel "pessoal" mudou de significado.

Uso::

    python3 checar-caminhos.py                         # vault inteiro (neste repo)
    python3 checar-caminhos.py a.md b.md               # só os arquivos dados (hooks)
    python3 checar-caminhos.py --raiz ~/ComoTestar     # outra árvore do ecossistema
    python3 checar-caminhos.py --texto < patch.txt     # blob no stdin (apply_patch do Codex)
    python3 checar-caminhos.py --stdin < patch.txt     # alias de --texto

A trava vale no vault e nas árvores derivadas (Planejamentos, Formalizados, ComoTestar,
ComoApresentar, Insights, Insites, Memorias). ``~/ComoTestar`` e irmãs são repositórios à
parte e carregam o próprio ``.caminhos-legado``.

Sai 1 se achar algo fora da allowlist de ``.caminhos-legado``.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path, PurePosixPath

VAULT = Path(os.environ.get("VALT") or Path.home() / "Valt").expanduser()

EXTENSOES = {".md", ".py", ".sh", ".json", ".mdc", ".example", ".env", ".yml", ".yaml"}
IGNORAR_DIRS = {".git", "__pycache__", "node_modules", ".cursor", "vendor", ".claude"}

REGRAS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "caminho com usuário",
        re.compile(r"/home/[A-Za-z0-9._-]+/"),
        "use ~/ ou $VALT/$SITES/$PLANEJAMENTOS/$FORMALIZADOS/$COMOTESTAR/$COMOAPRESENTAR/$INSITES/$INSIGHTS",
    ),
    (
        "montagem WSL",
        re.compile(r"/mnt/[a-z]/"),
        "esta máquina é Ubuntu nativo; as saídas vivem em ~/Planejamentos e ~/Formalizados",
    ),
    (
        "apelido SSH aposentado",
        re.compile(r"\b(github-(pessoal|trabalho|newcesar)|gh-(formal|pessoal))\b"),
        "os apelidos se chamam como a pasta: gh-seara / gh-trabalho / gh-pessoais / gh-legado",
    ),
]


def carregar_allowlist(raiz: Path) -> list[str]:
    """Prefixos relativos à raiz isentos da checagem, lidos do ``.caminhos-legado`` dela.

    São registros históricos — checklists de formatação e diários de continuidade descrevem
    máquinas passadas. Reescrevê-los apagaria a memória da migração.
    """
    allowlist = raiz / ".caminhos-legado"
    if not allowlist.exists():
        return []
    linhas = allowlist.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in linhas if ln.strip() and not ln.startswith("#")]


def isento(relativo: str, allowlist: list[str]) -> bool:
    partes = PurePosixPath(relativo).parts
    for padrao in allowlist:
        if padrao.startswith("**/"):
            # ``**/continuidade/`` isenta a pasta em qualquer profundidade
            if padrao.rstrip("/").removeprefix("**/") in partes:
                return True
        elif relativo == padrao or relativo.startswith(padrao.rstrip("/") + "/"):
            return True
    return False


def alvos(argumentos: list[str], raiz: Path) -> list[Path]:
    if argumentos:
        return [Path(a).expanduser().resolve() for a in argumentos]
    encontrados = []
    for caminho in raiz.rglob("*"):
        if not caminho.is_file() or caminho.suffix not in EXTENSOES:
            continue
        if IGNORAR_DIRS & set(caminho.relative_to(raiz).parts):
            continue
        encontrados.append(caminho)
    return sorted(encontrados)


def separar_raiz(argumentos: list[str]) -> tuple[Path, list[str]]:
    """Extrai ``--raiz <caminho>`` da linha de comando. Sem ele, a raiz é o vault."""
    if "--raiz" in argumentos:
        i = argumentos.index("--raiz")
        try:
            raiz = Path(argumentos[i + 1]).expanduser().resolve()
        except IndexError:
            raise SystemExit("--raiz exige um caminho") from None
        return raiz, argumentos[:i] + argumentos[i + 2 :]
    return VAULT, argumentos


# Marcadores estruturais de patch (apply_patch / unified). NÃO usar só +/- :
# bullets Markdown "- item" são texto solto e precisam ser vistos inteiros.
PATCH_MARCA = re.compile(
    r"(?m)^(?:\*\*\* Begin Patch|\*\*\* (?:Add|Update|Delete|Move) File:|"
    r"@@ |--- a/|\+\+\+ b/)"
)


def _linhas_para_checar(texto: str) -> list[tuple[int, str]]:
    """Em patch estrutural, só conteúdo novo (+ e cabeçalhos de arquivo); senão, tudo."""
    linhas = texto.splitlines()
    if not PATCH_MARCA.search(texto):
        return [(n, ln) for n, ln in enumerate(linhas, 1)]
    saida: list[tuple[int, str]] = []
    for n, ln in enumerate(linhas, 1):
        if ln.startswith("+") and not ln.startswith("+++"):
            saida.append((n, ln[1:]))
            continue
        m = re.match(r"\*\*\* (?:Add|Update|Delete|Move) File:\s*(.+)$", ln)
        if m:
            saida.append((n, m.group(1)))
            continue
        m = re.match(r"\+\+\+ b/(.+)$", ln)
        if m:
            saida.append((n, m.group(1)))
    return saida


def checar_texto(texto: str, rotulo: str = "<stdin>") -> list[str]:
    """Aplica as mesmas regras a um blob (patch do Codex ou texto solto no stdin)."""
    achados: list[str] = []
    for numero, linha in _linhas_para_checar(texto):
        for nome, padrao, dica in REGRAS:
            achado = padrao.search(linha)
            if achado:
                achados.append(
                    f"{rotulo}:{numero}: {nome} '{achado.group(0)}' — {dica}"
                )
    return achados


def main(argumentos: list[str]) -> int:
    if "--texto" in argumentos or "--stdin" in argumentos:
        blob = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        achados = checar_texto(blob)
        if achados:
            print(f"✗ {len(achados)} ocorrência(s) que não sobrevivem a uma formatação:\n")
            print("\n".join(achados))
            return 1
        print("✓ nenhum caminho ou apelido frágil encontrado")
        return 0

    raiz, argumentos = separar_raiz(argumentos)
    allowlist = carregar_allowlist(raiz)
    achados: list[str] = []

    for caminho in alvos(argumentos, raiz):
        try:
            relativo = str(caminho.relative_to(raiz))
        except ValueError:
            continue  # fora da raiz — hooks podem passar qualquer coisa
        if isento(relativo, allowlist):
            continue
        try:
            linhas = caminho.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue

        achados.extend(checar_texto("\n".join(linhas), relativo))

    if achados:
        print(f"✗ {len(achados)} ocorrência(s) que não sobrevivem a uma formatação:\n")
        print("\n".join(achados))
        print(
            "\nSe for registro histórico deliberado, acrescente o caminho em .caminhos-legado."
        )
        return 1

    print("✓ nenhum caminho ou apelido frágil encontrado")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
