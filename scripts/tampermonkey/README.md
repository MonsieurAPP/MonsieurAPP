# Monsieur Cuisine Bridge

Userscript Tampermonkey per il flusso app-to-browser:

1. Importa la ricetta dentro MonsieurAPP.
2. In MonsieurAPP controlla adattamenti, porzioni e scegli la base finale (`originale` o `proposta AI`).
3. Clicca `Conferma ricetta pronta`.
4. Dalla pagina job clicca `Apri con Tampermonkey`, oppure vai direttamente su Monsieur Cuisine.
5. Nella pagina Monsieur Cuisine compare un pannello con il bottone `Leggi ricetta confermata`.
6. Lo script recupera dall'istanza di MonsieurAPP con cui e' stato configurato il payload del job confermato indicato nell'hash `#mc-import=...`; se manca, usa l'ultima ricetta confermata disponibile.
7. Lo script prova a compilare titolo, porzioni, tempo totale, ingredienti e tutti i passaggi esportati; quelli non macchina vengono inseriti come step descrittivi con solo titolo e descrizione, gli step `Bilancia` restano singoli con descrizione nello stesso passaggio e, se il payload contiene `targetedWeight`, lo script tenta di compilare anche il campo `Peso`. Se uno step `Bilancia` richiede meno di 5 g, il bridge aggiunge una nota con il peso reale nella descrizione e forza il campo `Peso` a 5 g per evitare errori del sito Monsieur Cuisine. Gli step `Cottura personalizzata` vengono invece sdoppiati in uno step descrittivo e uno step tecnico con lo stesso titolo.
8. Controlla il risultato e premi `Salva` manualmente.

## Installazione

1. Se MonsieurAPP e' protetta da password, accedi prima alla UI della app nello stesso browser.
2. Installa Tampermonkey nel browser che usi gia' per Monsieur Cuisine.
3. Dalla pagina di MonsieurAPP premi `Installa o aggiorna in questo browser`: la app apre prima una guida con la versione corrente dello script e da li' puoi lanciare Tampermonkey in una nuova scheda.
4. Se il prompt non compare, usa `Scarica script`: il browser proporra' il nome `monsieur-cuisine-bridge.txt`. Poi apri il file e incollane il contenuto in un nuovo script Tampermonkey, oppure importalo manualmente se il browser o l'estensione lo consentono.
5. Salva o conferma l'aggiornamento.

Nota: in alcuni browser, dopo il click su `Aggiorna`, Tampermonkey puo' lasciare aperta una scheda nera. La guida di MonsieurAPP resta aperta apposta per non perdere il contesto.

## File

- [monsieur-cuisine-bridge.user.js](C:/sviluppo/Progetti/MonsieurAPP/scripts/tampermonkey/monsieur-cuisine-bridge.user.js)

## Limiti attuali

- E' un prototipo e dipende dal DOM reale di Monsieur Cuisine.
- Se scarichi lo script dalla pagina della app, il file viene configurato automaticamente per quell'istanza; il sorgente nel repository continua invece a puntare in fallback a `http://127.0.0.1:8000`.
- Non preme `Salva` in automatico.
- Il recupero del job finale passa dalla nostra app; se non esiste una ricetta confermata o l'istanza configurata non risponde, lo script non puo' compilare il form.
- Se Monsieur Cuisine richiede click o campi diversi da quelli previsti, bisognera' ritoccare i selettori.
