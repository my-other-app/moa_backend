import asyncio
import sys
import os
import logging

# Disable SQLAlchemy logging
logging.basicConfig()
logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)

# Add path to backend
sys.path.append('/backend')

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select

# Import models
import app.api.users.models
import app.api.clubs.models
import app.api.events.models
import app.api.events.volunteer.models
from app.api.events.models import EventRegistrationsLink

# Import service
from app.api.events.registration.service import mark_attendance

# Hardcoded DB URL
DATABASE_URL = "postgresql+asyncpg://admin:LOztplNdOw50d2025BsRAnaWnCad269yrV7BfUwavc@192.168.5.101:5432/doth"

async def main():
    try:
        engine = create_async_engine(DATABASE_URL)
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        
        async with async_session() as session:
            print("Finding a registration for Event 32...")
            reg = await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == 32).limit(1))
            if not reg:
                print("No registration found for Event 32")
                return
            
            print(f"Found registration: {reg.id} (Attended: {reg.is_attended})")
            
            # Test Mark Attendance (True)
            print("\nMarking as Attended (True)...")
            updated_reg = await mark_attendance(session, user_id=2, event_id=32, registration_id=str(reg.id), is_attended=True)
            print(f"Updated: {updated_reg.is_attended}, Attended On: {updated_reg.attended_on}")
            
            if updated_reg.is_attended:
                print("SUCCESS: Marked as Attended")
            else:
                print("FAILURE: Failed to mark as Attended")

            # Test Mark Attendance (False)
            print("\nMarking as Not Attended (False)...")
            updated_reg = await mark_attendance(session, user_id=2, event_id=32, registration_id=str(reg.id), is_attended=False)
            print(f"Updated: {updated_reg.is_attended}, Attended On: {updated_reg.attended_on}")

            if not updated_reg.is_attended:
                print("SUCCESS: Marked as Not Attended")
            else:
                print("FAILURE: Failed to mark as Not Attended")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
