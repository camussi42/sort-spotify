"""Teste de integração da CLI com a API falsificada (sem rede).

Valida `preview` e `sort` de ponta a ponta: tabela, CSV, backup e lotes.

    python test_cli.py
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile

from click.testing import CliRunner

os.environ.setdefault("CLIENT_ID", "fake-client-id")
os.environ.setdefault("CLIENT_SECRET", "fake-client-secret")

import sort as sort_mod
from spotify_api import SpotifyAPI, SpotifyAPIError
from spotify_auth import AuthError
from sorting import SortSpecError

FAILURES: list[str] = []
PID = "37i9dQZF1DXcBWIGoYBM5M"

ARTISTS = ["Alpha Band", "Beta Crew", "Gamma Unit"]
OWNER_ID = "user-123"


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok   {message}")
    else:
        FAILURES.append(message)
        print(f"  FAIL {message}")


def build_items(count: int) -> list[dict]:
    """Gera uma playlist sintética embaralhada (artista/álbum fora de ordem)."""
    items = []
    for i in range(count):
        artist = ARTISTS[(i * 7) % len(ARTISTS)]
        album = f"Album {i % 5}"
        year = 1990 + (i % 5)
        items.append(
            {
                "added_at": f"2024-01-{(i % 28) + 1:02d}T10:00:00Z",
                "track": {
                    "type": "track",
                    "uri": f"spotify:track:{i:04d}",
                    "name": f"Song {i:03d}",
                    "duration_ms": 60000 + i * 1000,
                    "disc_number": 1 + (i % 2),
                    "track_number": (i % 10) + 1,
                    "artists": [{"name": artist}],
                    "album": {"name": album, "release_date": f"{year}-05-05"},
                    "is_local": False,
                },
            }
        )
    return items


ITEMS = build_items(250)
CREATED: list[dict] = []
WRITES: list[tuple] = []


def fake_request(self, method, path, *, params=None, json_body=None):
    """Substitui toda a I/O HTTP por respostas determinísticas."""
    if method == "GET" and path == "/me":
        return {"id": "user-123", "display_name": "Tester"}

    if method == "GET" and path == f"/playlists/{PID}":
        return {
            "id": PID,
            "name": "Playlist Teste",
            "snapshot_id": "snap-1",
            "owner": {"id": OWNER_ID, "display_name": "Tester"},
            "public": True,
            "collaborative": False,
            "external_urls": {"spotify": f"https://open.spotify.com/playlist/{PID}"},
            "tracks": {"total": len(ITEMS)},
        }

    if method == "GET" and path == f"/playlists/{PID}/tracks":
        offset = (params or {}).get("offset", 0)
        limit = (params or {}).get("limit", 100)
        batch = ITEMS[offset : offset + limit]
        return {
            "items": batch,
            "total": len(ITEMS),
            "offset": offset,
            "next": f"https://api.spotify.com/v1/next/{offset}"
            if offset + limit < len(ITEMS)
            else None,
        }

    if method == "POST" and path == "/users/user-123/playlists":
        created = {
            "id": f"new-{len(CREATED) + 1}",
            "name": json_body.get("name"),
            "external_urls": {
                "spotify": f"https://open.spotify.com/playlist/new-{len(CREATED) + 1}"
            },
        }
        CREATED.append(created)
        return created

    if method in ("PUT", "POST") and "/items" in path:
        WRITES.append((method, path, list(json_body.get("uris") or [])))
        return {"snapshot_id": "snap-2"}

    raise SpotifyAPIError(404, f"rota não mapeada no fake: {method} {path}")


# Sempre evita chamadas reais de rede.
SpotifyAPI.request = fake_request
sort_mod.SpotifyAPI.request = fake_request


def install_build_api(mode: str = "user"):
    def _build(force_public: bool = False):
        return sort_mod.SpotifyAPI(object()), mode

    sort_mod.build_api = _build


URL = f"https://open.spotify.com/playlist/{PID}"


def invoke(args: list[str]):
    return CliRunner().invoke(sort_mod.cli, args, catch_exceptions=False)


def _expected_uris() -> list[str]:
    from sorting import Entry, sort_entries

    entries = [Entry.from_playlist_item(item, i) for i, item in enumerate(ITEMS)]
    return [e.uri for e in sort_entries(entries, "artist,album,year,trackno,title")]


def test_preview_public():
    print("preview (modo público)")
    install_build_api("client")
    result = invoke(["preview", URL, "--public", "--limit", "5"])
    check(result.exit_code == 0, f"exit 0 (obtido {result.exit_code})")
    check("Playlist Teste" in result.output, "mostra o nome da playlist")
    check("modo: client" in result.output, "indica modo público")
    check("250" in result.output, "mostra total de faixas")
    check("Song 000" in result.output, "renderiza a tabela")


def test_preview_csv_is_sorted():
    print("preview -> CSV agrupado por artista")
    install_build_api("client")
    workdir = tempfile.mkdtemp(prefix="sortspot-")
    csv_path = os.path.join(workdir, "out.csv")
    try:
        result = invoke(["preview", URL, "--public", "--csv", csv_path])
        check(result.exit_code == 0, f"exit 0 (obtido {result.exit_code})")
        check(os.path.isfile(csv_path), "CSV criado")

        with open(csv_path, encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        check(len(rows) == 250, f"CSV tem 250 linhas (obtido {len(rows)})")

        seen: list[str] = []
        for row in rows:
            if not seen or seen[-1] != row["artistas"]:
                seen.append(row["artistas"])
        check(len(seen) == len(set(seen)),
              f"cada artista em um bloco só ({seen})")
        check(len(set(seen)) == 3, f"os 3 artistas aparecem ({set(seen)})")

        groups: list[list[dict]] = []
        current: list[dict] = []
        for row in rows:
            if current and current[-1]["artistas"] != row["artistas"]:
                groups.append(current)
                current = []
            current.append(row)
        groups.append(current)

        inside_ok = True
        for group in groups:
            # cada álbum deve aparecer em um bloco contíguo dentro do artista
            album_run: list[str] = []
            for row in group:
                if not album_run or album_run[-1] != row["album"]:
                    album_run.append(row["album"])
            if len(album_run) != len(set(album_run)):
                inside_ok = False

            # dentro de cada álbum: data, disco e faixa crescentes
            runs: list[list[dict]] = []
            current_run: list[dict] = []
            for row in group:
                if current_run and current_run[-1]["album"] != row["album"]:
                    runs.append(current_run)
                    current_run = []
                current_run.append(row)
            runs.append(current_run)

            for run in runs:
                keys = [
                    (r["data_lancamento"], int(r["disco"]), int(r["faixa"]))
                    for r in run
                ]
                if keys != sorted(keys):
                    inside_ok = False

        check(inside_ok, "artista -> álbum em blocos; em cada álbum: data/disco/faixa")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_write_copy():
    print("sort --write copy")
    CREATED.clear()
    WRITES.clear()
    install_build_api("user")
    workdir = tempfile.mkdtemp(prefix="sortspot-")
    try:
        result = invoke([
            "sort", URL, "--write", "copy", "--yes",
            "--name", "Ordenada Teste", "--backup-dir", workdir,
        ])
        check(result.exit_code == 0,
              f"exit 0 (obtido {result.exit_code}: {result.output[-300:]})")
        check(len(CREATED) == 1, f"criou 1 playlist (obtido {len(CREATED)})")
        check(CREATED and CREATED[0]["name"] == "Ordenada Teste", "nome personalizado")
        check("Nova playlist" in result.output, "imprime o link da nova")

        check(all(len(w[2]) <= 100 for w in WRITES), "todo lote tem <= 100 URIs")
        check(sum(len(w[2]) for w in WRITES) == 250, "gravou 250 URIs")
        puts = [w for w in WRITES if w[0] == "PUT"]
        posts = [w for w in WRITES if w[0] == "POST"]
        check(len(puts) == 1 and len(puts[0][2]) == 100, "1 PUT com 100 URIs")
        check(len(posts) == 2, f"2 POSTs (obtido {len(posts)})")

        flat = [uri for w in WRITES for uri in w[2]]
        check(flat == _expected_uris(), "ordem gravada == ordem calculada")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_dry_run_writes_nothing():
    print("sort --dry-run")
    CREATED.clear()
    WRITES.clear()
    install_build_api("user")
    workdir = tempfile.mkdtemp(prefix="sortspot-")
    try:
        result = invoke([
            "sort", URL, "--write", "copy", "--dry-run", "--backup-dir", workdir,
        ])
        check(result.exit_code == 0,
              f"exit 0 (obtido {result.exit_code}: {result.output[-300:]})")
        check(not CREATED and not WRITES, "não grava nada")
        check(os.listdir(workdir) == [], "não deixa arquivo para trás")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_write_requires_login():
    print("sort --write sem login")
    install_build_api("client")
    result = invoke(["sort", URL, "--write", "copy", "--yes"])
    check(result.exit_code != 0, "recusa gravar sem login")
    check("login" in result.output, "orienta rodar o login")


def test_in_place_rejects_non_owner():
    print("sort --write in-place de playlist alheia")
    global OWNER_ID
    install_build_api("user")
    workdir = tempfile.mkdtemp(prefix="sortspot-")
    original = OWNER_ID
    OWNER_ID = "someone-else"
    try:
        result = invoke([
            "sort", URL, "--write", "in-place", "--yes", "--backup-dir", workdir,
        ])
        check(result.exit_code != 0, "recusa sobrescrever playlist alheia")
        check("dono" in result.output, "explica quem é o dono")
        check(os.listdir(workdir) == [], "não cria backup ao recusar")
    finally:
        OWNER_ID = original
        shutil.rmtree(workdir, ignore_errors=True)


def test_in_place_backup_and_write():
    print("sort --write in-place (dono)")
    global OWNER_ID
    WRITES.clear()
    install_build_api("user")
    workdir = tempfile.mkdtemp(prefix="sortspot-")
    original = OWNER_ID
    OWNER_ID = "user-123"
    try:
        result = invoke([
            "sort", URL, "--write", "in-place", "--yes", "--backup-dir", workdir,
        ])
        check(result.exit_code == 0,
              f"exit 0 (obtido {result.exit_code}: {result.output[-300:]})")
        files = os.listdir(workdir)
        check(len(files) == 1 and files[0].endswith(".json"),
              f"backup criado (obtido {files})")
        if files:
            with open(os.path.join(workdir, files[0]), encoding="utf-8") as handle:
                payload = json.load(handle)
            check(payload["name"] == "Playlist Teste", "backup guarda o nome")
            check(payload["snapshot_id"] == "snap-1", "backup guarda o snapshot")
            check(len(payload["original_order"]) == 250, "backup guarda a ordem original")
            check(payload["original_order"] != payload["new_order"],
                  "ordem original difere da nova")
        check(sum(len(w[2]) for w in WRITES) == 250, "gravou 250 URIs")
        check("Playlist sobrescrita" in result.output, "confirma a sobrescrita")
    finally:
        OWNER_ID = original
        shutil.rmtree(workdir, ignore_errors=True)


def test_local_files_need_force():
    print("itens locais exigem --force")
    install_build_api("user")
    ITEMS.append({
        "added_at": "2024-01-01T00:00:00Z",
        "track": {"type": "track", "uri": "spotify:local:x", "name": "Local File",
                  "duration_ms": 1000, "is_local": True,
                  "artists": [{"name": "Zz"}],
                  "album": {"name": "Home", "release_date": "2000-01-01"},
                  "disc_number": 1, "track_number": 1},
    })
    try:
        result = invoke(["sort", URL, "--write", "copy", "--yes"])
        check(result.exit_code != 0, "aborta sem --force")
        check("--force" in result.output, "orienta usar --force")

        CREATED.clear()
        WRITES.clear()
        result = invoke(["sort", URL, "--write", "copy", "--yes", "--force"])
        check(result.exit_code == 0,
              f"com --force passa (obtido {result.exit_code}: {result.output[-300:]})")
        check(sum(len(w[2]) for w in WRITES) == 250,
              "grava só as 250 válidas (local é pulado)")
    finally:
        ITEMS.pop()


def main() -> int:
    tests = [
        test_preview_public,
        test_preview_csv_is_sorted,
        test_write_copy,
        test_dry_run_writes_nothing,
        test_write_requires_login,
        test_in_place_rejects_non_owner,
        test_in_place_backup_and_write,
        test_local_files_need_force,
    ]
    for test in tests:
        test()
        print()

    if FAILURES:
        print(f"{len(FAILURES)} teste(s) falharam:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Todos os testes de integração passaram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


