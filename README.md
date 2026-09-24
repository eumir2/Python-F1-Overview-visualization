# F1 Overlay Qt

Overlay desktop trasparente per visualizzare il tracciato e la posizione delle monoposto di una sessione di Formula 1. L'applicazione usa [OpenF1](https://openf1.org) come sorgente dati.

> **Stato dell'API OpenF1 (24 settembre 2026):** al momento l'API restituisce
> `401 Unauthorized` anche per le richieste di sessioni storiche, non solo per
> quelle live. Di conseguenza, in questo momento nell'applicazione non
> funzionano né la modalità **Storico** né la modalità **Live**. La
> documentazione OpenF1 indica ancora lo storico come accessibile senza
> autenticazione, ma il comportamento del servizio non corrisponde a quella
> indicazione. Non è quindi un problema specifico degli URL costruiti
> dall'applicazione.

## Versioni del progetto

Questo repository contiene due versioni coesistenti dello stesso progetto:

- `f1_overlay_qt.py`: versione legacy/monolitica, dove la logica dell'applicazione e' concentrata in un unico file.
- `main.py` + `data.py` + `worker.py` + `widgets.py`: versione modulare, organizzata in file dedicati per dati, UI e richieste HTTP.

La versione modulare e' quella piu' strutturata e rappresenta l'evoluzione del progetto; la versione standalone e' conservata come riferimento storico / prototipo iniziale.

## Funzionalita'

- Replay di una gara storica con play/pausa e velocita' 1x, 5x, 20x o 50x.
- Download storico configurabile per 5, 10, 20 giri oppure per l'intera sessione.
- Modalita' Live con polling incrementale dei dati di posizione, giri e classifica.
- Tracciato con scia delle auto e colori distinti per pilota/team.
- Colorazione approssimata dei settori S1, S2 e S3 quando i dati disponibili lo permettono.
- Classifica opzionale con posizione, ultimo giro, tempi dei settori e gap live lungo il tracciato.
- Confronto dei tempi con il pilota immediatamente davanti: verde se migliore, giallo se peggiore.
- Gap live colorato in base al trend: verde se si riduce, giallo se aumenta.
- Telecamera dinamica con inseguimento di un pilota e zoom 2x, 4x o 8x.
- Personalizzazione del tema visivo: colori del tracciato, settori, testo e opacita' pannelli.
- Finestra sempre in primo piano, senza bordi nativi e ridimensionabile.

## Requisiti

- Windows, macOS o Linux.
- Python 3.10 o superiore consigliato.
- Connessione Internet per raggiungere l'API OpenF1 (il servizio attualmente
  risponde `401 Unauthorized` anche alle richieste storiche).
- Dipendenze Python:
  - `PySide6`
  - `requests`

## Installazione

