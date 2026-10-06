"""Wizard interativo no terminal.

Rode só ``python sort.py`` (sem argumentos) e o programa conduz você:

1. Banner + login
2. Escolha da playlist (lista paginada, ou colando o link)
3. Escolha da ordenação (as mesmas do README)
4. Passo 0 — ver o diff antes/depois
5. Gravar (sobrescrever com backup, ou criar cópia)

Tudo com ``input()`` via ``click.prompt``, então funciona em qualquer
terminal e continua testável com ``CliRunner(input=...)``.
"""

from __future__ import annotations

import sys

import click

from spotify_api import SpotifyAPI, SpotifyAPIError, parse_playlist_ref
from spotify_auth import AuthError, UserAuth, get_config, run_authorization_code_flow
from sorting import DEFAULT_SORT, Entry, SortSpecError, sort_entries

# ------------------------------------------------------------------ sinais
class Sinal:
    """Sentinela imprimível devolvida pelos passos do wizard."""

    def __init__(self, nome: str):
        self.nome = nome

    def __repr__(self) -> str:  # pragma: no cover - depuração
        return f"<{self.nome}>"


SAIR = Sinal("sair")
VOLTAR = Sinal("voltar")
MUDAR_SORT = Sinal("sort")
NOVA_PLAYLIST = Sinal("playlist")
SEGUIR = Sinal("seguir")
LINK = Sinal("link")
MAIS = Sinal("mais")

VAZIO = "\033[0m"  # nunca usado: cores deixadas de fora de propósito

PAGINA = 50
DIFF_LINHAS = 8

# Presets exatamente como descritos no README.
PRESETS: list[tuple[str, str | None]] = [
    ("Artista → Álbum → Faixa  (igual ao app, padrão)", "artist,album,year,trackno,title"),
    ("Título A–Z", "title"),
    ("Álbum A–Z", "album"),
    ("Data que foi adicionada à playlist", "date"),
    ("Duração", "duration"),
    ("Embaralhar", "random"),
    ("Personalizada… (digitar a cadeia)", None),
]


# ------------------------------------------------------------------- UI ---
def banner() -> None:
    """Banner ASCII na abertura do programa."""
    texto = "SORT SPOTIFY"
    try:
        from pyfiglet import figlet_format

        arte = figlet_format(texto, font="slant", width=80).rstrip("\n")
    except Exception:  # pragma: no cover - pyfiglet ausente
        arte = texto
    click.echo(arte)
    click.echo("Organiza suas playlists do Spotify como o app organiza.")
    click.echo("")


def secao(titulo: str) -> None:
    click.echo("")
    click.echo(f"\x1b[1m── {titulo} " + "─" * max(0, 60 - len(titulo)))


def ler(texto: str, *, permitir_vazio: bool = True) -> str:
    """Lê uma linha. Ctrl+C / EOF viram ``click.Abort`` tratado pelo chamador."""
    while True:
        try:
            resposta = click.prompt(
                texto, default="", show_default=False, prompt_suffix=": "
            )
        except (EOFError, KeyboardInterrupt):  # pragma: no cover - interativo
            raise click.Abort() from None
        resposta = (resposta or "").strip()
        if resposta or permitir_vazio:
            return resposta


def sim(texto: str, *, padrao: bool = False) -> bool:
    """Pergunta s/n. Vazio usa o padrão."""
    sufixo = " [S/n]: " if padrao else " [s/N]: "
    try:
        resposta = click.prompt(
            texto, default="", show_default=False, prompt_suffix=sufixo
        )
    except (EOFError, KeyboardInterrupt):  # pragma: no cover - interativo
        raise click.Abort() from None
    resposta = (resposta or "").strip().casefold()
    if not resposta:
        return padrao
    return resposta in ("s", "sim", "y", "yes")


