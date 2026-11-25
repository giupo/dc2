# DynaCamp — note per Claude (e per chi riprende il lavoro)

Questo file viene caricato automaticamente da Claude Code all'avvio. Serve a
ripartire da qui su qualunque PC: contesto, comandi, convenzioni, stato
attuale e prossimi passi. **Aggiornalo alla fine di ogni blocco di lavoro.**

## Chi è l'utente e come lavorare

- Parla **italiano**: rispondere in italiano. Il codice, le docstring e i
  commenti sono in inglese; README e docs in italiano (`web/README.md` e
  `tools/ui/README.md` in inglese).
- Conosce bene Python e C/C++, poco Go e Rust. Gioca a DCS World. I modelli di
  riferimento sono Falcon4/BMS (player bubble, ATO) e Hearts of Iron IV
  (granularità strategica).
- **Niente commit o push** se non li chiede esplicitamente: versiona lui.
- **Non usare `uv add` / `uv remove`** (il classificatore dei permessi li
  nega): modificare `pyproject.toml` a mano, poi `uv sync`.
- Non toccare i processi `dynacamp serve` dell'utente. I server di prova vanno
  su porte libere (per esempio `--port 50777 --api-port 8771`) e si fermano
  **per PID**, mai con `pkill -f` su pattern che potrebbero colpire altro.
- Verificare la UI con Chrome headless e un profilo isolato (`tools/ui/`), mai
  con il browser dell'utente.
- Misurare prima di dichiarare: per l'equilibrio e la calibrazione si usano
  simulazioni su più seed, che contano battaglie, perdite e conquiste.

## Cos'è

Motore headless di **campagna militare dinamica** ("DynaCamp"):
- **Core:** Python, deterministico a parità di seed, con granularità HOI4
  (province, brigate, rifornimenti, fronti).
- **Gioco:** integrazione **live** con DCS tramite un bridge Lua su TCP, con
  **player bubble** stile Falcon: ciò che è vicino ai giocatori lo simula DCS.
- **Interfacce:** API FastAPI con stream SSE, UI React/Vite/MapLibre con
  simboli NATO APP-6.

Documentazione completa:
- [README.md](README.md): tutto, compresa la "Storia del progetto" in 24 passi;
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): design;
- [docs/API.md](docs/API.md): endpoint;
- [web/README.md](web/README.md): UI;
- riferimento Sphinx in `docs/sphinx`.

## Comandi

```bash
uv sync --extra miz --group dev --group docs     # tutto (pydcs serve solo per .miz/loadouts/radio)
uv run pytest -q -W ignore::SyntaxWarning        # ~189 test in parallelo (xdist, -n auto), ~9 s; -n 0 = seriale
tools/watch-tests.sh                             # watcher: rilancia i test a ogni modifica (pytest-watcher)
lua5.1 tests/lua/test_bridge.lua src/dynacamp/adapters/dcs/lua/dynacamp_bridge.lua
cd web && npx tsc --noEmit -p . && npm run build # UI; `npm run gen:api` dopo ogni modifica all'API
uv run --group docs sphinx-build -q -b html docs/sphinx docs/sphinx/_build/html   # deve uscire senza avvisi

uv run dynacamp simulate scenarios/syria_m5.toml --days 5
uv run dynacamp serve scenarios/syria_m5.toml --no-dcs --compression 600   # UI su :8080
uv run dynacamp serve scenarios/syria_m5.toml --save syria.dcsave         # persistente: riprende se il file esiste
uv run --extra miz dynacamp build-miz scenarios/*.toml    # rigenera missions/*.miz
uv run --extra miz dynacamp loadouts                      # rigenera data/loadouts.json
uv run --extra miz dynacamp radio                         # rigenera data/radio.json
docker build -t dynacamp . && docker run --rm -p 8080:8080 -p 50555:50555 dynacamp
```

Controllo delle docstring (devono restare al 100%):

```bash
python3 -c "import ast,pathlib;print([f'{p}:{n.lineno} {n.name}' for p in pathlib.Path('src/dynacamp').rglob('*.py') for n in ast.walk(ast.parse(p.read_text())) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and not ast.get_docstring(n)])"
```

## Convenzioni e trappole note

- **Docstring** in stile Google, compatibili con Sphinx (napoleon), su ogni
  funzione e classe, comprese quelle annidate e private.
- **Bridge Lua:** se cambia, **rigenerare i `.miz`**. Lo controlla
  `test_shipped_missions_embed_the_current_bridge`. Il Lua deve girare con
  lua5.1, come DCS. I commenti lunghi si scrivono con `--[==[ ... ]==]`.
