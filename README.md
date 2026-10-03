# Memex

Legge **chat Teams**, **mail Outlook**, **pagine Notion** e **link salvati** (web, Instagram, Threads, video) e ne estrae task, scadenze, decisioni, domande aperte e info utili.
L'analisi la fa un **LLM che gira in locale**, su qualsiasi endpoint compatibile OpenAI (Ollama, LM Studio, llama.cpp, vLLM).
Il risultato è un report Markdown/JSON e, se vuoi, una nota nel tuo vault **Obsidian**.

Cosa esce dalla tua macchina: solo le chiamate alle API di Microsoft Graph e Notion (servono per leggere i tuoi dati) e il download dei link che salvi tu. I contenuti non vanno a nessun LLM in cloud.

## Installazione

Serve Python 3.11 o superiore.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .                 # base: web, Teams, Outlook, Notion
pip install -e '.[social]'       # + Instagram/YouTube/TikTok via yt-dlp (consigliato)
pip install -e '.[pdf]'          # + testo dai PDF linkati
pip install -e '.[render]' && playwright install chromium   # + pagine che vogliono JavaScript
pip install -e '.[transcribe]'   # + trascrizione audio di reel/video con faster-whisper
python -m digest init            # crea config.toml e links.txt
```

## 1. LLM locale

Imposta `[llm] base_url` e `model` in `config.toml`, poi:

```bash
python -m digest check
```

**Occhio al contesto con Ollama.** Il default è piccolo e i blocchi da ~12.000 caratteri vengono troncati senza avvisare. Avvialo con un contesto più ampio:

```bash
OLLAMA_CONTEXT_LENGTH=16384 ollama serve
```

Con LM Studio imposti la "Context Length" quando carichi il modello. Con un contesto più piccolo, abbassa `chunk_chars`.

**Modelli.** Un 12–14B instruct che segua bene le istruzioni e gestisca l'italiano (Qwen, Gemma, Mistral Small) basta e avanza. I 7–8B funzionano, ma perdono più dettagli. Per leggere il testo nelle immagini dei post imposta `vision_model` su un modello multimodale (es. `qwen2.5vl:7b`).

I modelli "reasoning" che rispondono con `<think>…</think>` sono gestiti. Se il tuo server non accetta `response_format`, il tool se ne accorge da solo e riprova senza. Puoi anche impostare `json_mode = "none"`.

## 2. Outlook e Teams (Microsoft Graph)

Serve una *app registration* tua sul tenant, solo con permessi **delegati**: il tool legge quello che vedi tu, niente di più.

1. Vai su [Entra admin center](https://entra.microsoft.com) → *App registrations* → *New registration*, scegli "single tenant" e non mettere nessun redirect URI.
2. In *Authentication* → *Advanced settings*, imposta **Allow public client flows = Yes**. Serve per il login con codice.
3. In *API permissions* → *Microsoft Graph* → *Delegated*, aggiungi `User.Read`, `Mail.Read` e `Chat.Read`.
   - Solo se vuoi anche i **canali** dei team aggiungi `Team.ReadBasic.All`, `Channel.ReadBasic.All` e `ChannelMessage.Read.All`. Questi richiedono il **consenso di un admin**.
4. Copia *Application (client) ID* e *Directory (tenant) ID* in `[graph]`, poi abilita `[mail]` e/o `[teams]`.
5. Lancia `python -m digest login`. Una volta sola: ti dà un codice da inserire su microsoft.com/devicelogin. Il token viene salvato in `data_dir` e si rinnova da solo.

Note:
- Se la tua azienda blocca il consenso degli utenti alle app, il punto 3 lo deve approvare l'IT. Non c'è modo pulito di aggirarlo, e se ci provi poi lo spieghi tu all'IT.
- Le chat Teams **personali** (account Microsoft consumer) non sono esposte da Graph. Funziona solo con account di lavoro o scuola.
- Se il refresh token scade (tipicamente dopo 90 giorni di inattività o per policy aziendali), i run schedulati si fermano con un messaggio chiaro. Rilanci `login` e riparti.

## 3. Notion

1. Su [notion.so/profile/integrations](https://www.notion.so/profile/integrations) crea una *internal integration* in sola lettura e copia il token.
2. Esportalo come `NOTION_TOKEN=secret_...`, oppure mettilo in `[notion] token`.
3. **Condividi con l'integrazione** le pagine o i database da leggere: menu `•••` → *Connections* → la tua integrazione. Le sottopagine ereditano l'accesso.

Il tool prende le pagine modificate dall'ultimo run e salta quelle dove sono cambiati solo i metadati e non il contenuto.

## 4. Link salvati

Aggiungi i link a `links.txt`, uno per riga, con una nota facoltativa che aiuta il modello a capire perché l'hai salvato:

```
https://www.instagram.com/reel/ABC123/   setup scrivania
https://www.threads.com/@utente/post/XYZ
https://blog.esempio.it/articolo-interessante
```

In alternativa: `python -m digest add URL "nota"`. Ogni link viene elaborato una volta sola. Quelli falliti vengono riprovati fino a `max_attempts`; li vedi con `python -m digest links` e li rimetti in coda con `links --retry`.

Come estrae il contenuto:

| Tipo | Metodo |
|---|---|
| Web | `trafilatura` (testo dell'articolo, senza menu e cookie banner); se è troppo corto e `use_playwright = true`, rende la pagina con Chromium headless |
| Instagram | `yt-dlp` (didascalia, autore), poi meta tag `og:` come ripiego; con `vision_model` legge anche l'immagine o la copertina, con `transcribe` trascrive l'audio dei reel |
| Threads | meta tag della pagina (testo del post); con `use_playwright` il testo renderizzato; con `vision_model` le immagini |
| YouTube/TikTok/Vimeo | `yt-dlp` (titolo, descrizione) più trascrizione opzionale |
| PDF | `pypdf` |

**Instagram** spesso vuole il login. Se i post risultano vuoti, imposta `cookies_from_browser = "firefox"` (o `"chrome"`): yt-dlp usa la sessione del browser in cui sei già loggato. Usalo per leggere i tuoi salvataggi, non per fare scraping a tappeto, che Meta non gradisce.

## 5. Obsidian (opzionale)

```toml
[obsidian]
enabled = true
vault = "~/Documents/Obsidian/MioVault"
```

Ad ogni run il tool:
- aggiunge una sezione alla nota del giorno `Digest/AAAA-MM-GG.md`. Le voci di "Tocca a te" e "Scadenze" sono task nel formato del plugin **Tasks** (`- [ ] … 📅 2026-10-03 ⏫`), quindi le ritrovi nelle tue query;
- crea una nota per ogni link in `Digest/Link/` con frontmatter (`url`, `tags`, `rilevanza`), comoda da interrogare con Dataview, e la collega con `[[wikilink]]`.

## Interfaccia grafica

```bash
cd frontend && npm install && npm run build && cd ..   # una volta
python -m digest serve                                  # http://127.0.0.1:8765
```

Solo `127.0.0.1`: i dati restano sulla macchina. Sezioni: **Dashboard** (documenti importati, quando, grafo dei documenti), **Link** (incolli gli URL, uno per riga, con titolo o descrizione facoltativi dopo l'URL), **Chat** (Teams, Slack), **Email** (Outlook, Gmail), **Ticket** (Jira, canali Slack di supporto). Ogni "Ingerisci" lancia `digest run --sources ...` e mostra il log in diretta.

**Grafo.** I documenti sono collegati solo da tag che scegli tu nel pannello "Tag di collegamento": i tag liberi del modello (`#ricette`) restano etichette e non creano archi. Opzionali: collegamento per stesso dominio. Gli elementi estratti si attaccano al documento di origine.