def menu(
    titulo: str,
    itens: list[tuple[str, object]],
    *,
    sair: str | None = "Sair",
    escolha: str = "Escolha",
) -> object:
    """Mostra opções numeradas e devolve o **valor** escolhido.

    Itens com valor :data:`SAIR`/``None`` são tratados normalmente; use
    ``sair=None`` para esconder a opção 0.
    """
    click.echo("")
    click.echo(f"\x1b[1m{titulo}\x1b[0m")
    for indice, (rotulo, _valor) in enumerate(itens, start=1):
        click.echo(f"  [{indice}] {rotulo}")
    if sair is not None:
        click.echo(f"  [0] {sair}")

    faixa = f"1-{len(itens)}" if len(itens) > 1 else "1"
    while True:
        bruto = ler(f"{escolha} ({faixa}" + (", 0 para sair" if sair else "") + ")")
        if not bruto:
            if sair is not None:
                return SAIR
            continue
        if bruto.isdigit():
            numero = int(bruto)
            if numero == 0 and sair is not None:
                return SAIR
            if 1 <= numero <= len(itens):
                return itens[numero - 1][1]
        click.echo(f"  ! opção inválida: {bruto!r}")


# ------------------------------------------------------------- leitura -----
def ler_itens(api: SpotifyAPI, playlist_id: str) -> list[dict]:
    """Lê a playlist inteira mostrando progresso (tqdm se disponível)."""
    barra = None

    def progresso(done, total):
        nonlocal barra
        if barra is None:
            try:
                from tqdm import tqdm

                barra = tqdm(
                    total=total or None,
                    unit="faixa",
                    dynamic_ncols=True,
                    leave=False,
                )
            except Exception:  # pragma: no cover - tqdm ausente
                barra = False
        if barra is False:
            click.echo(f"\r  lendo... {done}/{total}", nl=False, err=True)
            return
        barra.n = done
        if total:
            barra.total = total
        barra.refresh()

    itens = api.get_playlist_items(playlist_id, on_progress=progresso)

    if barra is False:  # pragma: no cover - sem tqdm
        click.echo("\r" + " " * 40 + "\r", nl=False, err=True)
    elif barra is not None:
        barra.close()
    return itens


def carregar(
    api: SpotifyAPI, ref: str, spec: str, opts: dict
) -> tuple[dict, list[dict], list[Entry], list[Entry]]:
    """Lê, converte em ``Entry`` e ordena.

    Devolve ``(playlist, itens, original, ordenado)``.
    """
    playlist_id = parse_playlist_ref(ref)
    playlist = api.get_playlist(playlist_id)
    itens = ler_itens(api, playlist_id)
    entradas = [Entry.from_playlist_item(item, i) for i, item in enumerate(itens)]
    ordenado = sort_entries(entradas, spec=spec, **opts)
    return playlist, itens, entradas, ordenado


def _curto(texto: str, largura: int) -> str:
    texto = texto or ""
    return texto if len(texto) <= largura else texto[: largura - 1] + "…"


def diff_antes_depois(
    original: list[Entry], ordenado: list[Entry], linhas: int = DIFF_LINHAS
) -> None:
    """Mostra lado a lado as primeiras posições antes e depois."""
    click.echo("")
    click.echo(f"  {'ANTES':<42}  DEPOIS")
    click.echo("  " + "-" * 42 + "  " + "-" * 42)
    for indice in range(min(linhas, len(original), len(ordenado))):
        antes = original[indice]
        depois = ordenado[indice]
        esquerda = f"{indice + 1:>2}. {_curto(antes.name, 30)}"
        esquerda += f" · {_curto(', '.join(antes.artists) or '—', 9)}"
        direita = f"{indice + 1:>2}. {_curto(depois.name, 30)}"
        direita += f" · {_curto(', '.join(depois.artists) or '—', 9)}"
        click.echo(f"  {esquerda:<42}  {direita}")
    restam = len(ordenado) - linhas
    if restam > 0:
        click.echo(f"  {'':<42}  … +{restam} faixas")


