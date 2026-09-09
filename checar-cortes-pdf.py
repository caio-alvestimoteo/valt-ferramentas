#!/usr/bin/env python3
"""checar-cortes-pdf.py — trava contra PDF que corta conteúdo no meio

O PDF formalizado é uma captura full-page fatiada em páginas A4. Se a fatia cair no lugar errado,
uma linha de texto é partida ao meio e o leitor perde informação — sem nenhum aviso de que aquilo
aconteceu. Este script mede as quebras **antes** de o PDF chegar em alguém.

Três invariantes, verificadas nesta ordem:

1. **Nenhum pixel perdido.** As faixas têm de ser contíguas: o topo de cada página é exatamente o
   fim da anterior. Uma lacuna significa conteúdo que sumiu do documento.
2. **Nenhuma linha de texto fatiada.** Toda quebra cai numa borda de elemento ou, quando isso é
   impossível, numa faixa horizontal sem tinta.
3. **Nenhum bloco maior que a página.** Um bloco que não cabe será cortado de qualquer jeito —
   quem gera o documento precisa saber para fatiá-lo na origem.

Uso::

    python3 scripts/checar-cortes-pdf.py ComoTestar/Seara/massaleve.com.br/politica.html
    python3 scripts/checar-cortes-pdf.py ~/Planejamentos/x.html --silencioso

Sai 1 se alguma invariante falhar. Rodado automaticamente por ``comotestar-para-pdf.py``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from valt_paths import PLANEJAMENTOS  # noqa: E402


def _pipeline():
    spec = importlib.util.spec_from_file_location(
        "pdf_imagem", Path(__file__).resolve().parent / "html-para-pdf-imagem.py"
    )
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def analisar(html_path: Path) -> dict:
    """Roda o mesmo cálculo de paginação do exportador e mede cada quebra."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    pdf = _pipeline()

    with tempfile.TemporaryDirectory() as tmp:
        png = str(Path(tmp) / "captura.png")
        layout_json = str(Path(tmp) / "layout.json")
        pdf.screenshot_full_page(str(html_path), png, layout_json)

        largura, altura_png = pdf.png_dimensions(png)
        layout = json.loads(Path(layout_json).read_text())
        intervalos = layout.get("intervals", [])
        altura = int(min(layout.get("contentBottom", altura_png), altura_png))
        faixas = pdf.choose_page_ranges(altura, intervalos)

        altos = [(a, b) for a, b in intervalos if b - a >= pdf.A4_PAGE_H]

        lacunas = []
        anterior = None
        for topo, base, _ in faixas:
            if anterior is not None and topo != anterior:
                lacunas.append((anterior, topo))
            anterior = base

        cortes = []
        with Image.open(png) as fonte:
            fonte.load()
            for topo, base, semantico in faixas[:-1]:
                registro = {"y": base, "semantico": semantico, "limpo": True, "tinta": 0}
                if not semantico:
                    # Mede a posição **depois** do ajuste — é ela que vai para o PDF.
                    ajustado = pdf.linha_sem_texto(fonte, base, largura)
                    perfil = pdf.perfil_de_tinta(fonte, ajustado, largura, 260)
                    piso = min(perfil) if perfil else 0
                    tolerancia = piso + max(8, int(largura * 0.004))
                    registro.update(
                        y=ajustado,
                        y_original=base,
                        ajuste=base - ajustado,
                        tinta=perfil[0] if perfil else 0,
                        piso=piso,
                        limpo=bool(perfil) and perfil[0] <= tolerancia,
                    )
                cortes.append(registro)

        preenchimento = [
            (base - topo) / pdf.A4_PAGE_H for topo, base, _ in faixas[:-1]
        ]

    return {
        "paginas": len(faixas),
        "altura": altura,
        "lacunas": lacunas,
        "cortes": cortes,
        "blocos_altos": altos,
        "preenchimento": preenchimento,
    }


def relatar(resultado: dict, nome: str, silencioso: bool) -> int:
    erros, avisos = [], []

    if resultado["lacunas"]:
        for fim, inicio in resultado["lacunas"]:
            erros.append(f"pixels perdidos entre y={fim} e y={inicio} ({inicio - fim}px sumiram)")

    for corte in resultado["cortes"]:
        if not corte["limpo"]:
            erros.append(
                f"quebra em y={corte['y']} corta linha de texto "
                f"(tinta {corte['tinta']}, piso {corte.get('piso', 0)})"
            )

    if resultado["blocos_altos"]:
        avisos.append(
            f"{len(resultado['blocos_altos'])} bloco(s) maior(es) que uma página A4 — "
            "serão cortados; fatie na origem se o conteúdo importar"
        )

    magras = [p for p in resultado["preenchimento"] if p < 0.45]
    if magras:
        avisos.append(f"{len(magras)} página(s) com menos de 45% de preenchimento")

    if not silencioso:
        arbitrarias = sum(1 for c in resultado["cortes"] if not c["semantico"])
        print(f"  páginas ................. {resultado['paginas']}")
        print(f"  pixels perdidos ......... {len(resultado['lacunas'])}")
        print(f"  quebras arbitrárias ..... {arbitrarias} (ajustadas para linha sem texto)")
        print(f"  blocos > 1 página ....... {len(resultado['blocos_altos'])}")

    for aviso in avisos:
        print(f"  ! {aviso}")
    for erro in erros:
        print(f"  ✗ {erro}")

    if erros:
        print(f"\n✗ {nome}: o PDF perderia informação no corte.")
        return 1
    if not silencioso:
        print(f"\n✓ {nome}: nenhuma linha cortada, nenhum pixel perdido.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Verifica se a paginação do PDF corta conteúdo")
    parser.add_argument("html", help="caminho do HTML, relativo a ~/Planejamentos ou absoluto")
    parser.add_argument("--silencioso", action="store_true", help="só imprime problemas")
    args = parser.parse_args(argv)

    caminho = Path(args.html).expanduser()
    if not caminho.is_absolute():
        caminho = PLANEJAMENTOS / args.html
    if not caminho.exists():
        raise SystemExit(f"HTML não encontrado: {caminho}")

    return relatar(analisar(caminho), caminho.name, args.silencioso)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
