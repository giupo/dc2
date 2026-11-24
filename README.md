# DynaCamp

Motore di **campagna militare dinamica** *headless*. La campagna va avanti da
sola: fronti, logistica, guerra aerea e difesa aerea. I giochi si collegano
tramite **adapter**, DCS World per primo, in modalità live. Si ispira a:

- **Hearts of Iron IV** per la granularità strategica: province, brigate,
  organizzazione, rifornimenti, linee del fronte;
- **Falcon 4 / BMS** per la **player bubble**: quello che è vicino a un
  giocatore lo simula il gioco, il resto lo simula il motore.

Il motore espone un'**API HTTP** (FastAPI, con schema OpenAPI completo) e ha
un'**interfaccia web** React con mappa reale e simbologia NATO.

> Stato: prototipo funzionante, con 92 test automatici (Python e bridge Lua)
> e verifiche visive della UI. **Non è ancora stato provato in DCS vero**: il
> bridge Lua è testato contro mock dell'ambiente DCS, e le missioni `.miz` e i
> nomi delle unità sono verificati con pydcs.

## Indice

- [Cosa fa](#cosa-fa)
- [Installazione](#installazione)
- [Avvio rapido](#avvio-rapido)
- [Il modello di campagna](#il-modello-di-campagna)
- [DCS World](#dcs-world)
- [Interfaccia web](#interfaccia-web)
- [API HTTP](#api-http)
- [Scenari](#scenari)
- [Comandi e strumenti](#comandi-e-strumenti)
- [Documentazione](#documentazione)
- [Sviluppo e test](#sviluppo-e-test)
- [Struttura del progetto](#struttura-del-progetto)
- [Storia del progetto](#storia-del-progetto)
- [Limiti noti e prossimi passi](#limiti-noti-e-prossimi-passi)

## Cosa fa

| Area | In breve |
|---|---|
| **Mappa** | Grafo di province collegate da strade, piste e ferrovie con la **geometria stradale reale** (OpenStreetMap). Territori calcolati con un diagramma di Voronoi e linee del fronte. |
| **Forze di terra** | Brigate con mezzi reali, organizzazione (coesione) e rifornimenti. Battaglie a **contatto** (3 km, 12 km per l'artiglieria) lungo le strade, ritirate, accerchiamenti. |
| **IA** | Per fazione: sicurezza di retrovie e fianchi, schieramento sul fronte, offensive con superiorità locale, riserve. |
| **Logistica** | Depositi, rifornimento diretto, **convogli fisici** sulle strade, ricomplementi, ricostituzione delle brigate distrutte. |
| **Artiglieria** | Missioni di fuoco continue sul fronte, a gittate realistiche. |
| **Aviazione / ATO** | Pacchetti CAS, BAI, STRIKE, CAP e SEAD/DEAD con scorte, budget di sortite, turnaround, rimpiazzi. **I giocatori prendono posto nei voli** dal menu F10. |
| **Difesa aerea / IADS** | Patriot, S-300, Hawk, NASAMS, Buk, Kub, Tor, Osa, Avenger e radar di scoperta in rete. I sistemi mobili **si spostano sulle strade**; c'è la guerra SEAD. |
| **Player bubble** | Ciò che è vicino ai giocatori passa a DCS e torna al motore con le perdite subite. I SAM compaiono quando un giocatore entra nella loro portata. |
| **DCS** | Bridge Lua live su TCP. Spawn in colonna sulle strade, voli con carichi armi dai template, SAM in allerta rossa, missioni `.miz` pronte. |
| **API e UI** | FastAPI con streaming SSE degli eventi; UI React, Vite e MapLibre con simboli APP-6, cerchi di portata dei SAM e rete IADS. |

## Installazione

Requisiti: Python ≥ 3.12 e [uv](https://docs.astral.sh/uv/). Per la UI servono
Node ≥ 20 e npm. `lua5.1` è opzionale e serve solo per i test del bridge.

```bash
uv sync                       # motore, CLI, API
uv sync --extra miz           # + pydcs, per generare le missioni DCS
uv sync --group docs          # + Sphinx, per la documentazione
(cd web && npm install && npm run build)   # interfaccia web
```

Il motore di simulazione usa solo la libreria standard. L'API usa FastAPI e
uvicorn.

## Avvio rapido

```bash
# una campagna headless, il più veloce possibile, con riepilogo giornaliero
uv run dynacamp simulate scenarios/syria_m5.toml --days 5
uv run dynacamp simulate scenarios/western_georgia.toml --days 10 --quiet --events out.jsonl

# motore + API + interfaccia web, senza DCS, a 600x
uv run dynacamp serve scenarios/syria_m5.toml --no-dcs --compression 600
#   -> UI su http://localhost:8080/   API su /api   documentazione interattiva su /docs

# campagna live con DCS (60x finché nessun giocatore è in gioco, poi 1:1)
uv run dynacamp serve scenarios/western_georgia.toml --compression 60
```

## Il modello di campagna

Il design completo è in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). In sintesi:

**Il core** (`CampaignState`, `Engine`) è indipendente da qualsiasi gioco e
deterministico a parità di seed. A ogni step, in substep da 60 s, girano i
sistemi `ai → ato → supply → movement → combat → artillery → air_defense → air`.
Ogni cambiamento finisce in un **event log**, che alimenta lo stream della UI.
Il tempo scorre compresso; diventa 1:1 appena un gioco simula qualcosa.

**Mappa e strade.** Le province sono nodi con terreno, infrastruttura, punti
vittoria ed eventuale deposito. I collegamenti hanno la polilinea della strada
reale, calcolata con `dynacamp roads` (OSRM su OpenStreetMap) e salvata in
`scenarios/<nome>.roads.json`. Movimento, linee di difesa e convogli stanno
sempre sulla strada, quindi niente scorciatoie sul mare tra città costiere. Se
OpenStreetMap ha una strada chiusa che nel gioco è aperta (per esempio il
ponte sull'Enguri), il collegamento resta retto.

**Combattimento a contatto.** Il difensore si schiera su una linea avanzata,
fino a 6 km lungo la strada da cui arriva l'attacco. L'attaccante avanza fino
alla posizione d'assalto, alla sua distanza d'ingaggio in linea d'aria: 3 km a
tiro diretto, 12 km per l'artiglieria. La battaglia si apre solo lì. Il
modello è di tipo HOI4: colpi assorbiti da difesa o sfondamento, organizzazione
che cala in fretta e forza che cala lentamente, modificatori di terreno e
fiumi, trinceramento. Chi cede si ritira lungo la strada; chi vince avanza con
metà delle forze e l'altra metà tiene la linea di partenza.

**IA.** Retrovie e fianchi: dalle divisioni nemiche si calcola dove il nemico
potrebbe entrare senza combattere (fino a 3 province) e si presidiano i colli
di bottiglia. Poi l'IA schiera il fronte, attacca dove ha superiorità locale
(più alta sui terreni difficili) e sposta le riserve.

**Logistica.** I depositi producono rifornimenti. Le brigate entro 40 km di
strada da un deposito si riforniscono direttamente; le altre dipendono da
**convogli** veri, attaccabili da aria e terra. Le brigate rifornite ricevono
ricomplementi, e quelle distrutte vengono ricostituite dopo 48 h.

**Aviazione e ATO.** Ogni ora l'ATO converte la situazione in richieste: CAS
sulle battaglie, CAP fuori dalla copertura SAM nemica, BAI su colonne e
convogli, STRIKE sui depositi, SEAD/DEAD sui siti SAM. Le richieste diventano
pacchetti con scorta e SEAD quando servono. Il decollo è almeno 30 minuti dopo
la pianificazione, per dare tempo ai giocatori. Fuori dalle bolle i voli sono
simulati in modo astratto: navigazione, attacchi, duelli aria-aria, perdite
contro la difesa aerea.

**Difesa aerea e IADS.** Ogni sito ha le sue componenti reali (radar,
lanciatori, posto comando) e un ciclo di vita: in movimento con radar spento,
messa in batteria, attivo, soppresso. Un sito vicino a un radar di scoperta
amico è **in rete**: vede di più, ingaggia meglio e può spegnere il radar
prima che arrivi un anti-radar. Un comando di difesa aerea per fazione
riposiziona i sistemi mobili sulle strade. Il corto raggio segue le brigate;
medio e lungo raggio coprono depositi e basi stando dietro al fronte.

**Player bubble e handoff.** Ogni giocatore proietta una bolla: 60 km in
aereo, 30 km in elicottero, 15 km a terra, con 10 km di isteresi in uscita.
Ciò che è dentro passa a DCS con il protocollo materializzazione → delegato →
smaterializzazione, e torna al motore con le perdite subite. Le battaglie non
vengono mai spezzate tra motore e gioco. Se DCS si disconnette, il motore si
riprende tutto.

## DCS World

**Missioni pronte** in `missions/`, una per scenario. Ognuna contiene:
- il bridge Lua, avviato all'inizio della missione;
- i gruppi template `DC_TPL_<aereo>_<COMPITO>` (late activation) con carichi
  armi composti automaticamente e modificabili nel mission editor;
- gli slot client sulle basi degli squadroni;
- una zona di trigger per provincia.

Brigate, convogli, voli IA e SAM non sono nella missione: li crea il motore
attorno ai giocatori. Per rigenerare le missioni:

```bash
uv sync --extra miz
uv run dynacamp build-miz scenarios/*.toml       # -> missions/<scenario>.miz
```

**Per giocare:**

1. Desanitizza `DCS World/Scripts/MissionScripting.lua`: commenta
   `sanitizeModule('require')` e `sanitizeModule('package')`. Serve a LuaSocket
   e va rifatto dopo ogni aggiornamento di DCS.
2. Avvia `uv run dynacamp serve scenarios/<scenario>.toml`.
3. Avvia la missione `missions/<scenario>.miz`. Il bridge si connette, e si
   riconnette, da solo.
4. Da uno slot, apri **F10 → Other → DynaCamp ATO**, scegli un pacchetto e
   unisciti a un volo con posti liberi. Ricevi briefing e waypoint sulla mappa
   F10. "My assignment" ripete il briefing.

**Cosa succede in gioco:**
- **Brigate, convogli e SAM** compaiono in colonna lungo la strada, agganciati
  alla rete stradale di DCS (`land.getClosestPointOnRoads`). Le brigate sono
  in scala: un'unità DCS rappresenta più mezzi reali, e le perdite tornano in
  proporzione.
- **Voli IA:** prendono tipo e armamento dai template e ricevono i task DCS
  adatti alla missione (CAP in orbita, CAS/BAI in zona, bombardamento, SEAD).
- **SAM:** sono i sistemi DCS veri, in allerta rossa e con fuoco libero.
- **Giocatori:** decolli, atterraggi e abbattimenti aggiornano gli squadroni.

## Interfaccia web

React, Vite, TypeScript e MapLibre GL, su mappa OpenFreeMap scura. Dettagli in
[web/README.md](web/README.md).

- **Mappa:**
  - territori, fronte, strade reali;
  - battaglie nel punto di contatto e traiettorie d'artiglieria;
  - brigate, convogli, voli, basi e SAM con **simboli NATO APP-6** (milsymbol);
  - cerchi di portata e scoperta dei SAM e collegamenti IADS.

  Ogni livello si attiva e disattiva, e il punto di vista "View as" sceglie
  quale parte è amica.
- **Pannelli:** Front, Forces, Logistics, ATO, AD (difesa aerea) e Events
  (feed in tempo reale filtrabile). Ogni elemento selezionato ha la sua scheda
  dettagli.
- **Sviluppo:** `npm run dev` (porta 5173, inoltra `/api` al motore). In
  produzione `npm run build`, e la UI la serve `dynacamp serve`.

## API HTTP

FastAPI, in sola lettura, documentata in [docs/API.md](docs/API.md) e in modo
interattivo su `/docs`. Lo schema OpenAPI è su `/openapi.json`;
`npm run gen:api` ne genera i tipi TypeScript.

| Endpoint | Contenuto |
|---|---|
| `/api/campaign` | orologio, fazioni, obiettivi, giocatori |
| `/api/map` | province con territori, strade, basi aeree |
| `/api/fronts` | linee del fronte, settori, battaglie, missioni di fuoco |
| `/api/forces` | brigate con stato e percorso |
| `/api/logistics` | depositi, convogli, rete di rifornimento |
| `/api/ato` | pacchetti, voli, squadroni, budget, board per i giocatori |
| `/api/airdefense` | siti SAM ed EWR, stato, collegamenti IADS |
| `/api/events` | log a pagine |
| `/api/stream` | eventi e orologio in tempo reale (SSE) |
| `/api/snapshot` | tutto in un documento |

Ogni posizione ha `pos` ([est, nord] in metri sul piano del teatro) e `ll`
([lat, lon] WGS84).

## Scenari

Gli scenari sono file TOML in `scenarios/`:
- `western_georgia.toml` (mappa Caucaso): il rosso attacca dall'Abkhazia verso
  Kutaisi;
- `syria_m5.toml` (mappa Syria): la battaglia per il corridoio dell'autostrada
  M5.

| Sezione | Contenuto |
|---|---|
| `[campaign]` | `name`, `theater` (`caucasus`, `syria`), `seed`, `start_hour` |
| `[[factions]]` | `id`, `name`, `side` (`blue`/`red`), `enemies`, `objectives` |
| `[[provinces]]` | `id`, `name`, `pos = [est, nord]`, `terrain`, `controller`, `infrastructure`, `victory_points`, `depot = {...}` |
| `[[links]]` | `a`, `b`, `kind` (`road`/`track`/`rail`), `river` |
| `[[divisions]]` | `id`, `name`, `faction`, `template`, `province` |
| `[[templates]]` | template di brigata personalizzati (opzionale) |
| `[[airbases]]` | `id`, `name` (nome dell'aeroporto DCS), `pos`, `province` oppure `faction` |
| `[[squadrons]]` | `id`, `name`, `faction`, `airbase`, `aircraft` (`f16c`, `fa18c`, `f15c`, `a10c`, `ah64d`, `su27`, `mig29s`, `su25t`, `su24m`, `mi24p`), `count`, `callsign` |
| `[[air_defense]]` | `id`, `name`, `faction`, `system` (`patriot`, `s300ps`, `hawk`, `nasams`, `buk`, `kub`, `tor`, `osa`, `avenger`, `ewr_fps117`, `ewr_1l13`, `ewr_55g6`), `province` |

Le coordinate si convertono da lat/lon con `python tools/dcs_ll.py <teatro> <lat> <lon>`.
Dopo aver modificato i collegamenti, ricalcola le strade con
`uv run dynacamp roads scenarios/<nome>.toml`.

## Comandi e strumenti

| Comando | Cosa fa |
|---|---|
| `dynacamp simulate <scenario>` | campagna headless (`--days`, `--seed`, `--events out.jsonl`, `--quiet`/`--verbose`) |
| `dynacamp serve <scenario>` | tempo reale con DCS, API e UI (`--compression`, `--no-dcs`, `--port`, `--api-port`, `--web-dir`) |
| `dynacamp roads <scenario>...` | geometria stradale dei collegamenti, da OSRM; serve la rete |
| `dynacamp build-miz <scenario>...` | missioni DCS (`--out-dir`, `--port`); serve l'extra `miz` |
| `dynacamp openapi [-o file]` | schema OpenAPI dell'API |
| `dynacamp lua-path` | percorso del bridge Lua |
| `tools/dcs_ll.py` | lat/lon → coordinate DCS e scenario |

## Documentazione

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): design e scelte del modello.
- [docs/API.md](docs/API.md): API HTTP.
- [web/README.md](web/README.md): interfaccia web e simbologia.
- **Riferimento del codice (Sphinx).** Tutti i moduli, le classi e le
  funzioni sono documentati con docstring in stile Google:

  ```bash
  uv sync --group docs
  uv run --group docs sphinx-build -b html docs/sphinx docs/sphinx/_build/html
  # apri docs/sphinx/_build/html/index.html
  ```

  Il sito include questa guida, l'architettura, l'API HTTP e un riferimento
  generato per ogni modulo, con link al sorgente. Si pubblica come qualsiasi
  sito Sphinx (GitHub Pages, Read the Docs).

## Sviluppo e test

```bash
uv run pytest                          # 92 test: motore, ATO, difesa aerea, API, DCS, .miz, bridge Lua
lua5.1 tests/lua/test_bridge.lua src/dynacamp/adapters/dcs/lua/dynacamp_bridge.lua
(cd web && npx tsc -b)                 # typecheck della UI
```

Dopo modifiche al backend dell'API: `cd web && npm run gen:api`. Dopo
modifiche al bridge Lua: `uv run dynacamp build-miz scenarios/*.toml`, perché
un test controlla che le missioni lo contengano aggiornato.

## Struttura del progetto

```
src/dynacamp/
  geo.py world.py forces.py state.py events.py     modello: geometria, mappa, forze, stato, log
  air.py air_defense.py                            aviazione e difesa aerea (modello)
  systems/                                         simulazione:
    ai.py                                            IA di teatro (retrovie, fronte, offensive, riserve)
    ato.py air.py                                    ATO, voli, combattimento aereo
    air_defense.py                                   SAM, IADS, SEAD, comando di difesa aerea
    supply.py movement.py combat.py contact.py       logistica, movimento, battaglie, contatto
    artillery.py control.py                          fuoco d'artiglieria, controllo delle province
  bubble.py engine.py runtime.py                   player bubble, handoff, loop in tempo reale
  projection.py geometry.py roads.py               lat/lon, territori e fronti, strade reali
  scenario.py cli.py                               scenari TOML, comandi
  api/                                             FastAPI: views, schemas, app, server
  adapters/base.py adapters/fake.py                porta core <-> gioco, gioco finto per i test
  adapters/dcs/                                    DCS: protocol, adapter TCP, catalog, miz, lua/bridge
scenarios/                                         scenari TOML + geometria stradale (.roads.json)
missions/                                          missioni DCS generate
web/                                               interfaccia web React + Vite + MapLibre
docs/                                              architettura, API, sito Sphinx (docs/sphinx)
tests/                                             test Python e Lua
tools/                                             conversione di coordinate
```

## Storia del progetto

Il progetto è cresciuto per passi, ognuno verificato con test e misure.
1. **Fondamenta.**
   - *Scelte:* core in Python, integrazione DCS live (e non a turni con
     generazione di `.miz`), granularità HOI4, player bubble alla Falcon.
   - *Prima versione:* province, brigate, battaglie, rifornimenti con convogli
     fisici, IA di base, event log e handoff verso il gioco con isteresi e
     timeout.
   - *DCS:* adapter TCP e bridge Lua, testato con `lua5.1` contro mock di DCS.
   - *Scenario:* Georgia occidentale.
2. **Scenario Siria.** Proiezione Transverse Mercator della mappa Syria (dai
   parametri di pydcs), validata sugli aeroporti con errore sotto 1 km.
3. **IA di retrovie e fianchi.** Analisi di esposizione: il flood-fill delle
   possibili infiltrazioni nemiche trova da solo i colli di bottiglia da
   presidiare.
4. **ATO e giocatori.**
   - *Pianificazione:* pacchetti CAS, BAI, STRIKE, CAP con scorte e SEAD,
     combattimento aereo astratto.
   - *Giocatori:* posti nei voli tramite il menu F10 in DCS, con briefing e
     waypoint.
   - *Calibrazione:* da 13% a circa 5% di perdite per sortita, con turnaround
     e budget di sortite.
5. **Missioni `.miz`** generate con pydcs: bridge incorporato, template con
   carichi composti dalle tabelle dei piloni, slot client. Tutti i nomi
   (aeroporti, unità) verificati contro i database di DCS.
6. **API HTTP.**
   - *Prima versione:* viste, proiezione inversa lat/lon, territori di
     Voronoi e linee del fronte, stream SSE.
   - *Poi FastAPI:* schemi Pydantic e OpenAPI completo.
7. **Interfaccia web** React, Vite e MapLibre.
   - *Corretti con gli screenshot:* il worker di MapLibre mancante dopo la
     build e i marker fuori posto.
   - *Bug del motore emerso dalla UI:* il budget di sortite si esauriva al
     mattino; ora c'è un token bucket.
8. **Simboli NATO** APP-6 con milsymbol, punto di vista amico/ostile, brigate
   affiancate quando occupano la stessa posizione.
9. **Combattimento a contatto.** Prima le battaglie si combattevano tra centri
   provincia a 28–44 km di distanza; ora a 3 km (12 km per l'artiglieria).
   Ritmo più alto: in Georgia il tempo senza combattimenti è sceso dall'80% al
   24%.
10. **Strade reali.**
    - *Strade:* percorsi da OSRM, unità e convogli sulle strade, niente
       attraversamenti del mare; in DCS colonne sulla strada e aggancio alla
       rete stradale del gioco.
    - *Più azione:* artiglieria continua, fronte presidiato.
    - *Sostenibilità:* ricomplementi e ricostituzione delle brigate.
11. **Difesa aerea e IADS.**
    - *Contenuto:* 12 sistemi, rete EWR, SEAD/DEAD, sistemi mobili sulle
      strade, spawn dei SAM in DCS quando un giocatore entra nella loro
      portata.
    - *Calibrazione:* la prima versione annientava le aviazioni in 5 giorni;
      ora le CAP stanno fuori portata, non si attacca senza SEAD, ci sono
      contromisure e rimpiazzi di aerei e batterie.
12. **Documentazione.** Questo README, il riferimento Sphinx con 520
    elementi documentati, e il test che impedisce di distribuire missioni con
    un bridge vecchio.

## Limiti noti e prossimi passi

- **DCS vero:** da provare. In particolare i parametri dei task d'attacco
  aereo e la resa dello spawn.
- **Persistenza:** manca il salvataggio e il caricamento della campagna,
  indispensabile per un server persistente.
- **Equilibrio:** in alcuni seed una parte conquista tutto in una settimana,
  per una spirale (perde i depositi, quindi non ha più ricomplementi). Possibili
  rimedi: rinforzi da fuori mappa, difesa più forte nelle ultime province,
  fasi e condizioni di vittoria.
- **IADS in DCS:** dentro DCS ogni sito combatte da solo. Il coordinamento
  (EMCON) richiederebbe integrare uno script come Skynet IADS.
- **Rotte aeree:** i voli non pianificano rotte attorno ai SAM.
- **Comandi dal web:** la UI è in sola lettura; ordini e posti ATO dal
  browser sono da fare.
