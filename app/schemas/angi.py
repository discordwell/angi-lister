"""Pydantic models matching the Angi Standard Lead JSON feed spec."""

from pydantic import BaseModel, Field


class PostalAddressModel(BaseModel):
    AddressFirstLine: str = ""
    AddressSecondLine: str = ""
    City: str = ""
    State: str = ""
    PostalCode: str = ""


class AngiLeadPayload(BaseModel):
    """Strict schema for Angi lead payloads. All fields from the spec."""

    FirstName: str
    LastName: str
    PhoneNumber: str
    PostalAddress: PostalAddressModel = Field(default_factory=PostalAddressModel)
    Email: str
    Source: str = ""
    Description: str = ""
    Category: str = ""
    Urgency: str = ""
    CorrelationId: str
    ALAccountId: str


# The set of fields we expect from Angi — used for schema drift detection
EXPECTED_FIELDS = {
    "FirstName", "LastName", "PhoneNumber", "PostalAddress", "Email",
    "Source", "Description", "Category", "Urgency", "CorrelationId", "ALAccountId",
}

EXPECTED_ADDRESS_FIELDS = {
    "AddressFirstLine", "AddressSecondLine", "City", "State", "PostalCode",
}
