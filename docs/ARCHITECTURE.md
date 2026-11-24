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

**Salvataggio** (`persistence.py`). Si salva l'intero `CampaignState` come
pickle compresso con gzip, dentro un involucro con versione del formato,
versione del pacchetto e compressione temporale. L'`EventLog` non salva i
subscriber, che appartengono al processo. Al caricamento ciò che teneva un
gioco torna al motore: authority `CAMPAIGN`, posti dei giocatori liberati come
in una disconnessione. Il log riceve un evento `campaign_resumed`. La
scrittura è atomica (file temporaneo, `fsync`, `os.replace`). In `serve` lo
stato si serializza sotto `Engine.lock`, mentre la scrittura su disco avviene
fuori dal lock. `persistence.Saver` serve i tre casi: salvataggio automatico
dal thread del motore, `POST /api/save` dal threadpool dell'API, e chiusura
del server. Un lock proprio impedisce che due salvataggi si accavallino; ogni
salvataggio emette `campaign_saved`.

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
   fronte più minacciate (3 stazioni, tirate fuori dalla copertura SAM nota),
   BAI su convogli e colonne nemiche *localizzati* in movimento o a ridosso del
   fronte, STRIKE sui depositi, SEAD/DEAD sui SAM noti che coprono il fronte,
   più le missioni di supporto (AWACS, tanker, trasporti: vedi sotto).
2. Per ogni richiesta, lo squadrone migliore in raggio con aerei disponibili
   (raggio +40% se un tanker è in stazione). Si aggiungono un volo SEAD se c'è
   difesa aerea sul bersaglio o un SAM noto lo copre (niente attacco se il SEAD
   non è disponibile) e una scorta se i caccia nemici possono arrivarci.
3. **Decollo almeno 30 minuti dopo la pianificazione**, così i giocatori hanno
   il tempo di scegliere un volo e avviare l'aereo.
4. Limiti: budget di 2,5 sortite per aereo al giorno (a token bucket, così non
   si esaurisce al mattino), al massimo 6 pacchetti offensivi attivi e 3 nuovi
   per ciclo per fazione, più 2 di supporto per ciclo con un'allocazione a
   parte; turnaround di 3 ore dopo ogni missione, un aereo di rimpiazzo ogni
   8 ore per squadrone fino all'organico.
5. **Identità e radio del pacchetto.** Ogni package ha un numero a 4 cifre
   (`air.package_number`: deterministico, unico per i primi 9000 package),
   condiviso dai suoi voli come nell'ATO di Falcon/BMS. Alla pianificazione
   riceve una frequenza UHF comune, e ogni volo una VHF interna
   (`dynacamp.radio.allocate`), in bande compatibili con le radio occidentali
   e russe (UHF 260–389,975, VHF 128–139,975). Ogni frequenza dista almeno
   50 kHz dalle frequenze ATC di **tutti gli aeroporti del teatro**
   (`data/radio.json`, generato dai dati di DCS con `dynacamp radio`), dalle
   frequenze di emergenza (121.5/243.0) e dagli altri package e voli attivi.
   Gli aerei IA in DCS parlano sulla UHF del package.
   Il **call sign** del volo nasce dallo squadrone (`Lobo 3`). In DCS gli
   aerei occidentali accettano solo i nomi di un elenco fisso: gli 8 generici
   (Enfield…Pontiac), più quelli del tipo (F-16 Viper…Jedi, A-10 Hawg…Tusk…)
   e le liste di AWACS e tanker. `dynacamp.callsigns` usa il nome dello
   squadrone se è nell'elenco, altrimenti gli dà un nome generico stabile.
   Gli squadroni che condividono lo stesso nome si dividono i numeri di volo.
   Il bridge lo applica alle unità IA (`DC.apply_callsign`), e il briefing
   mostra il "radio callsign" quando è diverso. Il lato rosso usa i numeri
   del template.

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

**Mission package** (`systems/briefing.py`). `briefing(state, flight, units)`
restituisce dati: missione, situazione, bersaglio con coordinate DDM,
minacce *note* lungo la rotta (campionata ogni 5 km), waypoint con nome, ETA
calcolati a ritroso dal TOT, quote e velocità, armamento da
`data/loadouts.json`, package, frequenze e supporti (AWACS e tanker). I numeri
restano in SI; i testi sono scritti nelle unità richieste
(`dynacamp/units.py`: `imperial` = kts/ft/NM, il default dei briefing, o
`metric`). `render()` ne fa il testo per DCS. Le unità della campagna vengono
dallo scenario (`[campaign] units`) o da `serve --units`.

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

