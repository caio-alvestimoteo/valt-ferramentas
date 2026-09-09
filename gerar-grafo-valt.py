#!/usr/bin/env python3
"""Gera um grafo navegável dos links Markdown do Valt.

O artefato final embute dados, CSS e D3 em um único HTML, em tela cheia no
estilo do graph view do Obsidian. Por padrão, usa o diretório Planejamentos
do OneDrive quando ele está montado e recorre a ~/Planejamentos nas demais
máquinas.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit


VAULT = Path(os.environ.get("VALT") or Path.home() / "Valt").expanduser()
VENDOR_D3 = Path(__file__).resolve().parent / "vendor" / "d3.v7.min.js"
PLANEJAMENTOS = Path.home() / "Planejamentos"
IGNORADOS = {".git", ".claude", ".agents", ".codex", "node_modules", "__pycache__"}
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
INFRA = {
    "indices",
    "manualDeMarca",
    "temas",
    "templates",
    "scripts",
    "WindowsSetup",
    "Trabalho",
}


def destino_padrao() -> Path:
    if PLANEJAMENTOS.parent.exists():
        return PLANEJAMENTOS / "grafo-valt.html"
    return Path.home() / "Planejamentos" / "grafo-valt.html"


def arquivos_markdown() -> list[Path]:
    return sorted(
        arquivo
        for arquivo in VAULT.rglob("*.md")
        if not any(parte in IGNORADOS for parte in arquivo.relative_to(VAULT).parts)
    )


def titulo_da_nota(arquivo: Path) -> str:
    try:
        with arquivo.open(encoding="utf-8", errors="replace") as stream:
            for linha in stream:
                if linha.startswith("# "):
                    return linha[2:].strip()
    except OSError:
        pass
    return arquivo.stem.replace("-", " ").replace("_", " ").strip().title()


def limpar_alvo(bruto: str) -> str | None:
    alvo = bruto.strip()
    if alvo.startswith("<") and ">" in alvo:
        alvo = alvo[1 : alvo.index(">")]
    else:
        # Títulos opcionais vêm após o endereço: (nota.md "título").
        alvo = re.split(r"\s+[\"']", alvo, maxsplit=1)[0]
    partes = urlsplit(alvo)
    if partes.scheme or partes.netloc or not partes.path:
        return None
    caminho = unquote(partes.path)
    if not caminho.lower().endswith(".md"):
        return None
    return caminho


def resolver_link(origem: Path, alvo: str) -> Path:
    if alvo.startswith("/"):
        candidato = Path(alvo)
    else:
        candidato = origem.parent / alvo
    return candidato.resolve()


def grupo_de(caminho_relativo: Path) -> str:
    if len(caminho_relativo.parts) == 1:
        return "Raiz"
    projeto = caminho_relativo.parts[0]
    return "Sistema" if projeto in INFRA else projeto


def montar_grafo(arquivos: list[Path]) -> tuple[list[dict], list[dict], dict]:
    existentes = {arquivo.resolve(): arquivo for arquivo in arquivos}
    arestas: set[tuple[str, str]] = set()
    quebrados: Counter[str] = Counter()
    saidas: Counter[str] = Counter()
    entradas: Counter[str] = Counter()

    for origem in arquivos:
        origem_id = origem.relative_to(VAULT).as_posix()
        texto = origem.read_text(encoding="utf-8", errors="replace")
        for bruto in LINK_RE.findall(texto):
            alvo_limpo = limpar_alvo(bruto)
            if not alvo_limpo:
                continue
            resolvido = resolver_link(origem, alvo_limpo)
            if resolvido not in existentes:
                quebrados[origem_id] += 1
                continue
            alvo_id = existentes[resolvido].relative_to(VAULT).as_posix()
            if alvo_id == origem_id:
                continue
            arestas.add((origem_id, alvo_id))

    for origem_id, alvo_id in arestas:
        saidas[origem_id] += 1
        entradas[alvo_id] += 1

    nos = []
    for arquivo in arquivos:
        relativo = arquivo.relative_to(VAULT)
        identificador = relativo.as_posix()
        projeto = relativo.parts[0] if len(relativo.parts) > 1 else "raiz"
        nos.append(
            {
                "id": identificador,
                "nome": titulo_da_nota(arquivo),
                "arquivo": identificador,
                "projeto": projeto,
                "grupo": grupo_de(relativo),
                "entrada": entradas[identificador],
                "saida": saidas[identificador],
                "grau": entradas[identificador] + saidas[identificador],
                "url": arquivo.resolve().as_uri(),
            }
        )

    links = [{"source": origem, "target": alvo} for origem, alvo in sorted(arestas)]
    grupos = Counter(no["grupo"] for no in nos)
    stats = {
        "notas": len(nos),
        "links": len(links),
        "orfaos": sum(no["grau"] == 0 for no in nos),
        "quebrados": sum(quebrados.values()),
        "grupos": dict(sorted(grupos.items())),
    }
    return nos, links, stats


def gerar_html(nos: list[dict], links: list[dict], stats: dict, d3: str) -> str:
    dados_json = json.dumps({"nodes": nos, "links": links}, ensure_ascii=False).replace(
        "</", "<\\/"
    )
    grupos_json = json.dumps(stats["grupos"], ensure_ascii=False).replace("</", "<\\/")
    gerado = datetime.now().astimezone().strftime("%d/%m/%Y %H:%M")
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="theme-color" content="#1e1e2e">
  <title>Grafo do Valt</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg:#1e1e2e;--bg2:#181825;--halo:#24243a;--line:#313244;
      --text:#cdd6f4;--sub:#a6adc8;--muted:#6c7086;--mauve:#cba6f7;
      --panel:rgba(24,24,37,.86);
      --display:"Archivo Black","Arial Black",Impact,sans-serif;
      --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    }}
    *{{box-sizing:border-box}}
    html,body{{margin:0;height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:var(--mono)}}
    #graph{{position:fixed;inset:0;width:100vw;height:100vh;display:block;
      background:radial-gradient(1100px 780px at 50% 44%,var(--halo),var(--bg) 72%)}}
    .overlay{{position:fixed;z-index:5}}
    .brand{{left:22px;top:20px;pointer-events:none;user-select:none}}
    .brand h1{{font:16px/1 var(--display);letter-spacing:.04em;text-transform:uppercase;margin:0;color:var(--text)}}
    .brand .stats{{margin-top:7px;font-size:10px;color:var(--muted);letter-spacing:.04em}}
    .brand .stats b{{color:var(--mauve);font-weight:700}}
    .controls{{right:22px;top:20px;display:flex;gap:6px}}
    .search{{width:min(300px,60vw);background:var(--panel);border:1px solid var(--line);color:var(--text);
      padding:8px 11px;font:11px var(--mono);outline:none;border-radius:6px;backdrop-filter:blur(8px)}}
    .search::placeholder{{color:var(--muted)}}
    .search:focus{{border-color:var(--mauve)}}
    .tool{{background:var(--panel);border:1px solid var(--line);color:var(--sub);min-width:32px;
      padding:6px;cursor:pointer;font:12px var(--mono);border-radius:6px;backdrop-filter:blur(8px)}}
    .tool:hover,.tool:focus-visible{{border-color:var(--mauve);color:var(--text)}}
    .legend{{left:22px;bottom:20px;background:var(--panel);border:1px solid var(--line);border-radius:8px;
      padding:10px 12px;backdrop-filter:blur(8px);max-height:46vh;overflow:auto}}
    .legend .cap{{font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}}
    .legend button{{display:flex;align-items:center;gap:8px;width:100%;background:none;border:0;color:var(--sub);
      font:10px var(--mono);padding:3px 2px;cursor:pointer;text-align:left}}
    .legend button:hover{{color:var(--text)}}
    .legend button.off{{opacity:.32;text-decoration:line-through}}
    .dot{{width:8px;height:8px;border-radius:50%;flex:none}}
    .count{{margin-left:auto;color:var(--muted);font-size:9px}}
    .hint{{right:22px;bottom:18px;color:var(--muted);font-size:9px;text-align:right;line-height:1.7;pointer-events:none}}
    .tooltip{{position:fixed;z-index:10;max-width:340px;pointer-events:none;background:var(--panel);
      border:1px solid var(--line);border-radius:8px;padding:9px 11px;font-size:10px;line-height:1.55;
      color:var(--sub);opacity:0;transform:translate(14px,14px);backdrop-filter:blur(8px)}}
    .tooltip strong{{display:block;color:var(--text);font-size:11px;margin-bottom:3px}}
    .tooltip .grp{{color:var(--mauve)}}
    .link{{stroke:#45475a;stroke-opacity:.30;stroke-width:.6}}
    .node{{cursor:pointer;stroke:none}}
    .node.orphan{{fill-opacity:.72}}
    .node.search-hit{{stroke:var(--text);stroke-width:1.6}}
    .label{{fill:var(--sub);font:500 8px var(--mono);text-anchor:middle;pointer-events:none;
      paint-order:stroke;stroke:rgba(24,24,37,.9);stroke-width:2.6px;stroke-linejoin:round}}
    @media(max-width:720px){{.legend{{max-height:30vh}}.search{{width:44vw}}}}
    @media(prefers-reduced-motion:reduce){{*{{transition:none!important}}}}
  </style>
</head>
<body>
  <svg id="graph" role="img" aria-label="Grafo das notas do Valt"></svg>
  <div class="overlay brand">
    <h1>Grafo do Valt</h1>
    <div class="stats"><b>{stats['notas']}</b> notas · <b>{stats['links']}</b> conexões · <b>{stats['orfaos']}</b> órfãs · {stats['quebrados']} links quebrados · {html.escape(gerado)}</div>
  </div>
  <div class="overlay controls">
    <input id="search" class="search" type="search" placeholder="buscar nota ou caminho…" aria-label="Buscar nota">
    <button id="zoom-in" class="tool" title="Aproximar">+</button>
    <button id="zoom-out" class="tool" title="Afastar">−</button>
    <button id="reset" class="tool" title="Centralizar">↺</button>
  </div>
  <div class="overlay legend" aria-label="Legenda de acervos">
    <div class="cap">Acervos</div>
    <div id="legend-items"></div>
  </div>
  <div class="overlay hint">arraste · role para zoom · aproxime para ver os nomes<br>clique abre a nota · duplo clique solta um nó fixado</div>
  <div id="tooltip" class="tooltip"></div>
  <script>{d3}</script>
  <script>
  (() => {{
    const raw = {dados_json};
    const groupCounts = {grupos_json};
    const colors = {{
      Seara:'#f38ba8',Newcesar:'#89b4fa',Sistema:'#6c7086',JaiminhoSetup:'#f9e2af',
      Pessoais:'#a6e3a1',Jaime:'#94e2d5',Raiz:'#d72638',Jaiminho:'#cba6f7',
      Pessoal:'#fab387',Hermes:'#74c7ec'
    }};
    const fallbackColors = ['#cba6f7','#94e2d5','#a6e3a1','#f38ba8','#fab387','#f9e2af'];
    Object.keys(groupCounts).sort().forEach((g,i) => {{ if (!colors[g]) colors[g] = fallbackColors[i % fallbackColors.length]; }});

    const svg = d3.select('#graph');
    let width = innerWidth, height = innerHeight;
    svg.attr('viewBox', [0,0,width,height]);
    const root = svg.append('g');
    const linkLayer = root.append('g');
    const nodeLayer = root.append('g');
    const labelLayer = root.append('g');
    const tooltip = document.querySelector('#tooltip');
    const hidden = new Set();
    let k = 1, hoverSet = null, query = '';

    const radius = d3.scaleSqrt().domain([0,d3.max(raw.nodes,d=>d.entrada)||1]).range([2.5,11]);
    const hubs = new Set([...raw.nodes].sort((a,b)=>b.entrada-a.entrada).slice(0,9).map(d=>d.id));
    const neighbors = new Map(raw.nodes.map(d=>[d.id,new Set([d.id])]));
    raw.links.forEach(l => {{ neighbors.get(l.source).add(l.target); neighbors.get(l.target).add(l.source); }});

    const simulation = d3.forceSimulation(raw.nodes)
      .force('link',d3.forceLink(raw.links).id(d=>d.id).distance(46).strength(.32))
      .force('charge',d3.forceManyBody().strength(d=>-52-Math.min(170,d.grau*6)))
      .force('collide',d3.forceCollide().radius(d=>radius(d.entrada)+3))
      .force('center',d3.forceCenter(width/2,height/2))
      .force('x',d3.forceX(width/2).strength(.03))
      .force('y',d3.forceY(height/2).strength(.036))
      .velocityDecay(.28);

    const links = linkLayer.selectAll('line').data(raw.links).join('line').attr('class','link');
    const nodes = nodeLayer.selectAll('circle').data(raw.nodes).join('circle')
      .attr('class',d=>'node'+(d.grau===0?' orphan':''))
      .attr('r',d=>radius(d.entrada)).attr('fill',d=>colors[d.grupo])
      .call(d3.drag().on('start',dragStarted).on('drag',dragged).on('end',dragEnded))
      .on('mouseenter',hovered).on('mousemove',moved).on('mouseleave',unhovered)
      .on('click',(event,d)=>{{ event.stopPropagation(); window.location.href=d.url; }})
      .on('dblclick',(event,d)=>{{ event.preventDefault(); event.stopPropagation(); d.fx=null; d.fy=null; simulation.alpha(.3).restart(); }});
    const labels = labelLayer.selectAll('text').data(raw.nodes).join('text')
      .attr('class','label').attr('dy',d=>radius(d.entrada)+10).text(d=>d.nome);

    const zoom = d3.zoom().scaleExtent([.1,9]).on('zoom',e=>{{
      k = e.transform.k;
      root.attr('transform',e.transform);
      paintLabels();
    }});
    svg.call(zoom).on('dblclick.zoom',null);
    simulation.on('tick',()=>{{
      links.attr('x1',d=>d.source.x).attr('y1',d=>d.source.y).attr('x2',d=>d.target.x).attr('y2',d=>d.target.y);
      nodes.attr('cx',d=>d.x).attr('cy',d=>d.y);
      labels.attr('x',d=>d.x).attr('y',d=>d.y);
    }});

    function active(d) {{ return !hidden.has(d.grupo); }}
    function labelBase(d) {{
      const start = hubs.has(d.id) ? .8 : 1.15;
      return Math.max(0, Math.min(.95, (k - start) / .7));
    }}
    function matches(d) {{
      return query.length>1 && (d.nome.toLocaleLowerCase('pt-BR').includes(query)||d.id.toLocaleLowerCase('pt-BR').includes(query));
    }}
    function paintLabels() {{
      labels.style('opacity',d=>{{
        if (!active(d)) return 0;
        if (hoverSet) return hoverSet.has(d.id)?1:0;
        if (query.length>1) return matches(d)?1:0;
        return labelBase(d);
      }});
    }}
    function paintNodes() {{
      nodes
        .classed('search-hit',d=>matches(d))
        .style('opacity',d=>{{
          if (hoverSet) return hoverSet.has(d.id)?1:.08;
          if (query.length>1) return matches(d)?1:.1;
          return 1;
        }});
      links.style('stroke-opacity',l=>{{
        if (hoverSet) return (hoverSet.has(l.source.id)&&hoverSet.has(l.target.id))?.85:.04;
        if (query.length>1) return .07;
        return null;
      }}).style('stroke-width',l=>hoverSet&&hoverSet.has(l.source.id)&&hoverSet.has(l.target.id)?1.4:null)
        .style('stroke',l=>hoverSet&&hoverSet.has(l.source.id)&&hoverSet.has(l.target.id)?'#585b70':null);
      paintLabels();
    }}
    function refreshVisibility() {{
      nodes.style('display',d=>active(d)?null:'none');
      labels.style('display',d=>active(d)?null:'none');
      links.style('display',d=>active(d.source)&&active(d.target)?null:'none');
      simulation.alpha(.2).restart();
      paintLabels();
    }}
    function dragStarted(event,d) {{ if(!event.active) simulation.alphaTarget(.16).restart(); d.fx=d.x; d.fy=d.y; }}
    function dragged(event,d) {{ d.fx=event.x; d.fy=event.y; }}
    function dragEnded(event) {{ if(!event.active) simulation.alphaTarget(0); }}
    function hovered(event,d) {{
      hoverSet = neighbors.get(d.id);
      tooltip.innerHTML = `<strong>${{escapeHtml(d.nome)}}</strong><span class="grp">${{escapeHtml(d.grupo)}}</span> · ${{escapeHtml(d.arquivo)}}<br>recebe ${{d.entrada}} · envia ${{d.saida}}`;
      tooltip.style.opacity = 1;
      paintNodes();
    }}
    function moved(event) {{ tooltip.style.left=event.clientX+'px'; tooltip.style.top=event.clientY+'px'; }}
    function unhovered() {{ hoverSet = null; tooltip.style.opacity = 0; paintNodes(); }}
    function escapeHtml(value) {{ const e=document.createElement('span'); e.textContent=value; return e.innerHTML; }}

    const legend = d3.select('#legend-items');
    Object.entries(groupCounts).sort((a,b)=>b[1]-a[1]).forEach(([group,count])=>{{
      const button = legend.append('button').attr('type','button').on('click',function(){{
        hidden.has(group)?hidden.delete(group):hidden.add(group); this.classList.toggle('off'); refreshVisibility();
      }});
      button.append('span').attr('class','dot').style('background',colors[group]);
      button.append('span').text(group); button.append('span').attr('class','count').text(count);
    }});

    const search = document.querySelector('#search');
    search.addEventListener('input',()=>{{ query = search.value.trim().toLocaleLowerCase('pt-BR'); paintNodes(); }});
    document.querySelector('#zoom-in').onclick=()=>svg.transition().duration(220).call(zoom.scaleBy,1.35);
    document.querySelector('#zoom-out').onclick=()=>svg.transition().duration(220).call(zoom.scaleBy,.74);
    document.querySelector('#reset').onclick=()=>svg.transition().duration(400).call(zoom.transform,d3.zoomIdentity);
    addEventListener('resize',()=>{{
      width=innerWidth; height=innerHeight;
      svg.attr('viewBox',[0,0,width,height]);
      simulation.force('center',d3.forceCenter(width/2,height/2)).alpha(.15).restart();
    }});
    paintLabels();
  }})();
  </script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saida", type=Path, default=destino_padrao())
    parser.add_argument("--dados", type=Path, help="salva também o JSON para diagnóstico")
    args = parser.parse_args()

    if not VENDOR_D3.exists():
        raise SystemExit(
            f"D3 não encontrado em {VENDOR_D3}. Consulte grafo-valt.md para vendorizá-lo."
        )

    arquivos = arquivos_markdown()
    nos, links, stats = montar_grafo(arquivos)
    d3 = VENDOR_D3.read_text(encoding="utf-8")
    saida = args.saida.expanduser().resolve()
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(gerar_html(nos, links, stats, d3), encoding="utf-8")

    if args.dados:
        dados = args.dados.expanduser().resolve()
        dados.parent.mkdir(parents=True, exist_ok=True)
        dados.write_text(
            json.dumps({"nodes": nos, "links": links, "stats": stats}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(f"Grafo gerado: {saida}")
    print(
        f"{stats['notas']} notas · {stats['links']} conexões · "
        f"{stats['orfaos']} órfãs · {stats['quebrados']} links quebrados ignorados"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
