# Architettura di DynaCamp

## Obiettivo

Un motore di campagna militare dinamica **headless**. Simula fronti, logistica,
combattimenti e decisioni dei comandi a granularità strategica (stile *Hearts of
Iron IV*) e va avanti da solo. I giochi (DCS per primo) si collegano tramite
**adapter**: dove ci sono giocatori umani la simulazione viene **delegata al
gioco** (player bubble, stile *Falcon 4 / BMS*); altrove la porta avanti il motore.

## Strati

```
            ┌───────────────────── CORE (puro Python, deterministico) ─────────────────────┐
            │ world.py     province, link, terreno, pathfinding                             │
            │ forces.py    elementi, template, Division, Convoy, Authority                  │
            │ state.py     CampaignState: mondo + forze + battaglie + event log + RNG       │
            │ air.py       aerei, basi, squadroni, pacchetti, voli e posti per i giocatori  │
            │ systems/     ai → ato → supply → movement → combat → air  (a ogni substep)    │
            │ bubble.py    player bubble: chi deve essere simulato dal gioco                │
            │ engine.py    loop, handoff, traduzione report/comandi                         │
            └──────────────────────────────┬───────────────────────────────────────────────┘
                                           │ porta: adapters/base.py (messaggi tipizzati)
              ┌────────────────────────────┼───────────────────────────┐
         adapters/fake.py           adapters/dcs/                api/ (FastAPI, sola lettura)
         (test/offline)       protocol.py + adapter.py         → web app (docs/API.md)
                                      │ TCP, JSON lines
                               lua/dynacamp_bridge.lua  (dentro la missione DCS)
```

Il core non importa nulla da `adapters/dcs`. Tutto ciò che sa di DCS (nomi dei
tipi di unità, sistemi di coordinate, country id, rapporti di rappresentazione)
sta nell'adapter.

## Modello di simulazione

**Mappa.** Grafo di province (`Province`) collegate da `Link` (road/track/rail,
eventuale fiume). Ogni provincia ha terreno, infrastruttura, controllore,
victory points ed eventualmente un deposito (`Depot`). Coordinate su piano
piatto in metri: `x` est, `y` nord.

**Unità.** `Division` aggregate (brigate) con conteggi reali di elementi
(carri, IFV, squadre di fanteria...), *organizzazione* (coesione, crolla in
battaglia e si recupera a riposo) e *rifornimenti* trasportati. I `Convoy` sono
colonne di camion fisiche che si muovono sul grafo.

**Tempo.** `Engine.step(dt)` spezza `dt` in substep da max 60 s. I sistemi
lavorano a ratei orari, quindi lo stesso codice gira a 1x (live) o compresso.

**Sistemi** (in ordine):

| sistema | cosa fa |
|---|---|
| `ai` | ogni 2 h, per fazione: 1) sicurezza di retrovie e fianchi (vedi sotto), 2) attacchi dove c'è superiorità locale (pesata per terreno e obiettivi), 3) riserve verso i settori più minacciati, senza sguarnire ciò che proteggono |
| `ato` | ogni ora, per fazione: richieste di missione → pacchetti (vedi *Aviazione*) |
| `supply` | produzione depositi, consumo (×1.5 in movimento, ×2.5 in battaglia), rifornimento diretto entro 40 km di strada da un deposito, invio convogli oltre; senza rifornimenti: decadimento org + attrito |
| `movement` | avanzamento lungo i link, arrivo, cattura di province ostili vuote, ripianificazione se la strada è tagliata |
| `combat` | battaglie per provincia: colpi assorbiti da defense/breakthrough, danni a org (veloci) e forza (lenti), ritirate, accerchiamenti, avanzata di metà degli attaccanti dopo la vittoria |
| `artillery` | missioni di fuoco continue sul fronte: chi ha artiglieria colpisce il nemico più vicino a portata (24 km organica, 40 km con lanciarazzi) |
| `air_defense` | stato dei siti SAM/EWR, rete IADS, ingaggi, anti-radiazione, ricarica, riposizionamento dei sistemi mobili |
| `air` | voli: decollo, navigazione, attacchi astratti contro la difesa aerea, combattimento aria-aria, rientro e turnaround |

