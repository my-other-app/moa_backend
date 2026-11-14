"""Two people saving each other at the same moment, in two real transactions
on two connections (not the suite's one rolled-back transaction): both rows
end mutual. And a removal racing a save leaves the saver's row not mutual.

The pair's critical section is held open a little (a sleep after the lock)
so the second transaction is sure to arrive while the first is inside it.
"""
import asyncio
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.profiles import network
from app.api.profiles.models import NetworkPeople
from app.api.profiles.schemas import SaveIn
from app.api.users.models import UserProfiles, Users, UserTypes


async def test_saving_each_other_at_once_leaves_both_rows_mutual(_engine, monkeypatch):
    from app.db.core import SESSION_KWARGS
    from app.db.listeners import add_loader_criteria

    maker = async_sessionmaker(bind=_engine, **SESSION_KWARGS)
    tag = uuid.uuid4().hex[:8]
    handles = (f"race-a-{tag}", f"race-b-{tag}")
    async with maker() as s:
        people = [Users(full_name=h, username=h, email=f"{h}@example.com", user_type=UserTypes.app_user) for h in handles]
        s.add_all(people)
        await s.flush()
        # No notices: this is about the rows.
        s.add_all([UserProfiles(user_id=p.id, full_name=p.full_name, notify_saves=False) for p in people])
        await s.commit()
        a_id, b_id = (p.id for p in people)

    real_lock = network.lock_pair

    async def slow_lock(session, x, y):
        await real_lock(session, x, y)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(network, "lock_pair", slow_lock)

    async def saves(viewer_id: int, handle: str):
        async with maker() as s:
            add_loader_criteria(s)
            viewer = await s.get(Users, viewer_id)
            return await network.save(s, viewer, SaveIn(method="search", handle=handle))

    async def removes(viewer_id: int, person_id: int):
        async with maker() as s:
            add_loader_criteria(s)
            viewer = await s.get(Users, viewer_id)
            np_id = await s.scalar(select(NetworkPeople.id).where(NetworkPeople.owner_id == viewer_id, NetworkPeople.person_id == person_id))
            await network.remove(s, viewer, np_id)

    async def rows():
        async with maker() as s:
            return {
                (r.owner_id, r.person_id): r.mutual
                for r in (await s.scalars(select(NetworkPeople).where(NetworkPeople.owner_id.in_((a_id, b_id))))).all()
            }

    try:
        for _ in range(3):
            got = await asyncio.gather(saves(a_id, handles[1]), saves(b_id, handles[0]))
            assert all(g["created"] for g in got)
            assert await rows() == {(a_id, b_id): True, (b_id, a_id): True}
            # One answer saw the other's row committed and says mutual.
            assert any(g["person"]["mutual"] for g in got)

            # A removes B while B saves A again: B's row ends not mutual.
            await asyncio.gather(removes(a_id, b_id), saves(b_id, handles[0]))
            assert await rows() == {(b_id, a_id): False}

            async with maker() as s:
                await s.execute(delete(NetworkPeople).where(NetworkPeople.owner_id.in_((a_id, b_id))))
                await s.commit()
    finally:
        async with maker() as s:
            await s.execute(delete(NetworkPeople).where(NetworkPeople.owner_id.in_((a_id, b_id))))
            await s.execute(delete(UserProfiles).where(UserProfiles.user_id.in_((a_id, b_id))))
            await s.execute(delete(Users).where(Users.id.in_((a_id, b_id))))
            await s.commit()
