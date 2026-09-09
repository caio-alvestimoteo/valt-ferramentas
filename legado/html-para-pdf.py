#!/usr/bin/env python3
"""Exporta HTML de Planejamentos para PDF em Formalizados.

Espelha a subárvore: Planejamentos/<caminho>/x.html -> Formalizados/<caminho>/x.pdf.
Cria a árvore de pastas destino se ela não existir.

Saídas (no OneDrive, fora do $HOME do WSL):
    Planejamentos = ~/Planejamentos
    Formalizados  = ~/Formalizados

Motor: weasyprint (lib Python, sem confinamento — grava no OneDrive sem problema,
ao contrário do Chromium via snap, que só escreve dentro do $HOME).

Exemplos:
    python3 scripts/html-para-pdf.py Seara/Food/Login/x.html
    python3 scripts/html-para-pdf.py --all
    python3 scripts/html-para-pdf.py --all --planejamentos /caminho/Planejamentos --formalizados /caminho/Formalizados

O argumento posicional pode ser um caminho absoluto de um .html, ou um caminho
relativo à raiz de Planejamentos (ex.: Seara/Food/Login/x.html).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Raízes canônicas de saída (OneDrive, acessível do WSL via /mnt/c).
PLANEJAMENTOS_PADRAO = Path.home() / "Planejamentos"
FORMALIZADOS_PADRAO = Path.home() / "Formalizados"


def carregar_weasyprint():
    try:
        from weasyprint import HTML  # import tardio: só quando for gerar PDF
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "weasyprint não encontrado. Instale com: "
            "sudo apt-get install -y weasyprint"
        ) from exc
    return HTML


def resolver_html(arg: str, planejamentos: Path) -> Path:
    """Aceita caminho absoluto OU relativo à raiz de Planejamentos."""
    p = Path(arg)
    if p.is_absolute():
        return p
    como_relativo = planejamentos / p
    return como_relativo if como_relativo.exists() else p


def destino_pdf(html: Path, planejamentos: Path, formalizados: Path) -> Path:
    """Caminho espelhado do PDF em Formalizados (cai na raiz se fora de Planejamentos)."""
    try:
        rel = html.resolve().relative_to(planejamentos.resolve())
    except ValueError:
        rel = Path(html.name)
    return (formalizados / rel).with_suffix(".pdf")


def exportar(HTML, html: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)  # cria a árvore espelhada
    HTML(filename=str(html.resolve())).write_pdf(str(out))
    if not out.exists():
        raise SystemExit(f"Falha ao gerar PDF de {html}")
    print(f"OK  {html}  ->  {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="HTML (Planejamentos) -> PDF (Formalizados).")
    parser.add_argument("html", nargs="?", help="caminho de um .html (abs. ou relativo a Planejamentos/)")
    parser.add_argument("--all", action="store_true", help="exporta todos os .html de Planejamentos/")
    parser.add_argument("--planejamentos", default=str(PLANEJAMENTOS_PADRAO), help="raiz dos HTML")
    parser.add_argument("--formalizados", default=str(FORMALIZADOS_PADRAO), help="raiz dos PDF")
    args = parser.parse_args()

    planejamentos = Path(args.planejamentos)
    formalizados = Path(args.formalizados)
    HTML = carregar_weasyprint()

    if args.all:
        htmls = sorted(planejamentos.rglob("*.html"))
        if not htmls:
            raise SystemExit(f"Nenhum .html em {planejamentos}")
        for html in htmls:
            exportar(HTML, html, destino_pdf(html, planejamentos, formalizados))
        print(f"\n{len(htmls)} PDF(s) gerado(s) em {formalizados}")
        return

    if not args.html:
        parser.error("informe um caminho .html ou use --all")
    html = resolver_html(args.html, planejamentos)
    if not html.exists():
        raise SystemExit(f"HTML não encontrado: {html}")
    exportar(HTML, html, destino_pdf(html, planejamentos, formalizados))


if __name__ == "__main__":
    main()
