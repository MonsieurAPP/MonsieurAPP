# Proposta: fase AI intermedia per adattare ricette a Monsieur Cuisine

## Obiettivo

Inserire una fase opzionale di adattamento tra:

1. estrazione ricetta dalla sorgente;
2. revisione umana;
3. upload o compilazione assistita verso Monsieur Cuisine.

La fase AI non deve "reinventare" la ricetta. Deve invece:

- rendere piu' strutturati gli step;
- evidenziare ambiguita';
- proporre una versione piu' adatta al form Monsieur Cuisine;
- lasciare sempre visibile l'originale per confronto e conferma.

## Valutazione sintetica

### Quando conviene

La fase AI ha valore soprattutto per ricette narrative, in particolare GialloZafferano, dove:

- gli step sono spesso descrittivi e poco strutturati;
- tempi, temperature e velocita' non sono quasi mai disponibili in campi separati;
- serve trasformare testo editoriale in istruzioni operative compatibili con Monsieur Cuisine.

### Quando non conviene

Non conviene usarla come passaggio obbligatorio per tutte le sorgenti.

Per Cookidoo il parsing deterministico esistente e' gia' piu' vicino al formato target, quindi il ritorno dell'AI sarebbe inferiore rispetto a costo, latenza e rischio.

## Posizionamento corretto nel flusso

La fase AI deve stare nel backend, non nello userscript Tampermonkey.

Motivi:

- le chiavi API non devono stare nel browser;
- il prompt deve essere versionato e controllabile;
- il risultato deve essere loggato e confrontabile con l'estrazione originale;
- si puo' introdurre una revisione umana chiara nella UI gia' esistente;
- e' piu' semplice gestire fallback e regole di sicurezza.

## Flusso proposto

### Flusso alto livello

1. L'utente invia l'URL della ricetta.
2. Il backend estrae la ricetta con parser deterministico.
3. Il sistema valuta se l'adattamento AI e' necessario.
4. Se necessario, genera una proposta AI strutturata.
5. La UI mostra originale, proposta AI, warning e differenze.
6. L'utente approva la proposta oppure usa la versione originale.
7. Solo dopo avviene la compilazione assistita o l'upload.

### Regola decisionale minima

La fase AI parte solo se almeno una delle condizioni seguenti e' vera:

- sorgente = `giallozafferano`;
- meno del 30% degli step ha almeno uno tra tempo, temperatura o velocita';
- sono presenti segnali di ambiguita' testuale come "finche'", "quanto basta", "a piacere", "regolatevi";
- l'utente richiede esplicitamente l'adattamento AI.

## Principi di sicurezza

La fase AI deve essere vincolata da regole forti.

### Regole consentite

- riscrivere gli step in forma piu' operativa;
- separare uno step molto lungo in piu' step brevi;
- normalizzare velocita' e segnali di reverse;
- proporre campi mancanti solo se deducibili in modo ragionevole dal testo;
- generare warning quando manca certezza.

### Regole vietate

- inventare ingredienti;
- modificare quantita' o unita' senza forte evidenza testuale;
- introdurre temperature, velocita' o tempi arbitrari;
- cambiare la tecnica di preparazione in modo sostanziale;
- eliminare informazioni originali senza mantenerne traccia.

## Contratto dati consigliato

La proposta AI non dovrebbe sovrascrivere `RecipeData`. Conviene introdurre un oggetto separato, per esempio `RecipeAdaptation`.

### Modello concettuale

```json
{
  "source_recipe": {
    "title": "Risotto ai funghi",
    "ingredients": [],
    "structured_ingredients": [],
    "steps": []
  },
  "adaptation": {
    "status": "proposed",
    "model": "gpt-5.4",
    "prompt_version": "v1",
    "summary": "Ricetta resa piu' operativa per Monsieur Cuisine.",
    "warnings": [
      "Tempo di tostatura non esplicitato nella sorgente.",
      "Velocita' di mescolamento dedotta in modo conservativo."
    ],
    "adapted_steps": [
      {
        "description": "Inserire la cipolla nel boccale e tritare.",
        "duration_seconds": 10,
        "temperature_c": null,
        "speed": "7",
        "reverse": false,
        "confidence": "high",
        "rationale": "Presente nel testo originale come 'tritare la cipolla'.",
        "source_refs": [1]
      }
    ],
    "ingredient_notes": [
      {
        "ingredient_index": 3,
        "issue": "Quantita' ambigua",
        "suggestion": "Lasciare invariata la riga originale"
      }
    ]
  }
}
```

## Output minimo richiesto al modello

Per ogni step adattato servono:

- `description`;
- `duration_seconds`;
- `temperature_c`;
- `speed`;
- `reverse`;
- `confidence`;
- `rationale`;
- `source_refs`.

