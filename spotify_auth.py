"""Autenticacao com a Web API do Spotify.

Dois modos de acesso:

- **Client Credentials**: le apenas playlists publicas e nao escreve nada.
  E o modo usado quando voce trabalha "pelo link" sem logar.
- **Authorization Code** (com servidor local de callback): le playlists publicas
  e privadas e permite criar / sobrescrever playlists.

As credenciais sao lidas do arquivo ``.env`` (CLIENT_ID, CLIENT_SECRET,
REDIRECT_URI).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

ACCOUNTS = "https://accounts.spotify.com"
_HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(_HERE, ".token.json")

DEFAULT_SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-public playlist-modify-private"
)


class AuthError(RuntimeError):
    """Erro de autenticacao com mensagem amigavel."""


def load_env(path: str | None = None) -> None:
    """Carrega variaveis de um arquivo ``.env`` para o ambiente.

    Variaveis ja presentes em ``os.environ`` nunca sao sobrescritas.
    """
    candidates: list[str] = []
    if path:
        candidates.append(path)
    candidates.append(os.path.join(_HERE, ".env"))
    candidates.append(os.path.join(os.getcwd(), ".env"))

    for candidate in candidates:
        if not candidate or not os.path.isfile(candidate):
            continue
        with open(candidate, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        return


def get_config(path: str | None = None) -> dict:
    """Le e valida as credenciais do app."""
    load_env(path)
    client_id = os.environ.get("CLIENT_ID", "").strip()
    client_secret = os.environ.get("CLIENT_SECRET", "").strip()
    redirect_uri = os.environ.get(
        "REDIRECT_URI", "http://127.0.0.1:8888/callback"
    ).strip()

    if not client_id or not client_secret or client_id.startswith("cole_"):
        raise AuthError(
            "CLIENT_ID/CLIENT_SECRET nao configurados.\n"
            "  1) Crie um app em https://developer.spotify.com/dashboard\n"
            "  2) Cadastre o Redirect URI "
            "http://127.0.0.1:8888/callback\n"
            "  3) Copie .env.example para .env e preencha."
        )

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "scopes": DEFAULT_SCOPES,
    }


def _post_token(data: dict) -> dict:
    """Envia um pedido de token padrao e devolve o JSON ou lanca AuthError."""
    try:
        resp = requests.post(
            f"{ACCOUNTS}/api/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    except requests.RequestException as exc:  # pragma: no cover - rede
        raise AuthError(f"Falha de rede com accounts.spotify.com: {exc}") from exc

    if resp.status_code != 200:
        try:
            detail = resp.json().get("error_description") or resp.json().get(
                "error", resp.text
            )
        except ValueError:
            detail = resp.text
        raise AuthError(f"Falha ao obter token ({resp.status_code}): {detail}")
    return resp.json()


class ClientCredentials:
    """Token de aplicacao: somente leitura de playlists publicas."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._token: str | None = None
        self._expires_at: float = 0.0

    @property
    def label(self) -> str:
        return "public (Client Credentials)"

    def get_token(self) -> str:
        now = time.time()
        if self._token and now < self._expires_at - 60:
            return self._token
        payload = _post_token(
            {
                "grant_type": "client_credentials",
                "client_id": self.cfg["client_id"],
                "client_secret": self.cfg["client_secret"],
            }
        )
        self._token = payload["access_token"]
        self._expires_at = now + float(payload.get("expires_in", 3600))
        return self._token

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0


class UserAuth:
    """Token de usuario, persistido em ``.token.json`` com refresh automatico."""

    def __init__(self, cfg: dict, token_file: str = TOKEN_FILE):
        self.cfg = cfg
        self.token_file = token_file
        self.data = self._read()

    def _read(self) -> dict | None:
        if not os.path.isfile(self.token_file):
            return None
        try:
            with open(self.token_file, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def _write(self) -> None:
        folder = os.path.dirname(self.token_file)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(self.token_file, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2)

    @property
    def label(self) -> str:
        return "usuario (Authorization Code)"

    @property
    def has_token(self) -> bool:
        return bool(self.data and self.data.get("refresh_token"))

    def save(self, payload: dict) -> None:
        self.data = {
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token")
            or (self.data or {}).get("refresh_token"),
            "expires_at": time.time() + float(payload.get("expires_in", 3600)),
            "scope": payload.get("scope", ""),
        }
        self._write()

    def invalidate(self) -> None:
        if self.data:
            self.data["access_token"] = None
            self.data["expires_at"] = 0

    def get_token(self) -> str:
        if not self.has_token:
            raise AuthError("Nao logado. Rode:  python sort.py login")
        now = time.time()
        if self.data.get("access_token") and now < float(
            self.data.get("expires_at", 0)
        ) - 60:
            return self.data["access_token"]
        return self._refresh()

    def _refresh(self) -> str:
        payload = _post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.data["refresh_token"],
                "client_id": self.cfg["client_id"],
                "client_secret": self.cfg["client_secret"],
            }
        )
        self.save(payload)
        return self.data["access_token"]

    def logout(self) -> bool:
        existed = os.path.isfile(self.token_file)
        if existed:
            os.remove(self.token_file)
        self.data = None
        return existed


def run_authorization_code_flow(cfg: dict, timeout: int = 180) -> dict:
    """Abre o navegador, captura o ``code`` num servidor local e troca por token."""
    redirect_uri = cfg["redirect_uri"]
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8888

    state = secrets.token_urlsafe(16)
    code_verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        )
        .decode("ascii")
        .rstrip("=")
    )

    captured: dict = {}
    done = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured["code"] = (query.get("code") or [None])[0]
            captured["state"] = (query.get("state") or [None])[0]
            captured["error"] = (query.get("error") or [None])[0]
            body = (
                "<html><body style=\"font-family:sans-serif;padding:3em\">"
                "<h2>Autenticacao concluida</h2>"
                "<p>Ja pode fechar esta aba e voltar ao terminal.</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *_args):
            return

    try:
        server = HTTPServer((host, port), _Handler)
    except OSError as exc:
        raise AuthError(
            f"Nao consegui abrir {host}:{port} para o callback: {exc}"
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": cfg["scopes"],
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    auth_url = f"{ACCOUNTS}/authorize?{urllib.parse.urlencode(params)}"

    print("\nAbrindo o navegador para o login do Spotify...")
    print("Se nao abrir, copie e abra:\n  " + auth_url + "\n")
    webbrowser.open(auth_url)

    finished = done.wait(timeout)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)

    if not finished:
        raise AuthError(f"Tempo esgotado aguardando o login ({timeout}s).")

    if captured.get("error"):
        raise AuthError(f"Spotify recusou a autorizacao: {captured['error']}")
    if captured.get("state") != state:
        raise AuthError("Estado do callback nao confere (possivel ataque CSRF).")
    if not captured.get("code"):
        raise AuthError("Nenhum code recebido do callback.")

    payload = _post_token(
        {
            "grant_type": "authorization_code",
            "code": captured["code"],
            "redirect_uri": redirect_uri,
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code_verifier": code_verifier,
        }
    )
    return payload


