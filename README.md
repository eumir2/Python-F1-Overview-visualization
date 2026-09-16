# F1 Overlay Qt

Overlay desktop trasparente per visualizzare il tracciato e la posizione delle monoposto di una sessione di Formula 1. L'applicazione usa [OpenF1](https://openf1.org) come sorgente dati e non richiede una API key.

## Funzionalita'

- Replay di una gara storica con play/pausa e velocita' 1x, 5x, 20x o 50x.
- Download storico configurabile per 5, 10, 20 giri oppure per l'intera sessione.
- Modalita' Live con polling incrementale dei dati di posizione, giri e classifica.
- Tracciato con scia delle auto e colori distinti per pilota/team.
- Colorazione approssimata dei settori S1, S2 e S3 quando i dati disponibili lo permettono.
- Classifica opzionale con posizione, ultimo giro e tempi dei settori.
- Confronto dei tempi con il pilota immediatamente davanti: verde se migliore, giallo se peggiore.
- Telecamera dinamica con inseguimento di un pilota e zoom 2x, 4x o 8x.
- Finestra sempre in primo piano, senza bordi nativi e ridimensionabile.

## Requisiti

- Windows, macOS o Linux.
- Python 3.10 o superiore consigliato.
- Connessione Internet per raggiungere l'API OpenF1.
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

Con l'ambiente virtuale attivo:

```powershell
python .\f1_overlay_qt.py
```

All'avvio l'applicazione carica automaticamente una sessione Race storica disponibile del 2023. Il caricamento avviene in un thread separato per non bloccare l'interfaccia.

## Utilizzo

### Modalita' Storico

1. Lasciare selezionato `Storico`.
2. Inserire facoltativamente una `session_key` OpenF1.
3. Selezionare la quantita' di gara da caricare: `5 giri`, `10 giri`, `20 giri` o `Gara intera`.
4. Premere `Carica`.
5. Usare `Play` per avviare o mettere in pausa il replay.
6. Selezionare la velocita' desiderata.

Se la `session_key` e' vuota, viene usata l'ultima gara disponibile del 2023. Per trovare una chiave di sessione consultare gli endpoint OpenF1 `/sessions` oppure la documentazione del servizio.

### Modalita' Live

1. Selezionare `Live`.
2. Lasciare vuoto il campo `session_key` per usare la sessione piu' recente, oppure inserire una chiave specifica.
3. Premere `Carica`.

La modalita' Live aggiorna i dati ogni tre secondi circa. I rate limit HTTP 429 vengono gestiti con backoff esponenziale e il polling riprende automaticamente dopo errori temporanei di rete.

### Controlli aggiuntivi

- Il menu del pilota abilita l'inseguimento della sua auto.
- Il menu dello zoom diventa disponibile quando e' selezionato un pilota.
- Il pulsante con la bandiera mostra o nasconde la classifica.
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

OpenF1 restituisce `404` quando non ci sono risultati e `422` quando una richiesta e' troppo ampia. Il primo caso viene trattato come lista vuota; il secondo viene mostrato come errore esplicito. Gli errori di rete e i rate limit `429` vengono ritentati automaticamente.

## Struttura del codice

Il progetto e' volutamente contenuto in un solo file:

- `SessionData`: conserva dati, coordinate, tempi e classifica della sessione.
- `DriverTrack`: gestisce campioni, interpolazione e scia di un pilota.
- `Worker`: esegue le richieste di rete fuori dal thread della UI.
- `TrackWidget`: disegna tracciato, settori, auto e camera dinamica.
- `StandingsPanel`: visualizza la classifica compatta.
- `TitleBar` e `MainWindow`: gestiscono controlli, layout e ciclo di vita dell'applicazione.

## Limiti noti

- La suddivisione dei settori sul tracciato e' approssimata: OpenF1 fornisce i tempi dei settori, ma non i confini geometrici esatti sul circuito.
- La disponibilita' dei dati dipende dalla sessione e dall'API OpenF1.
- La modalita' Live richiede una sessione attiva o una sessione recente disponibile nel servizio.
- Il programma non salva dati localmente e riscarica le informazioni quando viene caricata una nuova sessione.
- Non sono inclusi test automatici o pacchetti precompilati.

## Verifica rapida

Per controllare la sintassi senza avviare la GUI:

```powershell
python -m py_compile .\f1_overlay_qt.py
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