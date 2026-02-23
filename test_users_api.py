import asyncio
from sqlalchemy import select, func
from app.api.users.models import Users
from app.db.core import async_session_maker

async def test():
    async with async_session_maker() as session:
        base_query = select(Users)
        count_query = select(func.count(Users.id)).select_from(base_query.subquery())
        total_result = await session.execute(count_query)
        total = total_result.scalar() or 0
        print(f"Total (from subquery): {total}")
        
        count_query_2 = select(func.count(Users.id))
        total_result_2 = await session.execute(count_query_2)
        total_2 = total_result_2.scalar() or 0
        print(f"Total (direct): {total_2}")

asyncio.run(test())
