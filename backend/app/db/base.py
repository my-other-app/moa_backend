from sqlalchemy import DDL, event
from sqlalchemy.ext.declarative import declarative_base

_Base = declarative_base()

AbstractSQLModel = _Base

# Trigram indexes (events, clubs) need pg_trgm before any table is made. In
# production the migration discover_perf creates it; this covers create_all,
# which the test suite uses.
event.listen(AbstractSQLModel.metadata, "before_create", DDL("CREATE EXTENSION IF NOT EXISTS pg_trgm").execute_if(dialect="postgresql"))
# search_text is unaccented (app.api.search.text); the migration w2_search creates it in production.
event.listen(AbstractSQLModel.metadata, "before_create", DDL("CREATE EXTENSION IF NOT EXISTS unaccent").execute_if(dialect="postgresql"))
