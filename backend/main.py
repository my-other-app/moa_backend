import uvicorn
from app.config import settings
from app.core import config_checks

if __name__ == "__main__":
    # The production settings check (app/core/config_checks.py;
    # docs/OPERATIONS.md, section 19). Under APP_CONFIG_CHECKS=enforce a
    # missing, placeholder or half-set secret stops it here, before uvicorn
    # starts its workers: the process exits 78 and Docker's restart backs
    # off, rather than workers refusing in a loop. Under warn, the default,
    # each worker's startup reports the findings, with Sentry on.
    config_checks.enforce_or_exit(report=False)
    # APP_LOG_JSON: every line JSON, uvicorn's own included (app/core/
    # observability.py). Otherwise uvicorn's default logging, as before.
    log_options = {}
    if settings.LOG_JSON:
        from app.core.observability import logging_config

        log_options = {"log_config": logging_config()}
    uvicorn.run(
        "doth:application",
        host="0.0.0.0",
        port=settings.PORT,
        workers=settings.WORKERS,
        reload=settings.DEBUG,
        # nginx in front logs every request already; APP_ACCESS_LOG=false
        # drops uvicorn's copy. httptools (requirements.txt) parses HTTP when
        # installed, rather than the pure-Python h11.
        access_log=settings.ACCESS_LOG,
        # On SIGTERM, wait at most this long for requests in flight (and the
        # background tasks they run) before the app's own shutdown, which
        # finishes or hands over the mail in hand (doth.shutdown). Unset, it
        # waited for ever and Docker's kill came first, so that never ran.
        timeout_graceful_shutdown=4,
        **log_options,
    )
