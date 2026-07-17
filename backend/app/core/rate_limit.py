from slowapi import Limiter
from slowapi.util import get_remote_address

# In-memory store (no Redis in this stack today, and WORKERS=1 in production
# currently, so per-process limits are accurate). If the app is ever scaled to
# multiple workers/instances, switch to a Redis-backed storage_uri so limits
# are shared across processes.
limiter = Limiter(key_func=get_remote_address)