Da PowerShell, nella directory del progetto:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install PySide6 requests
```

Se PowerShell impedisce l'attivazione dell'ambiente virtuale, eseguire una volta:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## Avvio

La versione consigliata e' la modulare:

```powershell
python .\main.py
```

La versione legacy monolitica e' ancora presente per confronto o riferimento:

```powershell
python .\f1_overlay_qt.py
```

All'avvio l'applicazione prova a caricare automaticamente una sessione Race
storica disponibile del 2023. Il caricamento avviene in un thread separato per
non bloccare l'interfaccia. Con lo stato attuale dell'API OpenF1, la richiesta
fallisce con `401 Unauthorized`.

## Utilizzo

### Modalita' Storico

1. Lasciare selezionato `Storico`.
2. Inserire facoltativamente una `session_key` OpenF1.
3. Selezionare la quantita' di gara da caricare: `5 giri`, `10 giri`, `20 giri` o `Gara intera`.
4. Premere `Carica`.
5. Usare `Play` per avviare o mettere in pausa il replay.
6. Selezionare la velocita' desiderata.

Se la `session_key` e' vuota, viene usata l'ultima gara disponibile del 2023.
Attualmente anche questa ricerca fallisce con `401 Unauthorized`; inserire una
chiave storica numerica non evita il problema.

### Modalita' Live

1. Selezionare `Live`.
2. Lasciare vuoto il campo `session_key` per usare la sessione piu' recente, oppure inserire una chiave specifica.
3. Premere `Carica`.

La modalita' Live aggiorna i dati ogni tre secondi circa. I rate limit HTTP
429 vengono gestiti con backoff esponenziale e il polling riprende
automaticamente dopo errori temporanei di rete. Attualmente la modalità Live
richiede autenticazione OpenF1 e non è disponibile senza credenziali valide.

### Controlli aggiuntivi

- Il menu del pilota abilita l'inseguimento della sua auto.
- Il menu dello zoom diventa disponibile quando e' selezionato un pilota.
- Il pulsante con la bandiera mostra o nasconde la classifica.
- Il pulsante con l'ingranaggio apre il pannello delle impostazioni visive.
- La casella `settori` abilita i colori di confronto per S1, S2 e S3.
- La barra superiore permette di trascinare la finestra.
- La maniglia nell'angolo inferiore destro permette di ridimensionarla.
- Il pulsante `X` chiude l'applicazione e interrompe il polling Live.

## Dati e gestione delle richieste

L'applicazione usa questi endpoint OpenF1:

- `/sessions` per identificare la sessione.
- `/drivers` per nomi, numeri e colori dei piloti.
- `/location` per le coordinate delle auto.
- `/laps` per tempi sul giro e tempi dei settori.
- `/position` per la classifica nel tempo.

Le richieste storiche a `location`, `laps` e `position` vengono suddivise in blocchi temporali per evitare richieste troppo grandi. In particolare:

- le posizioni vengono scaricate in blocchi da 15 minuti;
- i giri e la classifica vengono scaricati in blocchi da 25 minuti;
- la ricerca iniziale dei giri richiesti e' limitata a 45 minuti per motivi di sicurezza.

OpenF1 restituisce `404` quando non ci sono risultati e `422` quando una richiesta
e' troppo ampia. Il primo caso viene trattato come lista vuota; il secondo viene
mostrato come errore esplicito. Gli errori di rete e i rate limit `429` vengono
ritentati automaticamente. Attualmente il servizio restituisce anche `401`
(`Unauthorized`) per le richieste storiche e live. Il progetto non implementa
ancora il flusso OAuth2 necessario per ottenere un token OpenF1 autenticato.

## Struttura del codice

Il progetto e' organizzato in moduli dedicati nella versione modulare:

- `main.py`: entry point dell'applicazione e coordinamento dei componenti principali.
- `data.py`: modello dati, tema, calcoli di distanza/tracciato e logica di sessione.
- `worker.py`: richieste HTTP verso OpenF1, polling live e rate limiter adattivo.
- `widgets.py`: componenti Qt per tracciato, pannello classifica, impostazioni e barra titolo.
- `SessionData`: conserva dati, coordinate, tempi e classifica della sessione.
- `DriverTrack`: gestisce campioni, interpolazione e scia di un pilota.
- `TrackWidget`: disegna tracciato, settori, auto e camera dinamica.
- `StandingsPanel`: visualizza la classifica compatta.
- `SettingsPanel`: permette la personalizzazione del tema a runtime.
- `TitleBar` e `MainWindow`: gestiscono controlli, layout e ciclo di vita dell'applicazione.

La versione `f1_overlay_qt.py` mantiene invece il medesimo comportamento ma con tutti i concetti contenuti in un unico file.

## Limiti noti

- La suddivisione dei settori sul tracciato e' approssimata: OpenF1 fornisce i tempi dei settori, ma non i confini geometrici esatti sul circuito.
- La disponibilita' dei dati dipende dalla sessione e dall'API OpenF1.
- La modalita' Live richiede una sessione attiva o una sessione recente disponibile nel servizio.
- Il programma non salva dati localmente e riscarica le informazioni quando viene caricata una nuova sessione.
- Non sono inclusi test automatici o pacchetti precompilati.

## Verifica rapida

Per controllare la sintassi senza avviare la GUI, puoi verificare il modulo principale:

```powershell
python -m py_compile .\main.py
```

Per verificare che le dipendenze siano installate:

```powershell
python -c "import PySide6, requests; print('Dipendenze OK')"
```

## Commit Git

Per aggiungere il file di documentazione al prossimo commit:

```powershell
git add README.md f1_overlay_qt.py
git commit -m "docs: add F1 overlay usage guide"
```

Il comando `git add f1_overlay_qt.py` e' necessario solo se si desidera includere nello stesso commit anche le modifiche al codice gia' presenti nella working tree.