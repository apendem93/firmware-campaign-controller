from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from controller.config import settings

engine = create_async_engine(
    settings().database_url, pool_pre_ping=True, pool_size=10, max_overflow=10
)
sessions = async_sessionmaker(engine, expire_on_commit=False)


async def db_now(session):
    return await session.scalar(select(func.clock_timestamp()))
