-- ask_all.db has no schema of its own: data/sync_all.py copies the tables of BI-02, BI-03 and LC-04 into it (prefixed bi02_, bi03_, lc04_).
-- This file exists only so db._paths("ask") has a schema path; running init_db("ask") is a no-op.
SELECT 1;
