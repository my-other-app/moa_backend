import sys
sys.path.append('.')
import asyncio
from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload
from app.db.setup import AsyncSessionLocal
from app.api.events.models import Events
from app.api.events.schemas import EventCreateUpdateResponse

async def main():
    try:
        async with AsyncSessionLocal() as session:
            db_event = await session.execute(
                select(Events)
                .order_by(Events.id.desc())
                .options(
                    joinedload(Events.category),
                    joinedload(Events.club),
                    selectinload(Events.interests),
                    selectinload(Events.speakers),
                )
                .limit(1)
            )
            created_event = db_event.scalar_one_or_none()
            if not created_event:
                print("No events found")
                return
                
            print(f"Testing event ID: {created_event.id}")
            result = EventCreateUpdateResponse.model_validate(created_event)
            print("SUCCESS!")
    except Exception as e:
        print("ERROR:", type(e).__name__)
        print(e)
            
if __name__ == "__main__":
    asyncio.run(main())