- **API:** se cambia, `npm run gen:api`. TypeScript resta fissato a ~5.9,
  perché la versione 7 non funziona con openapi-typescript.
- **`state.emit(kind, **data)`:** non usare chiavi chiamate `kind`, perché
  collidono con il primo argomento. Si usano per esempio `player_kind` e
  `delivery`.
- **Quote:** solo da `air.waypoint_altitudes` / `air.flight_altitude`
  (briefing, rotta DCS, SAM, sensori). Non usare `cruise_alt_m` direttamente
  per la quota di un volo.
- **Coordinate:** nei file degli scenari (TOML e `.roads.json` formato 2)
  solo `ll = [lat, lon]` WGS84, letti da `scenario.point` e `roads.load`.
  Dentro il motore `pos`/`Vec2` = [est, nord] sul piano del teatro = [DCS z,
  DCS x]. `pos` negli scenari solo per teatri senza proiezione (test).
  Proiezioni: `projection.py`, la cui andata e ritorno nel Caucaso perde
  ~6 m (serie troncate), mentre in Siria è al centimetro.
- **Unità di misura:** i calcoli sono sempre in SI. I testi per i giocatori
  usano `dynacamp/units.py` (`imperial` = kts/ft/NM, il default, oppure
  `metric`).
- **Nebbia di guerra:** le *decisioni* (ATO, IA, artiglieria, briefing) usano
  `systems/intel.py`, mai la verità. La *fisica* (combattimenti, ingaggi) usa
  la verità. Le viste API con `?faction=` mostrano solo i contatti.
- **Call sign DCS:** si calcolano solo con `dynacamp.callsigns`. Gli elenchi
  per tipo sono copiati da pydcs e verificati da `tests/test_callsigns.py`;
  se si aggiunge un aereo con nomi propri, aggiornare `TYPE_NAMES`.
- **Frequenze radio:** si assegnano solo con `dynacamp.radio.allocate`, lontano
  dalle ATC degli aeroporti della zona (`data/radio.json`), dalle frequenze di
  emergenza e dagli altri voli. Requisito esplicito dell'utente.
- **Comandi API:** ogni nuovo `POST` deve avere
  `dependencies=[Depends(require_token)]` (vedi `api/app.py`).
- **Endpoint SSE** (`/api/stream`, `/api/positions/stream`) sono infiniti: i
  test che percorrono gli endpoint devono escludere i path che finiscono in
  `/stream`.
- **Marker MapLibre:** usano `subpixelPositioning: true`, altrimenti
  l'animazione va a scatti di 1 px. L'animazione non passa da React.
