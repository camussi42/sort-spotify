"""Testes do núcleo: ordenação estilo Spotify, parsing e lotes de escrita.

Sem dependência de rede nem de pytest::

    python test_sorting.py
"""

from __future__ import annotations

import sys

from spotify_api import SpotifyAPI, parse_playlist_ref
from sorting import DEFAULT_SORT, Entry, SortSpecError, normalize, parse_spec, sort_entries

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok   {message}")
    else:
        FAILURES.append(message)
        print(f"  FAIL {message}")


def make(index, name, artists, album, album_date, disc=1, track_no=0, **kw):
    entry = Entry(index=index)
    entry.name = name
    entry.artists = artists
    entry.album = album
    entry.album_date = album_date
    entry.disc = disc
    entry.track_no = track_no
    entry.uri = kw.pop("uri", f"spotify:track:{index}")
    for key, value in kw.items():
        setattr(entry, key, value)
    return entry


def test_normalize():
    print("normalize")
    check(normalize("Café COM Acentuação") == "cafe com acentuacao", "casefold + acentos")
    check(normalize("  Espaço   Duplo  ") == "espaco duplo", "colapsa espaços")
    check(normalize("Álbum", keep_accents=True) == "álbum", "keep_accents mantém")
    check(normalize("The Beatles", ignore_the=True) == "beatles", "ignore_the: the")
    check(normalize("A Hard Day", ignore_the=True) == "hard day", "ignore_the: a")
    check(normalize(None) == "", "None vira string vazia")


def test_artist_sort_groups_albums():
    """Caso principal: artista agrupa e dentro dele vêm álbum/faixa."""
    print("sort por artista (padrão)")
    entries = [
        make(0, "Zebra", ["Alpha"], "Second", "2001-01-01", track_no=1),
        make(1, "Yankee", ["Alpha"], "First", "1999-05-05", track_no=2),
        make(2, "Echo", ["Beta"], "Solo", "2010-01-01", track_no=1),
        make(3, "Alpha", ["Alpha"], "First", "1999-05-05", track_no=1),
        make(4, "Mike", ["Alpha"], "Second", "2001-01-01", track_no=2),
        make(5, "Delta", ["Beta"], "Solo", "2010-01-01", track_no=2),
    ]
    ordered = sort_entries(entries, DEFAULT_SORT)
    got = [(e.artists[0], e.name) for e in ordered]
    expected = [
        ("Alpha", "Alpha"), ("Alpha", "Yankee"),
        ("Alpha", "Zebra"), ("Alpha", "Mike"),
        ("Beta", "Echo"), ("Beta", "Delta"),
    ]
    check(got == expected, f"artista -> album -> faixa (obtido: {got})")


def test_plain_artist_only_would_scatter():
    """Confirma que sort só por artista deixa o interior embaralhado."""
    print("comparação: sort só por artista")
    entries = [
        make(0, "Zebra", ["Alpha"], "Second", "2001-01-01"),
        make(1, "Yankee", ["Alpha"], "First", "1999-05-05"),
        make(2, "Echo", ["Beta"], "Solo", "2010-01-01"),
        make(3, "Alpha", ["Alpha"], "First", "1999-05-05"),
    ]
    titles = [e.name for e in sort_entries(entries, "artist")]
    # Só por artista a ordem interna de "Alpha" vem da posição original
    # (Zebra, Yankee, Alpha) — embaralhada, sem respeitar álbum/faixa.
    check(titles == ["Zebra", "Yankee", "Alpha", "Echo"],
          f"só artista -> interior embaralhado ({titles})")
    check(titles[:3] != ["Alpha", "Yankee", "Zebra"],
          "sort só por artista NÃO organiza por álbum/faixa")


def test_missing_metadata_goes_last():
    print("metadados ausentes")
    entries = [
        make(0, "Song", ["Artist"], "Album", "2000-01-01", track_no=1),
        make(1, "Podcast", [], "", "", uri="spotify:episode:1", is_episode=True),
    ]
    check(sort_entries(entries, "artist,album")[-1].name == "Podcast",
          "episódio sem artista vai para o fim")
    empty_album = [
        make(0, "B", ["A"], "Zzz", "2000-01-01"),
        make(1, "A", ["A"], "", ""),
    ]
    check(sort_entries(empty_album, "album")[-1].album == "", "álbum vazio vai p/ fim")


def test_other_sorts():
    print("outros sorts")
    entries = [
        make(0, "Charlie", ["Z"], "B", "1990-01-01", duration_ms=200000,
             added_at="2024-01-01T00:00:00Z"),
        make(1, "Alpha", ["A"], "A", "2000-01-01", duration_ms=100000,
             added_at="2022-01-01T00:00:00Z"),
        make(2, "Bravo", ["M"], "C", "1980-01-01", duration_ms=300000,
             added_at="2023-01-01T00:00:00Z"),
    ]
    check([e.name for e in sort_entries(entries, "title")]
          == ["Alpha", "Bravo", "Charlie"], "sort title")
    check([e.name for e in sort_entries(entries, "duration")]
          == ["Alpha", "Charlie", "Bravo"], "sort duration")
    check([e.name for e in sort_entries(entries, "date")]
          == ["Alpha", "Bravo", "Charlie"], "sort date")
    check([e.name for e in sort_entries(entries, "year")]
          == ["Bravo", "Charlie", "Alpha"], "sort year")
    check([e.name for e in sort_entries(entries, "title", descending=True)]
          == ["Charlie", "Bravo", "Alpha"], "sort title desc")
    check(len(sort_entries(entries, "random", seed=7)) == 3, "sort random não quebra")


