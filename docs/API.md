# DynaCamp HTTP API (v1)

API per la web app: mappa, fronti, forze, logistica, ATO ed eventi in tempo
reale, più il controllo del tempo e gli ordini all'ATO. È un'app **FastAPI** (`src/dynacamp/api/app.py`) servita
da uvicorn nello stesso processo del motore, in un thread a parte.

- **Documentazione interattiva:** `http://localhost:8080/docs`
- **Schema OpenAPI 3.1:** `http://localhost:8080/openapi.json`. Ogni risposta ha
  un modello Pydantic (`api/schemas.py`), quindi lo schema è completo e da qui
  la web app può generare i suoi tipi, per esempio
  `npx openapi-typescript http://localhost:8080/openapi.json -o src/api.d.ts`.

```bash
uv run dynacamp serve scenarios/syria_m5.toml                       # con DCS, API su :8080
uv run dynacamp serve scenarios/syria_m5.toml --no-dcs --compression 600   # senza DCS, per sviluppare la UI
```

Opzioni: `--api-host` (default `127.0.0.1`), `--api-port` (default `8080`, `0`
la disattiva). CORS è aperto (`*`), quindi un dev server su un'altra porta può
chiamarla direttamente. I parametri sono validati: un valore sbagliato dà 422
con il dettaglio dell'errore.

## Autenticazione

La lettura (`GET`) è libera. I comandi (`POST`) vogliono
`Authorization: Bearer <token>`, altrimenti rispondono `401` con
`WWW-Authenticate: Bearer`. Il confronto del token è a tempo costante. Lo
schema OpenAPI dichiara lo schema `HTTPBearer`, quindi su `/docs` il pulsante
*Authorize* abilita le prove.

Da dove viene il token:
- da `--api-token TOKEN`;
- dalla variabile d'ambiente `DYNACAMP_API_TOKEN`;
- altrimenti `serve` ne genera uno casuale a ogni avvio e lo stampa, insieme
  al link `http://host:porta/#token=...` per la UI. Il frammento `#` non
  arriva mai al server, quindi non finisce nei log.

```bash
curl -X POST -H "Authorization: Bearer $DYNACAMP_API_TOKEN" -H 'Content-Type: application/json' \
     -d '{"paused": true}' http://localhost:8080/api/control
```

### `GET /api/auth`

`{"required": bool, "authenticated": bool}`. `authenticated` dice se la
richiesta aveva il token giusto, ed è sempre `true` quando il token non è
richiesto. La UI lo usa per sapere se sbloccare i comandi.

Il token protegge i comandi, non i dati. Le viste senza `faction`, cioè
quelle dell'arbitro, restano leggibili da chiunque raggiunga la porta. Il
traffico è HTTP in chiaro: per esporre il servizio serve un reverse proxy con
HTTPS.

## Convenzioni

- **Posizioni**: ogni oggetto ha `pos` = `[est, nord]` in metri sul piano del
  teatro (lo stesso frame degli scenari; per DCS `[z, x]`) e `ll` = `[lat, lon]`
  WGS84, pronto per Leaflet/MapLibre. `ll` è `null` solo per teatri senza
  proiezione nota. Le linee (rotte, fronti, territori) hanno `pos` e `ll` come
  liste di punti.
- **Tempi**: `{"t": secondi di campagna, "label": "D2 14:30"}`. Il giorno 1
  inizia alle 00:00; gli scenari partono alle 06:00 (`start_hour`).
- **Consistenza**: ogni risposta è presa sotto il lock del motore, quindi
  descrive uno stato coerente tra due step. Gli endpoint sono funzioni
  sincrone, che FastAPI esegue nel suo threadpool: il lock non blocca mai
  l'event loop.
- Gli id (`province`, `division`, `flight`...) sono stabili e collegano gli
  oggetti fra endpoint diversi.

## Endpoint

### Vista per fazione: `?faction=<id>` (nebbia di guerra)

Tutti gli endpoint dinamici (`fronts`, `forces`, `logistics`, `ato`,
`airdefense`, `events`, `stream`, `snapshot`) accettano `?faction=<id>`.
Senza il parametro la risposta è la **vista arbitro**, cioè la verità. Con il
parametro è quello che quella fazione sa:

