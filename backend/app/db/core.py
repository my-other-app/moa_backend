from typing import Annotated
from fastapi import Depends
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.db.listeners import add_loader_criteria
from app.config import settings
from app.db.registry import *

engine = create_async_engine(settings.DATABASE_URL)

AsyncSessionLocal = async_sessionmaker(autocommit=False, autoflush=False, bind=engine)


async def get_session():
    async with AsyncSessionLocal() as session:
        add_loader_criteria(session)
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