Il modello deve sempre distinguere tra:

- valore estratto o dedotto dal testo;
- valore non determinabile;
- warning da mostrare all'utente.

## Strategia prompt

### Input al modello

- metadati ricetta;
- ingredienti originali;
- ingredienti gia' strutturati dal parser;
- step originali;
- regole di compatibilita' Monsieur Cuisine;
- schema JSON di output obbligatorio.

### Istruzioni chiave del prompt

- usa solo le informazioni presenti nel testo o inferenze conservative;
- se manca evidenza, lascia il campo nullo;
- non cambiare dosi o ingredienti;
- mantieni lo stile operativo e conciso;
- genera warning espliciti per ogni punto ambiguo;
- rispondi solo con JSON valido.

## Integrazione UI consigliata

La pagina dettaglio import e' gia' il punto naturale di revisione.

### Sezioni da aggiungere

- stato adattamento AI: non richiesto, in corso, pronto, fallito;
- riepilogo warning;
- confronto tra step originali e step adattati;
- selettore `Usa originale` / `Usa proposta AI`;
- eventuale approvazione step-by-step per casi ambigui.

### Comportamento consigliato

- default su versione originale finche' l'utente non approva la proposta AI;
- evidenziazione visiva degli step con confidenza bassa;
- possibilita' di copiare sia il JSON originale sia quello adattato.

## Integrazione backend consigliata

### Nuovo servizio

Creare un servizio dedicato, ad esempio `app/services/recipe_adaptation.py`, con responsabilita' distinte:

- valutare se serve l'AI;
- costruire il prompt;
- invocare il modello;
- validare lo schema JSON di risposta;
- produrre warning e fallback.

### Configurazione runtime attuale

L'integrazione reale e' ora predisposta in modalita' OpenAI-compatible via HTTP.

Variabili ambiente supportate:

- `AI_API_KEY`: token del provider;
- `AI_MODEL`: modello da invocare;
- `AI_BASE_URL`: base URL API, default `https://api.openai.com/v1`;
- `AI_CHAT_COMPLETIONS_PATH`: path endpoint, default `/chat/completions`;
- `AI_TIMEOUT_SECONDS`: timeout richiesta, default `45`;
- `AI_ENABLED`: `1` o `0`, default `1`.

Se `AI_API_KEY` o `AI_MODEL` mancano, il sistema usa automaticamente il fallback deterministico senza bloccare il job.

### Nuovo stato job

Il job puo' aggiungere questi stati:

- `extracting`;
- `adapting`;
- `awaiting_review`;
- `completed`;
- `failed`.

`completed` dovrebbe significare "pronto per upload o copia", non necessariamente gia' pubblicato.

## Fallback

Se l'AI fallisce, il flusso non deve bloccarsi.

Fallback richiesti:

- mantenere sempre disponibile l'estrazione deterministica;
- mostrare l'errore AI come warning non bloccante;
- consentire comunque copia e compilazione manuale.

## Osservabilita'

Per rendere la fase AI difendibile nel tempo servono log minimi:

- sorgente ricetta;
- prompt version;
- modello usato;
- durata chiamata;
- esito validazione schema;
- numero warning;
- percentuale step adattati con confidenza bassa.

Non serve loggare il prompt completo se contiene dati inutili o se aumenta troppo il rumore, ma va almeno mantenuta una versione serializzabile della richiesta e della risposta in ambiente debug.

## Piano incrementale consigliato

### Fase 1

Solo design e persistenza dati:

- introdurre `RecipeAdaptation`;
- aggiungere stati job e campi UI;
- nessuna chiamata reale al modello.

### Fase 2

Modalita' shadow:

- eseguire l'adattamento AI solo per logging e confronto;
- non usarlo per l'output finale;
- raccogliere casi in cui migliora davvero la ricetta.

### Fase 3

Assistita con approvazione:

- mostrare la proposta AI all'utente;
- richiedere approvazione esplicita;
- usare la proposta solo se confermata.

### Fase 4

Automazione mirata:

- usare la proposta AI automaticamente solo per campi ad alta confidenza;
- mantenere i campi ambigui in revisione manuale.

## Raccomandazione finale

La fase AI e' consigliata, ma solo come livello di adattamento assistito nel backend.

La soluzione piu' prudente e utile e':

- parser deterministico sempre attivo;
- AI solo quando serve o su richiesta;
- output separato dalla ricetta originale;
- revisione umana obbligatoria prima dell'upload.

Questa impostazione massimizza il valore sul caso difficile, soprattutto GialloZafferano, senza introdurre una dipendenza opaca o fragile nel flusso principale.