def test_parse_spec():
    print("parse_spec")
    check(parse_spec("artist, album ,year") == ["artist", "album", "year"],
          "aliases e espaços")
    check(parse_spec("artista,titulo") == ["artist", "title"], "aliases em pt-BR")
    try:
        parse_spec("artist,coisa")
    except SortSpecError:
        check(True, "chave inválida levanta SortSpecError")
    else:
        check(False, "chave inválida levanta SortSpecError")


def test_parse_playlist_ref():
    print("parse_playlist_ref")
    pid = "37i9dQZF1DXcBWIGoYBM5M"
    check(parse_playlist_ref(f"https://open.spotify.com/playlist/{pid}?si=abc") == pid,
          "URL com query string")
    check(parse_playlist_ref(f"spotify:playlist:{pid}") == pid, "URI spotify:")
    check(parse_playlist_ref(pid) == pid, "ID puro")
    check(parse_playlist_ref(f"https://example.com/playlist/{pid}") == pid,
          "URL genérica")
    try:
        parse_playlist_ref("nada-aqui")
    except ValueError:
        check(True, "entrada inválida levanta ValueError")
    else:
        check(False, "entrada inválida levanta ValueError")


def test_entry_parsing():
    print("Entry.from_playlist_item")
    track_item = {
        "added_at": "2024-01-01T00:00:00Z",
        "track": {
            "type": "track", "uri": "spotify:track:1", "name": "Song",
            "duration_ms": 123000, "disc_number": 1, "track_number": 3,
            "artists": [{"name": "A"}, {"name": "B"}],
            "album": {"name": "Album", "release_date": "2001-02-03"},
        },
    }
    entry = Entry.from_playlist_item(track_item, 0)
    check(entry.artists == ["A", "B"], "artistas")
    check(entry.album == "Album" and entry.album_date == "2001-02-03", "álbum/data")
    check(entry.track_no == 3 and entry.disc == 1, "disco/faixa")
    check(entry.playable, "faixa normal é gravável")

    local = Entry.from_playlist_item(
        {"added_at": "", "track": {"type": "track", "uri": "spotify:local:x",
                                   "name": "Local", "is_local": True}}, 1)
    check(not local.playable, "arquivo local não é gravável")

    restricted = Entry.from_playlist_item({"added_at": "", "track": None}, 2)
    check(restricted.restricted and not restricted.playable, "track null é restrito")
    check(restricted.name != "", "restrito ganha rótulo legível")


def test_entry_parsing_formato_novo():
    """Fev/2026: /items devolve a faixa em "item" (não "track")."""
    print("Entry.from_playlist_item (formato novo /items)")
    novo = {
        "added_at": "2026-05-24T18:39:28Z",
        "is_local": False,
        "item": {
            "type": "track", "episode": False,
            "uri": "spotify:track:4cMs", "name": "Witch Death Cult",
            "duration_ms": 424870, "disc_number": 1, "track_number": 3,
            "artists": [{"name": "1782"}, {"name": "Acid Mammoth"}],
            "album": {"name": "Doom Sessions, Vol. 2", "release_date": "2020-09-18"},
        },
    }
    entry = Entry.from_playlist_item(novo, 0)
    check(entry.uri == "spotify:track:4cMs", "lê a faixa de 'item'")
    check(entry.name == "Witch Death Cult", "lê o nome")
    check(entry.artists == ["1782", "Acid Mammoth"], "lê os artistas")
    check(entry.album == "Doom Sessions, Vol. 2", "lê o álbum")
    check(entry.track_no == 3 and entry.disc == 1, "disco/faixa")
    check(entry.playable, "é gravável")

    local = Entry.from_playlist_item(
        {"added_at": "", "is_local": True, "item": {"type": "track",
                                                    "uri": "spotify:local:x",
                                                    "name": "Local"}}, 1)
    check(not local.playable, "local (is_local no embrulho) não é gravável")

    removida = Entry.from_playlist_item({"added_at": "", "item": None}, 2)
    check(removida.restricted and not removida.playable, "item null é restrito")


def test_write_full_batching():
    print("write_full lotes de 100")

    class _Provider:
        def get_token(self):
            return "token"

    api = SpotifyAPI(_Provider())
    calls = {"put": [], "post": []}
    api.replace_items = lambda pid, uris: calls["put"].append(list(uris))
    api.add_items = lambda pid, uris: calls["post"].append(list(uris))

    uris = [f"spotify:track:{i}" for i in range(250)]
    progress = []
    api.write_full("pid", uris, replace=True,
                   on_progress=lambda done, total: progress.append(done))

    check(len(calls["put"]) == 1, "1 chamada de PUT (replace)")
    check(len(calls["put"][0]) == 100, "PUT carrega 100 URIs")
    check(len(calls["post"]) == 2, "2 chamadas de POST")
    check(sum(len(c) for c in calls["post"]) == 150, "POSTs somam 150 URIs")
    flat = calls["put"][0] + [u for c in calls["post"] for u in c]
    check(flat == uris, "ordem preservada de ponta a ponta")
    check(progress[-1] == 250, "progresso termina em 250")

    calls["put"].clear()
    calls["post"].clear()
    api.write_full("pid", [], replace=True)
    check(calls["put"] == [[]] and not calls["post"], "lista vazia limpa a playlist")


def main() -> int:
    tests = [
        test_normalize,
        test_artist_sort_groups_albums,
        test_plain_artist_only_would_scatter,
        test_missing_metadata_goes_last,
        test_other_sorts,
        test_parse_spec,
        test_parse_playlist_ref,
        test_entry_parsing,
        test_entry_parsing_formato_novo,
        test_write_full_batching,
    ]
    for test in tests:
        test()
        print()

    if FAILURES:
        print(f"{len(FAILURES)} teste(s) falharam:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Todos os testes passaram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

