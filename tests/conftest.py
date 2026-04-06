"""Test fixtures — uses SQLite for fast, isolated tests."""

import os

# Override DATABASE_URL before any app imports
os.environ["DATABASE_URL"] = "sqlite:///./test.db"
os.environ["ANGI_API_KEY"] = "test-key"
os.environ["RESEND_API_KEY"] = ""
os.environ["SENDER_EMAIL"] = "test@example.com"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Tenant, AngiMapping
from app.db.session import get_db, get_bypass_db, get_admin_db
from app.routers.console import get_console_db
from app.main import create_app


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def set_sqlite_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=eng)
    return eng


@pytest.fixture
def db(engine):
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def client(db):
    app = create_app()

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_bypass_db] = override_get_db
    app.dependency_overrides[get_admin_db] = override_get_db
    app.dependency_overrides[get_console_db] = override_get_db

    with TestClient(app) as c:
        yield c


@pytest.fixture
def seeded_db(db):
    """DB with demo tenants and mappings."""
    t1 = Tenant(
        name="Hoffmann Brothers", slug="hoffmann-brothers",
        brand_color="#1e3a5f", phone="(314) 555-0101",
        email="service@hoffmannbros.example.com", email_from_name="Hoffmann Brothers",
        timezone="America/Chicago",
    )
    t2 = Tenant(
        name="Paschal Air, Plumbing & Electric", slug="paschal-air",
        brand_color="#e63946", phone="(479) 555-0102",
        email="leads@paschalair.example.com", email_from_name="Paschal Air",
        timezone="America/Chicago",
    )
    db.add_all([t1, t2])
    db.flush()

    db.add(AngiMapping(al_account_id="100001", tenant_id=t1.id))
    db.add(AngiMapping(al_account_id="100002", tenant_id=t2.id))
    db.flush()

    return db


@pytest.fixture
def seeded_client(seeded_db):
    app = create_app()

    def override_get_db():
        yield seeded_db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_bypass_db] = override_get_db
    app.dependency_overrides[get_admin_db] = override_get_db
    app.dependency_overrides[get_console_db] = override_get_db

    with TestClient(app) as c:
        yield c


SAMPLE_LEAD = {
    "FirstName": "Jane",
    "LastName": "Doe",
    "PhoneNumber": "5551234567",
    "PostalAddress": {
        "AddressFirstLine": "123 Main St",
        "AddressSecondLine": "",
        "City": "St. Louis",
        "State": "MO",
        "PostalCode": "63101",
    },
    "Email": "jane.doe@example.com",
    "Source": "Angie's List Quote Request",
    "Description": "Need AC repair, unit not cooling.",
    "Category": "St. Louis - HVAC Repair",
    "Urgency": "This Week",
    "CorrelationId": "test-corr-001",
    "ALAccountId": "100001",
}