Sviluppo (backend + frontend con hot reload, con il venv attivo e `pip install -e .` già fatto):

```bash
cd frontend && npm install && npm run run    # avvia `python -m digest serve` (8765) e Vite (http://localhost:5173)
```

Ctrl+C ferma entrambi. Lo script usa `sh`: su Windows servono Git Bash o WSL, oppure due terminali con `python -m digest serve` e `npm run dev`. Typecheck: `npm run typecheck`.

## Immagini

Per ogni link tiene **una** immagine di copertina, scelta in quest'ordine: `image` del JSON-LD, `og:image`, `twitter:image`, miniatura di yt-dlp. Sta in `data_dir/media/<sha1>.<ext>` (la stessa immagine si salva una volta sola), compare come miniatura nella coda link e nel dettaglio del grafo, e con `[obsidian] copy_media = true` viene copiata nel vault (`Digest/Link/media/`) e incorporata nella nota.

Il download è protetto perché l'URL viene da contenuti non tuoi: solo https sulla porta standard, ogni indirizzo risolto deve essere pubblico (niente rete di casa o localhost), i redirect si controllano a ogni salto, limite di dimensione mentre scarica (`max_image_bytes`) e formato deciso dai byte del file, non dal Content-Type. SVG rifiutato perché può contenere script. Se un'immagine non è utilizzabile il link viene analizzato lo stesso. Con "Elimina" spariscono anche il file e la copia nel vault, a meno che un altro link la usi ancora.

