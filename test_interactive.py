"""Testes do wizard interativo (sem rede, sem login real).

Cobre lista paginada, colar link, presets de ordenação, diff e os dois
modos de gravação — tudo com API falsificada e stdin simulado.

    python test_interactive.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import click
from click.testing import CliRunner

os.environ.setdefault("CLIENT_ID", "fake-client-id")
os.environ.setdefault("CLIENT_SECRET", "fake-client-secret")

import interactive as inter
from interactive import (
    SAIR,
    SEGUIR,
    VOLTAR,
    passo_diff,
    passo_gravar,
    passo_playlist,
    passo_sort,
    wizard,
)

FAILURES: list[str] = []
PID = "37i9dQZF1DXcBWIGoYBM5M"
OWNER = "user-123"


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok   {message}")
    else:
        FAILURES.append(message)
        print(f"  FAIL {message}")


def build_items(count: int = 12) -> list[dict]:
    artists = ["Zebra", "Alpha", "Mike"]
    items = []
    for i in range(count):
        artist = artists[(i * 7) % len(artists)]
        items.append(
            {
                "added_at": f"2024-01-{(i % 28) + 1:02d}T10:00:00Z",
                "track": {
                    "type": "track",
                    "uri": f"spotify:track:{i:04d}",
                    "name": f"Song {i:03d}",
                    "duration_ms": 60000 + i * 1000,
                    "disc_number": 1,
                    "track_number": (i % 10) + 1,
                    "artists": [{"name": artist}],
                    "album": {
                        "name": f"Album {i % 3}",
                        "release_date": f"{2000 + (i % 3)}-05-05",
                    },
                    "is_local": False,
                },
            }
        )
    return items


ITEMS = build_items(12)


class FakeAPI:
    """Finge a Web API: playlists, leitura e escrita."""

    def __init__(self, playlists: list[dict] | None = None, owner: str = OWNER):
        self.owner = owner
        self.items = list(ITEMS)
        if playlists is None:
            self.playlists = [
                {
                    "id": PID,
                    "name": "Playlist Teste",
                    "owner": {"id": owner, "display_name": "Tester"},
                    "tracks": {"total": len(self.items)},
                }
            ]
        else:
            self.playlists = playlists
        self.created: list[dict] = []
        self.writes: list[tuple] = []

    def get_current_user_playlists(self, limit: int = 50, offset: int = 0) -> dict:
        batch = self.playlists[offset : offset + limit]
        has_more = offset + limit < len(self.playlists)
        return {
            "items": batch,
            "next": "https://api.spotify.com/v1/next" if has_more else None,
            "total": len(self.playlists),
        }

    def get_playlist(self, playlist_id: str) -> dict:
        return {
            "id": playlist_id,
            "name": "Playlist Teste",
            "snapshot_id": "snap-1",
            "owner": {"id": self.owner, "display_name": "Tester"},
            "external_urls": {
                "spotify": f"https://open.spotify.com/playlist/{playlist_id}"
            },
            "tracks": {"total": len(self.items)},
        }

    def get_playlist_items(self, playlist_id: str, on_progress=None) -> list[dict]:
        if on_progress:
            on_progress(len(self.items), len(self.items))
        return list(self.items)

    def get_current_user(self) -> dict:
        return {"id": "user-123", "display_name": "Tester"}

    def create_playlist(
        self, user_id: str, name: str, *, public: bool = False, description: str = ""
    ) -> dict:
        created = {
            "id": f"new-{len(self.created) + 1}",
            "name": name,
            "external_urls": {
                "spotify": "https://open.spotify.com/playlist/new-1",
            },
        }
        self.created.append(created)
        return created

    def write_full(
        self, playlist_id: str, uris: list[str], *, replace: bool = True,
        on_progress=None,
    ) -> None:
        self.writes.append((playlist_id, list(uris)))
        if on_progress:
            on_progress(len(uris), len(uris))


ME = {"id": "user-123", "display_name": "Tester"}

def run_wizard(api: FakeAPI, stdin: str):
    """Roda o wizard com stdin simulado via comando click temporário."""

    @click.command()
    def _cmd():
        raise SystemExit(wizard(api=api, me=dict(ME)))

    return CliRunner().invoke(_cmd, [], input=stdin, catch_exceptions=False)


def test_passo_playlist_escolhe_da_lista():
    print("passo_playlist: escolhe da lista")
    api = FakeAPI()

    @click.command()
    def _cmd():
        result = passo_playlist(api, "user-123")
        click.echo(f"RESULT={result!r}")

    result = CliRunner().invoke(_cmd, [], input="1\n", catch_exceptions=False)
    check(result.exit_code == 0, f"exit 0 (obtido {result.exit_code})")
    check(f"RESULT='{PID}'" in result.output, "devolve o id da playlist")


def test_passo_playlist_colar_link():
    print("passo_playlist: colar link")
    api = FakeAPI()
    url = f"https://open.spotify.com/playlist/{PID}"

    @click.command()
    def _cmd():
        result = passo_playlist(api, "user-123")
        click.echo(f"RESULT={result!r}")

    result = CliRunner().invoke(_cmd, [], input=f"2\n{url}\n",
                                catch_exceptions=False)
    check(result.exit_code == 0, f"exit 0 (obtido {result.exit_code})")
    check(url in result.output, "aceita o link colado")


def test_passo_playlist_paginacao():
    print("passo_playlist: paginação Ver mais")
    playlists = [
        {
            "id": f"{i:022d}",
            "name": f"PL {i:02d}",
            "owner": {"id": "user-123", "display_name": "Tester"},
            "tracks": {"total": 10},
        }
        for i in range(55)
    ]
    api = FakeAPI(playlists=playlists)

    @click.command()
    def _cmd():
        result = passo_playlist(api, "user-123")
        click.echo(f"RESULT={result!r}")

    # página 1: 50 playlists + link + mais = 52 opções; "mais" é a última.
    result = CliRunner().invoke(_cmd, [], input="52\n1\n", catch_exceptions=False)
    check(result.exit_code == 0, f"exit 0 (obtido {result.exit_code})")
    check("página 2" in result.output, "avança para a página 2")
    check("RESULT='0000000000000000000050'" in result.output,
          "escolhe a primeira da página 2")


def test_passo_sort_preset():
    print("passo_sort: preset artista")

    @click.command()
    def _cmd():
        spec, opts = passo_sort()
        click.echo(f"SPEC={spec}")

    result = CliRunner().invoke(_cmd, [], input="1\n",
                                catch_exceptions=False)
    check(result.exit_code == 0, f"exit 0 (obtido {result.exit_code})")
    check("SPEC=artist,album,year,trackno,title" in result.output,
          "preset 1 é a cadeia padrão")


def test_passo_sort_personalizada_ptbr():
    print("passo_sort: personalizada em pt-BR")

    @click.command()
    def _cmd():
        spec, opts = passo_sort()
        click.echo(f"SPEC={spec} OPTS={opts!r}")

    result = CliRunner().invoke(
        _cmd, [], input="7\nartista,titulo\n", catch_exceptions=False
    )
    check(result.exit_code == 0, f"exit 0 (obtido {result.exit_code})")
    check("SPEC=artista,titulo" in result.output, "aceita cadeia em pt-BR")
    check("OPTS={}" in result.output, "sem perguntas extras")

def test_passo_diff_erro_volta_singleton():
    print("passo_diff: erro mantém singletons")
    api = FakeAPI()
    original_carregar = inter.carregar
    inter.carregar = lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))

    @click.command()
    def _cmd():
        out = passo_diff(api, PID, "title", {})
        click.echo(f"IS_VOLTAR={out is VOLTAR} IS_SEGUIR={out is SEGUIR}")

    try:
        result = CliRunner().invoke(_cmd, [], input="2\n", catch_exceptions=False)
    finally:
        inter.carregar = original_carregar
    check(result.exit_code == 0, f"exit 0 (obtido {result.exit_code})")
    check("IS_VOLTAR=True" in result.output, "Voltar é o singleton VOLTAR")


def test_passo_diff_segue_para_gravar():
    print("passo_diff: mostra antes/depois e segue")
    api = FakeAPI()

    @click.command()
    def _cmd():
        out = passo_diff(api, PID, "artist,album,year,trackno,title", {})
        click.echo(f"IS_TUPLE={isinstance(out, tuple)}")

    # export? não. próximo passo: 1 = continuar.
    result = CliRunner().invoke(_cmd, [], input="n\n1\n", catch_exceptions=False)
    check(result.exit_code == 0, f"exit 0 (obtido {result.exit_code})")
    check("ANTES" in result.output and "DEPOIS" in result.output,
          "diff lado a lado")
    check("IS_TUPLE=True" in result.output, "Continuar devolve o contexto")


def test_wizard_copy_fim_a_fim():
    print("wizard: lista -> sort -> diff -> copy")
    api = FakeAPI()
    # playlist 1, preset 1, export n, continuar 1, copy 2, nome vazio,
    # criar s, outra n.
    stdin = "1\n1\nn\n1\n2\n\ns\nn\n"
    result = run_wizard(api, stdin)
    check(result.exit_code == 0, f"exit 0 (obtido {result.exit_code})")
    check("Logado como: Tester" in result.output, "mostra quem logou")
    check(len(api.created) == 1, f"criou 1 playlist (obtido {len(api.created)})")
    check(len(api.writes) == 1 and len(api.writes[0][1]) == 12,
          "gravou as 12 faixas ordenadas")
    flat = api.writes[0][1]
    original = [i["track"]["uri"] for i in ITEMS]
    check(flat != original, "ordem gravada difere da original embaralhada")
    check("OK! Nova playlist" in result.output, "confirma a criação")


def test_wizard_in_place_com_backup():
    print("wizard: in-place com backup")
    api = FakeAPI()
    workdir = tempfile.mkdtemp(prefix="wiz-")
    cwd = os.getcwd()
    os.chdir(workdir)
    try:
        # playlist 1, preset título, export n, continuar 1, in-place 1,
        # confirmar s, outra n.
        stdin = "1\n2\nn\n1\n1\ns\nn\n"
        result = run_wizard(api, stdin)
        check(result.exit_code == 0, f"exit 0 (obtido {result.exit_code})")
        backups = os.listdir(os.path.join(workdir, "backup"))
        check(len(backups) == 1, f"backup criado (obtido {backups})")
        check("OK! Playlist sobrescrita" in result.output, "confirma sobrescrita")
    finally:
        os.chdir(cwd)
        shutil.rmtree(workdir, ignore_errors=True)


def test_wizard_recusa_playlist_alheia():
    print("wizard: playlist alheia só permite copy")
    from sorting import Entry, sort_entries

    api = FakeAPI(owner="someone-else")

    @click.command()
    def _cmd():
        playlist = api.get_playlist(PID)
        items = api.get_playlist_items(PID)
        entries = [Entry.from_playlist_item(i, n) for n, i in enumerate(items)]
        ordered = sort_entries(entries)
        info = {"nao_gravaveis": [], "locais": 0, "removidas": 0,
                "dono_id": "someone-else"}
        out = passo_gravar(api, (playlist, items, entries, ordered, info),
                           "artist,album,year,trackno,title")
        click.echo(f"OUT={out!r}")

    # única opção é copy (1); nome vazio=default; s=criar; n=outra.
    result = CliRunner().invoke(_cmd, [], input="1\n\ns\nn\n",
                                catch_exceptions=False)
    check(result.exit_code == 0, f"exit 0 (obtido {result.exit_code})")
    opcoes = result.output.split("Como gravar")[1].split("[0]")[0]
    check("Sobrescrever" not in opcoes, "não oferece in-place p/ alheia")
    check(len(api.created) == 1, "cria a cópia mesmo assim")


def main() -> int:
    tests = [
        test_passo_playlist_escolhe_da_lista,
        test_passo_playlist_colar_link,
        test_passo_playlist_paginacao,
        test_passo_sort_preset,
        test_passo_sort_personalizada_ptbr,
        test_passo_diff_erro_volta_singleton,
        test_passo_diff_segue_para_gravar,
        test_wizard_copy_fim_a_fim,
        test_wizard_in_place_com_backup,
        test_wizard_recusa_playlist_alheia,
    ]
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append(f"{test.__name__}: {exc!r}")
            print(f"  FAIL {test.__name__} levantou {exc!r}")
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FALHA(S):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"todas as {len(tests)} verificações do wizard passaram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
