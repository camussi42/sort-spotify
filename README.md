# sort-spotify

Ordena playlists do Spotify **do jeito que o app ordena** — por artista **e**
por álbum/faixa dentro do artista — em vez do A-Z seco que fazem a maioria
dos sites.

Script local em Python. Sem site, sem servidor, sem hospedagem.

## Por que é diferente

Ao ordenar só por nome de artista, o interior do bloco do artista fica
embaralhado (as faixas mantêm a ordem aleatória em que estavam). Aqui a
ordenação é uma **cadeia de chaves** com desempate estável:

```
artist, album, year, trackno, title
```

Resultado:

```
Alpha Band
  ├─ Album 0 (1990)
  │    ├─ 1-1 Song 000
  │    └─ 1-2 Song 020
  └─ Album 1 (1991)
       └─ 1-1 Song 005
Beta Crew
  ...
```

Além disso: texto normalizado com `casefold()` + remoção de acentos (e
opcionalmente ignorando artigos), faixas sem metadados indo para o fim, e
desempate estável pela ordem original.

## Requisitos

- Python 3.10+
- Um app no [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)

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

### 1. Só olhar como ficaria (sem login)

```bash
python sort.py preview "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
```

Funciona com link para playlist **pública**. Mostra uma tabela e não grava nada.

Exporte a ordem para conferir antes de gravar:

```bash
python sort.py preview "<link>" --csv ordenado.csv
python sort.py preview "<link>" --json ordenado.json
```

### 2. Logar (necessário para gravar)

```bash
python sort.py login
```

Abre o navegador, você autoriza, e o token fica salvo em `.token.json`
(refresh automático). Escopos: leitura pública/privada + modificação de
playlist. Para sair: `python sort.py logout`.

### 3. Gravar

```bash
# Cria uma NOVA playlist ordenada (mantém a original intacta)
python sort.py sort "<link>" --write copy --name "Minha ordenada"

# SOBRESCREVE a playlist original (só funciona se você for o dono)
python sort.py sort "<link>" --write in-place
```

Em `in-place` ele **sempre** salva um backup em `backup/<id>_<data>.json`
com a ordem original, o `snapshot_id` e todos os itens, antes de escrever.

Flags de segurança:

| Flag | Efeito |
| --- | --- |
| `--dry-run` | Faz tudo (leitura, cálculo) menos a chamada de escrita |
| `--yes` | Não pede confirmação |
| `--force` | Aceita perder itens locais/removidos do catálogo |
| `--backup-dir DIR` | Muda a pasta do backup |

## Opções de ordenação

```bash
--sort artist,album,year,trackno,title   # padrão
--sort title                             # título
--sort album                             # álbum
--sort date                              # data em que foi adicionada
--sort duration                          # duração
--sort random                            # embaralhar (aceita --sort random)
```

Campos aceitos (e sinônimos): `artist|artista`, `title|titulo|track`,
`album`, `date|added|data`, `duration|duracao`, `year|ano|release`,
`trackno|faixa`, `random`, `index`.

Outras opções:

```bash
--desc           ordem decrescente
--all-artists    usa todos os artistas da faixa, não só o primeiro
--ignore-the     ignora artigos no início (the, a, o, as, os)
--keep-accents   não remove acentos na comparação
--limit N        linhas no preview (padrão 20)
--public         força modo público (sem login)
```

## Como funciona a gravação

A API do Spotify **não tem endpoint de ordenar** — só ler e substituir. Então:

1. Lê todos os itens paginando (`limit=100`) até o fim.
2. Calcula a ordem nova.
3. Escreve em lotes de 100: 1 `PUT` (substitui as 100 primeiras) + N `POST`
   (acrescenta o resto).

Isso é automático e coberto por teste com playlist de 250 faixas.

## Segredos e arquivos locais

- `.env` — credenciais do app (está no `.gitignore`)
- `.token.json` — token de usuário (está no `.gitignore`)
- `backup/` — backups antes de sobrescrever

Nada disso sai da sua máquina.

## Testes

```bash
python test_sorting.py   # núcleo: ordenação, parsing, lotes
python test_cli.py       # CLI de ponta a ponta com API falsificada
```

Sem dependência de rede e sem pytest.

## Limitações conhecidas

1. **Item local (`spotify:local:`) e faixa removida do catálogo** não podem
   ser regravados pela API. O script avisa e exige `--force` para prosseguir;
   sem isso ele aborta. Isso vale para `copy` e `in-place`.
2. **Sobrescrever zera a data "adicionado em"** das faixas — a API não
   permite preservar `added_at` ao substituir. O backup guarda a ordem
   original, mas a data de adição volta a contar a partir de agora.
3. **`--write exige login` mesmo que o link seja público** — sem um token de
   usuário a API não cria nem edita playlist. Só `preview` funciona sem login.
4. **O critério secundário do Spotify não é documentado.** A cadeia padrão
   (`artist,album,year,trackno,title`) é a nossa melhor reprodução. Compare
   com o app e ajuste com `--sort` se precisar.
5. **O fluxo `login` não foi testado contra o Spotify real** por não haver
   credenciais neste ambiente — o fluxo de token (PKCE + refresh) está
   implementado mas precisa de uma primeira execução com um app válido.
