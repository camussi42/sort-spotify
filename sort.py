#!/usr/bin/env python3
"""sort-spotify — ordena playlists do Spotify do jeito que o app ordena.

Exemplos::

    # Só olhar como ficaria (modo link / sem login)
    python sort.py preview "https://open.spotify.com/playlist/..." --sort artist

    # Logar uma vez para poder gravar
    python sort.py login

    # Sobrescrever a própria playlist, mantendo o link
    python sort.py sort "https://open.spotify.com/playlist/..." --write in-place

    # Gerar uma NOVA playlist ordenada
    python sort.py sort "https://open.spotify.com/playlist/..." --write copy
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time

import click

from spotify_api import SpotifyAPI, SpotifyAPIError, parse_playlist_ref
from spotify_auth import (
    AuthError,
    ClientCredentials,
    UserAuth,
    get_config,
    run_authorization_code_flow,
)
from sorting import DEFAULT_SORT, Entry, SortSpecError, sort_entries


# ------------------------------------------------------------------ wizard
def _run_wizard() -> None:
    """Roda o modo interativo (``python sort.py`` sem argumentos)."""
    from interactive import wizard

    raise SystemExit(wizard())

BACKUP_DIR = "backup"

TABLE_HEADERS = ["#", "Título", "Artista(s)", "Álbum", "Ano", "D", "F", "Dur."]
TABLE_CAPS = [5, 42, 30, 34, 10, 2, 3, 6]


# ------------------------------------------------------------------ helpers
def fmt_duration(ms: int) -> str:
    seconds = int((ms or 0) / 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _clip(text, width: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= width else text[: width - 1] + "…"


def print_table(rows: list[list], headers: list[str], caps: list[int]) -> None:
    """Imprime tabela alinhada com larguras limitadas."""
    widths: list[int] = []
    for i, header in enumerate(headers):
        width = len(header)
        for row in rows:
            width = max(width, len(str(row[i])))
        widths.append(min(width, caps[i]))

    def line(cells) -> str:
        return "  ".join(
            _clip(cell, widths[i]).ljust(widths[i]) for i, cell in enumerate(cells)
        )

    click.echo(line(headers))
    click.echo("  ".join("-" * w for w in widths))
    for row in rows:
        click.echo(line(row))


def to_rows(entries: list[Entry], start: int = 1) -> list[list]:
    rows: list[list] = []
    for offset, entry in enumerate(entries):
        rows.append(
            [
                start + offset,
                entry.name,
                ", ".join(entry.artists) or "—",
                entry.album or "—",
                entry.album_date or "—",
                entry.disc if entry.album else "—",
                entry.track_no if entry.album else "—",
                fmt_duration(entry.duration_ms),
            ]
        )
    return rows


def export_csv(path: str, entries: list[Entry]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "pos", "titulo", "artistas", "album", "data_lancamento",
                "disco", "faixa", "duracao", "adicionado_em", "uri",
            ]
        )
        for position, entry in enumerate(entries, start=1):
            writer.writerow(
                [
                    position, entry.name, "; ".join(entry.artists), entry.album,
                    entry.album_date, entry.disc, entry.track_no,
                    fmt_duration(entry.duration_ms), entry.added_at, entry.uri,
                ]
            )


def export_json(path: str, entries: list[Entry]) -> None:
    payload = [
        {
            "pos": position,
            "title": entry.name,
            "artists": entry.artists,
            "album": entry.album,
            "album_date": entry.album_date,
            "disc": entry.disc,
            "track_number": entry.track_no,
            "duration_ms": entry.duration_ms,
            "added_at": entry.added_at,
            "uri": entry.uri,
        }
        for position, entry in enumerate(entries, start=1)
    ]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def backup_playlist(
    backup_dir: str,
    playlist: dict,
    items: list[dict],
    ordered: list[Entry],
    playlist_id: str,
) -> str:
    """Salva a ordem original + metadados antes de sobrescrever."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(backup_dir, f"{playlist_id}_{stamp}.json")
    payload = {
        "playlist_id": playlist_id,
        "name": playlist.get("name"),
        "snapshot_id": playlist.get("snapshot_id"),
        "owner": (playlist.get("owner") or {}).get("id"),
        "backed_up_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "original_order": [
            ((item.get("track") or {}).get("uri") or item.get("uri"))
            for item in items
        ],
        "new_order": [entry.uri for entry in ordered],
        "items": items,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


