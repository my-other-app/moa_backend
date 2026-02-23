import asyncio
from fastapi import Request
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.api.events.router import list_all_events_admin
from app.core.response.pagination import PaginationParams
import os
import sys

# Ensure backend matches python path
sys.path.append(os.path.join(os.path.dirname(__file__), "backend"))

DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/moa"

class MockRequest:
    def __init__(self):
        self.url = type('obj', (object,), {'path': '/test'})
        self.query_params = {}

class MockAdmin:
    id = 1
    role = "admin"

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
            print("SUCCESS! Data:")
            for item in res['items']:
                print(f"- {item['name']}")
        except Exception as e:
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test())
