#!/usr/bin/env python3
"""
html-para-pdf-imagem.py — PDF fiel via screenshot full-page

Gera PDF pixel-perfect a partir de um HTML de planejamento:
  1. Playwright (Chromium via npx) captura screenshot full-page da pagina inteira
  2. Capturas muito altas sao divididas em paginas A4 sem perda visual
  3. img2pdf converte os PNGs -> PDF sem recompressao
  4. anotações /Link URI tornam os <a href> do HTML clicáveis no PDF

Uso:
  python3 scripts/html-para-pdf-imagem.py <caminho-relativo-a-Planejamentos.html>
  python3 scripts/html-para-pdf-imagem.py Seara/salada.com.br/Repositorio/como-funciona.html
  python3 scripts/html-para-pdf-imagem.py <caminho> --continuo   # uma pagina so, sem quebra

Quando usar --continuo: documentos densos, com tabelas longas e cards lado a lado, em que a
quebra em folhas A4 parte um bloco no meio. A pagina fica com a largura do A4 e a altura do
documento inteiro — nao ha o que cortar. O limite e ~200 polegadas de altura.

Saida espelhada:
  ~/Planejamentos/<path>.html  ->  ~/Formalizados/<path>.pdf

Requisitos:
  node (>= 16), img2pdf, python3-pikepdf
  sudo apt install img2pdf python3-pikepdf
  npx playwright install chromium   (primeira vez)
"""

import sys, os, subprocess, shutil, tempfile, glob, struct, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playwright_util import garantir_engine

PLANEJAMENTOS = os.path.expanduser("~/Planejamentos")
FORMALIZADOS  = os.path.expanduser("~/Formalizados")
VIEWPORT_W    = 1920
A4_PAGE_H     = round(VIEWPORT_W * 297 / 210)


def find_html(arg: str) -> str:
    if os.path.isabs(arg) and os.path.exists(arg):
        return arg
    candidate = os.path.join(PLANEJAMENTOS, arg)
    if os.path.exists(candidate):
        return candidate
    raise FileNotFoundError(f"HTML nao encontrado: {arg}")


def mirror_pdf_path(html_path: str) -> str:
    rel     = os.path.relpath(html_path, PLANEJAMENTOS)
    pdf_rel = os.path.splitext(rel)[0] + ".pdf"
    return os.path.join(FORMALIZADOS, pdf_rel)