- **Mappa base fragile:** viene da `tiles.openfreemap.org`, che uBlock Origin
  può bloccare in parte (`/planet`). `MapView` mostra un avviso; se lo stile
  non arriva, passa a uno sfondo neutro. Se si perde WebGL, `map.style`
  diventa `null` finché MapLibre non lo ricostruisce: in quel periodo `map`
  vale `null` per gli effetti React. Dettagli in `web/README.md` ("When the
  base map fails").
- **Test in parallelo** (`pytest-xdist`, `-n auto` di default): ogni test
  deve essere indipendente. Niente porte fisse (porta 0), file solo in
  `tmp_path`, niente stato globale lasciato sporco.
- **Insegne degli squadroni:** in `web/public/insignia/`, solo con licenza
  libera (pubblico dominio, CC0), e ogni file ha la sua riga in `CREDITS.md`,
  controllato da un test. Lo scenario le collega con `insignia = "<file>"` in
  `[[squadrons]]`. Mancano, perché su Wikipedia sono solo "fair use" o non ci
  sono: 31 e 47 Sqn RAF, EC 1/2, EC 1/12, 111 Filo, 338 Mira, NATO E-3A, VMA-223
  e 31° reggimento russo (questi ultimi due bloccati dal limite di Commons:
  da riprovare).
- **Bullseye:** `Faction.bullseye` (Vec2, obbligatorio) è fisso per tutta la
  campagna. Per le chiamate si usa solo `dynacamp.bullseye.label` /
  `bearing_range` (magnetiche, grande cerchio); la UI ha `bullsLabel`, che
  deve restare identica. Se cambia il bullseye di uno scenario, rigenerare i
  `.miz` (lo controlla `test_shipped_missions_have_the_scenario_bullseyes`).
  Da verificare in DCS: la declinazione approssimata
  (`MAGNETIC_VARIATION_DEG`) contro quella che DCS calcola dalla data.
- **Postazioni SAM:** un sito fermo sta nella sua `site` (centro della
  provincia, oppure 3 km lungo una strada, vedi `air_defense.place_sites`),
  non nel centro. Se crei siti fuori da scenario e sistema ADA, chiama
  `place_sites`.
- **Archivio ATO:** i package conclusi escono da `state.packages`/`flights`
  ma restano in `state.archive`/`archived_flights` (ultimi
  `air.ARCHIVE_MAX`). Così gli eventi della UI restano cliccabili
  (`GET /api/ato/packages/{id}`, `.../flights/{id}/package`). Chi cerca un volo
  per id dagli eventi deve guardare in entrambi.
- **Nuovi campi di `CampaignState`:** i salvataggi vecchi li ricevono al
  default (`persistence._add_missing_fields`). Bumpare `FORMAT_VERSION`
  solo per i cambi incompatibili (rinomine, tipi diversi).
- **Python 3.14:** il motore gira in un solo thread con `Engine.lock` (RLock);
  l'API legge sotto lock.

## Stato attuale (2026-11-25)

Tutto funziona in simulazione, ma **non è ancora stato provato in un DCS
vero**. Funzionalità implementate:

- **Scenari:** Georgia occidentale e Siria (corridoio M5), con strade reali
  (OSRM) e missioni `.miz` pronte.
- **Forze di terra:**
  - movimento su strada, battaglie a contatto, artiglieria;
  - logistica con convogli, ricomplementi e ricostituzione;
  - IA di teatro: retrovie, fianchi, fronte, offensive, riserve.
- **Aviazione:**
  - 64 tipi di aereo;
  - ATO con CAS, BAI, STRIKE, CAP, DEAD e scorte;
  - rotte attorno ai SAM noti (`systems/routing.py`, grafo di visibilità,
    deviazione max 1,6×), stazioni con l'intera orbita fuori dagli ombrelli,
    reazione in volo quando spara un SAM sconosciuto (`flight_rerouted`);
  - quello che non si evita chiede il SEAD, che fa da jammer per il package
    (ECM: in DCS non ci sono jammer IA). DEAD = STRIKE sul sito 2 min dopo il
    SEAD; `Task.DEAD` esiste solo come task di *package*. Le scorte tengono
    una barriera sul bordo del primo ombrello;
  - **scramble** (`systems/scramble.py`): un aereo nemico *noto* entro 30 NM
    da una base, o da un asset entro 20 NM da una base, fa partire 2
    intercettori in 5 min. Sono voli CAP con `target_kind == "flight"`
    (`target_id` = l'intruso), che non contano tra le stazioni CAP dell'ATO.
    Inseguono l'ultima posizione nota (`scramble.pursue`) e non entrano nei
    SAM noti. Escluse dall'allarme: i caccia nemici, gli intrusi già coperti
    da un nostro caccia e quelli già intercettati da meno di un'ora;
  - supporto: AWACS, tanker, trasporti con ponte aereo e rifornimento in
    elicottero;
  - giocatori nei voli dal menu F10;
  - numeri di package a 4 cifre stile BMS;
  - squadroni blu con nomi e call sign reali (USAF/USN/USMC/Army, RAF, NATO),
    dove possibile presi dagli enum di DCS (Lobo, Tusk, Devil...). Il call
    sign arriva alle unità IA in DCS; se il nome non è vocalizzabile, si usa
    un sostituto generico, mostrato nel briefing come "radio callsign".
- **Mission package:** briefing, waypoint, minacce note, armamento,
  frequenze e supporti. È stampabile in A4 dalla UI.
- **Difesa aerea:** 12 sistemi, rete IADS, SAM mobili su strada, SEAD.
- **Nebbia di guerra:** quadro informativo per fazione, con sensori aerei,
  terrestri, ELINT e lanci SAM.
- **Tempo reale:**
  - ciclo a tick fisso a 10 Hz;
  - pausa, play e accelerazione da 1× a 7200× (`POST /api/control`), rifiutate
    mentre un gioco è live;
  - posizioni a 5 Hz interpolate a 60 fps nella UI.
- **Unità e radio:** unità SI o kts/ft/NM; frequenze lontane dalle torri.
- **Deploy:** Dockerfile in un'immagine unica (~140 MB, utente non-root,
  healthcheck), con la campagna salvata nel volume `/data`.
  Variabili: `DYNACAMP_COMPRESSION` (compressione iniziale; vince `--compression`)
  e `DYNACAMP_API_TOKEN`.
- **Salvataggio e ripresa** (`persistence.py`): pickle gzip dell'intero
  `CampaignState`, con ripresa deterministica.
  - `simulate`/`serve` accettano un `.dcsave` al posto dello scenario.
  - `--save` salva in automatico (`--autosave-minutes`, default 10) e
    all'uscita (Ctrl-C/SIGTERM). Se il file esiste, `serve` riprende da lì
    (`--new` per ricominciare).
  - Al caricamento, ciò che teneva DCS torna al motore.
  - Dalla UI: pulsante del dischetto (`POST /api/save`, con token). Tutto passa
    da `persistence.Saver`. Lo stato del salvataggio è in `/api/campaign.save`.
- **Ordini all'ATO** (`systems/orders.py`, UI in `web/src/components/Planner.tsx`):
  - `POST /api/ato/packages` (nuovo package contro un bersaglio noto, pianificato
    come l'IA), `.../flights/{id}/route` (rotta, anche in volo),
    `.../packages/{id}/tot` e `.../cancel`. Tutti con token e `faction`
    facoltativo (assente = arbitro).
  - I rifiuti sono `OrderError` (409/404) con il motivo per il pianificatore.
  - Rotta cambiata → `state.dirty` → `UpdateRoute` con lo snapshot del volo
    (task al bersaglio) verso DCS; se il volo è `MATERIALIZING` si rimanda al
    tick dopo. I giocatori seduti ricevono il briefing nuovo
    (`Engine.brief_players`).

## Prossimi passi / problemi aperti

1. **Prova in DCS vero:** task AWACS/Tanker/Bombing, spawn su strada,
   frequenze dei gruppi, menu F10, handoff.
2. **Salvataggi:** fatti salva/carica da CLI e salvataggio dalla UI. Restano
   - slot e storico (oggi c'è un solo file), più il caricamento dalla UI;
   - formato robusto ai refactoring: il pickle si rompe se cambiano molto le
     classi di stato. Se serve, bumpare `persistence.FORMAT_VERSION`.
3. **Equilibrio:** in alcuni seed una parte conquista tutto in 4–7 giorni, per
   una spirale (perde i depositi, quindi non ha più ricomplementi). Non dipende
   dalla nebbia. Idee: rinforzi da fuori mappa, difesa più forte nelle ultime
   province, fasi e condizioni di vittoria.
4. **Sicurezza:** fatto il bearer token per i `POST` (`--api-token` /
   `DYNACAMP_API_TOKEN`, altrimenti generato e stampato come link
   `#token=`; lucchetto nella UI). Restano
   - la lettura aperta, compresa la vista dell'arbitro;
   - HTTPS, da fare con un reverse proxy;
   - token per fazione, se serviranno.
5. **Aria:** mancano
   - missioni di ricognizione per rinfrescare il quadro informativo;
   - OCA sulle basi;
   - decollo dal parcheggio nelle bolle;
   - scramble: solo il 10–15% raggiunge l'intruso (5 min di allarme,
     inseguimento puro); consumano sortite, e in Siria i package BAI calano
     di ~30%. Possibili: rotta d'intercetto anticipata, pool di allerta
     separato dal budget;
   - modello aria-aria poco letale (`A2A_KILL` 0,1 + 15 min di pausa per
     volo): un caccia lascia scappare un bombardiere che rientra;
   - da verificare in DCS: opzione ECM (`ECM_USING` = 13, `ALWAYS` = 3) dei
     voli SEAD e rotte con molti waypoint.
6. **DCS:**
   - i contatti scoperti in gioco non tornano al quadro informativo;
   - IADS coordinato (Skynet);
   - strutture statiche bersagliabili.
7. **Web:** fatti gli ordini all'ATO dal browser. Mancano
   - posti nei voli dal browser (oggi solo dal menu F10);
   - ordini alle forze di terra;
   - token per fazione: oggi chi ha il token ordina per qualunque fazione
     (la UI usa quella di "View as").

## File da ricordare

- `src/dynacamp/data/loadouts.json`, `src/dynacamp/data/radio.json`: generati
  dai dati di DCS. Vanno versionati, perché il runtime non richiede pydcs.
- `missions/*.miz`: generati. Vanno versionati e copiati in DCS.
- `tools/ui/`: script di verifica della UI in Chrome headless. Su questo PC
  serve `CHROME=chromium`. Fanno screenshot, stampa PDF e misura della
  fluidità.
