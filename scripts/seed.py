"""Seed the database with demo tenants and Angi account mappings.

Usage:
    python -m scripts.seed
    python -m scripts.seed --reset
"""

import argparse
import sys

from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import SessionLocal
from app.models import Base, Tenant, AngiMapping


DEMO_TENANTS = [
    {
        "name": "Apex HVAC Indianapolis",
        "slug": "apex-hvac",
        "brand_color": "#2563eb",
        "phone": "(317) 555-0101",
        "email": "service@apexhvac.example.com",
        "timezone": "America/New_York",
        "email_from_name": "Apex HVAC",
        "intro_template": (
            "Hi {{ first_name }},\n\n"
            "Thanks for reaching out about {{ category or 'your HVAC needs' }}! "
            "Our certified technicians are ready to help. We pride ourselves on same-day "
            "service and transparent pricing.\n\n"
            "We'll be in touch shortly to confirm your availability and provide a detailed estimate."
        ),
        "al_account_ids": ["100001"],
    },
    {
        "name": "BlueWave Plumbing Co",
        "slug": "bluewave-plumbing",
        "brand_color": "#0d9488",
        "phone": "(317) 555-0102",
        "email": "hello@bluewaveplumbing.example.com",
        "timezone": "America/New_York",
        "email_from_name": "BlueWave Plumbing",
        "intro_template": (
            "Hello {{ first_name }},\n\n"
            "We got your request about {{ category or 'plumbing services' }}. "
            "BlueWave has been serving Indianapolis for over 15 years and we'd love to help.\n\n"
            "One of our licensed plumbers will reach out to schedule a convenient time for you."
        ),
        "al_account_ids": ["100002"],
    },
    {
        "name": "Spark Electric Services",
        "slug": "spark-electric",
        "brand_color": "#d97706",
        "phone": "(317) 555-0103",
        "email": "info@sparkelectric.example.com",
        "timezone": "America/New_York",
        "email_from_name": "Spark Electric",
        "intro_template": (
            "Hey {{ first_name }}!\n\n"
            "Your {{ category or 'electrical service' }} request came through. "
            "Spark Electric handles everything from panel upgrades to outlet installations.\n\n"
            "We'll follow up shortly to get you on the schedule."
        ),
        "al_account_ids": ["100003"],
    },
]


def seed(reset: bool = False) -> None:
    db = SessionLocal()
    try:
        if reset:
            print("Resetting: dropping and recreating tables...")
            from app.db.session import engine
            Base.metadata.drop_all(engine)
            Base.metadata.create_all(engine)

        existing = db.query(Tenant).count()
        if existing > 0 and not reset:
            print(f"Database already has {existing} tenant(s). Skipping seed (use --reset to reseed).")
            return

        for t_data in DEMO_TENANTS:
            al_account_ids = t_data.pop("al_account_ids")

            # Check if tenant already exists by slug
            existing_tenant = db.query(Tenant).filter(Tenant.slug == t_data["slug"]).first()
            if existing_tenant:
                print(f"  Tenant '{t_data['name']}' already exists, skipping.")
                t_data["al_account_ids"] = al_account_ids  # restore for next run
                continue

            tenant = Tenant(**t_data)
            db.add(tenant)
            db.flush()

            for acc_id in al_account_ids:
                existing_mapping = db.query(AngiMapping).filter(
                    AngiMapping.al_account_id == acc_id
                ).first()
                if not existing_mapping:
                    db.add(AngiMapping(al_account_id=acc_id, tenant_id=tenant.id))

            print(f"  Created tenant: {tenant.name} (ALAccountIds: {al_account_ids})")
            t_data["al_account_ids"] = al_account_ids  # restore

        db.commit()
        total = db.query(Tenant).count()
        mappings = db.query(AngiMapping).count()
        print(f"\nDone. {total} tenants, {mappings} account mappings.")
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Seed demo data")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate tables first")
    args = parser.parse_args()
    seed(reset=args.reset)


if __name__ == "__main__":
    main()