| | Vista per fazione |
|---|---|
| Proprie e alleate | Tutto, come nella vista arbitro. |
| Brigate, convogli, SAM nemici | Solo se nel quadro informativo, **all'ultima posizione nota**, con un blocco `intel` (`source`, `seen`, `age_s`, `stale`, `identified`). Ordini, rotte, rifornimenti, organizzazione, missili e componenti sono nascosti (`order.kind = "unknown"`, `null`). |
| Aerei nemici | Non nei `packages` ma in `ato.tracks`. Una traccia non identificata ha callsign `TRACK xxxx`, tipo "Unknown aircraft" e dimensione approssimata. |
| Depositi nemici | Visibili, con `stock` e `trucks` a `null`. |
| Settori del fronte | Solo i propri, con la potenza nemica stimata dal quadro. |
| Eventi | I propri e quelli pubblici (battaglie, conquiste, ingaggi, perdite). |

Una fazione inesistente dà 404.

### `GET /api/intel?faction=<id>`

Il quadro informativo di una fazione:
- `contacts[]`: `id`, `kind` (`division`, `convoy`, `flight`, `sam`),
  `faction`, `pos`/`ll` all'ultima posizione nota, `source`, `seen`,
  `first_seen`, `age_s`, `stale`, `identified`, `detail` (quello che si sa:
  nome, tipo, forza stimata, rotta per gli aerei...);
- `sensors[]`: i sensori di ricerca aerea della fazione (`awacs`, `aircraft`,
  `ground_radar`) con posizione e portata. Il radar di un aereo è un cono:
  `heading_deg` e `half_angle_deg` (null per i sensori che guardano tutto
  intorno);
- `counts`: numero di contatti per tipo.

Il parametro `faction` è obbligatorio.

### `GET /api/control`, `POST /api/control`

Pausa e velocità dell'orologio di campagna. `POST` (con token, vedi
*Autenticazione*) con
`{"paused": true|false, "compression": 3600}`; i campi omessi restano invariati.
La risposta (`paused`, `compression`, `rate`, `min_compression`,
`max_compression`) è anche in `/api/campaign` (`control`) e in ogni `tick` dello
stream. `rate` è la velocità effettiva: 0 in pausa, 1 se un gioco è live,
altrimenti la compressione. Mentre un gioco è live il tempo deve scorrere 1:1:
pausa e accelerazione rispondono 409. Una compressione fuori da
[1, 7200] risponde 422. Senza token, o con un token sbagliato, la risposta
è 401.

### `GET /api/save`, `POST /api/save`

Salvataggio della campagna sul file di `dynacamp serve --save FILE`. La
risposta è uguale per `GET` e `POST`, e si trova anche in `/api/campaign`
(`save`):

```json
{"enabled": true, "file": "syria.dcsave", "autosave_minutes": 10.0,
 "last": {"saved_at": 1795554880.1, "time": {"t": 29215.9, "label": "D1 08:06"}, "trigger": "api"}}
```

- `enabled`: `false` se il server non ha `--save`.
- `saved_at`: ora reale del salvataggio, in secondi Unix.
- `trigger`: chi l'ha chiesto, cioè `auto` (salvataggio automatico), `api` o
  `exit` (chiusura del server).
- `last`: riguarda solo l'esecuzione corrente del server; vale `null` finché
  non si salva.

`POST` salva subito; serve il token. Risponde 409 senza `--save` e 500 se il
file non si può scrivere: in quel caso il salvataggio precedente resta
intatto. Ogni salvataggio emette l'evento `campaign_saved`
(`file`, `trigger`, `campaign_time`).

### `GET /api/positions/stream?hz=5&faction=<id>`

Server-Sent Events `positions` a `hz` campioni al secondo (0,5–20), solo con
ciò che si muove: `{"t", "d": {id: [lat, lon]}, "c": {...}, "f": {...}, "s": {...}}`
(brigate, convogli, voli in aria, siti SAM). Con `faction` vale la nebbia di
guerra. Serve ad animare la mappa: il client interpola tra i campioni.
`GET /api/positions` restituisce un campione singolo.