# ----------------------------------------------------------------- contexto
def build_api(force_public: bool = False) -> tuple[SpotifyAPI, str]:
    """Escolhe o provedor: usuário logado se existir, senão modo público."""
    cfg = get_config()
    if not force_public:
        user_auth = UserAuth(cfg)
        if user_auth.has_token:
            try:
                user_auth.get_token()
            except AuthError:
                click.echo("! Login expirado, usando modo público.", err=True)
            else:
                return SpotifyAPI(user_auth), "user"
    return SpotifyAPI(ClientCredentials(cfg)), "client"


def sort_options(fn):
    """Opções de ordenação compartilhadas por `preview` e `sort`."""
    options = [
        click.option(
            "--sort", "sort_spec", default=DEFAULT_SORT, show_default=True,
            help="Cadeia de chaves, ex: artist,album,year,trackno,title",
        ),
        click.option(
            "--ignore-the", is_flag=True,
            help="Ignora artigos no início (the, a, o, as, os...)",
        ),
        click.option(
            "--keep-accents", is_flag=True,
            help="Não remove acentos na comparação",
        ),
        click.option(
            "--all-artists", is_flag=True,
            help="Usa todos os artistas da faixa, não só o primeiro",
        ),
        click.option("--desc", "descending", is_flag=True, help="Ordem decrescente"),
        click.option(
            "--csv", "csv_path", type=click.Path(dir_okay=False),
            help="Exporta a ordem final em CSV",
        ),
        click.option(
            "--json", "json_path", type=click.Path(dir_okay=False),
            help="Exporta a ordem final em JSON",
        ),
    ]
    for option in reversed(options):
        fn = option(fn)
    return fn


def _sort_kwargs(opts: dict) -> dict:
    return {
        k: v
        for k, v in opts.items()
        if k in ("keep_accents", "ignore_the", "all_artists", "descending")
    }


def load_and_sort(
    api: SpotifyAPI, playlist_id: str, opts: dict, show_progress: bool = True
) -> tuple[dict, list[dict], list[Entry]]:
    """Lê a playlist inteira (paginada) e devolve a ordem nova."""
    playlist = api.get_playlist(playlist_id)

    def progress(done, total):
        if show_progress and total > 100:
            click.echo(f"  lendo... {done}/{total}", nl=False, err=True)
            click.echo("\r", nl=False, err=True)

    items = api.get_playlist_items(playlist_id, on_progress=progress)
    if show_progress and len(items) > 100:
        click.echo(f"  lendo... {len(items)}/{len(items)}", err=True)

    entries = [Entry.from_playlist_item(item, i) for i, item in enumerate(items)]
    ordered = sort_entries(entries, **opts)
    return playlist, items, ordered


def emit_exports(opts: dict, ordered: list[Entry]) -> None:
    if opts.get("csv_path"):
        export_csv(opts["csv_path"], ordered)
        click.echo(f"CSV   -> {opts['csv_path']}")
    if opts.get("json_path"):
        export_json(opts["json_path"], ordered)
        click.echo(f"JSON  -> {opts['json_path']}")


def _confirm_or_abort(yes: bool, dry_run: bool, message: str) -> None:
    if yes or dry_run:
        return
    if not click.confirm(message, default=False):
        raise click.ClickException("Cancelado pelo usuário.")


def _report_progress(writer, playlist_id: str, uris: list[str], replace: bool) -> None:
    total = len(uris)

    def on_progress(done, size):
        pct = int(done * 100 / size) if size else 100
        click.echo(f"  gravando... {done}/{size} ({pct}%)", nl=False, err=True)
        click.echo("\r", nl=False, err=True)

    writer(playlist_id, uris, replace=replace, on_progress=on_progress)
    click.echo(f"  gravando... {total}/{total} (100%)")


