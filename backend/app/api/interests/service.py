from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.interests.models import InterestCategory, InterestIconType, Interests
from app.core.validations.schema import validate_relations, validate_unique
from app.response import CustomHTTPException


async def create_interest_category(
    session: AsyncSession,
    name: str,
    icon: str | None = None,
    icon_type: InterestIconType | None = None,
):
    await validate_unique(session, unique={"name": (InterestCategory, name)})
    if icon_type and not icon:
        raise CustomHTTPException(
            400, "Invalid Request", errors={"icon": "This field is required."}
        )
    interest_category = InterestCategory(name=name, icon=icon, icon_type=icon_type)

    session.add(interest_category)
    await session.commit()
    await session.refresh(interest_category)
    return interest_category


async def create_interest(
    session: AsyncSession,
    name: str,
    category_id: int,
    icon_type: InterestIconType | None = None,
    icon: str | None = None,
):
    await validate_relations(session, {"category_id": (InterestCategory, category_id)})
    await validate_unique(session, unique={"name": (Interests, name)})
    if icon_type and not icon:
        raise CustomHTTPException(
            400, "Invalid Request", errors={"icon": "This field is required."}
        )
    interet = Interests(
        name=name, icon=icon, icon_type=icon_type, category_id=category_id
    )
    session.add(interet)
    await session.commit()
    await session.refresh(interet)
    return interet


async def list_interests(session: AsyncSession):
    query = select(Interests).options(joinedload(Interests.category))
    result = await session.execute(query)
    data = {}
    for interest in result.scalars():
        if not data.get(interest.category.id):
            data[interest.category.id] = {
                "id": interest.category.id,
                "name": interest.category.name,
                "icon": interest.category.icon,
                "icon_type": interest.category.icon_type,
                "interests": [
                    {
                        "id": interest.id,
                        "name": interest.name,
                        "icon": interest.icon,
                        "icon_type": interest.icon_type,
                    }
                ],
            }
        else:
            data[interest.category.id]["interests"].append(
                {
                    "id": interest.id,
                    "name": interest.name,
                    "icon": interest.icon,
                    "icon_type": interest.icon_type,
                }
            )
    return list(data.values())
