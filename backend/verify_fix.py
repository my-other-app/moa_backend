import sys
import os
from datetime import datetime
from uuid import uuid4

# Add the current directory to sys.path to allow imports from app
sys.path.append(os.getcwd())

from app.api.events.models import EventRegistrationsLink
from app.api.events.registration.schemas import EventRegistrationPublicMin
from app.api.users.schemas import UserPublic

def test_schema_serialization():
    # Mock UserPublic
    user_mock = UserPublic(
        id=1,
        user_type="guest",
        full_name="John Doe",
        email="john@example.com",
        phone="1234567890",
        username="johndoe"
    )

    # Mock EventRegistrationsLink
    registration = EventRegistrationsLink(
        id=uuid4(),
        event_id=1,
        user_id=1,
        ticket_id="TICKET-123",
        full_name="John Doe",
        email="john@example.com",
        phone="1234567890",
        is_attended=False,
        is_paid=True,
        actual_amount=100.0,
        paid_amount=100.0,
        is_won=False,
        created_at=datetime.now(),
        additional_details={"College Name": "Test University"}
    )
    
    # Manually attach the user relationship mock since we are not using DB
    registration.user = user_mock

    # Validate with schema
    # We need to ensure from_attributes=True is working and properties are accessed
    schema_obj = EventRegistrationPublicMin.model_validate(registration)

    print(f"Name: {schema_obj.name}")
    print(f"Institution: {schema_obj.institution}")

    assert schema_obj.name == "John Doe"
    assert schema_obj.institution == "Test University"
    print("Verification Successful!")

if __name__ == "__main__":
    test_schema_serialization()
