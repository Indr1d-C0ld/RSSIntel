-- RSSIntel — l'indice di ricerca segue anche le CANCELLAZIONI degli articoli.
-- Finora esisteva solo il trigger d'inserimento (items_ai): eliminando un feed,
-- la cascata toglieva gli articoli ma le loro righe restavano in items_fts
-- (2.048 trovate l'08/10/2026). Verificato che il trigger scatta anche quando la
-- cancellazione arriva dalla cascata della chiave esterna.
-- Rieseguibile senza danni. Come www-data:
--   sudo -u www-data sqlite3 /percorso/del/rssintel.db < 2026-10-08_fts_cancellazioni.sql
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  DELETE FROM items_fts WHERE rowid = old.id;
END;
-- pulizia una tantum delle righe gia' orfane
DELETE FROM items_fts WHERE rowid NOT IN (SELECT id FROM items);