def resumo(playlist: dict, itens: list[Entry], ordenado: list[Entry]) -> dict:
    """Imprime o resumo e devolve contagens úteis (perdíveis, dono...)."""
    nao_gravaveis = [e for e in ordenado if not e.playable]
    locais = sum(1 for e in nao_gravaveis if e.is_local)
    removidas = sum(1 for e in nao_gravaveis if e.restricted)
    dono = (playlist.get("owner") or {}).get("display_name") or "?"

    click.echo("")
    click.echo(f"  Playlist : {playlist.get('name')}")
    click.echo(f"  Dono     : {dono}")
    click.echo(f"  Faixas   : {len(itens)}")
    if nao_gravaveis:
        click.echo(
            f"  ! {len(nao_gravaveis)} item(ns) nao podem ser regravados "
            f"(locais: {locais}, removidas do catalogo: {removidas})."
        )
    return {
        "nao_gravaveis": nao_gravaveis,
        "locais": locais,
        "removidas": removidas,
        "dono_id": (playlist.get("owner") or {}).get("id"),
    }


# --------------------------------------------------------------- login -----
def garantir_login(user_auth: UserAuth) -> dict:
    """Garante um token valido; refaz o login se o refresh falhar."""
    if not user_auth.has_token:
        click.echo("  Faca login no navegador para liberar a gravacao.")
        user_auth.save(run_authorization_code_flow(get_config()))
    try:
        user_auth.get_token()
    except AuthError:
        click.echo("  Sessao expirada - refazendo o login.")
        user_auth.save(run_authorization_code_flow(get_config()))
        user_auth.get_token()
    return user_auth.data or {}


# ------------------------------------------------------------- passos ------
def pedir_link() -> str:
    """Pede um link/URI/ID até ser válido."""
    while True:
        bruto = ler("Cole o link, a URI ou o ID da playlist")
        if not bruto:
            return SAIR  # type: ignore[return-value]
        try:
            parse_playlist_ref(bruto)
        except ValueError as exc:
            click.echo(f"  ! {exc}")
            continue
        return bruto


def _rotulo_playlist(playlist: dict, meu_id: str) -> str:
    dono_id = (playlist.get("owner") or {}).get("id") or ""
    dono = (playlist.get("owner") or {}).get("display_name") or "?"
    total = (playlist.get("tracks") or {}).get("total", "?")
    etiqueta = "sua" if dono_id and dono_id == meu_id else dono
    nome = playlist.get("name") or "(sem nome)"
    return f"{nome}  ·  {total} faixas  ·  {etiqueta}"


def passo_playlist(api: SpotifyAPI, meu_id: str) -> str | Sinal:
    """Lista as playlists do usuário (paginadas) ou deixa colar o link."""
    pagina = 0
    while True:
        secao("Escolha a playlist")
        dados = api.get_current_user_playlists(limit=PAGINA, offset=pagina * PAGINA)
        itens = dados.get("items") or []

        if not itens:
            if pagina == 0:
                click.echo("  Nenhuma playlist na sua conta.")
                return pedir_link()
            click.echo("  Fim da lista.")
            pagina = max(0, pagina - 1)
            dados = api.get_current_user_playlists(
                limit=PAGINA, offset=pagina * PAGINA
            )
            itens = dados.get("items") or []

        opcoes: list[tuple[str, object]] = [
            (_rotulo_playlist(p, meu_id), p.get("id") or "") for p in itens
        ]
        opcoes.append(("Colar link / URI / ID", LINK))
        if dados.get("next"):
            opcoes.append((f"Ver mais (página {pagina + 2})", MAIS))

        escolha = menu(
            f"Suas playlists — página {pagina + 1}",
            opcoes,
            sair="Sair",
            escolha="Nº",
        )
        if escolha is SAIR:
            return SAIR
        if escolha is LINK:
            return pedir_link()
        if escolha is MAIS:
            pagina += 1
            continue
        if escolha:
            return str(escolha)
        return SAIR  # pragma: no cover - defendido pelo menu


