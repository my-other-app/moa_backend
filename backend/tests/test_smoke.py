async def test_ping(client):
    response = await client.get("/api/v1/../ping")
    # /ping is mounted at the app root, not under /api/v1
    response = await client.request("HEAD", "/ping")
    assert response.status_code == 204


async def test_create_user_and_query(session, make_user):
    from sqlalchemy import select
    from app.api.users.models import Users

    user = await make_user(email="smoke@example.com")
    result = await session.execute(select(Users).where(Users.id == user.id))
    assert result.scalar_one().email == "smoke@example.com"
