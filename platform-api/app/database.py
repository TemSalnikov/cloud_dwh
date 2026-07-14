from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.database_url, echo=False)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db():
    from sqlalchemy import select

    from app.auth import hash_password
    from app.models import stack, user  # noqa: F401
    from app.models.user import PricingSettings, User
    from app.services.pricing import DEFAULT_PRICES

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE stacks ADD COLUMN IF NOT EXISTS owner_id UUID"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_stacks_owner_id ON stacks (owner_id)"))
        await conn.execute(text("ALTER TABLE stacks ADD COLUMN IF NOT EXISTS blocked_reason TEXT"))
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_superuser BOOLEAN DEFAULT FALSE"))
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE"))

    # Enum ALTER must be outside a multi-statement transaction on older Postgres.
    async with engine.connect() as conn:
        autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for value in ("stopped", "blocked", "updating"):
            try:
                await autocommit.execute(
                    text(f"ALTER TYPE stackstatus ADD VALUE IF NOT EXISTS '{value}'")
                )
            except Exception:
                pass

    async with SessionLocal() as session:
        pricing = await session.get(PricingSettings, 1)
        if not pricing:
            session.add(PricingSettings(id=1, prices=dict(DEFAULT_PRICES)))

        result = await session.execute(
            select(User).where(User.email == settings.bootstrap_admin_email.lower())
        )
        admin = result.scalar_one_or_none()
        if not admin:
            session.add(
                User(
                    email=settings.bootstrap_admin_email.lower(),
                    name=settings.bootstrap_admin_name,
                    password_hash=hash_password(settings.bootstrap_admin_password),
                    is_superuser=True,
                    is_active=True,
                )
            )
        elif not admin.is_superuser:
            admin.is_superuser = True

        await session.commit()


async def get_db():
    async with SessionLocal() as session:
        yield session
