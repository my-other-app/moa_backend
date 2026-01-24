#!/usr/bin/env python3
"""
Script to add a volunteer to the database.
Run this from the backend directory with the .env file available.

Usage: python add_volunteer.py
"""
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
import os

# You need to set these environment variables or replace with actual values
DATABASE_URL = os.environ.get('APP_DATABASE_URL', 'postgresql+asyncpg://user:pass@localhost:5432/dbname')

async def add_volunteer():
    # Email of the volunteer to add
    email = "muhammedr7025@gmail.com"
    
    engine = create_async_engine(DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with async_session() as session:
        # First, get the user ID for this email
        result = await session.execute(
            text("SELECT id FROM users WHERE email = :email"),
            {"email": email}
        )
        user_row = result.fetchone()
        user_id = user_row[0] if user_row else None
        
        # Get a recent event to add volunteer to
        result = await session.execute(
            text("SELECT id, name, club_id FROM events WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 1")
        )
        event_row = result.fetchone()
        
        if not event_row:
            print("No events found in database")
            return
        
        event_id = event_row[0]
        event_name = event_row[1]
        club_id = event_row[2]
        
        print(f"Adding {email} as volunteer for event '{event_name}' (ID: {event_id})")
        
        # Check if already a volunteer
        result = await session.execute(
            text("SELECT id FROM volunteers WHERE email = :email AND event_id = :event_id AND deleted_at IS NULL"),
            {"email": email, "event_id": event_id}
        )
        if result.fetchone():
            print("Already a volunteer for this event!")
            return
        
        # Insert volunteer record
        await session.execute(
            text("""
                INSERT INTO volunteers (email, event_id, user_id, club_id, is_approved, created_at, updated_at)
                VALUES (:email, :event_id, :user_id, :club_id, true, NOW(), NOW())
            """),
            {"email": email, "event_id": event_id, "user_id": user_id, "club_id": club_id}
        )
        await session.commit()
        
        print(f"✅ Successfully added {email} as volunteer for event ID {event_id}")

if __name__ == "__main__":
    asyncio.run(add_volunteer())
