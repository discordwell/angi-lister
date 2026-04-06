"""Seed the database with demo tenants and Angi account mappings.

Usage:
    python -m scripts.seed
    python -m scripts.seed --reset
"""

import argparse
import sys

from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import SessionLocal, set_tenant
from app.models import Base, Tenant, AngiMapping, TenantHomeBase, TenantJobRule, TenantSpecial


DEMO_TENANTS = [
    {
        "name": "Hoffmann Brothers",
        "slug": "hoffmann-brothers",
        "brand_color": "#1e3a5f",
        "phone": "(314) 555-0101",
        "email": "service@hoffmannbros.example.com",
        "timezone": "America/Chicago",
        "email_from_name": "Hoffmann Brothers",
        "intro_template": (
            "Hi {{ first_name }},\n\n"
            "Thanks for reaching out about {{ category or 'your home service needs' }}! "
            "Hoffmann Brothers has been serving the St. Louis area for over 40 years "
            "with trusted HVAC, plumbing, and electrical services.\n\n"
            "One of our certified technicians will be in touch shortly to confirm your "
            "availability and provide a detailed estimate."
        ),
        "al_account_ids": ["100001"],
    },
    {
        "name": "Paschal Air, Plumbing & Electric",
        "slug": "paschal-air",
        "brand_color": "#e63946",
        "phone": "(479) 555-0102",
        "email": "leads@paschalair.example.com",
        "timezone": "America/Chicago",
        "email_from_name": "Paschal Air",
        "intro_template": (
            "Hello {{ first_name }},\n\n"
            "We received your request about {{ category or 'home services' }}. "
            "Paschal Air, Plumbing & Electric has been Northwest Arkansas's go-to "
            "service provider for decades.\n\n"
            "A member of our team will reach out to schedule a convenient time for you."
        ),
        "al_account_ids": ["100002"],
    },
    {
        "name": "Heartland Home Services",
        "slug": "heartland-home",
        "brand_color": "#2d6a4f",
        "phone": "(816) 555-0103",
        "email": "info@heartlandhome.example.com",
        "timezone": "America/Chicago",
        "email_from_name": "Heartland Home Services",
        "intro_template": (
            "Hey {{ first_name }}!\n\n"
            "Your {{ category or 'HVAC service' }} request came through. "
            "Heartland Home Services keeps Midwest homes comfortable year-round.\n\n"
            "We'll follow up shortly to get you on the schedule."
        ),
        "al_account_ids": ["100003"],
    },
]


def seed(reset: bool = False) -> None:
    db = SessionLocal()
    set_tenant(db, "__bypass__")
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

        # --- Personalization config for Hoffmann Brothers -------------------------
        hoffmann = db.query(Tenant).filter(Tenant.slug == "hoffmann-brothers").first()
        if hoffmann and not db.query(TenantHomeBase).filter(TenantHomeBase.tenant_id == hoffmann.id).first():
            hoffmann.personalization_enabled = True
            hoffmann.sample_email = (
                "We got your request and we're excited to help! Hoffmann Brothers has been "
                "keeping St. Louis homes comfortable since 1978. Whether it's a quick fix or "
                "a full replacement, our certified techs have you covered.\n\n"
                "Let's get something on the calendar — reply to this email or give us a call."
            )
            hoffmann.pricing_tiers = [
                {"max_mi": 1, "text": "$39 diagnostic"},
                {"max_mi": 5, "text": "$59 diagnostic"},
                {"max_mi": 15, "text": "$79 diagnostic"},
            ]

            # Home bases
            db.add(TenantHomeBase(
                tenant_id=hoffmann.id, name="Main Office",
                address="2950 Sublette Ave, St. Louis, MO 63139",
                lat=38.6059, lng=-90.2858,
            ))
            db.add(TenantHomeBase(
                tenant_id=hoffmann.id, name="Chesterfield Branch",
                address="16090 Swingley Ridge Rd, Chesterfield, MO 63017",
                lat=38.6555, lng=-90.5638,
            ))

            # Job rules
            db.add(TenantJobRule(
                tenant_id=hoffmann.id, category_pattern="HVAC", rule_type="whitelist",
            ))
            db.add(TenantJobRule(
                tenant_id=hoffmann.id, category_pattern="Heating", rule_type="whitelist",
            ))
            db.add(TenantJobRule(
                tenant_id=hoffmann.id, category_pattern="Plumbing", rule_type="whitelist",
            ))
            db.add(TenantJobRule(
                tenant_id=hoffmann.id, category_pattern="Water Heater", rule_type="wantlist",
            ))
            db.add(TenantJobRule(
                tenant_id=hoffmann.id, category_pattern="Roofing", rule_type="blacklist",
            ))

            # Specials
            db.add(TenantSpecial(
                tenant_id=hoffmann.id,
                name="Water Heater Replacement Discount",
                description="$100 off any water heater replacement installation",
                discount_text="$100 off install",
                conditions={"category_contains": "Water Heater"},
            ))
            db.add(TenantSpecial(
                tenant_id=hoffmann.id,
                name="Spring AC Tune-Up",
                description="$49 AC tune-up special for the spring season",
                discount_text="$49 tune-up",
                conditions={
                    "category_contains": "AC",
                    "valid_after": "2026-03-01",
                    "valid_before": "2026-06-30",
                },
            ))

            db.commit()
            print("  Added personalization config for Hoffmann Brothers")

        total = db.query(Tenant).count()
        mappings = db.query(AngiMapping).count()
        home_bases = db.query(TenantHomeBase).count()
        print(f"\nDone. {total} tenants, {mappings} account mappings, {home_bases} home bases.")
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Seed demo data")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate tables first")
    args = parser.parse_args()
    seed(reset=args.reset)


if __name__ == "__main__":
    main()
