import uvicorn
from app.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "doth:application",
        host="0.0.0.0",
        port=settings.PORT,
        workers=settings.WORKERS,
        reload=settings.DEBUG,
    )