# ---------------------------------------------------------------------- cli
@click.group(help=__doc__, invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """Ordena playlists do Spotify como o app. Sem subcomando, abre o modo interativo."""
    if ctx.invoked_subcommand is None:
        _run_wizard()


@cli.command("login")
def login_cmd():
    """Abre o navegador e autoriza criar/sobrescrever playlists."""
    cfg = get_config()
    user_auth = UserAuth(cfg)
    user_auth.save(run_authorization_code_flow(cfg))
    me = SpotifyAPI(user_auth).get_current_user()
    click.echo(f"\nLogado como: {me.get('display_name') or me.get('id')}")
    click.echo("Agora você pode usar --write copy e --write in-place.")


@cli.command("logout")
def logout_cmd():
    """Remove o token salvo localmente."""
    if UserAuth(get_config()).logout():
        click.echo("Logout feito.")
    else:
        click.echo("Nenhum login salvo.")


@cli.command()
@click.argument("ref")
@sort_options
@click.option("--limit", default=20, show_default=True, help="Linhas no preview")
@click.option(
    "--public", "force_public", is_flag=True, help="Força modo público (sem login)"
)
def preview(ref, limit, force_public, **opts):
    """Mostra como a playlist ficaria ordenada, sem gravar nada."""
    try:
        playlist_id = parse_playlist_ref(ref)
    except ValueError as exc:
        raise click.ClickException(str(exc))

    try:
        api, mode = build_api(force_public=force_public)
        playlist, items, ordered = load_and_sort(
            api, playlist_id, {"spec": opts["sort_spec"], **_sort_kwargs(opts)}
        )
    except (AuthError, SpotifyAPIError, SortSpecError) as exc:
        raise click.ClickException(str(exc))

    click.echo("")
    click.echo(f"Playlist : {playlist.get('name')}")
    click.echo(f"Dono     : {(playlist.get('owner') or {}).get('display_name')}")
    click.echo(
        f"Faixas   : {len(items)}  |  modo: {mode}  |  sort: {opts['sort_spec']}"
    )
    click.echo("")

    print_table(to_rows(ordered[:limit]), TABLE_HEADERS, TABLE_CAPS)
    if len(ordered) > limit:
        click.echo(f"... +{len(ordered) - limit} faixas")

    skipped = [e for e in ordered if not e.playable]
    if skipped:
        click.echo(
            f"\n! {len(skipped)} item(ns) nao podem ser regravados "
            "(arquivo local ou removido do catálogo)."
        )

    emit_exports(opts, ordered)

    if mode == "client":
        click.echo(
            "\nModo somente leitura (playlist pública). "
            "Para gravar, rode: python sort.py login"
        )
    else:
        click.echo(
            "\nPara gravar:  --write in-place  (sobrescreve)  |  "
            "--write copy  (cria nova ordenada)"
        )


@cli.command("sort")
@click.argument("ref")
@sort_options
@click.option(
    "--write", "write_mode",
    type=click.Choice(["none", "copy", "in-place"]),
    default="none", show_default=True,
    help="none=só mostra, copy=cria nova, in-place=sobrescreve",
)
@click.option("--name", default=None, help="Nome da playlist criada em --write copy")
@click.option("--public", "make_public", is_flag=True, help="Playlist nova pública")
@click.option("--yes", is_flag=True, help="Não pede confirmação")
@click.option("--dry-run", is_flag=True, help="Faz tudo menos a chamada de escrita")
@click.option("--force", is_flag=True, help="Permite perder itens locais/restritos")
@click.option(
    "--backup-dir", default=BACKUP_DIR, show_default=True,
    type=click.Path(file_okay=False),
)
def sort_cmd(
    ref, write_mode, name, make_public, yes, dry_run, force, backup_dir, **opts
):
    """Ordena a playlist e (opcionalmente) grava de volta no Spotify."""
    sort_spec = opts["sort_spec"]

    try:
        playlist_id = parse_playlist_ref(ref)
    except ValueError as exc:
        raise click.ClickException(str(exc))

    try:
        api, mode = build_api()
        playlist, items, ordered = load_and_sort(
            api, playlist_id, {"spec": sort_spec, **_sort_kwargs(opts)}
        )
    except (AuthError, SpotifyAPIError, SortSpecError) as exc:
        raise click.ClickException(str(exc))

    click.echo("")
    click.echo(f"Playlist : {playlist.get('name')}")
    click.echo(f"Faixas   : {len(items)}  |  modo: {mode}  |  sort: {sort_spec}")
    click.echo("")
    print_table(to_rows(ordered[:15]), TABLE_HEADERS, TABLE_CAPS)
    if len(ordered) > 15:
        click.echo(f"... +{len(ordered) - 15} faixas")

    emit_exports(opts, ordered)

    if write_mode == "none":
        click.echo("\nNada gravado (use --write copy | --write in-place).")
        return

    if mode != "user":
        raise click.ClickException(
            "Para gravar é preciso logar:  python sort.py login"
        )

    _guard_unusable(ordered, force)
    uris = [e.uri for e in ordered if e.playable]

    if write_mode == "in-place":
        _write_in_place(
            api, playlist, playlist_id, items, ordered, uris,
            yes, dry_run, backup_dir,
        )
    else:
        _write_copy(api, playlist, uris, sort_spec, name, make_public, yes, dry_run)


def _guard_unusable(ordered: list[Entry], force: bool) -> None:
    """Aborta se a gravação perderia itens locais/removidos, a menos que --force."""
    unusable = [e for e in ordered if not e.playable]
    if not unusable:
        return
    local = sum(1 for e in unusable if e.is_local)
    restricted = sum(1 for e in unusable if e.restricted)
    click.echo(
        f"\n! {len(unusable)} item(ns) seriam PERDIDOS "
        f"(locais: {local}, removidos do catálogo: {restricted})."
    )
    if not force:
        raise click.ClickException(
            "Use --force para aceitar essa perda, ou --write none para só ver."
        )


def _write_in_place(
    api, playlist, playlist_id, items, ordered, uris, yes, dry_run, backup_dir
) -> None:
    owner_id = (playlist.get("owner") or {}).get("id")
    me = api.get_current_user()
    if owner_id != me.get("id"):
        raise click.ClickException(
            f"Você não é dono desta playlist (dono: {owner_id}). "
            "Use --write copy para criar uma ordenada."
        )

    _confirm_or_abort(
        yes, dry_run, f"Sobrescrever '{playlist.get('name')}' ({len(uris)} faixas)?"
    )
    if dry_run:
        click.echo("Dry-run: nada foi gravado.")
        return
    path = backup_playlist(backup_dir, playlist, items, ordered, playlist_id)
    click.echo(f"Backup    -> {path}")

    _report_progress(api.write_full, playlist_id, uris, replace=True)
    url = (playlist.get("external_urls") or {}).get("spotify", "")
    click.echo(f"\nOK! Playlist sobrescrita: {url}")


def _write_copy(
    api, playlist, uris, sort_spec, name, make_public, yes, dry_run
) -> None:
    me = api.get_current_user()
    target_name = name or f"{playlist.get('name')} (ordenada)"
    _confirm_or_abort(
        yes, dry_run, f"Criar nova playlist '{target_name}' com {len(uris)} faixas?"
    )
    if dry_run:
        click.echo("Dry-run: nada foi gravado.")
        return

    created = api.create_playlist(
        me["id"],
        target_name,
        public=make_public,
        description=f"Ordenada por: {sort_spec}",
    )
    _report_progress(api.write_full, created["id"], uris, replace=True)
    url = (created.get("external_urls") or {}).get("spotify", "")
    click.echo(f"\nOK! Nova playlist: {url}")
    click.echo("Obs: itens locais não podem ser copiados pela API.")


def main() -> None:
    try:
        cli()
    except (AuthError, SpotifyAPIError) as exc:
        click.echo(f"Erro: {exc}", err=True)
        sys.exit(1)
    except KeyboardInterrupt:  # pragma: no cover
        click.echo("\nInterrompido.", err=True)
        sys.exit(130)


if __name__ == "__main__":
    main()



