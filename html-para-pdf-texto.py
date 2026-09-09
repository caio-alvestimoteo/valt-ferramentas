#!/usr/bin/env python3
"""
html-para-pdf-texto.py — PDF continuo com texto selecionavel (page.pdf do Chromium)

Mesmo enquadramento do modo --continuo do html-para-pdf-imagem.py (pagina unica,
largura de 1920px CSS mapeada em 210 mm), mas impresso pelo motor do Chromium em
vez de screenshot + img2pdf — o cliente consegue selecionar e copiar o texto.

Quando usar: sempre que o destinatario precisar copiar informacoes do PDF
(orcamentos, propostas). O modo imagem continua valendo quando fidelidade
pixel-perfect importar mais que texto vivo.

Uso:
  python3 scripts/html-para-pdf-texto.py <caminho-relativo-a-Planejamentos.html>
  python3 scripts/html-para-pdf-texto.py Seara/Food/OrcamentoCliente/carrinho-inteligente.html

Saida espelhada:
  ~/Planejamentos/<path>.html  ->  ~/Formalizados/<path>.pdf

O PDF sai com os estilos de tela (emulateMedia 'screen'), igual ao que o modo
imagem entregava — o @media print dos documentos foi pensado para a rota
weasyprint e mudaria o visual.

Requisitos: node (>= 16) e o chromium do Playwright (ver playwright_util.py).
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playwright_util import garantir_engine

PLANEJAMENTOS = os.path.expanduser("~/Planejamentos")
FORMALIZADOS = os.path.expanduser("~/Formalizados")
VIEWPORT_W = 1920
MM_POR_PX = 210.0 / VIEWPORT_W  # largura fisica fixa na do A4


def find_html(arg: str) -> str:
    if os.path.isabs(arg) and os.path.exists(arg):
        return arg
    candidato = os.path.join(PLANEJAMENTOS, arg)
    if os.path.exists(candidato):
        return candidato
    raise FileNotFoundError(f"HTML nao encontrado: {arg}")


def mirror_pdf_path(html_path: str) -> str:
    rel = os.path.relpath(html_path, PLANEJAMENTOS)
    return os.path.join(FORMALIZADOS, os.path.splitext(rel)[0] + ".pdf")


def gerar_pdf_texto(html_path: str, pdf_path: str) -> None:
    url = f"file://{os.path.abspath(html_path)}"
    # scale faz o layout de 1920px CSS caber nos 210 mm (~793.7px) da folha.
    scale = round(793.7 / VIEWPORT_W, 5)

    pw_dir = garantir_engine("chromium")
    script_path = os.path.join(pw_dir, "._pdf_texto.cjs")
    script = f"""
const {{chromium}} = require('playwright');
(async () => {{
  const browser = await chromium.launch({{args: ['--no-sandbox', '--disable-dev-shm-usage']}});
  const page = await browser.newPage();
  await page.setViewportSize({{width: {VIEWPORT_W}, height: 900}});
  await page.goto({json.dumps(url)}, {{waitUntil: 'networkidle', timeout: 30000}});
  await page.waitForTimeout(2000);
  await page.emulateMedia({{media: 'screen'}});
  const h = await page.evaluate(() => document.documentElement.scrollHeight);
  const alturaMm = Math.ceil(h * {MM_POR_PX} * 100) / 100 + 1;
  await page.pdf({{
    path: {json.dumps(pdf_path)},
    printBackground: true,
    width: '210mm',
    height: alturaMm + 'mm',
    scale: {scale},
    pageRanges: '1',
    margin: {{top: 0, right: 0, bottom: 0, left: 0}},
  }});
  await browser.close();
  console.log('pdf ok, altura ' + alturaMm + 'mm (' + h + 'px)');
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    with open(script_path, "w") as f:
        f.write(script)
    try:
        os.makedirs(os.path.dirname(pdf_path) or ".", exist_ok=True)
        subprocess.run(
            ["node", os.path.basename(script_path)], cwd=pw_dir, check=True, timeout=120
        )
    finally:
        if os.path.exists(script_path):
            os.unlink(script_path)


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not argv:
        print(__doc__)
        sys.exit(1)

    html_path = find_html(argv[0])
    pdf_path = mirror_pdf_path(html_path)
    print(f"HTML:      {html_path}")
    print(f"PDF saida: {pdf_path}")
    gerar_pdf_texto(html_path, pdf_path)
    kb = os.path.getsize(pdf_path) // 1024
    print(f"\nPDF gerado: {pdf_path}  ({kb} KB)")


if __name__ == "__main__":
    main()