## Tempo reale, animazione e controllo del tempo

**Ciclo di gioco** (`runtime.run_realtime`). È un ciclo a tick fisso (default
10 Hz, `serve --tick-hz`). A ogni tick:
- il motore avanza del tempo reale trascorso moltiplicato per
  `Engine.time_rate`;
- poi dorme per il resto del budget del frame, sempre almeno 2 ms, così CPU,
  API e adapter respirano.

Un tick in ritardo non viene recuperato con passi extra: il successivo copre
l'intervallo più lungo, con un tetto di 1 s, così uno stallo non diventa un
salto di tempo.

**Controllo del tempo** (`engine.TimeControl`). `Engine.control` ha `paused` e
`compression` (1–7200×), cambiabili a ogni tick con `POST /api/control`.
`time_rate` vale:
- 1 se un gioco simula parte della campagna (`Engine.live`): DCS non si può
  né fermare né accelerare, e l'API rifiuta con 409;
- 0 in pausa: il motore continua a girare con `dt = 0`, quindi i messaggi del
  gioco e gli handoff vengono ancora gestiti;
- altrimenti la compressione.

Ogni cambio emette un evento `time_control`.

**Animazione della mappa**:
- *server:* `api/views.positions` produce un campione compatto
  (`id -> [lat, lon]`) di ciò che si muove, con la nebbia di guerra se c'è una
  fazione, e `GET /api/positions/stream` lo invia via SSE a 5 Hz;
- *client* (`web/src/lib/livePositions.ts`): fa interpolazione delle entità
  come nel netcode dei giochi. Disegna circa 2 periodi nel passato, interpola
  linearmente tra i campioni a ogni `requestAnimationFrame`, ed estrapola per
  al massimo 300 ms se i campioni ritardano;
- *marker:* MapLibre con `subpixelPositioning`, senza passare da React. Le
  viste complete (stato, statistiche) restano ogni 3 s.

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

* **DCS vero:** il bridge non è ancora stato provato in un DCS reale. Da
  verificare: parametri dei task (soprattutto `weaponType` del Bombing e i task
  AWACS/Tanker), resa dello spawn su strada, frequenze dei gruppi.
* **Salvataggi:** `persistence.py` salva l'intero `CampaignState` come
  pickle gzip (generatore casuale compreso, quindi la ripresa è
  deterministica). Il formato è fragile rispetto ai refactoring delle classi;
  per una compatibilità lunga servirebbe una serializzazione esplicita e
  versionata. C'è un solo file per server, senza slot né storico.
* **Aria:** mancano:
  - OCA sulle basi e la ricostituzione completa degli squadroni (oggi c'è solo
    il rimpiazzo graduale);
  - il decollo dal parcheggio dei voli IA nelle bolle: oggi partono in volo
    vicino alla base;
  - le rotte attorno ai SAM;
  - le missioni di ricognizione per rinfrescare il quadro informativo.
* **Nebbia di guerra in DCS:** vale per motore, UI e briefing. Quello che i
  giocatori scoprono in DCS non torna ancora al quadro informativo.
* **Equilibrio:** in alcuni seed una parte conquista tutto in pochi giorni, per
  una spirale (perde i depositi, quindi non ha più ricomplementi). Non dipende
  dalla nebbia di guerra: è stato verificato con un'IA onnisciente.
* **IADS in DCS:** dentro DCS ogni sito combatte da solo. L'EMCON coordinato
  richiederebbe uno script come Skynet.
* **Strutture statiche:** depositi, ponti e siti come oggetti DCS bersagliabili
  non ci sono ancora.
* **Radio:** le frequenze evitano le torri, ma non radiofari e TACAN, perché
  pydcs non le espone. Stanno comunque in bande diverse.
* **API web:** l'unico comando è il controllo del tempo (`POST /api/control`).
  È protetto da un bearer token unico, senza utenti né ruoli. La lettura è
  libera, compresa la vista dell'arbitro, e il traffico è in chiaro: per
  esporla servono HTTPS e un reverse proxy. Mancano
  ordini e posti ATO dal browser. I territori sono celle di Voronoi dei centri
  provincia, quindi sulla costa si estendono in mare: la UI li maschera con la
  costa.
