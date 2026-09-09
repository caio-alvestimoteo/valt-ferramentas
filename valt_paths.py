"""Caminhos canônicos do ecossistema — espelho Python de ``paths.env``.

Tudo deriva de ``$HOME``; nenhum caminho carrega nome de usuário. Ver ``~/Valt/paths.env``.

Uso::

    from valt_paths import VALT, SITES, PLANEJAMENTOS, FORMALIZADOS, COMOTESTAR, COMOAPRESENTAR, espelhar

    destino = espelhar(nota_md, PLANEJAMENTOS, ".html")
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "VALT", "SITES", "PLANEJAMENTOS", "FORMALIZADOS", "COMOTESTAR",
    "COMOAPRESENTAR", "FERRAMENTAS", "espelhar",
]


def _raiz(env: str, padrao: str) -> Path:
    return Path(os.environ.get(env) or Path.home() / padrao).expanduser()


VALT = _raiz("VALT", "Valt")
SITES = _raiz("SITES", "Sites")
PLANEJAMENTOS = _raiz("PLANEJAMENTOS", "Planejamentos")
FORMALIZADOS = _raiz("FORMALIZADOS", "Formalizados")
COMOTESTAR = _raiz("COMOTESTAR", "ComoTestar")
COMOAPRESENTAR = _raiz("COMOAPRESENTAR", "ComoApresentar")
FERRAMENTAS = _raiz("FERRAMENTAS", "Sites/Trabalho/valt-ferramentas")


def espelhar(
    origem: Path | str,
    destino_raiz: Path,
    extensao: str,
    origem_raiz: Path | None = None,
) -> Path:
    """Mapeia uma nota para a árvore derivada, preservando o caminho relativo.

    ``~/Valt/Seara/Food/x.md`` → ``~/Planejamentos/Seara/Food/x.html``

    A fonte é o vault por padrão. Um roteiro de como testar nasce em ``~/ComoTestar``, fora do
    vault, e por isso passa a própria raiz::

        espelhar(doc, PLANEJAMENTOS, ".html", origem_raiz=COMOTESTAR)

    Levanta ``ValueError`` se ``origem`` não estiver dentro da raiz — o espelhamento só faz
    sentido a partir da fonte.
    """
    raiz = (origem_raiz or VALT).expanduser().resolve()
    origem = Path(origem).expanduser().resolve()
    try:
        relativo = origem.relative_to(raiz)
    except ValueError as erro:
        raise ValueError(f"{origem} está fora de {raiz}") from erro
    return destino_raiz / relativo.with_suffix(extensao)
