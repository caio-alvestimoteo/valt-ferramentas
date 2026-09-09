"""Localiza o Playwright instalado e roda scripts Node contra ele.

O Playwright vive no cache do ``npx``, num diretório de nome imprevisível. Um script Node só
consegue dar ``require('playwright')`` se estiver **dentro** desse ``node_modules`` — daí o
`executar_script`, que escreve o script lá, roda e limpa.

Compartilhado por ``html-para-pdf-imagem.py`` (chromium) e ``prints-como-testar.py`` (firefox).
"""

from __future__ import annotations

import glob
import os
import subprocess

__all__ = ["encontrar_node_modules", "garantir_engine", "executar_script"]


def encontrar_node_modules() -> str | None:
    """Localiza o node_modules que contém o playwright (cache do npx)."""
    padroes = [
        os.path.expanduser("~/.npm/_npx/*/node_modules"),
        os.path.expanduser("~/node_modules"),
        "/usr/lib/node_modules",
        "/usr/local/lib/node_modules",
    ]
    for padrao in padroes:
        for d in glob.glob(padrao):
            if os.path.isdir(os.path.join(d, "playwright")):
                return d
    return None


def garantir_engine(engine: str = "chromium") -> str:
    """Devolve o node_modules do playwright, instalando o engine se ainda não houver.

    ``engine`` é ``chromium`` ou ``firefox``. O download passa de 90 MB na primeira vez.
    """
    diretorio = encontrar_node_modules()
    if not diretorio:
        print(f"     Playwright não encontrado — instalando {engine} via npx...")
        subprocess.run(
            ["npx", "--yes", "playwright", "install", engine], check=True, timeout=600
        )
        diretorio = encontrar_node_modules()
        if not diretorio:
            raise RuntimeError("Falha ao instalar o playwright")

    # O engine pode faltar mesmo com o pacote presente (ex.: só chromium baixado).
    if not _engine_baixado(engine):
        print(f"     Engine {engine} ausente — baixando (uma vez só)...")
        subprocess.run(
            ["npx", "--yes", "playwright", "install", engine], check=True, timeout=600
        )
    return diretorio


def _engine_baixado(engine: str) -> bool:
    cache = os.path.expanduser("~/.cache/ms-playwright")
    if not os.path.isdir(cache):
        return False
    return any(nome.startswith(engine) for nome in os.listdir(cache))


def executar_script(corpo_js: str, node_modules: str, nome: str = "._playwright.cjs") -> str:
    """Roda um script CJS dentro do node_modules do playwright e devolve o stdout."""
    caminho = os.path.join(node_modules, nome)
    with open(caminho, "w", encoding="utf-8") as arquivo:
        arquivo.write(corpo_js)
    try:
        resultado = subprocess.run(
            ["node", caminho],
            check=True,
            timeout=600,
            capture_output=True,
            text=True,
        )
        if resultado.stderr.strip():
            print(resultado.stderr.strip())
        return resultado.stdout
    finally:
        if os.path.exists(caminho):
            os.remove(caminho)
