"""Regras de ordenação que imitam o sort nativo do Spotify.

A diferença em relação aos sites comuns: em vez de uma chave só (``artist``),
usamos uma **cadeia de chaves** com desempate estável. Ordenar por ``artist``
também agrupa por álbum/faixa dentro do artista — exatamente o comportamento
que deixa a playlist "organizada".

Cadeia padrão::

    artist, album, year, trackno, title
"""

from __future__ import annotations

import random
import re
import unicodedata
from dataclasses import dataclass, field

# Sentinela: valor ausente vai para o FIM da lista (padrão do app).
EMPTY_LAST = "\uffff"

DEFAULT_SORT = "artist,album,year,trackno,title"

ARTICLES = ("the", "uma", "um", "os", "as", "an", "a", "o")

SORT_ALIASES = {
    "artist": "artist",
    "artists": "artist",
    "artista": "artist",
    "artistas": "artist",
    "title": "title",
    "titulo": "title",
    "título": "title",
    "nome": "title",
    "track": "title",
    "album": "album",
    "álbum": "album",
    "album_name": "album",
    "date": "date",
    "added": "date",
    "added_at": "date",
    "data": "date",
    "duration": "duration",
    "duracao": "duration",
    "duração": "duration",
    "length": "duration",
    "year": "year",
    "ano": "year",
    "release": "year",
    "release_date": "year",
    "trackno": "trackno",
    "track_no": "trackno",
    "faixa": "trackno",
    "number": "trackno",
    "random": "random",
    "aleatorio": "random",
    "posicao": "index",
    "index": "index",
    "indice": "index",
}


def normalize(
    text,
    *,
    keep_accents: bool = False,
    ignore_the: bool = False,
) -> str:
    """Normaliza texto para comparação igual à do app (casefold + sem acento)."""
    if text is None:
        return ""
    value = str(text)
    if not keep_accents:
        value = "".join(
            ch
            for ch in unicodedata.normalize("NFKD", value)
            if not unicodedata.combining(ch)
        )
    value = value.casefold().strip()
    value = re.sub(r"\s+", " ", value)
    if ignore_the and value:
        for article in ARTICLES:
            if value.startswith(article + " "):
                value = value[len(article) + 1 :]
                break
    return value


@dataclass
class Entry:
    """Visão normalizada de um item da playlist."""

    index: int
    uri: str = ""
    name: str = ""
    artists: list[str] = field(default_factory=list)
    album: str = ""
    album_date: str = ""
    disc: int = 1
    track_no: int = 0
    duration_ms: int = 0
    added_at: str = ""
    is_local: bool = False
    is_episode: bool = False
    restricted: bool = False

    @classmethod
    def from_playlist_item(cls, item: dict, index: int) -> "Entry":
        entry = cls(index=index, added_at=item.get("added_at") or "")
        track = item.get("track")

        if not track:
            entry.restricted = True
            entry.name = "(indisponível — removida do catálogo)"
            return entry

        entry.is_local = bool(track.get("is_local"))
        entry.is_episode = track.get("type") == "episode"
        entry.uri = track.get("uri") or ""
        entry.name = track.get("name") or ""
        entry.duration_ms = int(track.get("duration_ms") or 0)

        if entry.is_episode:
            # Episódios ficam no fim quando se ordena por artista.
            entry.artists = []
            entry.album = ""
            entry.album_date = ""
            return entry

        entry.artists = [
            artist.get("name", "")
            for artist in (track.get("artists") or [])
            if artist
        ]
        album = track.get("album") or {}
        entry.album = album.get("name") or ""
        entry.album_date = album.get("release_date") or ""
        entry.disc = int(track.get("disc_number") or 1)
        entry.track_no = int(track.get("track_number") or 0)
        return entry

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    @property
    def playable(self) -> bool:
        """Pode ser gravado de volta na API? (local/restrito não pode)."""
        return bool(self.uri) and not self.is_local and not self.restricted


class SortSpecError(ValueError):
    """Cadeia de ordenação inválida."""


def parse_spec(spec: str) -> list[str]:
    """Converte ``"artist,album,year"`` em campos canônicos."""
    fields: list[str] = []
    for raw in (spec or "").split(","):
        token = raw.strip().lower()
        if not token:
            continue
        field_name = SORT_ALIASES.get(token)
        if field_name is None:
            raise SortSpecError(
                f"Chave desconhecida: '{raw.strip()}'. "
                f"Válidas: {', '.join(sorted(set(SORT_ALIASES)))}"
            )
        if field_name not in fields:
            fields.append(field_name)
    if not fields:
        raise SortSpecError("Cadeia de ordenação vazia.")
    return fields


def _text(value, opts: dict) -> str:
    """Normaliza texto; vazio vai para o fim."""
    result = normalize(
        value,
        keep_accents=opts.get("keep_accents", False),
        ignore_the=opts.get("ignore_the", False),
    )
    return result if result else EMPTY_LAST


def _year_key(date_str: str) -> tuple[int, int, int]:
    if not date_str:
        return (9999, 99, 99)
    match = re.match(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", str(date_str))
    if not match:
        return (9999, 99, 99)
    return (
        int(match.group(1)),
        int(match.group(2) or 0),
        int(match.group(3) or 0),
    )


def entry_key(entry: Entry, fields: list[str], opts: dict) -> tuple:
    """Monta a tupla de ordenação para uma entrada."""
    parts: list = []
    for name in fields:
        if name == "artist":
            names = entry.artists if opts.get("all_artists") else entry.artists[:1]
            parts.append(_text(", ".join(names), opts))
        elif name == "title":
            parts.append(_text(entry.name, opts))
        elif name == "album":
            parts.append(_text(entry.album, opts))
        elif name == "year":
            parts.append(_year_key(entry.album_date))
        elif name == "date":
            parts.append(entry.added_at or "9999-99-99T99:99:99Z")
        elif name == "duration":
            parts.append(entry.duration_ms)
        elif name == "trackno":
            parts.append((entry.disc, entry.track_no))
        elif name == "index":
            parts.append(entry.index)
        else:  # pragma: no cover - parse_spec ja valida
            raise SortSpecError(f"Chave nao suportada: {name}")
    return tuple(parts)


def sort_entries(
    entries: list[Entry],
    spec: str = DEFAULT_SORT,
    *,
    keep_accents: bool = False,
    ignore_the: bool = False,
    all_artists: bool = False,
    descending: bool = False,
    seed: int | None = None,
) -> list[Entry]:
    """Retorna uma nova lista ordenada.

    A ordenação é **estável**: empates reais mantêm a ordem original, então o
    desempate por ``added_at``/posição acontece de graça.
    """
    fields = parse_spec(spec)
    opts = {
        "keep_accents": keep_accents,
        "ignore_the": ignore_the,
        "all_artists": all_artists,
    }

    work = list(entries)

    if "random" in fields:
        if seed is not None:
            random.seed(seed)
        random.shuffle(work)
        fields = [f for f in fields if f != "random"]
        if not fields:
            return work

    return sorted(
        work,
        key=lambda entry: entry_key(entry, fields, opts),
        reverse=descending,
    )