def screenshot_full_page(html_path: str, png_path: str, layout_path: str):
    """Captura a página e registra intervalos de componentes que não devem ser cortados."""
    url = f"file://{os.path.abspath(html_path)}"

    # Resolver compartilhado com prints-como-testar.py — ver scripts/playwright_util.py
    pw_dir = garantir_engine("chromium")

    # Escreve o script dentro do diretório do node_modules para que require() funcione
    script_path = os.path.join(pw_dir, "._screenshot.cjs")
    script = f"""
const {{chromium}} = require('playwright');
const fs = require('fs');
(async () => {{
  const browser = await chromium.launch({{args: ['--no-sandbox', '--disable-dev-shm-usage']}});
  const page    = await browser.newPage();
  await page.setViewportSize({{width: {VIEWPORT_W}, height: 900}});
  await page.goto({repr(url)}, {{waitUntil: 'networkidle', timeout: 30000}});
  await page.waitForTimeout(2000);
  const layout = await page.evaluate(() => {{
    const selectors = [
      'h1', 'h2', 'h3', 'h4', 'p', 'li', 'tr', 'img',
      'figure', 'table', 'pre', '.card',
      '.hero', '.semana', '.geral',
      '.fc-node', '.summary-strip > div', '.block-integ', '.alert',
      '.finding', '.evidence-card', '.tested-form-head', '.flow-heading',
      '.page-url', '.toc-col > a', '.status-pill'
    ].join(',');
    const intervalOf = (element) => {{
        const rect = element.getBoundingClientRect();
        const guarded = element.matches('.hero, .semana, .geral, .alert');
        const guard = guarded ? 12 : 0;
        return [
          Math.max(0, Math.round(rect.top + window.scrollY - guard)),
          Math.round(rect.bottom + window.scrollY + guard)
        ];
    }};
    const intervals = Array.from(document.querySelectorAll(selectors))
      .map(intervalOf)
      .filter(([top, bottom]) => bottom > top && bottom > 0);

    // Evita título órfão: mantém o início de cada tópico com o primeiro nó do fluxo.
    document.querySelectorAll('.page-topic').forEach((article) => {{
      const first = article.firstElementChild;
      const firstNode = article.querySelector('.fc-node');
      if (first && firstNode) {{
        const [top] = intervalOf(first);
        const [, bottom] = intervalOf(firstNode);
        if (bottom > top && bottom - top < {A4_PAGE_H}) intervals.push([top, bottom]);
      }}
    }});

    // Mantém subtítulos com a primeira unidade de conteúdo que os explica.
    document.querySelectorAll('h2, h3, h4, .flow-heading').forEach((heading) => {{
      let next = heading.nextElementSibling;
      if (next && /^(UL|OL)$/.test(next.tagName) && next.firstElementChild) next = next.firstElementChild;
      if (next) {{
        const [top] = intervalOf(heading);
        const [, bottom] = intervalOf(next);
        if (bottom > top && bottom - top < {A4_PAGE_H}) intervals.push([top, bottom]);
      }}
    }});

    const footer = document.querySelector('.footer-note');
    const contentBottom = footer ? intervalOf(footer)[1] + 40 : document.documentElement.scrollHeight;

    // Links http(s) com posição no documento — viram anotações clicáveis no PDF depois do img2pdf.
    const links = Array.from(document.querySelectorAll('a[href]')).map((a) => {{
      const rect = a.getBoundingClientRect();
      return {{
        href: a.href,
        x: Math.round(rect.left + window.scrollX),
        y: Math.round(rect.top + window.scrollY),
        w: Math.round(rect.width),
        h: Math.round(rect.height),
      }};
    }}).filter((l) => l.w > 2 && l.h > 2 && /^https?:/i.test(l.href || ''));

    return {{height: document.documentElement.scrollHeight, contentBottom, intervals, links}};
  }});
  fs.writeFileSync({repr(layout_path)}, JSON.stringify(layout));
  await page.screenshot({{path: {repr(png_path)}, fullPage: true}});
  await browser.close();
  console.log('screenshot ok');
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    with open(script_path, "w") as f:
        f.write(script)

    try:
        subprocess.run(
            ["node", os.path.basename(script_path)],
            cwd=pw_dir,
            check=True,
            timeout=90,
        )
    finally:
        if os.path.exists(script_path):
            os.unlink(script_path)


def png_to_pdf(png_paths: list[str], pdf_path: str):
    os.makedirs(os.path.dirname(pdf_path) or ".", exist_ok=True)
    subprocess.run(
        ["img2pdf", *png_paths, "--pagesize", "A4", "--fit", "into", "--output", pdf_path],
        check=True,
    )


def _links_http(links) -> list:
    """Só URLs absolutas http(s) com caixa útil — o resto não vira anotação."""
    saida = []
    for bruto in links or []:
        href = (bruto.get("href") or "").strip()
        if not href.lower().startswith(("http://", "https://")):
            continue
        try:
            x, y, w, h = (int(bruto["x"]), int(bruto["y"]), int(bruto["w"]), int(bruto["h"]))
        except (KeyError, TypeError, ValueError):
            continue
        if w <= 2 or h <= 2:
            continue
        saida.append({"href": href, "x": x, "y": y, "w": w, "h": h})
    return saida


def aplicar_links_no_pdf(
    pdf_path: str,
    links: list,
    *,
    paginas: list,
    png_largura: int,
    png_altura_pagina=None,
) -> int:
    """Sobrepõe anotações ``/Link`` URI no PDF gerado por img2pdf (que é só imagem).

    ``paginas`` lista ``(topo_px, base_px)`` de cada página no PNG full-page. Em página única
    contínua, use ``[(0, altura)]``. Coordenadas do DOM têm origem no topo; PDF, na base.
    Devolve quantos links foram aplicados.
    """
    from pikepdf import Array, Dictionary, Name, Pdf

    uteis = _links_http(links)
    if not uteis or png_largura <= 0:
        return 0

    pdf = Pdf.open(pdf_path, allow_overwriting_input=True)
    aplicados = 0
    try:
        for indice, (topo, base) in enumerate(paginas):
            if indice >= len(pdf.pages):
                break
            page = pdf.pages[indice]
            mediabox = page.mediabox
            largura_pt = float(mediabox[2] - mediabox[0])
            altura_pt = float(mediabox[3] - mediabox[1])
            # Paginado A4: a faixa costuma ser preenchida até A4_PAGE_H (com padding).
            altura_px = png_altura_pagina or max(1, base - topo)
            escala_x = largura_pt / png_largura
            escala_y = altura_pt / altura_px

            anotacoes = []
            for link in uteis:
                y0, y1 = link["y"], link["y"] + link["h"]
                if y1 <= topo or y0 >= base:
                    continue
                local_y = link["y"] - topo
                llx = link["x"] * escala_x
                urx = (link["x"] + link["w"]) * escala_x
                # Origem PDF embaixo: inverter Y.
                ury = altura_pt - local_y * escala_y
                lly = altura_pt - (local_y + link["h"]) * escala_y
                llx = max(0.0, min(largura_pt, llx))
                urx = max(0.0, min(largura_pt, urx))
                lly = max(0.0, min(altura_pt, lly))
                ury = max(0.0, min(altura_pt, ury))
                if urx - llx < 1 or ury - lly < 1:
                    continue
                anotacoes.append(
                    Dictionary(
                        Type=Name.Annot,
                        Subtype=Name.Link,
                        Rect=Array([llx, lly, urx, ury]),
                        Border=Array([0, 0, 0]),
                        A=Dictionary(S=Name.URI, URI=link["href"]),
                    )
                )
            if not anotacoes:
                continue
            if Name.Annots not in page:
                page[Name.Annots] = pdf.make_indirect(Array([]))
            for anot in anotacoes:
                page.Annots.append(pdf.make_indirect(anot))
                aplicados += 1
        pdf.save(pdf_path)
    finally:
        pdf.close()
    return aplicados


def png_dimensions(png_path: str):
    """Lê largura/altura no IHDR sem acionar os limites de imagem do ImageMagick."""
    with open(png_path, "rb") as png:
        header = png.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"PNG inválido ou sem IHDR: {png_path}")
    return struct.unpack(">II", header[16:24])


def choose_page_ranges(height: int, intervals: list[list[int]]) -> list[tuple[int, int]]:
    """Escolhe quebras A4 fora de elementos semânticos visíveis.

    Devolve ``(top, bottom, semantico)`` — ``semantico`` diz se a quebra caiu numa borda de
    elemento. Quando é ``False``, o corte é inevitável (o bloco é mais alto que uma página) e
    precisa do ajuste por pixel de ``linha_sem_texto``, senão corta no meio de uma linha.
    """
    # Um bloco mais alto que a página não cabe em página nenhuma: mantê-lo aqui só faria ele vetar
    # todas as quebras internas e empurrar o corte para uma posição arbitrária. Descartá-lo devolve
    # a decisão aos filhos dele (parágrafos, figuras, linhas de tabela), que cabem e protegem.
    intervals = [(a, b) for a, b in intervals if b - a < A4_PAGE_H]

    ranges = []
    top = 0
    min_fill = int(A4_PAGE_H * 0.52)
    candidates = sorted({bottom for _, bottom in intervals})
    # Bordas reais de elemento. Só um corte que cai exatamente numa delas dispensa a conferência
    # por pixel: "não sobrepõe nada" não é prova de segurança quando não há nada para sobrepor —
    # num documento sem elementos reconhecidos, toda posição pareceria segura e o corte cairia
    # no meio do texto sem ninguém perceber.
    bordas = {b for _, b in intervals} | {a for a, _ in intervals}

    def is_safe(position):
        # Uma tolerância de poucos pixels ainda deixa bordas/sombras órfãs na
        # página seguinte. Só as bordas exatas do elemento são quebras seguras.
        return not any(start < position < end for start, end in intervals)

    while top < height:
        desired = min(top + A4_PAGE_H, height)
        semantico = True
        if desired == height:
            bottom = height
        elif is_safe(desired):
            bottom = desired
            semantico = desired in bordas
        else:
            safe = [
                candidate for candidate in candidates
                if top + min_fill <= candidate <= desired and is_safe(candidate)
            ]
            if safe:
                bottom = max(safe)
            else:
                # Nenhuma borda de elemento serve: o conteúdo aqui é mais alto que uma página.
                bottom = desired
                semantico = False
        if bottom <= top:
            bottom = min(top + A4_PAGE_H, height)
            semantico = False
        ranges.append((top, bottom, semantico))
        top = bottom
    return ranges


def perfil_de_tinta(source, y: int, largura: int, limite: int) -> list[int]:
    """Quantos pixels escuros há em cada faixa horizontal, subindo ``limite`` px a partir de ``y``."""
    esquerda, direita = int(largura * 0.12), int(largura * 0.88)
    perfil = []
    for delta in range(limite):
        candidato = y - delta
        if candidato <= 4:
            break
        faixa = source.crop((esquerda, candidato - 2, direita, candidato + 2)).convert("L")
        perfil.append(sum(faixa.histogram()[:150]))
        faixa.close()
    return perfil


def linha_sem_texto(source, y: int, largura: int, limite: int = 260) -> int:
    """Sobe a partir de ``y`` até a faixa horizontal mais limpa que existir.

    Último recurso para blocos mais altos que uma página — um print de página inteira, por
    exemplo. Sem isso a quebra cai onde calhar e fatia a linha de texto ao meio.

    Não dá para exigir tinta zero: a borda vertical e a sombra do próprio card aparecem em
    **todas** as linhas e formam um piso constante. O que identifica um vão entre linhas de texto
    é a tinta estar nesse piso — por isso a referência é o mínimo da janela, não o zero absoluto.

    Só sobe, nunca desce: a página encurta, mas nunca passa do A4.
    """
    perfil = perfil_de_tinta(source, y, largura, limite)
    if not perfil:
        return y
    piso = min(perfil)
    tolerancia = piso + max(8, int(largura * 0.004))
    for delta, tinta in enumerate(perfil):
        if tinta <= tolerancia:
            return y - delta
    return y


def split_png_a4(
    png_path: str,
    output_dir: str,
    intervals: list[list[int]],
    content_height: int,
) -> list:
    """Fatia a captura em A4, usando quebras seguras calculadas a partir do DOM.

    Devolve lista de ``(caminho_png, topo_px, base_px)`` — o intervalo no PNG original, para
    mapear links clicáveis em cada página do PDF.
    """
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    pages = []

    def has_foreground(image):
        preview = image.convert("L")
        preview.thumbnail((240, 400))
        dark_pixels = sum(preview.histogram()[:140])
        preview.close()
        return dark_pixels > 2

    with Image.open(png_path) as source:
        source.load()
        width, source_height = source.size
        height = min(source_height, content_height)
        background = source.getpixel((0, 0))
        ranges = choose_page_ranges(height, intervals)

        # Ajusta por pixel as quebras que não caíram numa borda de elemento, para nenhuma linha
        # de texto ser fatiada ao meio. Reencadeia o topo da página seguinte no valor corrigido.
        ajustadas = []
        top_real = 0
        for top, bottom, semantico in ranges:
            if not semantico and bottom < height:
                bottom = max(linha_sem_texto(source, bottom, width), top_real + 1)
            ajustadas.append((top_real, bottom))
            top_real = bottom
        ranges = [(a, b) for a, b in ajustadas if b > a]

        for index, (top, bottom) in enumerate(ranges, 1):
            page = source.crop((0, top, width, bottom))
            if bottom == height and not has_foreground(page):
                page.close()
                continue
            if page.height < A4_PAGE_H:
                padded = Image.new(source.mode, (width, A4_PAGE_H), background)
                padded.paste(page, (0, 0))
                page.close()
                page = padded
            page_path = os.path.join(output_dir, f"page-{index:03d}.png")
            page.save(page_path, "PNG", compress_level=6)
            page.close()
            pages.append((page_path, top, bottom))
    return pages


def gerar_continuo(html_path: str, pdf_path: str) -> int:
    """Uma página só, do tamanho exato do documento — sem corte nenhum."""
    largura_pt = 210 / 25.4 * 72  # 210 mm — a mesma largura do A4
    limite_lado_pt = 14400  # 200 polegadas, limite prático dos leitores de PDF.

    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "captura.png")
        layout_json = os.path.join(tmp, "layout.json")
        print("1/2  screenshot full-page (playwright chromium)...")
        screenshot_full_page(html_path, png, layout_json)

        largura, altura_png = png_dimensions(png)
        layout = json.loads(open(layout_json, encoding="utf-8").read())
        altura = int(min(layout.get("contentBottom", altura_png), altura_png))
        if altura < altura_png:
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = None
            with Image.open(png) as fonte:
                fonte.load()
                fonte.crop((0, 0, largura, altura)).save(png, "PNG", compress_level=6)

        altura_pt = altura * largura_pt / largura
        print(f"     página única: {largura}x{altura}px → {largura_pt / 72:.1f} x {altura_pt / 72:.0f} polegadas")
        if altura_pt > limite_lado_pt:
            print(f"\n✗ a página teria {altura_pt / 72:.0f} polegadas, acima do limite de {limite_lado_pt / 72:.0f} que os leitores de PDF aceitam.\n  Gere com --paginado.")
            return 1

        print("2/2  converte PNG -> PDF (img2pdf)...")
        os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
        subprocess.run(["img2pdf", png, "--imgsize", f"{largura_pt:.3f}x{altura_pt:.3f}pt", "--output", pdf_path], check=True)
        n_links = aplicar_links_no_pdf(pdf_path, layout.get("links") or [], paginas=[(0, altura)], png_largura=largura)
        print(f"     links clicáveis: {n_links}")
    return 0


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    continuo = "--continuo" in sys.argv[1:] or "--pagina-unica" in sys.argv[1:]

    if not argv:
        print(__doc__)
        sys.exit(1)

    html_path = find_html(argv[0])
    pdf_path  = mirror_pdf_path(html_path)

    if continuo:
        print(f"HTML:      {html_path}")
        print(f"PDF saida: {pdf_path}")
        codigo = gerar_continuo(html_path, pdf_path)
        if codigo == 0:
            kb = os.path.getsize(pdf_path) // 1024
            print(f"\nPDF gerado: {pdf_path}  ({kb} KB)")
        sys.exit(codigo)

    print(f"HTML:      {html_path}")
    print(f"PDF saida: {pdf_path}")

    with tempfile.TemporaryDirectory(dir=os.path.expanduser("~")) as tmp:
        png_path = os.path.join(tmp, "screenshot.png")
        layout_path = os.path.join(tmp, "layout.json")

        print("1/3  screenshot full-page (playwright chromium)...")
        screenshot_full_page(html_path, png_path, layout_path)
        w, h = png_dimensions(png_path)
        print(f"     PNG: {w}x{h}px  ({os.path.getsize(png_path)//1024} KB)")

        print("2/3  divide a captura em paginas A4 com quebras semanticas...")
        layout = json.loads(open(layout_path, encoding="utf-8").read())
        paginas = split_png_a4(png_path, tmp, layout["intervals"], layout["contentBottom"])
        print(f"     paginas: {len(paginas)}  ({VIEWPORT_W}x{A4_PAGE_H}px cada, sem cortar componentes)")

        print("3/3  converte PNGs -> PDF (img2pdf)...")
        png_to_pdf([p[0] for p in paginas], pdf_path)
        n_links = aplicar_links_no_pdf(
            pdf_path,
            layout.get("links") or [],
            paginas=[(topo, base) for _, topo, base in paginas],
            png_largura=w,
            png_altura_pagina=A4_PAGE_H,
        )
        print(f"     links clicáveis: {n_links}")

    kb = os.path.getsize(pdf_path) // 1024
    print(f"\nPDF gerado: {pdf_path}  ({kb} KB)")


if __name__ == "__main__":
    main()