### `GET /api/campaign`
Nome, teatro, orologio, `live` (true quando DCS sta simulando qualcosa: tempo
1:1), giocatori connessi (posizione, lato, volo assegnato) e per fazione:
province, victory points, divisioni, aerei, obiettivi (con `achieved`) e
`bullseye` (`pos`, `ll`: il punto di riferimento delle chiamate BULLS del
lato). `magnetic_variation_deg` è la declinazione del teatro (est positiva):
una rotta BULLS è la rotta vera meno questo valore.

### `GET /api/map`
- `provinces[]`: `id, name, pos, ll, terrain, infrastructure, victory_points,
  owner, controller, depot, neighbors[]`, `territory` = poligono della provincia
  (cella di Voronoi del centro, raggio massimo 60 km). Colorando i territori per
  `controller` si ottiene la mappa di controllo in stile HOI4.
- `links[]`: `a, b, kind (road|track|rail), river, length_km`, segmento `pos/ll`.
- `airbases[]`: `id, name` (nome dell'aeroporto DCS), `pos, ll, province, owner`.

La geometria non cambia durante la campagna: si può caricare una volta e poi
aggiornare solo `controller` (da `/api/campaign` o dagli eventi
`province_captured`).

### `GET /api/fronts`
- `lines[]`: linee del fronte come polilinee (`pos`, `ll`), `factions` [a, b],
  `length_km`, province coinvolte, battaglie in corso lungo la linea. Sono i
  confini condivisi tra territori di fazioni in guerra.
- `sectors[]`: per fazione e provincia di fronte, potenza propria e nemica
  adiacente (per heatmap di pressione) e divisioni presenti.
- `battles[]`: `id, province, kind, pos, ll, attacker, defender, attackers[],
  defenders[], started, hours, delegated` (true se la combatte DCS). `kind` è
  `assault` (per la provincia) o `meeting` (scontro d'incontro sulla strada:
  `province` è la più vicina, `pos`/`ll` il punto dello scontro).

### `GET /api/forces`
`divisions[]`: `id, name, faction, template, location, pos, ll, strength,
org, supply` (0..1), `elements` (mezzi per tipo), `order {kind, target}`,
`route` (province e polilinea del percorso residuo, o null), `battle`,
`authority` (`campaign | materializing | delegated | dematerializing`),
`moving`. Più `battles[]` come sopra.

### `GET /api/logistics`
- `depots[]`: scorte, capacità, produzione oraria, camion disponibili.
- `convoys[]`: posizione, carico, camion, origine/destinazione, `returning`,
  rotta residua, `authority`.
- `network[faction][province]`: deposito più vicino, km di strada, `direct`
  (entro 40 km: rifornimento diretto senza convogli).
- `divisions[]`: livello di rifornimento, `out_of_supply`, `direct`,
  `convoy_incoming`.

### `GET /api/ato`
- `packages[]`: `id, number, faction, task, target, pos, ll, tot, created, flights[]`.
  `number` è il numero di package a 4 cifre (1000–9999), come nell'ATO di
  Falcon/BMS: lo condividono tutti i voli del package (attacco, SEAD, scorta).
- ogni volo: `package_number, callsign, squadron, airbase, aircraft, task, status`
  (`scheduled | airborne | on_station | rtb | done | cancelled`),
  `size, ai, seats_free, players[]` (con stato del posto), `takeoff, tot`,
  `pos, ll`, `waypoints` (polilinea), `target_index, next_waypoint, target`,
  `authority`, `on_station_until` (CAP), `locked_waypoints` (waypoint iniziali
  che non si possono più spostare), `fuel_km` (rotta più lunga pianificabile).
- ogni package ha anche `earliest_tot`, il TOT più presto a cui si può spostare,
  e `archived` (sempre `false` qui; `true` per i package conclusi letti per id).
- `squadrons[]`: forza, disponibili, impegnati, in turnaround, compiti.
- `sortie_budget_left` per fazione, `flights_airborne`.
- `boards[side]`: esattamente ciò che vedono i giocatori nel menu F10.

### `GET /api/ato/packages/{package_id}` e `GET /api/ato/flights/{flight_id}/package`
Un package (con i voli), come in `/api/ato`, letto per id o tramite uno dei
suoi voli. Funziona anche **dopo la fine della missione**: i package conclusi
lasciano l'ATO ma restano nell'archivio del motore (gli ultimi 2000), con
`archived: true`. Servono alla UI per mostrare di cosa parla un evento.
Con `?faction=` un package nemico è 404, come uno sconosciuto.

### `GET /api/ato/flights/{flight_id}/briefing`
Mission package di un volo: `units`, `package_number`, `bullseye` (`pos`, `ll`,
`coordinates`), `title`, `mission`, `situation[]`, `target`
(descrizione, composizione, coordinate, rilevamento dalla base), `threats[]`
(SAM lungo la rotta, SHORAD, caccia nemici, ciascuno con `severity`),
`waypoints[]` (`index, name, eta, alt_m, speed_kmh, note, pos, ll`),
`loadout[]` (`store, count`), `comms` (UHF di package e VHF del volo, assegnate
dall'ATO lontano dalle frequenze ATC degli aeroporti del teatro), `support[]`
(AWACS e tanker disponibili), `package[]` e `text` (il briefing come
arriva al giocatore in DCS). Risponde 404 se il volo non esiste più.

`?units=imperial|metric` sceglie le unità dei **testi** (descrizioni, minacce,
`text`): `imperial` = nodi, piedi, miglia nautiche; `metric` = km/h, m, km.
Senza parametro valgono le unità della campagna (`units` in `/api/campaign`,
di default `imperial`). I campi numerici restano sempre in SI (`alt_m`,
`speed_kmh`, `range_km`).
Bersaglio, minacce e waypoint hanno anche `bulls`, per esempio `"045/32"`:
rotta magnetica e distanza (NM in imperiale, km in metrico) dal bullseye del
lato.

### Ordini all'ATO (`POST`, con token)

Un pianificatore umano può fare quello che fa un giocatore di Falcon/BMS nella
schermata ATO. La logica sta in `systems/orders.py`. Ogni comando vuole il
token e accetta un `faction` facoltativo: se c'è, l'ordine vale solo per i
package e i voli di quella fazione; se manca, l'ordine è dell'arbitro. Un
ordine rifiutato non cambia nulla e risponde 409 con il motivo in `detail`
(per esempio `route too long: 950 km, fuel for 800 km`); un volo, package o
fazione sconosciuti danno 404.

Le posizioni (`PointIn`) sono `{"ll": [lat, lon]}` oppure
`{"pos": [est, nord]}` in metri; se ci sono entrambi vale `ll`.

- **`POST /api/ato/packages`** ordina un nuovo package:
  `{"faction", "task", "target": {"kind", "id", "point"}, "squadron"?, "tot"?}`.
  Viene pianificato come lo farebbe l'IA: volo principale, poi SEAD e scorta
  quando servono. Il bersaglio deve essere noto alla fazione (nebbia di
  guerra). Abbinamenti ammessi:

  | task | `target.kind` | `target.id` |
  |---|---|---|
  | CAS | `battle` | battaglia che coinvolge la fazione |
  | BAI | `division`, `convoy` | contatto nel quadro informativo |
  | STRIKE | `depot` | provincia nemica con deposito |
  | SEAD | `sam` | contatto SAM |
  | DEAD | `sam` | contatto SAM; `squadron` è quello degli attaccanti (deve fare STRIKE) |
  | CAP, AWACS, TANKER | `point` | — (serve `point`) |
  | TRANSPORT | `airlift`, `resupply` | base amica con deposito / brigata amica (elicotteri) |

  Senza `squadron` sceglie il migliore disponibile; senza `tot` il più presto
  possibile. Rifiuta se il budget di sortite è esaurito, se la rotta resta
  sotto un SAM noto che non può aggirare e nessuno può fare SEAD (per un DEAD:
  se nessuno può fare SEAD), se è fuori portata o se `tot` è troppo presto. Risponde con il package (come in `/api/ato`) ed emette
  `package_ordered`.
- **`POST /api/ato/flights/{flight_id}/route`** sostituisce la rotta di un
  volo, dal decollo all'atterraggio:
  `{"waypoints": [PointIn, ...], "target_index", "faction"?}` (da 3 a 24
  punti). Vale prima e durante la missione. Regole:
  - i waypoint già volati (`locked_waypoints`) e l'atterraggio non si spostano;
  - il bersaglio si sposta solo per CAP, AWACS e tanker (il package lo
    segue); per un altro bersaglio serve un nuovo package;
  - la lunghezza non supera `fuel_km` (due volte il raggio d'azione, di più
    con un tanker in stazione), a meno che la rotta attuale sia già più lunga;
  - per un volo a terra il decollo si ricalcola dal TOT e non può cadere nel
    passato.

  Un volo in DCS riceve subito la nuova rotta (con i task al bersaglio); i
  giocatori seduti ricevono un nuovo briefing. Emette `flight_replanned`.
- **`POST /api/ato/packages/{package_id}/tot`** `{"tot": secondi, "faction"?}`
  sposta il TOT di un package con tutti i voli ancora a terra; decolli e fine
  stazione si spostano dello stesso tempo. Più tardi si può sempre (fino a 24
  ore avanti), prima solo fino a `earliest_tot`. Emette `package_retimed`.
- **`POST /api/ato/packages/{package_id}/cancel`** `{"faction"?}` annulla i
  voli del package ancora a terra (gli aerei tornano allo squadrone); quelli in
  volo proseguono. Risponde `{"cancelled": [callsign, ...]}`; i giocatori
  seduti nei voli annullati ricevono un messaggio in gioco.

```bash
curl -X POST -H "Authorization: Bearer $DYNACAMP_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"faction": "blue", "task": "CAP", "target": {"kind": "point", "point": {"ll": [35.0, 37.0]}}}' \
  http://localhost:8080/api/ato/packages
```

### `GET /api/airdefense`
- `sites[]`: `id, name, faction, system, system_name, role` (`long | medium |
  short | ewr`), `status` (`active | setting_up | moving | suppressed`),
  `pos, ll, location, range_km, detection_km, ceiling_m, missiles,
  missiles_max, elements, emitting, networked_to` (EWR che lo collega
  all'IADS), `route` (percorso stradale se in movimento), `authority`.
- `links[]`: collegamenti IADS EWR → sito, come polilinee.

### `GET /api/events?since=0&kinds=a,b&limit=500`
Eventi in ordine, a pagine: `{"events": [...], "next": N}`. Si richiama con
`since=next`. Ogni evento: `seq, time, kind, data`. I tipi principali sono
`province_captured`, `battle_started/ended`, `division_retreated/destroyed`,
`convoy_dispatched/delivered/destroyed`, `package_planned`, `flight_takeoff`,
`air_strike`, `air_engagement`, `aircraft_lost`, `seat_taken`,
`player_joined/left` e `handoff`. `battle_started/ended` hanno `battle_kind`
(`assault`/`meeting`); `contact_broken` è una brigata che si sgancia da uno
scontro d'incontro. `air_engagement` ha `phase` (`BVR`/`WVR`), `shots` e
`losses` (tiratore, bersaglio); `flight_rtb` ha `reason` (`bingo fuel`,
`winchester`, `losses`, `end of station`).

### `GET /api/stream?since=N`
Server-Sent Events. Manda `event: event` (stesso formato di `/api/events`,
con `id` = seq) per ogni evento di campagna e `event: tick` ogni secondo con
`{"time", "live"}`. Senza `since` parte dagli eventi nuovi.

```js
const es = new EventSource("http://localhost:8080/api/stream");
es.addEventListener("event", (m) => {
  const e = JSON.parse(m.data);
  if (e.kind === "province_captured") refresh("fronts");
});
es.addEventListener("tick", (m) => setClock(JSON.parse(m.data).time.label));
```

### `GET /api/snapshot`
Tutte le viste sopra in un solo documento (~50 KB per gli scenari demo).

## Schema di integrazione consigliato

1. All'avvio: `/api/map` (geometria) e `/api/snapshot`.
2. Ascoltare `/api/stream` per orologio ed eventi (toast, timeline).
3. Ogni 2–5 s, o quando arrivano eventi rilevanti, ricaricare `/api/forces`,
   `/api/logistics`, `/api/ato` e `/api/fronts`. Sono piccoli.

Comandi (dare ordini, prendere posti ATO dal web) non sono ancora esposti:
l'API è in sola lettura.
