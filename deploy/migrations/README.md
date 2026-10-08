# Migrazioni

Servono solo a chi **aggiorna** un'installazione esistente: `schema.sql`
contiene gia' tutto per le installazioni nuove.

Ognuna va eseguita **una sola volta** (SQLite non ha `ADD COLUMN IF NOT EXISTS`:
una seconda esecuzione fallisce con "duplicate column name", senza danni), come
l'utente che possiede il database:

```bash
sudo -u www-data sqlite3 /percorso/del/rssintel.db < 2026-10-08_allerte.sql
```

Non sono comunque indispensabili: la webapp aggiunge da sola le colonne mancanti
(`watch_ensure()` al primo salvataggio di una ricerca, `feeds_classification_ensure()`
alla prima apertura di Feeds). Eseguirle porta il database subito allo stato
completo, cosi' anche il worker delle allerte trova le tabelle dal primo giro.

| File | Cosa aggiunge |
|---|---|
| `2026-10-08_allerte.sql` | colonne `watch`, `last_checked_at`, `last_error` su `saved_searches`; tabella `watch_hits` |
| `2026-10-08_fonti.sql` | colonne `category`, `reliability`, `reliability_note` su `feeds` |
