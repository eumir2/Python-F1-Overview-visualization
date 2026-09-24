# Changelog

Tutte le modifiche rilevanti di questo progetto sono documentate qui.

## [Unreleased]

### Aggiunto
- Overlay desktop trasparente per Formula 1 con finestra sempre in primo piano.
- Supporto alla modalita' storico con replay di sessioni passate.
- Selezione della durata della sessione in 5, 10, 20 giri o gara intera.
- Modalita' live con polling incrementale e gestione automatica dei rate limit.
- Visualizzazione del tracciato con scia delle auto e colore per pilota/team.
- Indicazione approssimata dei settori S1, S2 e S3 sul circuito.
- Classifica in tempo reale con posizione, ultimo giro e tempi di settore.
- Telecamera dinamica con inseguimento di un pilota e zoom.
- Gestione di temi e opzioni visive dall'interfaccia.
- Pannello di impostazioni e resize grip per ridimensionare la finestra.

### Migliorato
- Download dei dati OpenF1 suddiviso in blocchi per evitare richieste troppo grandi.
- Logica di interpolazione per una posizione piu' fluida delle auto in replay/live.
- Gestione robuste degli errori API: 404, 422 e 429.
- Separazione dei moduli in logica di dati, worker e UI per una struttura piu' chiara.

### Corretto
- Reset delle sessioni e riavvio pulito tra caricamento storico e live.
- Backoff adattivo per evitare richieste massicce durante periodi di traffico elevato.
- Controlli UI per la gestione del playback, camera e zoom.

### Note
- OpenF1 restituisce attualmente `401 Unauthorized` sia per le richieste storiche sia per quelle live; il progetto non implementa ancora il flusso OAuth2.
- Il README documenta lo stato corrente dell'accesso OpenF1 e i limiti della modalita' storico/live.
- I file Python generati, inclusa la cartella `__pycache__/`, sono esclusi da Git.
- Sono presenti due versioni del progetto: una legacy monolitica in `f1_overlay_qt.py` e una modulare in `main.py`, `data.py`, `worker.py` e `widgets.py`.
- La versione modulare e' la struttura attuale piu' organizzata e manutenibile; la versione monolitica e' mantenuta come riferimento storico.
- Non sono ancora presenti test automatici.
