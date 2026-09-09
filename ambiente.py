#!/usr/bin/env python3
"""Ciclo de vida dos ambientes de ``~/Sites`` — sobe, usa, derruba e apaga o refazível.

O disco desta máquina é o gargalo: cada projeto clonado traz `node_modules`, `vendor`,
imagem Docker, cache de build e volume de banco. Somados, enchem o SSD em semanas. A regra
do vault (``indices/higiene-ambientes.md``) é simples: **o que se refaz por comando, some
ao derrubar; o que não se refaz, sai daqui com dump antes**.

Este script executa essa regra. Nada aqui apaga volume nomeado de banco por conta própria:
o clone `~/Sites/Seara/food` já foi perdido uma vez e só sobreviveu porque o volume
`food_db_data` ficou de pé.

Uso::

    python3 scripts/ambiente.py status                 # o que ocupa o disco hoje
    python3 scripts/ambiente.py status Seara/food      # só um projeto
    python3 scripts/ambiente.py derrubar Seara/food    # para os containers, preserva volumes
    python3 scripts/ambiente.py limpar Seara/food      # simulação: diz o que apagaria
    python3 scripts/ambiente.py limpar Seara/food --aplicar
    python3 scripts/ambiente.py dump Seara/food        # dump do banco antes de mexer em dados
    python3 scripts/ambiente.py limpar --tudo --aplicar # poda global do Docker (sem volumes)

Sem Docker instalado o script continua funcionando: cuida só dos artefatos em disco e
avisa que a parte de containers ficou de fora.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from valt_paths import SITES  # noqa: E402

# Pastas que qualquer projeto refaz com um comando (`npm ci`, `composer install`, `build`).
# Só entram na conta quando **não** estão versionadas — ver `descartaveis()`.
DESCARTAVEIS = {
    "node_modules", "vendor", ".venv", "venv", "bower_components",
    ".next", ".nuxt", ".svelte-kit", ".angular", ".turbo", ".parcel-cache",
    "dist", "build", "out", "target", "coverage",
    ".cache", ".pytest_cache", ".mypy_cache", ".gradle", "__pycache__",
}

# Nunca entram em varredura: ou são o próprio histórico, ou são dados de verdade.
INTOCAVEIS = {".git", "uploads", "files", ".dumps"}

IMAGENS_DE_BANCO = ("mysql", "mariadb", "postgres", "percona")

# Serviços que, instalados no sistema, sobem no boot e comem RAM para sempre. Banco é container.
SERVICOS_SQL = ("mysql", "mariadb", "postgresql", "mongod", "redis-server")

# Daemons do Docker: só devem existir enquanto há container de pé.
DAEMONS_DOCKER = ("dockerd", "containerd")

# Onde fica o registro do que cada terminal subiu. tmpfs: some no reboot e não ocupa disco.
SESSOES = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "valt-ambientes"

SUDOERS = Path("/etc/sudoers.d/valt-ambiente")


# ---------------------------------------------------------------- utilidades


def _rodar(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def _rodar_sudo(cmd: list[str], senha: str | None = None) -> subprocess.CompletedProcess:
    """Comando com sudo apto a autenticar.

    Num terminal de verdade, o sudo pergunta a senha ali mesmo. Sem terminal (o prompt `!` do
    Claude Code, por exemplo), não há onde perguntar — aí a senha vem por pipe, lida uma vez
    no início do `acoplar` e repassada a cada sudo via ``-S``. O `_rodar` não serve para isso:
    ele captura stdin/stdout e o sudo falha em silêncio.
    """
    if senha is not None:
        return subprocess.run([cmd[0], "-S", *cmd[1:]], check=False, text=True,
                              input=senha + "\n", stderr=subprocess.DEVNULL)
    return subprocess.run(cmd, check=False)


def humano(bytes_: int) -> str:
    for unidade in ("B", "KB", "MB", "GB", "TB"):
        if bytes_ < 1024 or unidade == "TB":
            return f"{bytes_:.0f} {unidade}" if unidade in ("B", "KB") else f"{bytes_:.1f} {unidade}"
        bytes_ /= 1024
    return f"{bytes_:.1f} TB"


def tamanho(caminho: Path) -> int:
    saida = _rodar(["du", "-sb", str(caminho)])
    if saida.returncode != 0:
        return 0
    try:
        return int(saida.stdout.split()[0])
    except (IndexError, ValueError):
        return 0


def docker_ok() -> bool:
    """Docker existe e responde? O script degrada sem ele em vez de quebrar."""
    if not shutil.which("docker"):
        return False
    return _rodar(["docker", "info"]).returncode == 0


def versionado(repo: Path, alvo: Path) -> bool:
    """A pasta tem arquivo rastreado pelo git? Se tem, ela é código — não se apaga."""
    if not (repo / ".git").exists():
        return False
    saida = _rodar(["git", "-C", str(repo), "ls-files", "--error-unmatch", str(alvo)])
    return saida.returncode == 0


# ------------------------------------------------------------------ projetos


@dataclass
class Projeto:
    raiz: Path

    @property
    def nome(self) -> str:
        return str(self.raiz.relative_to(SITES))

    @property
    def compose(self) -> Path | None:
        for nome in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
            if (self.raiz / nome).exists():
                return self.raiz / nome
        return None

    @property
    def projeto_compose(self) -> str:
        """Nome do projeto compose: o do diretório, como o Docker faz por padrão.

        Sobrescreva com ``--compose`` quando o projeto subiu com ``-p`` diferente — o Food
        sobe com ``-p food`` justamente para reaproveitar o volume `food_db_data`.
        """
        return "".join(c for c in self.raiz.name.lower() if c.isalnum() or c in "-_")

    def descartaveis(self) -> list[tuple[Path, int]]:
        """Pastas refazíveis, não versionadas, com o tamanho de cada uma."""
        achados: list[tuple[Path, int]] = []
        for atual, dirs, _ in os.walk(self.raiz):
            dirs[:] = [d for d in dirs if d not in INTOCAVEIS]
            for d in list(dirs):
                if d in DESCARTAVEIS:
                    caminho = Path(atual) / d
                    dirs.remove(d)  # não descer: a pasta inteira já é candidata
                    if not versionado(self.raiz, caminho):
                        achados.append((caminho, tamanho(caminho)))
        return sorted(achados, key=lambda par: -par[1])

    def containers(self, todos: bool = True) -> list[dict[str, str]]:
        if not docker_ok():
            return []
        cmd = ["docker", "ps", "--filter", f"label=com.docker.compose.project={self.projeto_compose}",
               "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.State}}"]
        if todos:
            cmd.insert(2, "-a")
        saida = _rodar(cmd)
        linhas = [ln for ln in saida.stdout.splitlines() if ln.strip()]
        return [dict(zip(("id", "nome", "imagem", "estado"), ln.split("\t"))) for ln in linhas]

    def imagens(self) -> list[tuple[str, str]]:
        if not docker_ok():
            return []
        saida = _rodar(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}"])
        prefixo = self.projeto_compose
        return [tuple(ln.split("\t")) for ln in saida.stdout.splitlines()
                if ln.startswith(f"{prefixo}-") or ln.startswith(f"{prefixo}_")]

    def volumes(self) -> list[str]:
        if not docker_ok():
            return []
        saida = _rodar(["docker", "volume", "ls", "-q", "--filter",
                        f"label=com.docker.compose.project={self.projeto_compose}"])
        return [ln for ln in saida.stdout.splitlines() if ln.strip()]


def achar_projetos(alvo: str | None) -> list[Projeto]:
    if not SITES.exists():
        return []
    if alvo:
        raiz = (SITES / alvo).expanduser()
        if not raiz.is_dir():
            raise SystemExit(f"projeto não encontrado: ~/Sites/{alvo}")
        return [Projeto(raiz.resolve())]
    projetos: list[Projeto] = []
    for guarda_chuva in sorted(p for p in SITES.iterdir() if p.is_dir() and not p.name.startswith(".")):
        filhos = [p for p in sorted(guarda_chuva.iterdir()) if p.is_dir() and not p.name.startswith(".")]
        if (guarda_chuva / ".git").exists() or not filhos:
            projetos.append(Projeto(guarda_chuva))
            continue
        projetos.extend(Projeto(f) for f in filhos)
    return projetos


# ------------------------------------------------------------------ comandos


def cmd_status(args) -> int:
    livre = shutil.disk_usage(Path.home())
    print(f"\nDisco: {humano(livre.used)} usados · {humano(livre.free)} livres "
          f"({livre.free / livre.total:.0%} do total)\n")

    projetos = achar_projetos(args.projeto)
    if not projetos:
        print(f"~/Sites vazio — nada clonado ainda ({SITES}).\n")
    total_recuperavel = 0
    for proj in projetos:
        lixo = proj.descartaveis()
        containers = proj.containers()
        de_pe = [c for c in containers if c["estado"] == "running"]
        recuperavel = sum(t for _, t in lixo)
        total_recuperavel += recuperavel
        if not lixo and not containers:
            continue
        print(f"■ {proj.nome} — {humano(tamanho(proj.raiz))} em disco")
        for caminho, tam in lixo[:6]:
            print(f"    {humano(tam):>9}  {caminho.relative_to(proj.raiz)}")
        if len(lixo) > 6:
            print(f"    {'':>9}  (+{len(lixo) - 6} pastas menores)")
        if recuperavel:
            print(f"    → {humano(recuperavel)} refazíveis por comando")
        if containers:
            print(f"    → {len(containers)} containers ({len(de_pe)} de pé), "
                  f"{len(proj.imagens())} imagens, {len(proj.volumes())} volumes")
        print()

    if total_recuperavel:
        print(f"Recuperável agora, sem perder nada: {humano(total_recuperavel)}\n")

    if docker_ok():
        print("Docker:")
        print(_rodar(["docker", "system", "df"]).stdout.rstrip() + "\n")
    else:
        print("Docker ausente ou parado — nenhum daemon consumindo RAM.\n")

    print("Memória agora:")
    for linha in resumo_memoria():
        print(linha)
    abertas = sorted(SESSOES.glob("*.lista")) if SESSOES.exists() else []
    if abertas:
        print(f"  terminais com ambiente de pé: {len(abertas)} "
              f"(`ambiente.py sessao listar` mostra quais)")
    print()
    return 0


def cmd_derrubar(args) -> int:
    """Derruba o que está de pé: containers, dev server node e o próprio daemon do Docker.

    Nunca passa `-v`: volume de banco fica onde está.
    """
    for proj in achar_projetos(args.projeto):
        de_pe = proj.containers(todos=False)
        node = processos_node(dentro=proj.raiz)
        if not de_pe and not node:
            continue
        print(f"■ {proj.nome}")
        if de_pe:
            nome = args.compose or proj.projeto_compose
            print(f"    {len(de_pe)} container(s) — derrubando")
            if proj.compose:
                _rodar(["docker", "compose", "-p", nome, "down", "--remove-orphans"], cwd=proj.raiz)
            else:
                _rodar(["docker", "stop", *[c["id"] for c in de_pe]])
            print(f"    volumes preservados: {', '.join(proj.volumes()) or 'nenhum'}")
        if node:
            liberado = encerrar_processos(node, aplicar=True)
            print(f"    {len(node)} processo(s) node encerrado(s) — {humano(liberado)} de RAM")
    parar_docker_ocioso()
    return 0


def cmd_dump(args) -> int:
    """Dump do banco antes de qualquer mexida em dados. Sai em `<projeto>/.dumps/`."""
    if not docker_ok():
        print("Docker ausente ou parado — sem banco de pé para dumpar.")
        return 1
    for proj in achar_projetos(args.projeto):
        bancos = [c for c in proj.containers(todos=False)
                  if any(m in c["imagem"].lower() for m in IMAGENS_DE_BANCO)]
        if not bancos:
            print(f"■ {proj.nome}: nenhum container de banco de pé — suba antes de dumpar.")
            continue
        destino_dir = proj.raiz / ".dumps"
        destino_dir.mkdir(exist_ok=True)
        for c in bancos:
            env = dict(ln.split("=", 1) for ln in
                       _rodar(["docker", "exec", c["id"], "env"]).stdout.splitlines() if "=" in ln)
            destino = destino_dir / f"{c['nome']}-{date.today().isoformat()}.sql.gz"
            if "postgres" in c["imagem"].lower():
                usuario = env.get("POSTGRES_USER", "postgres")
                interno = ["pg_dumpall", "-U", usuario]
            else:
                senha = env.get("MYSQL_ROOT_PASSWORD") or env.get("MARIADB_ROOT_PASSWORD", "")
                interno = ["mysqldump", "-uroot", f"-p{senha}", "--all-databases",
                           "--single-transaction", "--routines", "--events"]
            print(f"■ {proj.nome}: dump de {c['nome']} → {destino.name}")
            with destino.open("wb") as saida:
                dump = subprocess.Popen(["docker", "exec", c["id"], *interno],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                gzip_proc = subprocess.Popen(["gzip", "-9"], stdin=dump.stdout, stdout=saida)
                dump.stdout.close()
                gzip_proc.wait()
                erro = dump.stderr.read().decode(errors="replace")
                dump.wait()
            if dump.returncode != 0 or destino.stat().st_size < 1024:
                print(f"    ✗ falhou: {erro.strip()[:200] or 'dump vazio'}")
                destino.unlink(missing_ok=True)
                return 1
            print(f"    ✓ {humano(destino.stat().st_size)} — guarde fora da máquina se for único")
    return 0


def _apagar(caminho: Path, aplicar: bool) -> int:
    tam = tamanho(caminho)
    if aplicar:
        shutil.rmtree(caminho, ignore_errors=True)
    return tam


def cmd_limpar(args) -> int:
    aplicar = args.aplicar
    prefixo = "" if aplicar else "[simulação] "
    liberado = 0

    if args.tudo and not args.projeto:
        if docker_ok():
            print(f"{prefixo}poda global do Docker: containers parados, imagens sem uso, "
                  f"redes órfãs e cache de build")
            if aplicar:
                print(_rodar(["docker", "system", "prune", "-af"]).stdout.rstrip())
                print(_rodar(["docker", "builder", "prune", "-af"]).stdout.rstrip())
            print("    volumes nomeados NÃO entram na poda — dados ficam.")
        else:
            print("Docker ausente ou parado — poda global pulada.")

        orfaos = [n for n in processos_node(dentro=SITES) if n["ppid"] == 1]
        if orfaos:
            print(f"{prefixo}encerrando {len(orfaos)} processo(s) node órfão(s) sob ~/Sites "
                  f"({humano(sum(n['rss'] for n in orfaos))} de RAM)")
            encerrar_processos(orfaos, aplicar)

    for proj in achar_projetos(args.projeto):
        lixo = proj.descartaveis()
        containers = proj.containers()
        imagens = proj.imagens()
        if not lixo and not containers and not imagens:
            continue
        print(f"\n■ {proj.nome}")

        de_pe = [c for c in containers if c["estado"] == "running"]
        if de_pe and docker_ok():
            print(f"  {prefixo}derrubando {len(de_pe)} container(s) de pé")
            if aplicar:
                nome = args.compose or proj.projeto_compose
                if proj.compose:
                    _rodar(["docker", "compose", "-p", nome, "down", "--remove-orphans"], cwd=proj.raiz)
                else:
                    _rodar(["docker", "stop", *[c["id"] for c in de_pe]])

        node = processos_node(dentro=proj.raiz)
        if node:
            print(f"  {prefixo}encerrando {len(node)} processo(s) node "
                  f"({humano(sum(n['rss'] for n in node))} de RAM)")
            encerrar_processos(node, aplicar)

        for caminho, tam in lixo:
            print(f"  {prefixo}apagando {caminho.relative_to(proj.raiz)} ({humano(tam)})")
            liberado += _apagar(caminho, aplicar) if aplicar else tam

        if imagens and docker_ok():
            print(f"  {prefixo}removendo {len(imagens)} imagem(ns) do projeto")
            if aplicar:
                _rodar(["docker", "rmi", "-f", *[img[0] for img in imagens]])

        volumes = proj.volumes()
        if volumes:
            if args.dados:
                print(f"  ⚠ volumes com dados: {', '.join(volumes)}")
                print(f"  {prefixo}rode `ambiente.py dump {proj.nome}` com o banco de pé ANTES")
                if aplicar:
                    if not list((proj.raiz / ".dumps").glob("*.sql.gz")):
                        print("  ✗ nenhum dump em .dumps/ — recuso apagar volume sem dump.")
                        return 1
                    _rodar(["docker", "volume", "rm", "-f", *volumes])
                    print(f"  volumes removidos ({len(volumes)}); dump preservado em .dumps/")
            else:
                print(f"  volumes preservados: {', '.join(volumes)} (use --dados para removê-los)")

    if aplicar:
        parar_docker_ocioso()

    print()
    if liberado:
        print(f"{'Liberado' if aplicar else 'Liberaria'}: {humano(liberado)} em disco")
    if not aplicar:
        print("Nada foi apagado — repita com --aplicar.")
    return 0


# ------------------------------------------------------- memória viva (RAM)
#
# A queixa não é só disco: daemon parado é RAM parada. Nada aqui roda em segundo plano —
# tudo é leitura de /proc e de arquivo, sob demanda.


def rss(pid: int) -> int:
    """RSS do processo em bytes, ou 0 se ele já morreu."""
    try:
        for linha in Path(f"/proc/{pid}/status").read_text().splitlines():
            if linha.startswith("VmRSS:"):
                return int(linha.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _campo_proc(pid: int, arquivo: str) -> str:
    try:
        return Path(f"/proc/{pid}/{arquivo}").read_text()
    except OSError:
        return ""


def processos(comandos: tuple[str, ...], dentro: Path | None = None) -> list[dict]:
    """Processos vivos cujo executável casa com `comandos`, opcionalmente sob um diretório.

    O diretório sai do `cwd` real do processo (``/proc/<pid>/cwd``) — é assim que se sabe que
    aquele `node` é o dev server de um projeto de ~/Sites, e não o Jaime.
    """
    achados: list[dict] = []
    for entrada in Path("/proc").iterdir():
        if not entrada.name.isdigit():
            continue
        pid = int(entrada.name)
        comm = _campo_proc(pid, "comm").strip()
        if not comm or not any(comm.startswith(c) for c in comandos):
            continue
        try:
            cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            cwd = Path("/")
        if dentro and not (cwd == dentro or dentro in cwd.parents):
            continue
        stat = _campo_proc(pid, "stat").rsplit(") ", 1)[-1].split()
        ppid = int(stat[1]) if len(stat) > 1 else 0
        achados.append({"pid": pid, "comm": comm, "cwd": cwd, "ppid": ppid, "rss": rss(pid)})
    return sorted(achados, key=lambda proc: -proc["rss"])


def servicos_sql_ativos() -> list[tuple[str, str]]:
    """Bancos instalados no sistema (não em container) que sobem sozinhos no boot."""
    if not shutil.which("systemctl"):
        return []
    achados = []
    for servico in SERVICOS_SQL:
        estado = _rodar(["systemctl", "is-active", f"{servico}.service"]).stdout.strip()
        habilitado = _rodar(["systemctl", "is-enabled", f"{servico}.service"]).stdout.strip()
        if estado == "active" or habilitado == "enabled":
            achados.append((servico, f"{estado}/{habilitado}"))
    return achados


def containers_teimosos() -> list[tuple[str, str]]:
    """Containers com política de restart que os traz de volta sozinhos — inclusive no boot."""
    if not docker_ok():
        return []
    ids = _rodar(["docker", "ps", "-aq"]).stdout.split()
    if not ids:
        return []
    saida = _rodar(["docker", "inspect", "-f",
                    "{{.Name}}\t{{.HostConfig.RestartPolicy.Name}}", *ids]).stdout
    return [(ln.split("\t")[0], ln.split("\t")[1]) for ln in saida.splitlines()
            if "\t" in ln and ln.split("\t")[1] not in ("no", "", "<no value>")]


def processos_node(dentro: Path | None = None) -> list[dict]:
    return processos(("node", "bun", "deno", "vite", "next", "esbuild"), dentro=dentro)


def resumo_memoria() -> list[str]:
    """O que está comendo RAM agora por causa de projeto — sem vigia, é só ler /proc."""
    linhas: list[str] = []

    daemons = processos(DAEMONS_DOCKER)
    if daemons:
        de_pe = len(_rodar(["docker", "ps", "-q"]).stdout.split()) if docker_ok() else 0
        alerta = "  ← RAM parada, nenhum container de pé" if de_pe == 0 else ""
        linhas.append(f"  daemons do Docker: {humano(sum(d['rss'] for d in daemons))}{alerta}")
    else:
        linhas.append("  daemons do Docker: parados ✓")

    node = processos_node(dentro=SITES)
    if node:
        orfaos = [n for n in node if n["ppid"] == 1]
        extra = f", {len(orfaos)} órfão(s) sem terminal dono" if orfaos else ""
        linhas.append(f"  node sob ~/Sites: {len(node)} processo(s), "
                      f"{humano(sum(n['rss'] for n in node))}{extra}")
    else:
        linhas.append("  node sob ~/Sites: nenhum ✓")

    sql = servicos_sql_ativos()
    if sql:
        linhas.append("  ⚠ banco no sistema (devia ser container): "
                      + ", ".join(f"{s} [{e}]" for s, e in sql))
    else:
        linhas.append("  banco nativo no sistema: nenhum ✓")

    teimosos = containers_teimosos()
    if teimosos:
        nomes = ", ".join(f"{n.lstrip('/')} [{p}]" for n, p in teimosos)
        linhas.append(f"  ⚠ containers que voltam sozinhos: {nomes}"
                      " — conserte com `ambiente.py acoplar --aplicar`")
    return linhas


def encerrar_processos(procs: list[dict], aplicar: bool) -> int:
    """Pede para sair, espera, e insiste no que ficou pendurado. Devolve a RAM liberada."""
    liberado = sum(p["rss"] for p in procs)
    if not aplicar:
        return liberado
    for proc in procs:
        try:
            os.kill(proc["pid"], signal.SIGTERM)
        except OSError:
            pass
    for _ in range(20):
        if not [p for p in procs if Path(f"/proc/{p['pid']}").exists()]:
            return liberado
        time.sleep(0.25)
    for proc in [p for p in procs if Path(f"/proc/{p['pid']}").exists()]:
        try:
            os.kill(proc["pid"], signal.SIGKILL)
        except OSError:
            pass
    return liberado


def parar_docker_ocioso(verboso: bool = True) -> bool:
    """Sem container de pé, o daemon não tem por que existir — o socket o acorda depois."""
    if not docker_ok() or not shutil.which("systemctl"):
        return False
    if _rodar(["docker", "ps", "-q"]).stdout.split():
        return False
    parou = False
    for servico in ("docker.service", "containerd.service"):
        if _rodar(["systemctl", "is-active", servico]).stdout.strip() != "active":
            continue
        if _rodar(["sudo", "-n", "systemctl", "stop", servico]).returncode == 0:
            parou = True
        else:
            if verboso:
                print(f"    ({servico} continua de pé — falta rodar `ambiente.py acoplar --aplicar`)")
            return parou
    if parou and verboso:
        print("    daemons do Docker parados — voltam sozinhos no próximo comando docker")
    return parou


# ----------------------------------------------------------------- sessão
#
# Cada terminal que sobe alguma coisa deixa um arquivo dizendo o que subiu. Quem derruba é o
# `trap` de saída do próprio terminal (``bootstrap/shell/ambiente.sh``). Não há vigia rodando:
# o "processo" que lembra de limpar é o bash que você já tinha aberto.


def _arquivo_sessao(pid: int) -> Path:
    return SESSOES / f"{pid}.lista"


def sessao_registrar(pid: int, cwd: Path) -> None:
    """Anota que este terminal subiu algo neste projeto. Fora de ~/Sites, ignora."""
    try:
        partes = cwd.relative_to(SITES).parts
    except ValueError:
        return  # fora de ~/Sites não é ambiente de projeto — nada a derrubar depois
    if not partes:
        return
    # Mesma convenção de `achar_projetos`: <guarda-chuva>/<projeto>, ou o repo solto na raiz.
    projeto = SITES.joinpath(*partes[:2])
    SESSOES.mkdir(parents=True, exist_ok=True)
    arquivo = _arquivo_sessao(pid)
    registros = set(arquivo.read_text().splitlines()) if arquivo.exists() else set()
    registros.add(str(projeto))
    arquivo.write_text("\n".join(sorted(r for r in registros if r)) + "\n")


def sessao_encerrar(pid: int, verboso: bool = True) -> None:
    """Derruba o que aquele terminal subiu: containers do projeto e node sob a pasta dele."""
    arquivo = _arquivo_sessao(pid)
    if not arquivo.exists():
        return
    projetos = [Path(ln) for ln in arquivo.read_text().splitlines() if ln.strip()]
    arquivo.unlink(missing_ok=True)
    for raiz in projetos:
        if not raiz.is_dir():
            continue
        proj = Projeto(raiz)
        de_pe = proj.containers(todos=False)
        node = [n for n in processos_node(dentro=raiz) if n["pid"] != os.getpid()]
        if not de_pe and not node:
            continue
        if verboso:
            print(f"[valt] {proj.nome}: derrubando {len(de_pe)} container(s) "
                  f"e {len(node)} processo(s) node")
        if de_pe and docker_ok():
            if proj.compose:
                _rodar(["docker", "compose", "-p", proj.projeto_compose, "down", "--remove-orphans"],
                       cwd=proj.raiz)
            else:
                _rodar(["docker", "stop", *[c["id"] for c in de_pe]])
        encerrar_processos(node, aplicar=True)
    parar_docker_ocioso(verboso=verboso)


def sessao_varrer(verboso: bool = True) -> None:
    """Rede de segurança: sessões cujo terminal morreu sem rodar o trap (kill -9, queda de luz).

    Roda ao abrir um terminal novo — de graça, e sem ninguém vigiando enquanto isso.
    """
    if not SESSOES.exists():
        return
    for arquivo in SESSOES.glob("*.lista"):
        pid = int(arquivo.stem) if arquivo.stem.isdigit() else 0
        if pid and Path(f"/proc/{pid}").exists():
            continue
        sessao_encerrar(pid, verboso=verboso)
        arquivo.unlink(missing_ok=True)


def cmd_sessao(args) -> int:
    if args.acao == "registrar":
        sessao_registrar(args.pid, Path(args.cwd or os.getcwd()).resolve())
    elif args.acao == "encerrar":
        sessao_encerrar(args.pid, verboso=not args.silencioso)
    elif args.acao == "varrer":
        sessao_varrer(verboso=not args.silencioso)
    elif args.acao == "preservar":
        _arquivo_sessao(args.pid).unlink(missing_ok=True)
        print("Este terminal não derruba mais nada ao fechar.")
    elif args.acao == "listar":
        arquivos = sorted(SESSOES.glob("*.lista")) if SESSOES.exists() else []
        if not arquivos:
            print("Nenhum terminal com ambiente de pé.")
        for arquivo in arquivos:
            vivo = "vivo" if Path(f"/proc/{arquivo.stem}").exists() else "morto"
            print(f"terminal {arquivo.stem} ({vivo}): {', '.join(arquivo.read_text().split())}")
    return 0


# ---------------------------------------------------------------- acoplamento


def cmd_acoplar(args) -> int:
    """Prende a regra às ferramentas: Docker sob demanda, banco em container, nada no boot."""
    aplicar = args.aplicar
    prefixo = "" if aplicar else "[simulação] "
    pendencias = 0

    # Sem terminal (prompt `!` do Claude Code), o sudo não tem onde pedir a senha:
    # ela chega por pipe — `echo <senha> | ambiente.py acoplar --aplicar` — e é
    # repassada a cada sudo via -S. Num terminal de verdade, o sudo pergunta sozinho.
    senha = None
    if aplicar and not sys.stdin.isatty():
        senha = sys.stdin.readline().strip() or None

    print("\n1 · Docker sob demanda (o socket acorda o daemon)")
    if not shutil.which("docker"):
        print("    docker não instalado — rode de novo depois de instalar.")
    elif not shutil.which("systemctl"):
        print("    sem systemd — pulei.")
    elif _rodar(["systemctl", "is-enabled", "docker.service"]).stdout.strip() == "enabled":
        pendencias += 1
        print(f"    {prefixo}docker.service sobe no boot → desabilitar e deixar o socket acordá-lo")
        if aplicar:
            _rodar_sudo(["sudo", "systemctl", "disable", "docker.service"], senha)
            _rodar_sudo(["sudo", "systemctl", "enable", "--now", "docker.socket"], senha)
            print("    ✓ o daemon só existe enquanto houver container")
    else:
        print("    ✓ docker.service não sobe no boot")

    print("\n2 · Containers que voltam sozinhos")
    teimosos = containers_teimosos()
    if not teimosos:
        print("    ✓ nenhum container com restart automático")
    else:
        pendencias += len(teimosos)
        for nome, politica in teimosos:
            print(f"    {prefixo}{nome.lstrip('/')} está como `{politica}` → vira `no`")
        if aplicar:
            _rodar(["docker", "update", "--restart=no", *[n.lstrip("/") for n, _ in teimosos]])
            print("    ✓ nenhum container renasce no boot")

    print("\n3 · Banco fora do sistema (só em container)")
    sql = servicos_sql_ativos()
    if not sql:
        print("    ✓ nenhum banco instalado no sistema")
    else:
        pendencias += len(sql)
        for servico, estado in sql:
            print(f"    {prefixo}{servico}.service [{estado}] → desabilitar (os dados ficam intactos)")
        if aplicar:
            for servico, _ in sql:
                _rodar_sudo(["sudo", "systemctl", "disable", "--now", f"{servico}.service"], senha)
            print("    ✓ banco só sobe pelo compose do projeto")

    print("\n4 · Derrubar o daemon ao fechar o terminal, sem pedir senha")
    if SUDOERS.exists():
        print(f"    ✓ {SUDOERS} já instalado")
    elif not shutil.which("systemctl"):
        print("    sem systemd — não se aplica.")
    else:
        pendencias += 1
        regra = (f"{getpass.getuser()} ALL=(root) NOPASSWD: "
                 "/usr/bin/systemctl stop docker.service, /usr/bin/systemctl stop containerd.service")
        conteudo = (
            "# Instalado por ~/Valt/scripts/ambiente.py (higiene de ambientes).\n"
            "# Permite parar o daemon do Docker ao fechar o terminal, sem senha.\n"
            "# Para remover: sudo rm /etc/sudoers.d/valt-ambiente\n"
            f"{regra}\n"
        )
        print(f"    {prefixo}instalaria {SUDOERS} com uma regra, e só ela:")
        print(f"      {regra}")
        if aplicar:
            temporario = Path("/tmp/valt-ambiente.sudoers")
            temporario.write_text(conteudo, encoding="utf-8")
            if _rodar(["visudo", "-c", "-f", str(temporario)]).returncode != 0:
                print("    ✗ regra inválida — nada instalado")
                temporario.unlink(missing_ok=True)
                return 1
            instalado = _rodar_sudo(["sudo", "install", "-m", "0440", "-o", "root", "-g", "root",
                                     str(temporario), str(SUDOERS)], senha)
            temporario.unlink(missing_ok=True)
            print("    ✓ instalado" if instalado.returncode == 0 else
                  "    ✗ falhou; sem isso o daemon fica de pé até o próximo `derrubar` com sudo")

    print("\n5 · Gatilho no terminal")
    bashrc = Path.home() / ".bashrc"
    texto = bashrc.read_text(errors="replace") if bashrc.exists() else ""
    if "bootstrap/shell/ambiente.sh" in texto:
        print("    ✓ ~/.bashrc carrega os atalhos e arma o trap de saída")
    else:
        pendencias += 1
        print(f"    {prefixo}falta carregar ambiente.sh no ~/.bashrc → rode ~/Valt/bootstrap/bootstrap.sh")

    print()
    if not aplicar and pendencias:
        print(f"{pendencias} pendência(s). Nada foi alterado — repita com --aplicar.")
    elif not pendencias:
        print("Acoplado: nada sobe sem você mandar, nada sobrevive ao terminal.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Ciclo de vida dos ambientes de ~/Sites: status, derrubar, limpar, dump.",
        epilog="Regra completa: ~/Valt/indices/higiene-ambientes.md",
    )
    sub = p.add_subparsers(dest="comando", required=True)

    s = sub.add_parser("status", help="o que ocupa disco hoje (não altera nada)")
    s.add_argument("projeto", nargs="?", help="caminho relativo a ~/Sites, ex.: Seara/food")
    s.set_defaults(func=cmd_status)

    d = sub.add_parser("derrubar", help="para os containers do projeto, preservando volumes")
    d.add_argument("projeto", nargs="?")
    d.add_argument("--compose", help="nome do projeto compose, se subiu com -p diferente")
    d.set_defaults(func=cmd_derrubar)

    du = sub.add_parser("dump", help="dump do banco em <projeto>/.dumps/ (container de pé)")
    du.add_argument("projeto")
    du.set_defaults(func=cmd_dump)

    l = sub.add_parser("limpar", help="apaga o refazível; simula por padrão")
    l.add_argument("projeto", nargs="?")
    l.add_argument("--tudo", action="store_true", help="inclui a poda global do Docker")
    l.add_argument("--aplicar", action="store_true", help="executa de verdade")
    l.add_argument("--dados", action="store_true", help="também remove volumes — exige dump antes")
    l.add_argument("--compose", help="nome do projeto compose, se subiu com -p diferente")
    l.set_defaults(func=cmd_limpar)

    a = sub.add_parser("acoplar", help="prende a regra ao Docker/SQL/boot; simula por padrão")
    a.add_argument("--aplicar", action="store_true", help="executa de verdade (usa sudo)")
    a.set_defaults(func=cmd_acoplar)

    se = sub.add_parser("sessao", help="registro do que cada terminal subiu (usado pelo trap)")
    se.add_argument("acao", choices=["registrar", "encerrar", "varrer", "preservar", "listar"])
    se.add_argument("--pid", type=int, default=os.getppid(), help="terminal dono (padrão: o pai)")
    se.add_argument("--cwd", help="pasta de onde o comando saiu")
    se.add_argument("--silencioso", action="store_true")
    se.set_defaults(func=cmd_sessao)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
