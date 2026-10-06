"""Cliente mínimo para a Web API do Spotify.

Foco em duas coisas que a API do Spotify exige:

1. **Leitura paginada** — itens de playlist vêm 100 a 100.
2. **Escrita em lotes** — ``PUT`` substitui no máx. 100 URIs por vez; o resto
   entra com ``POST`` em lotes de 100.
"""

from __future__ import annotations

import re
import time

import requests

BASE = "https://api.spotify.com/v1"
MAX_BATCH = 100

# Caminhos atuais e legados (a API renomeou /tracks -> /items em escrita).
_READ_PATHS = ("tracks", "items")
_WRITE_PATHS = ("items", "tracks")


class SpotifyAPIError(RuntimeError):
    """Erro da API com status legível."""

    def __init__(self, status: int, message: str):
        super().__init__(f"Spotify API {status}: {message}")
        self.status = status
        self.message = message


_PLAYLIST_ID_RE = re.compile(r"playlist[/:]([A-Za-z0-9]{22})")
_RAW_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


def parse_playlist_ref(ref: str) -> str:
    """Extrai o ID de uma URL, URI ``spotify:playlist:`` ou ID puro."""
    ref = (ref or "").strip().strip("'\"")
    if not ref:
        raise ValueError("Link/URI da playlist vazio.")
    if _RAW_ID_RE.match(ref):
        return ref
    match = _PLAYLIST_ID_RE.search(ref)
    if match:
        return match.group(1)
    match = re.search(r"/playlist/([A-Za-z0-9]{22})", ref)
    if match:
        return match.group(1)
    raise ValueError(f"Nao consegui extrair o ID da playlist de: {ref}")


class SpotifyAPI:
    """Cliente com retry/backoff e paginação automática."""

    def __init__(self, token_provider):
        self._tp = token_provider
        self.session = requests.Session()

    # ------------------------------------------------------------------ http
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ):
        url = path if path.startswith("http") else f"{BASE}{path}"
        last_error: SpotifyAPIError | None = None

        for attempt in range(6):
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {self._tp.get_token()}"},
                    timeout=60,
                )
            except requests.RequestException as exc:  # pragma: no cover - rede
                last_error = SpotifyAPIError(0, f"erro de rede: {exc}")
                time.sleep(min(2 ** attempt, 16))
                continue

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "1") or "1")
                time.sleep(max(1, min(wait, 60)))
                continue

            if resp.status_code >= 500:
                last_error = SpotifyAPIError(resp.status_code, "erro do servidor")
                time.sleep(min(2 ** attempt, 16))
                continue

            if resp.status_code not in (200, 201, 202, 204):
                try:
                    payload = resp.json()
                    message = payload.get("error", {})
                    message = message.get("message") if isinstance(message, dict) else message
                    message = message or resp.text
                except ValueError:
                    message = resp.text
                raise SpotifyAPIError(resp.status_code, message)

            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()

        raise last_error or SpotifyAPIError(429, "limite de requisicoes")

    def _write(self, method: str, playlist_id: str, body: dict):
        """Tenta o caminho novo e cai para o legado em 404/405."""
        last: SpotifyAPIError | None = None
        for suffix in _WRITE_PATHS:
            try:
                return self.request(
                    method, f"/playlists/{playlist_id}/{suffix}", json_body=body
                )
            except SpotifyAPIError as exc:
                if exc.status in (404, 405):
                    last = exc
                    continue
                raise
        raise last or SpotifyAPIError(404, "endpoint de escrita nao encontrado")

    def _read(self, playlist_id: str, params: dict):
        last: SpotifyAPIError | None = None
        for suffix in _READ_PATHS:
            try:
                return self.request(
                    "GET", f"/playlists/{playlist_id}/{suffix}", params=params
                )
            except SpotifyAPIError as exc:
                if exc.status in (404, 405):
                    last = exc
                    continue
                raise
        raise last or SpotifyAPIError(404, "endpoint de leitura nao encontrado")

    # ----------------------------------------------------------- operacoes
    def get_current_user(self) -> dict:
        return self.request("GET", "/me")

    def get_playlist(self, playlist_id: str) -> dict:
        fields = (
            "id,name,description,owner(id,display_name),public,collaborative,"
            "snapshot_id,tracks(total)"
        )
        return self.request(
            "GET",
            f"/playlists/{playlist_id}",
            params={"fields": fields},
        )

    def get_playlist_items(
        self, playlist_id: str, on_progress=None
    ) -> list[dict]:
        """Le todos os itens, paginando de 100 em 100."""
        items: list[dict] = []
        offset = 0
        total: int | None = None

        while True:
            data = self._read(
                playlist_id,
                {
                    "limit": 100,
                    "offset": offset,
                    "additional_types": "track,episode",
                },
            )
            batch = data.get("items") or []
            items.extend(batch)
            total = data.get("total", total)
            offset += len(batch)

            if on_progress:
                on_progress(len(items), total or len(items))

            if not data.get("next") or not batch:
                break

        return items

    def create_playlist(
        self,
        user_id: str,
        name: str,
        *,
        public: bool = False,
        description: str = "",
    ) -> dict:
        return self.request(
            "POST",
            f"/users/{user_id}/playlists",
            json_body={
                "name": name,
                "public": public,
                "collaborative": False,
                "description": description,
            },
        )

    def replace_items(self, playlist_id: str, uris: list[str]) -> dict:
        """Substitui TODOS os itens (max. 100 por chamada)."""
        return self._write("PUT", playlist_id, {"uris": list(uris)})

    def add_items(self, playlist_id: str, uris: list[str]) -> dict:
        """Acrescenta itens ao final (max. 100 por chamada)."""
        return self._write("POST", playlist_id, {"uris": list(uris)})

    def write_full(
        self,
        playlist_id: str,
        uris: list[str],
        *,
        replace: bool = True,
        on_progress=None,
    ) -> None:
        """Grava a lista inteira: 1x PUT (substitui) + N x POST (acrescenta)."""
        chunks = [
            uris[i : i + MAX_BATCH] for i in range(0, len(uris), MAX_BATCH)
        ]
        if not chunks:
            chunks = [[]]

        if replace:
            self.replace_items(playlist_id, chunks[0])
            done = len(chunks[0])
        else:
            done = 0
            chunks = chunks or [[]]

        if on_progress:
            on_progress(done, len(uris))

        for chunk in (chunks[1:] if replace else chunks):
            if not chunk:
                continue
            self.add_items(playlist_id, chunk)
            done += len(chunk)
            if on_progress:
                on_progress(done, len(uris))