Se Python non si fida del certificato di un sito (`CERTIFICATE_VERIFY_FAILED`), tipicamente un proxy aziendale che intercetta il TLS, il link fallisce con quel messaggio: la verifica non viene mai disattivata. Imposta `SSL_CERT_FILE` con il certificato della tua azienda.

## Screenshot e documenti (PDF)

Dalla sezione **Link** della GUI puoi trascinare, scegliere o incollare (Ctrl+V) **screenshot** (PNG, JPEG, WebP, fino a 10 MB) e **PDF** (fino a 50 MB), con un titolo o una nota facoltativi. Solo caricamento manuale: nessuna cartella viene monitorata. Servono `[llm] vision_model` (es. `gemma3:4b`) per gli screenshot e per le pagine scansionate.

- **Screenshot**: il modello di visione trascrive il testo e descrive l'immagine, poi segue la normale analisi (riassunto, tag, scadenze e task).
- **PDF di testo**: il testo si legge con `pypdf`. **PDF di immagini (scansioni)**: la decisione è per pagina (poco testo = scansione); l'immagine della pagina si prende dal PDF e la legge il modello di visione. Si leggono le prime `max_pdf_pages` pagine (default 15) e il troncamento è dichiarato nel testo.
- Le scansioni sono supportate quando l'immagine nel PDF è un JPEG (scanner e app per telefono lo usano quasi sempre). Per gli altri formati servirebbe una libreria di immagini (Pillow o PyMuPDF), che **non è inclusa**: la pagina viene segnalata come non leggibile, senza inventare nulla.
- Dai documenti e dagli screenshot escono anche scadenze e task (es. scadenza di un'assicurazione), collegati al documento.
- Sicurezza: il PDF viene solo letto, mai eseguito; controllo che sia davvero un PDF e il limite di dimensione; se è protetto da password lo rifiuto (la password non viene mai chiesta né salvata). Il file originale è scaricabile dalla GUI.
- Con `[obsidian] copy_documents = true` il PDF viene copiato nel vault e incorporato nella nota. "Elimina" toglie il file, la copia nel vault, la nota, la copertina e gli elementi estratti.
- Il modello di visione sbaglia sui numeri lunghi (IBAN, codici): controlla i dati importanti sull'originale.

## Ricette

Una ricetta non viene riassunta: viene **elencata** (ingredienti con le quantità, preparazione numerata e gli eventuali consigli dell'autore), senza "Da provare" né "cerca altre ricette". Si riconosce in due modi: dal `Recipe` schema.org (JSON-LD) della pagina, che è un dato esatto e **vince sul modello**, oppure dal modello stesso (didascalie, screenshot, PDF).

- **Ingredienti completi**: se nel testo c'è un elenco ben formato ("Ingredienti: ..."), viene letto così com'è e completa quello che un modello piccolo salta. Le quantità si riportano come scritte, con le frasi originali ("1 spicchio d'aglio"): una quantità che non compare nel testo viene scartata invece di fidarsi del modello.
- **Tag**: `cucina-<tipo>` (es. `cucina-italiana`) più uno per ingrediente (`pomodoro`, `basilico`...), mai `ingredienti`. Come tutti i tag usano il vocabolario condiviso, quindi `pomodori` diventa `pomodoro` se esiste già, e non collegano nulla finché non li scegli tu.
- Le ricette non entrano negli "In evidenza" e non generano task. Nella GUI si leggono da "Dettagli" (coda link) o dal pannello del grafo.
- **Rielabora**: il bottone nella coda link rimette un link già fatto in coda (i tag che hai messo a mano restano), utile per rifare con il nuovo formato quello che avevi già ingerito.

## Aree

Le aree sono macroargomenti scelti da te (di partenza: Ricette, Software, Posti, Progetti, Lavoro, Documenti, Salute, più **Altro**, che non si può eliminare). Ogni informazione ha **una sola** area, scelta dal modello da quella lista chiusa; se non ne trova una adatta usa Altro, e **non resta mai senza area**. Le aree servono a raggruppare, colorare e filtrare il grafo: **non creano collegamenti**.

- **Proposte**: se nessuna area calza, il modello può suggerirne una nuova. Non diventa un'area finché non la approvi nella sezione **Aree** (puoi cambiare nome, descrizione e colore). Se la rifiuti, non verrà più riproposta.
- **Approvazione**: parte un job che rivede **tutto l'archivio** con i dati già salvati (titolo, riassunto, tag), senza riscaricare né reingerire, e sposta nella nuova area **solo ciò che ci rientra**. Non sposta altro e le aree che hai scelto tu a mano non vengono mai toccate. Si può rilanciare: `python -m digest reclassify --area nome`.
- **A mano**: dal dettaglio di un nodo del grafo cambi l'area (resta "scelta da te" anche dopo una rielaborazione). Puoi anche aggiungere un'area tua (attiva subito), modificarla ed eliminarla (le sue voci passano ad Altro).
- **Grafo**: colore per area o per sorgente, chip per mostrare/nascondere un'area, opzione "isole per area".
- **Obsidian**: per ogni area puoi scegliere "fuori dal vault" (di default nessuna lo è): niente note, righe né immagini di quell'area, e niente "In evidenza" né conteggi in quel run, perché sono testo libero. "Ripulisci il vault" toglie ciò che c'è già, senza toccare database e report.
- Le ricette vanno in Ricette, i PDF e gli screenshot seguono la normale classificazione.

## Tag

Ogni informazione ha almeno un tag (link, mail, chat, ticket, Notion). I tag servono anche al modello locale: a ogni estrazione gli vengono passati i tag già in uso (prima quelli di collegamento che hai scelto, poi i più usati, al massimo ~150) con l'istruzione di riusare quello esistente se è semanticamente adatto. Un tag nomina un argomento, strumento o progetto citato nel testo, quindi `#onfeather-free` può marcare sia il progetto OnFeather sia i progetti che lo usano.

Dopo la risposta del modello, senza nuove chiamate, le varianti vengono riportate al tag esistente (maiuscole, accenti, singolare/plurale: `ricetta` → `ricette`). Se il modello non dà tag utilizzabili, vengono messi due tag automatici (sorgente e tipo, per i link `link` e il dominio): nella GUI hanno il bordo tratteggiato e non entrano mai nel vocabolario del modello. Nella GUI puoi aggiungere e togliere tag a mano (non si può togliere l'ultimo); quelli manuali non vengono sovrascritti se rielabori un link.

Come sempre, un tag crea collegamenti nel grafo solo se lo scegli tu in "Tag di collegamento".

## Slack, Gmail, Jira

- **Slack**: crea una app con token utente (`xoxp-`) o bot (`xoxb-`, da invitare nei canali) con gli scope di sola lettura elencati in `config.example.toml`. `channels` per le chat, `ticket_channels` per i canali di supporto.
- **Gmail**: IMAP in sola lettura con una *password per app* (richiede la verifica in due passaggi). Non usare la password dell'account.
- **Jira Cloud**: email + API token da id.atlassian.com. I ticket modificati dall'ultimo run vengono riletti, quelli identici saltati.

I segreti meglio da variabili d'ambiente: `SLACK_TOKEN`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`.

## Uso

```bash
python -m digest run                         # tutte le sorgenti abilitate
python -m digest run --sources links         # solo i link
python -m digest run --dry-run               # scarica e salva in reports/dryrun i testi che andrebbero al modello
python -m digest run --since 2026-09-20 --reprocess --no-advance   # rianalizza un periodo senza toccare lo stato
```

L'output finisce in `reports/digest_AAAA-MM-GG_HHMM.md` e `.json`, più `reports/latest.md`. Il report contiene: In evidenza, Tocca a te, Scadenze, Task di altri, Domande aperte, Decisioni, Info utili, Idee, Link salvati, Problemi.

L'esecuzione è **incrementale**: per ogni sorgente il tool salva un cursore e gli ID già analizzati in `data_dir/state.sqlite`. Se il modello fallisce su un blocco, quei documenti non vengono segnati come visti e tornano al run successivo. Se una sorgente fallisce, le altre vanno avanti comunque; il codice di uscita è 1 solo se almeno una sorgente è fallita del tutto.

Per adattare l'estrazione ai tuoi interessi modifica `[extract] focus`. I prompt sono in `digest/extract.py` se vuoi metterci le mani.

## Docker

Un solo container: il frontend viene compilato nello stage di build e servito dallo stesso processo Python dell'API, sulla porta 8765.

```bash
mkdir config
cp docker/config.docker.toml config/config.toml     # poi modificala (LLM, abilita le sorgenti che usi)
cp .env.example .env                                 # segreti e token: non va mai committato
docker compose up -d --build                         # GUI + API su http://127.0.0.1:8765
```

- **Immagine**: build in due stadi (Node per il frontend, `python:3.12-slim` per l'app), utente non privilegiato, filesystem in sola lettura, nessuna capability, `no-new-privileges`. Include `yt-dlp` e `pypdf`; **non** Chromium né Whisper (sono enormi): `--build-arg EXTRAS=pdf` toglie anche `yt-dlp`.
- **Dati**: stato, immagini, documenti, report, cache del token Microsoft e `links.txt` stanno nel volume `digest-data` (`/data`): è l'unica cosa da salvare. La configurazione è in `./config/config.toml`, montata in sola lettura.
- **LLM**: dentro il container `localhost` è il container stesso. Usa `http://host.docker.internal:11434/v1` (Ollama sull'host) oppure l'indirizzo del tuo server Ollama in LAN, in `[llm] base_url`.
- **Accesso**: la porta è pubblicata **solo su loopback** (`127.0.0.1:8765`): la GUI legge mail e chat e può cancellare dati. Per usarla da un'altra macchina pubblica la porta sull'indirizzo LAN **e** imposta `DIGEST_GUI_TOKEN` (HTTP Basic: qualsiasi utente, il token come password) e `DIGEST_ALLOWED_HOSTS` con l'indirizzo con cui la raggiungi. Il server **rifiuta di partire** su un'interfaccia non loopback senza token (la compose lo consente solo perché pubblica su loopback: `DIGEST_ALLOW_UNAUTHENTICATED=1`). Il token viaggia in chiaro su HTTP: fuori dalla LAN metti un reverse proxy con TLS davanti.
- **Segreti**: `SLACK_TOKEN`, `GMAIL_APP_PASSWORD`, `JIRA_API_TOKEN`, `NOTION_TOKEN`... nel file `.env` (la compose li passa al container). Nel `config.toml` basta abilitare la sezione.
- **Login Microsoft** (Teams, Outlook), una volta sola: `docker compose run --rm digest login`.
- **Run periodici**: `docker compose run --rm digest run` dal cron dell'host (come nella sezione sotto). Non farlo partire mentre c'è un ingest avviato dalla GUI: il blocco "un solo job alla volta" vale solo dentro la GUI.
- **Obsidian**: monta il vault (`- /percorso/Vault:/vault` in `docker-compose.yml`) e imposta `[obsidian] vault = "/vault"`.
- **Rete con TLS intercettato** (proxy aziendale): monta il certificato e decommenta `SSL_CERT_FILE` nella compose, altrimenti i siti falliscono con `CERTIFICATE_VERIFY_FAILED`.
- **Salute**: l'endpoint `/healthz` non richiede il token e non restituisce dati; è quello che usa l'`HEALTHCHECK`.

## Sviluppo

```bash
pip install -e ".[social,pdf]"
python -m unittest discover -s tests -t .        # test del backend
cd frontend && npm ci && npm run typecheck && npm run build
```

La CI di GitHub (`.github/workflows/ci.yml`) esegue gli stessi controlli e costruisce l'immagine Docker.

## Esecuzione periodica

**Linux/macOS (cron)**, ogni giorno feriale alle 8:30 e alle 14:00:
```cron
30 8,14 * * 1-5  cd /percorso/memex && .venv/bin/python -m digest run >> reports/cron.log 2>&1
```

**Windows (Utilità di pianificazione)**:
```powershell
schtasks /Create /TN "Memex" /SC DAILY /ST 08:30 /TR "cmd /c cd /d C:\percorso\memex && .venv\Scripts\python.exe -m digest run"
```

Il primo `login` va fatto a mano da terminale. I run schedulati non sono interattivi e usano il token in cache.

## Struttura

```
digest/
  __main__.py      CLI (init, login, check, add, links, run, serve, reclassify)
  config.py        caricamento TOML + override da env
  llm.py           client OpenAI-compatibile, parsing JSON robusto, visione
  extract.py       prompt, suddivisione in blocchi, normalizzazione e dedup
  areas.py         aree (macro-argomenti), proposte e riclassificazione
  tags.py          vocabolario condiviso dei tag
  recipes.py       ricette strutturate (JSON-LD + modello)
  media.py         download sicuro e archivio delle immagini di copertina
  purge.py         cancellazione sicura da database, report e vault
  report.py        report Markdown/JSON
  obsidian.py      scrittura nel vault
  state.py         SQLite: cursori, visti, coda link, storico
  web/             server HTTP locale + servizio (statistiche, grafo, job)
  sources/
    msgraph.py     Outlook + Teams (MSAL device code)
    notion.py      API Notion
    slack.py       canali e DM Slack (chat e ticket)
    gmail.py       Gmail via IMAP (sola lettura)
    jira.py        ticket Jira Cloud
    links.py       web / Instagram / Threads / video
    documents.py   PDF (testo o scansioni)
frontend/          React + Vite + TypeScript (nessuna libreria di grafi o grafici)
docker/            entrypoint e config di esempio per il container
tests/             test del backend (unittest)
```

Test: `python -m unittest discover -s tests -t .`

## Licenza

MIT, con obbligo di menzione dell'autore: vedi [LICENSE.md](LICENSE.md). Copyright (c) 2026 Riccardo Giordano (RickyLeRoi).
