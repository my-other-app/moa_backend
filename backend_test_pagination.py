import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.api.clubs.service import get_all_clubs

DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/moa"

async def test():
    engine = create_async_engine(DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        clubs = await get_all_clubs(session, limit=2)
        print(clubs)

asyncio.run(test())
