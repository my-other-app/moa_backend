import asyncio
from fastapi import Request
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.api.events.router import list_all_events_admin
from app.core.response.pagination import PaginationParams

DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/moa"

class MockRequest:
    def __init__(self):
        self.url = type('obj', (object,), {'path': '/test'})

class MockAdmin:
    pass

async def test():
    engine = create_async_engine(DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        try:
            res = await list_all_events_admin(
                request=MockRequest(),
                session=session,
                pagination=PaginationParams(limit=2, offset=0),
                user=MockAdmin()
            )
            print("SUCCESS")
        except Exception as e:
            import traceback
            traceback.print_exc()

asyncio.run(test())
