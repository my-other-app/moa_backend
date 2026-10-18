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
    )
