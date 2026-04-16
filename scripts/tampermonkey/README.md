# Monsieur Cuisine Bridge

Userscript Tampermonkey per il flusso app-to-browser:

1. Importa la ricetta dentro MonsieurAPP.
2. In MonsieurAPP controlla adattamenti, porzioni e scegli la base finale (`originale` o `proposta AI`).
3. Clicca `Conferma ricetta pronta`.
4. Dalla pagina job clicca `Apri con Tampermonkey`, oppure vai direttamente su Monsieur Cuisine.
5. Nella pagina Monsieur Cuisine compare un pannello con il bottone `Leggi ricetta confermata`.
6. Lo script recupera dalla app locale il payload del job confermato indicato nell'hash `#mc-import=...`; se manca, usa l'ultima ricetta confermata disponibile.
7. Lo script prova a compilare titolo, porzioni, tempo totale, ingredienti e tutti i passaggi esportati; quelli non macchina vengono inseriti come step descrittivi con solo titolo e descrizione, gli step `Bilancia` restano singoli con descrizione nello stesso passaggio e, se il payload contiene `targetedWeight`, lo script tenta di compilare anche il campo `Peso`, mentre gli step `Cottura personalizzata` vengono sdoppiati in uno step descrittivo e uno step tecnico con lo stesso titolo.
8. Controlla il risultato e premi `Salva` manualmente.

## Installazione

1. Installa Tampermonkey nel browser che usi gia' per Monsieur Cuisine.
2. Apri Tampermonkey e crea un nuovo script.
3. Sostituisci il contenuto con il file `monsieur-cuisine-bridge.user.js`.
4. Salva.

## File

- [monsieur-cuisine-bridge.user.js](C:/sviluppo/Progetti/MonsieurAPP/scripts/tampermonkey/monsieur-cuisine-bridge.user.js)

## Limiti attuali

- E' un prototipo e dipende dal DOM reale di Monsieur Cuisine.
- Se scarichi lo script dalla pagina della app, il file viene configurato automaticamente per quell'istanza; il sorgente nel repository continua invece a puntare in fallback a `http://127.0.0.1:8000`.
- Non preme `Salva` in automatico.
- Il recupero del job finale passa dalla nostra app; se non esiste una ricetta confermata o la app non risponde, lo script non puo' compilare il form.
- Se Monsieur Cuisine richiede click o campi diversi da quelli previsti, bisognera' ritoccare i selettori.
