-- RSSIntel — classificazione delle fonti (fase 1). Una volta sola, come www-data:
--   sudo -u www-data sqlite3 /percorso/del/rssintel.db < migrate_sources.sql
ALTER TABLE feeds ADD COLUMN category         TEXT;  -- tipo di testata (vedi source_categories())
ALTER TABLE feeds ADD COLUMN reliability      TEXT;  -- A-F, codice dell'Ammiragliato; NULL = non valutata
ALTER TABLE feeds ADD COLUMN reliability_note TEXT;  -- motivazione del grado