def passo_sort() -> tuple[str, dict] | Sinal:
    """Escolhe a cadeia de ordenação e as opções."""
    secao("Como ordenar")
    rotulos = [(rotulo, spec) for rotulo, spec in PRESETS]
    escolha = menu("Ordenação", rotulos, sair="Voltar", escolha="Nº")

    if escolha is SAIR:
        return VOLTAR
    if escolha is None:  # personalizada
        spec = ""
        while not spec:
            spec = ler(
                "Cadeia (ex: artist,album,year,trackno,title | aceita em pt-BR)"
            )
            if not spec:
                return VOLTAR
    else:
        spec = str(escolha)

    opts: dict = {
        "keep_accents": False,
        "ignore_the": False,
        "all_artists": False,
        "descending": False,
    }

    if spec != "random":
        opts["descending"] = sim("Ordem decrescente")

    if sim("Opcoes avancadas (artigos, acentos, todos os artistas)"):
        opts["ignore_the"] = sim("  Ignorar artigos no inicio (the, a, o...)")
        opts["keep_accents"] = sim("  Manter acentos na comparacao")
        opts["all_artists"] = sim("  Usar todos os artistas da faixa")

    return spec, opts


def passo_diff(
    api: SpotifyAPI, ref: str, spec: str, opts: dict
) -> tuple[dict, list[dict], list[Entry], list[Entry], dict] | Sinal:
    """Carrega, mostra o diff e decide o próximo passo."""
    secao("Passo 0 — ver antes de gravar")
    try:
        playlist, itens, original, ordenado = carregar(api, ref, spec, opts)
    except (ValueError, SpotifyAPIError) as exc:
        click.echo(f"  ! {exc}")
        return menu("O que fazer?", [("Tentar de novo", SEGUIR), ("Voltar", VOLTAR)])

    click.echo("")
    click.echo(f"  Ordenacao: {spec}")
    diff_antes_depois(original, ordenado)
    info = resumo(playlist, itens, ordenado)

    if sim("Exportar a ordem em CSV/JSON"):
        from sort import emit_exports

        csv_path = ler("Caminho do CSV (vazio pula)")
        json_path = ler("Caminho do JSON (vazio pula)")
        emit_exports({"csv_path": csv_path or None, "json_path": json_path or None}, ordenado)

    contexto = (playlist, itens, original, ordenado, info)
    acao = menu(
        "Proximo passo",
        [
            ("Continuar para gravar", SEGUIR),
            ("Mudar a ordenacao", MUDAR_SORT),
            ("Escolher outra playlist", NOVA_PLAYLIST),
        ],
        sair="Sair",
        escolha="Nº",
    )
    if acao is SEGUIR:
        return contexto  # type: ignore[return-value]
    return acao  # type: ignore[return-value]


