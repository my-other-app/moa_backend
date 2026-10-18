import uvicorn
from app.config import settings

if __name__ == "__main__":
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
    )
