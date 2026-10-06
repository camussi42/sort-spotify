![](./assets/logo.png)
Ordena playlists do Spotify **do jeito que o app ordena** — ou do jeito que você preferir :)

Script local em Python. Sem site, sem servidor, sem hospedagem.



## Requisitos

- [Python 3.10+](https://www.python.org/downloads/)
- [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)

## Instalação

```bash
pip install -r requirements.txt
```

Crie o app no Dashboard e cadastre em **Redirect URIs** exatamente:

```
http://127.0.0.1:8888/callback
```

Depois copie as credenciais:

```bash
copy .env.example .env     # Windows
# cp .env.example .env     # macOS/Linux
```

E preencha `CLIENT_ID` e `CLIENT_SECRET` no `.env`.

## Uso

```bash
pip install -r requirements.txt
python sort.py
```


## Limitações conhecidas

1. **Item local (`spotify:local:`) e faixa removida do catálogo** não podem
   ser regravados pela API. 
2. **Sobrescrever zera a data "adicionado em"** das faixas — a API não
   permite preservar `added_at` ao substituir. O backup guarda a ordem
   original, mas a data de adição volta a contar a partir de agora.
3. **`--write exige login` mesmo que o link seja público** — sem um token de
   usuário a API não cria nem edita playlist. Só `preview` funciona sem login.
4. **O critério secundário do Spotify não é documentado.** A cadeia padrão
   (`artist,album,year,trackno,title`) é a nossa melhor reprodução. Compare
   com o app e ajuste com `--sort` se precisar.