def passo_gravar(
    api: SpotifyAPI,
    contexto: tuple,
    spec: str,
    backup_dir: str = "backup",
) -> Sinal:
    """Escolhe copy/in-place, faz os avisos e grava."""
    # import tardio evita circular (sort.py chama o wizard quando não há subcomando)
    from sort import _report_progress, backup_playlist

    playlist, itens, original, ordenado, info = contexto
    playlist_id = playlist.get("id") or ""

    secao("Gravar")
    meu_id = (api.get_current_user() or {}).get("id") or ""
    dono_id = info.get("dono_id") or ""
    pode_sobrescrever = bool(dono_id) and dono_id == meu_id

    if info["nao_gravaveis"]:
        click.echo("")
        click.echo(
            f"  Atencao: {len(info['nao_gravaveis'])} item(ns) seriam PERDIDOS "
            f"(locais: {info['locais']}, removidas: {info['removidas']})."
        )
        if not sim("Aceitar essa perda e prosseguir?", padrao=False):
            return VOLTAR

    opcoes: list[tuple[str, object]] = []
    if pode_sobrescrever:
        opcoes.append(("Sobrescrever a original (com backup)", "in-place"))
    opcoes.append(("Criar uma nova playlist ordenada", "copy"))

    acao = menu("Como gravar", opcoes, sair="Voltar", escolha="Nº")
    if acao is SAIR or acao is None:
        return VOLTAR

    uris = [e.uri for e in ordenado if e.playable]

    if acao == "in-place":
        alvo = playlist.get("name") or "(sem nome)"
        if not sim(f"Sobrescrever '{alvo}' ({len(uris)} faixas)?", padrao=False):
            return VOLTAR
        caminho = backup_playlist(backup_dir, playlist, itens, ordenado, playlist_id)
        click.echo(f"  Backup -> {caminho}")
        _report_progress(api.write_full, playlist_id, uris, replace=True)
        url = (playlist.get("external_urls") or {}).get("spotify", "")
        click.echo(f"\n  OK! Playlist sobrescrita: {url}")
    else:
        padrao = f"{playlist.get('name')} (ordenada)"
        nome = ler(f"Nome da nova playlist [{padrao}]") or padrao
        if not sim(f"Criar '{nome}' com {len(uris)} faixas?", padrao=False):
            return VOLTAR
        me = api.get_current_user()
        criada = api.create_playlist(
            me["id"], nome, public=False, description=f"Ordenada por: {spec}"
        )
        _report_progress(api.write_full, criada["id"], uris, replace=True)
        url = (criada.get("external_urls") or {}).get("spotify", "")
        click.echo(f"\n  OK! Nova playlist: {url}")

    if sim("Escolher outra playlist?", padrao=True):
        return NOVA_PLAYLIST
    return SAIR


# ------------------------------------------------------------- wizard ------
def wizard(api: SpotifyAPI | None = None, me: dict | None = None) -> int:
    """Programa interativo completo. Retorna o código de saída."""
    try:
        banner()
        if api is None or me is None:
            cfg = get_config()
            user_auth = UserAuth(cfg)
            garantir_login(user_auth)
            api = SpotifyAPI(user_auth)
            me = api.get_current_user()
        click.echo(f"  Logado como: {me.get('display_name') or me.get('id')}")
    except click.Abort:
        click.echo("\n  Ate logo!")
        return 0
    except (AuthError, SpotifyAPIError) as exc:
        click.echo(f"  Erro: {exc}", err=True)
        return 1

    passo = "playlist"
    ref = ""
    spec = DEFAULT_SORT
    opts: dict = {}
    contexto = None

    while True:
        try:
            if passo == "playlist":
                escolha = passo_playlist(api, me.get("id") or "")
                if escolha is SAIR:
                    break
                ref = str(escolha)
                contexto = None
                passo = "sort"

            elif passo == "sort":
                escolha = passo_sort()
                if escolha is VOLTAR:
                    passo = "playlist"
                    continue
                spec, opts = escolha  # type: ignore[misc]
                passo = "diff"

            elif passo == "diff":
                escolha = passo_diff(api, ref, spec, opts)
                if isinstance(escolha, tuple):
                    contexto = escolha
                    passo = "gravar"
                elif escolha is MUDAR_SORT:
                    passo = "sort"
                elif escolha is NOVA_PLAYLIST:
                    passo = "playlist"
                else:
                    break

            elif passo == "gravar" and contexto is not None:
                escolha = passo_gravar(api, contexto, spec)
                if escolha is NOVA_PLAYLIST:
                    passo = "playlist"
                elif escolha is SAIR:
                    break
                else:
                    passo = "diff"

            else:  # pragma: no cover - estado indefinido
                break

        except click.Abort:
            click.echo("\n  [Ctrl+C] Encerrado.")
            break
        except (AuthError, SpotifyAPIError) as exc:
            click.echo(f"\n  ! {exc}")
            if not sim("Tentar continuar?", padrao=True):
                break
            passo = "playlist"

    click.echo("\n  Ate logo!")
    return 0



