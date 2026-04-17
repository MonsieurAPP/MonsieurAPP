# Deploy su Render

Questa app e' pronta per essere pubblicata su Render come web service Docker.

## Cosa fa la configurazione attuale

- usa `render.yaml` per creare un web service Docker;
- imposta un `dockerCommand` esplicito che avvia Uvicorn su `0.0.0.0:$PORT`, evitando dipendenze dal launcher locale;
- espone health check su `/health` e mantiene anche l'alias API `/api/health`;
- gli endpoint `/health` e `/api/health` restituiscono anche metadati operativi (`timestamp`, `uptime`, `service`) oltre ai flag AI;
- mantiene `numInstances: 1` perche' i job sono in memoria e non sono condivisi tra istanze;
- monta un disco persistente su `/app/persisted-data` per conservare il prompt template personalizzato tra restart e deploy;
- chiede in fase di creazione le variabili `AI_API_KEY`, `AI_MODEL` e `AI_BASE_URL`.

## Passi in Render

1. Pubblica il repository su GitHub o GitLab.
2. In Render crea un nuovo Blueprint e collega il repository.
3. Durante la creazione conferma il servizio `monsieurapp` definito in `render.yaml`.
4. Inserisci i valori di `AI_API_KEY` e `AI_MODEL` quando Render li richiede.
5. Se stai usando un provider OpenAI-compatible diverso da OpenAI, imposta anche `AI_BASE_URL` con l'endpoint corretto del provider. Esempio Groq: `https://api.groq.com/openai/v1`.
6. Attendi il primo deploy e verifica `https://<tuo-servizio>.onrender.com/health` oppure `https://<tuo-servizio>.onrender.com/api/health`.

## Variabili utili

- `AI_API_KEY`: chiave del provider AI.
- `AI_MODEL`: modello AI da usare.
- `AI_BASE_URL`: obbligatoria se usi un endpoint OpenAI-compatible diverso dal default `https://api.openai.com/v1`.
- `AI_CHAT_COMPLETIONS_PATH`: opzionale, default `/chat/completions`.
- `AI_TIMEOUT_SECONDS`: opzionale, default `45`.
- `PUBLIC_APP_BASE_URL`: opzionale, utile solo se vuoi forzare un URL pubblico specifico invece di usare gli header proxy.

## Note operative

- La cronologia job e l'ultima ricetta confermata sono in memoria: si azzerano a ogni restart o nuovo deploy.
- Il file locale `.env.local` non entra nell'immagine Docker: i valori AI vanno copiati esplicitamente nelle environment variables di Render.
- Lo userscript Tampermonkey va riscaricato dalla pagina della app deployata, cosi' viene configurato per il dominio Render corrente.
- Se nel pannello Tampermonkey compare ancora un riferimento a `127.0.0.1` o a un vecchio host, significa che nel browser e' rimasta una versione precedente dello script: riscaricalo dalla pagina deployata e reinstallalo sopra la versione gia' presente.
- Le cartelle locali di sviluppo come `playwright_state`, `debug` e i file `.env*` sono escluse dal build context Docker.