**Strade.** I collegamenti tra province hanno la geometria della strada
reale: `dynacamp roads <scenario>` li calcola una volta con OSRM su dati
OpenStreetMap e li salva in `<scenario>.roads.json`, versionato con lo
scenario, così il motore non ha bisogno di rete. Movimento, linee di difesa,
posizioni d'assalto, convogli e percorsi mostrati nella UI stanno sulla
polilinea stradale, quindi niente scorciatoie sul mare. Se OSM ha una strada chiusa
che nel gioco è aperta (es. il ponte sull'Enguri) e il percorso diventa un giro
assurdo (oltre 2.5 volte la linea d'aria), il collegamento resta retto. In DCS i
mezzi vengono messi in colonna lungo la strada e il bridge li aggancia al
punto di strada più vicino (`land.getClosestPointOnRoads`).

**Fronte e logoramento.** Le brigate sulle province di confine si schierano
sulla linea avanzata verso il vicino più minaccioso. Le brigate rifornite e fuori
combattimento ricevono ricomplementi (1% di forza all'ora, pagati con le scorte
del deposito). Una brigata distrutta viene ricostituita al deposito migliore
della fazione dopo 48 h, a metà forza.

**Contatto.** Le battaglie si combattono dove le forze sono davvero a
portata di sensori e armi (`systems/contact.py`), non tra due centri provincia
a decine di km:

- il difensore si schiera su una linea di difesa avanzata, fino a 6 km dal
  centro della provincia (o al 30% del collegamento) lungo l'asse d'attacco.
  Con più assi, i difensori si distribuiscono;
- l'attaccante avanza lungo la strada fino alla posizione d'assalto, alla sua
  distanza d'ingaggio dalla linea: 3 km per le unità a tiro diretto, 12 km per
  l'artiglieria, che spara dalle retrovie. La battaglia si apre solo lì, e chi
  arriva dopo si unisce quando raggiunge la propria posizione;
- un attacco fallito si ritira lungo la strada. Dopo la vittoria metà degli
  attaccanti avanza e occupa la provincia, l'altra metà rientra sulla linea di
  partenza.

Le distanze sono in linea d'aria e convertite in avanzamento lungo la strada.
In DCS le due forze si materializzano quindi a contatto visivo.

**Retrovie e fianchi.** Da ogni divisione nemica il motore fa un flood-fill
sul grafo per 3 salti, fermandosi alle province che teniamo con truppe: è dove
il nemico potrebbe entrare senza combattere. Le nostre province vuote raggiunte
sono *esposte*, con un peso che dipende da importanza (depositi, victory points,
obiettivi nemici, basi aeree attive) e vicinanza del nemico. L'IA sposta le
divisioni una alla volta dove riducono di più l'esposizione totale. Siccome un
presidio protegge anche ciò che sta dietro, trova da sola i colli di bottiglia:
tenere Tabqa protegge Al-Bab e Aleppo. Lo stesso calcolo impedisce di mandare
al fronte una riserva la cui partenza scoprirebbe qualcosa.

Tutto ciò che cambia finisce nell'**event log** (`events.py`), la base per la
futura UI web (timeline, replay, storia del fronte). `CampaignState.snapshot()`
dà una vista JSON dello stato corrente.

**Determinismo.** Senza giochi collegati, stesso scenario + stesso seed = stessa
campagna (c'è un test). Con un gioco live gli input esterni entrano come eventi.

## Aviazione e ATO

**Modello.** `Airbase` (appartiene a chi controlla la provincia, oppure a una
fazione fissa per le basi nelle retrovie fuori mappa), `Squadron` (tipo di
aereo, aerei in carico, impegnati, in turnaround), `Package` (un obiettivo e un
TOT) fatto di `Flight`. I voli si muovono liberamente nel piano, non sul grafo.
Sono entità come le altre, quindi bubble e handoff funzionano allo stesso modo:
un volo IA dentro una bolla lo fa volare DCS.

**Pianificazione** (`systems/ato.py`, ogni ora):

1. Richieste per priorità: CAS sulle nostre battaglie, CAP sulle province di
   fronte più minacciate (2 stazioni), BAI su convogli e colonne nemiche in
   movimento o a ridosso del fronte, STRIKE sui depositi.
2. Per ogni richiesta, lo squadrone migliore in raggio con aerei disponibili.
   Si aggiungono un volo SEAD se c'è difesa aerea a corto raggio sul bersaglio
   e una scorta se i caccia nemici possono arrivarci.
3. **Decollo almeno 30 minuti dopo la pianificazione**, così i giocatori hanno
   il tempo di scegliere un volo e avviare l'aereo.
4. Limiti: budget di 1.5 sortite per aereo al giorno, al massimo 4 pacchetti
   offensivi attivi per fazione, turnaround di 4 ore dopo ogni missione.

**Risoluzione astratta** (fuori dalle bolle): perdite contro la difesa aerea
(SHORAD/AAA delle divisioni entro 15 km, ridotte se il SEAD del pacchetto è
passato), danni a divisioni, convogli e depositi. BAI contro bersagli mobili fa
ricognizione armata: segue la colonna. Aria-aria: un volo combatte un ingaggio
alla volta, prima si risolvono le coppie caccia-caccia (così la scorta impegna
gli intercettori). Gli attaccanti intercettati rientrano, i caccia rientrano
se perdono metà della formazione.

**Giocatori nei pacchetti.** Un giocatore non ha un volo suo: prende un *posto*
in un volo pianificato dall'ATO e sostituisce uno degli aerei IA dello
squadrone. Il resto del volo resta IA. Flusso:

```
engine ──PublishAto(side, board)──► gioco: menu "DynaCamp ATO" per i giocatori
gioco  ──JoinRequest(player, flight, compiti possibili per il suo aereo)──► engine
engine ──NotifyPlayer(briefing + waypoint sulla mappa)──► gioco
gioco  ──PlayerEvent(takeoff | landed | lost)──► engine (contabilità squadrone)
```

Si può entrare fino a 10 minuti dopo il decollo. Se un giocatore si disconnette,
un posto non ancora volato si libera, uno in volo si considera rientrato. Se un
volo è tutto di giocatori, il motore non lo simula: i suoi effetti arrivano da
DCS come perdite sui bersagli, che sono comunque nella bolla del giocatore.

## Difesa aerea e IADS

**Modello** (`air_defense.py`). Ogni sito è un'entità mobile come le brigate,
con le sue componenti (radar di ricerca/tiro, lanciatori, posto comando),
1:1 con le unità DCS. Catalogo: Patriot, S-300PS (lungo raggio); Hawk, NASAMS,
SA-11 Buk, SA-6 Kub (medio); SA-15 Tor, SA-8 Osa, Avenger (corto); radar di
scoperta FPS-117, 1L13, 55G6. Ciclo di vita: `moving` (radar spento) →
`setting_up` (tempo di messa in batteria del sistema) → `active`; il SEAD può
portarlo a `suppressed`. Senza radar o senza lanciatori il sito è distrutto e
una batteria sostitutiva arriva al deposito principale dopo 36 h.

**IADS.** Un sito entro metà del raggio di un EWR amico attivo è in rete: vede
ciò che vede l'EWR, ingaggia meglio (+15%) e spesso spegne il radar prima che
arrivi un anti-radiazione.

**Ingaggi** (`systems/air_defense.py`, fuori dalle bolle): salve di 2 missili
sul volo nemico più vicino rilevato entro portata e quota massima, al più una
volta ogni 10 minuti per coppia sito-volo, con efficacia ridotta da
contromisure e manovre. Il corto raggio raggiunge i jet solo in fase d'attacco,
quando sono bassi; gli elicotteri sono sempre bassi. I missili si ricaricano
entro 60 km di strada da un deposito.

**SEAD/DEAD.** I voli SEAD colpiscono i radar accesi entro 60 km. L'ATO
pianifica SEAD/DEAD sui siti a medio e lungo raggio che coprono il fronte,
aggiunge una scorta SEAD a ogni pacchetto che entra in copertura, non manda
attaccanti sotto un ombrello SAM se non ha SEAD disponibili e arretra le
stazioni CAP fuori dalla copertura nemica. Anche l'artiglieria colpisce i siti;
se la provincia cade, i siti che ci si trovano sono persi.

**Comando di difesa aerea.** Ogni 2 h, per fazione, riposiziona i sistemi
mobili lungo le strade (`route_entity`). Il corto raggio segue le brigate,
soprattutto quelle in battaglia. Medio e lungo raggio coprono depositi, basi e
città, penalizzati se troppo esposti al fronte. Si sposta solo per un guadagno
netto (×2 lungo, ×1.6 medio, ×1.25 corto), un sito medio/lungo per volta. Se
minacciati da truppe o presi di mira dal SEAD, i siti si spostano
("shoot and scoot").

**Gli aerei** ricevono rimpiazzi (1 per squadrone ogni 8 h fino all'organico)
e volano 2.5 sortite per aereo al giorno.

**DCS.** Un sito si materializza quando un giocatore entra nella sua *portata*
(`bubble_reach`), non solo nella bolla: entrare nella copertura di un S-300 fa
comparire l'S-300. È un gruppo con i tipi DCS veri (`catalog.SAM_TYPES`,
verificati contro pydcs), schierato sulla strada, in allerta rossa e con
fuoco libero. L'IADS dentro DCS (EMCON coordinato) richiederebbe uno script
dedicato, per esempio Skynet: in gioco ogni sito combatte da solo.

## Supporto aereo e nebbia di guerra

**Supporto** (`Task.AWACS`, `Task.TANKER`, `Task.TRANSPORT`; `Task.support`,
`Task.orbits`):
- AWACS e tanker volano in orbita (`ON_STATION`) attorno a un punto dato da
  `ato.support_station`: dietro il baricentro del fronte, verso le basi
  amiche, e più indietro finché è fuori dai SAM nemici noti;
- `air.tanker_on_station` allunga il raggio in `ato._allocate` e la stazione
  delle CAP;
- `air.awacs_on_station` dà il bonus ai duelli aria-aria;
- i trasporti caricano al decollo (`_load_cargo`) e consegnano al bersaglio
  (`air.deliver`): `airlift` verso il deposito di una base, `resupply` alle
  brigate entro 15 km;
- i pacchetti di supporto hanno un'allocazione propria (`MAX_NEW_SUPPORT`) e
  non contano nel limite dei pacchetti attivi.

**Nebbia di guerra** (`systems/intel.py`, primo sistema della pipeline):
- *quadro:* `state.intel[fazione][entità] = Contact(position, seen,
  first_seen, source, identified, detail)`;
- *scansione:* `scan` ogni 60 s con tre gruppi di sensori (aria, terra,
  ELINT), più `reveal` sugli eventi (lancio SAM);
- *scadenza:* `_expire` toglie i contatti scaduti (`TTL_S`) o le entità
  sparite; `stale` usa `STALE_S`;
- *giocatori:* l'`Engine` copia le loro posizioni in `state.player_positions`,
  così anche loro fanno da sensori.

Consumatori del quadro:
- `ato.requests`, cioè BAI, convogli e SEAD, tramite `intel.knows`,
  `intel.located` e `intel.known`;
- `intel.known_covering`, che sostituisce `air_defense.covering` in
  pianificazione (CAP, stazioni, scorte SEAD);
- `ai`, con `intel.known_divisions_in` al posto di `hostile_divisions_in`;
- `artillery` (`TARGET_FRESH_S`);
- `briefing.threats`;
- le viste API con `viewer`.

La simulazione fisica (combattimenti, ingaggi SAM, duelli) usa sempre la
verità.

## Player bubble e handoff

Ogni giocatore umano proietta una bolla (raggio per tipo: aereo 60 km,
elicottero 30 km, terra 15 km, configurabile in `BubbleConfig`).

* **Isteresi:** si entra al raggio, si esce solo oltre raggio + 10 km, così le
  unità sul bordo non vengono spawnate e despawnate continuamente.
* **Chiusura sulle battaglie:** una battaglia non viene mai spezzata. Se un
  partecipante (o la provincia) è nella bolla, vengono delegati tutti.

Ogni entità ha un'`Authority` con questa macchina a stati:

```
 CAMPAIGN ──(entra nella bolla)──► MATERIALIZING ──(MaterializeAck)──► DELEGATED
     ▲                                  │ timeout/errore                   │ (esce dalla bolla)
     │                                  ▼                                  ▼
     └────────(DematerializeAck)──── DEMATERIALIZING ◄──────────────────────┘
```

* Negli stati di transizione l'entità è **congelata**: nessuno la simula.
* Da `DELEGATED` il gioco è autoritativo su **posizione e perdite**. Il motore
  riceve `EntityState` periodici: aggiorna la posizione (e l'avanzamento sul
  link, così al rientro l'unità riparte da dov'era), applica le perdite (il
  gioco può solo togliere, mai aggiungere) e le converte in perdita di
  organizzazione. Una battaglia delegata non subisce danni calcolati dal motore,
  ma il motore continua a decidere chi cede (ritirate, catture).
* **Gli ordini restano della campagna.** L'IA può riassegnare un'unità delegata
  e il motore manda un `UpdateRoute` al gioco.
* Se il gioco si disconnette, o un handoff va in timeout, il motore **si
  riprende tutto** con l'ultimo stato noto.
* Mentre il gioco tiene qualcosa (`engine.live`), il runtime forza il tempo a
  1x. Altrimenti usa la compressione configurata.

## Adapter DCS

* `protocol.py` (puro, testato) costruisce le **tabelle complete dei gruppi
  DCS**: tipi da `catalog.py`, formazione in colonna, rotta "On Road" lungo le
  province del percorso. Converte poi le unità vive riportate da DCS in
  elementi della campagna.
* **Scala di rappresentazione:** una brigata vera ha centinaia di mezzi. In DCS
  un'unità rappresenta `RATIO[element]` elementi (es. 1 Abrams = 6 MBT), con un
  tetto di 40 unità per entità e 20 per gruppo. Le perdite tornano nella stessa
  scala: perdere 1 Abrams su 15 vuol dire perdere 1/15 dei carri della brigata.
* Nomi delle unità: `DC.<entity>.<element>.<n>`. Così il bridge Lua non deve
  sapere nulla: riporta solo nomi e posizioni.
* **Voli:** il gruppo prende tipo, livrea e **carico armi** da un gruppo
  *template* messo nel mission editor (late activation) con nome
  `DC_TPL_<aereo>_<COMPITO>` o `DC_TPL_<aereo>` (es. `DC_TPL_f16c_SEAD`,
  `DC_TPL_a10c`). Rotta con task DCS: EngageTargets (CAP/scorta/SEAD), Orbit
  per la durata del CAP, EngageTargetsInZone (CAS/BAI), Bombing (STRIKE),
  atterraggio alla base di casa.
* **Menu F10:** per ogni gruppo con un giocatore, "DynaCamp ATO" con i
  pacchetti, i voli con posti liberi, "My assignment" e "Leave my flight".
  Briefing con `outTextForGroup`, waypoint con `markToGroup`. Le capacità
  dell'aereo del giocatore (`catalog.PLAYER_TASKS`) filtrano i voli a cui può
  unirsi: un A-10 non prende posto in un CAP.
* `miz.py` (richiede pydcs) genera la missione di uno scenario: bridge
  incorporato, template con carichi composti dalle tabelle dei piloni di pydcs
  (scelte valide per costruzione), slot client, zone delle province. I nomi di
  aeroporti e tipi di unità del catalogo sono verificati contro i database di
  pydcs. Un test fa girare il bridge in `lua5.1` sulla tabella `mission` reale
  del `.miz` generato.
* `adapter.py`: server TCP (thread di I/O + code), API `send`/`poll` non bloccante.
* `lua/dynacamp_bridge.lua`: client LuaSocket nell'ambiente mission. Ogni 0.2 s
  legge i comandi (`spawn`/`route`/`despawn`/`ping`), ogni 2 s manda giocatori
  (`coalition.getPlayers`) e stato dei gruppi. Ha un JSON integrato. È testato
  con `lua5.1` contro mock di DCS e LuaSocket (`tests/lua/test_bridge.lua`).

Coordinate: core `(x est, y nord)` ⇄ DCS `(x nord, z est)`. Nelle tabelle dei
gruppi DCS l'est si chiama `y`.

## Limiti noti / prossimi passi

* **Persistenza:** manca il salvataggio/caricamento dello stato (fondamentale
  per un server persistente). Lo snapshot JSON esiste già.
* **Aria, da fare:** SAM a lungo raggio come entità, AWACS/tanker, OCA sulle
  basi, ricostituzione degli squadroni. Il decollo dei voli IA nelle bolle
  avviene in volo vicino alla base: lo spawn a terra/al parcheggio non c'è.
* **Da verificare in DCS:** i parametri dei task (soprattutto `weaponType` del
  Bombing) e i nomi dei tipi di aereo in `catalog.py`.
* **Bilanciamento IA/combattimento:** l'IA è volutamente semplice (niente
  battle plan, niente gestione delle retrovie). I parametri sono in testa ai
  moduli dei sistemi.
* **Spawn in DCS:** il bridge non è ancora stato provato in un DCS vero. Prima
  di farlo vanno verificati i nomi dei tipi di unità (`catalog.py`) e le
  coordinate dello scenario.
* **Strutture statiche:** depositi, ponti e SAM come oggetti DCS bersagliabili
  non ci sono ancora.
* **API web:** c'è, in sola lettura (`docs/API.md`): viste pure in
  `api/views.py`, modelli Pydantic in `api/schemas.py` (OpenAPI completo),
  app FastAPI con SSE in `api/app.py`, uvicorn in un thread in `api/server.py`. Le posizioni sono
  convertite in lat/lon con la proiezione del teatro (`projection.py`).
  I territori sono celle di Voronoi dei centri provincia (`geometry.py`), quindi
  sulla costa si estendono in mare: la UI li maschera con la costa. Mancano i
  comandi (ordini, posti ATO dal web).
