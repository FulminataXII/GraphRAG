-- Runs once, only against a fresh (empty) Postgres data directory — the official postgres
-- image's own entrypoint convention for /docker-entrypoint-initdb.d. A volume that predates
-- this file already has its databases and is left untouched; see MANUAL.md / docker-compose.yml
-- for how to create them by hand on such a volume.
--
-- One Postgres instance serves three tools (graphrag, LiteLLM, Phoenix). Each gets its OWN
-- database so any one of them can be reset or migrated without touching the others' tables —
-- sharing `public` (the original setup) meant Phoenix couldn't be reset without dropping
-- LiteLLM's tables too, and vice versa. `${POSTGRES_DB}` (graphrag's own database) is already
-- created by the postgres image itself from POSTGRES_DB; this script adds the other two.
--
-- No explicit OWNER clause: the postgres image's entrypoint runs every init script connected
-- AS $POSTGRES_USER, and `.sql` files (unlike `.sh` scripts) are piped straight to psql with no
-- shell/env-var substitution — an `OWNER ${POSTGRES_USER}` here would be a literal, invalid
-- role name, not an expanded value. Omitting OWNER is both correct and simpler: CREATE DATABASE
-- defaults the owner to the connecting user, which already is $POSTGRES_USER.
CREATE DATABASE phoenix;
CREATE DATABASE litellm;
