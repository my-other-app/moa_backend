"""A store item made straight in the database, as a test sets one up.

Items are the club's and an event sells only those it offers, so an item
made for an event needs both: its club, and the event's offer of it. The
API does the same (store.service.create_item with an event)."""
import uuid

from app.api.store.models import EventStoreItems, EventStoreOffers


def shelf_item(session, event, **fields) -> EventStoreItems:
    """An item of the event's club, offered on the event; added to the session, not flushed."""
    item = EventStoreItems(id=uuid.uuid4(), club_id=event.club_id, event_id=event.id, **fields)
    session.add(item)
    session.add(EventStoreOffers(event_id=event.id, item_id=item.id, sort_order=fields.get("sort_order", 0)))
    return item
