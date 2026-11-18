from datetime import datetime, date
from typing import Optional, List
from sqlalchemy import (
    BigInteger, String, Integer, Float, Boolean, DateTime, Date, Text, ForeignKey, select, func, desc
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import settings

engine = create_async_engine(settings.async_database_url, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_vip: Mapped[bool] = mapped_column(Boolean, default=False)  # Безлимитный доступ

    # Счётчики дневных лимитов
    evals_today: Mapped[int] = mapped_column(Integer, default=0)
    chats_today: Mapped[int] = mapped_column(Integer, default=0)
    last_reset_date: Mapped[date] = mapped_column(Date, default=date.today)

    # Индивидуальные лимиты (если None — берутся глобальные из BotSettings)
    custom_eval_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    custom_chat_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BotSettings(Base):
    __tablename__ = "bot_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    daily_eval_limit: Mapped[int] = mapped_column(Integer, default=settings.DEFAULT_DAILY_EVAL_LIMIT)
    daily_chat_limit: Mapped[int] = mapped_column(Integer, default=settings.DEFAULT_DAILY_CHAT_LIMIT)


class Evaluation(Base):
    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)

    source_type: Mapped[str] = mapped_column(String(16))  # "link" или "photo"
    source_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # ссылка или file_id фото

    gift_title: Mapped[str] = mapped_column(String(256))
    collection_name: Mapped[str] = mapped_column(String(128))
    mint_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    model_name: Mapped[str] = mapped_column(String(128), default="Неизвестно")
    model_rarity: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    backdrop_name: Mapped[str] = mapped_column(String(128), default="Неизвестно")
    backdrop_rarity: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    symbol_name: Mapped[str] = mapped_column(String(128), default="Неизвестно")
    symbol_rarity: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    min_price_ton: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_price_ton: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_price_ton: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    overall_rarity: Mapped[str] = mapped_column(String(64), default="Обычная")
    collector_interest: Mapped[str] = mapped_column(String(64), default="Средний")
    confidence: Mapped[str] = mapped_column(String(64), default="Средняя")
    price_dynamics: Mapped[str] = mapped_column(String(128), default="Нет данных за период")
    market_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Favorite(Base):
    __tablename__ = "favorites"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    evaluation: Mapped["Evaluation"] = relationship("Evaluation", lazy="joined")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(16))  # "user" или "model"
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ErrorLog(Base):
    __tablename__ = "error_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    module: Mapped[str] = mapped_column(String(64))
    error_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        res = await session.execute(select(BotSettings).where(BotSettings.id == 1))
        if not res.scalar_one_or_none():
            session.add(BotSettings(
                id=1,
                daily_eval_limit=settings.DEFAULT_DAILY_EVAL_LIMIT,
                daily_chat_limit=settings.DEFAULT_DAILY_CHAT_LIMIT,
            ))
            await session.commit()


async def get_or_create_user(tg_id: int, username: Optional[str] = None, full_name: Optional[str] = None) -> User:
    async with async_session() as session:
        res = await session.execute(select(User).where(User.tg_id == tg_id))
        user = res.scalar_one_or_none()
        today = date.today()

        if not user:
            user = User(
                tg_id=tg_id,
                username=username,
                full_name=full_name,
                last_reset_date=today,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
        else:
            changed = False
            if username and user.username != username:
                user.username = username
                changed = True
            if full_name and user.full_name != full_name:
                user.full_name = full_name
                changed = True
            if user.last_reset_date != today:
                user.evals_today = 0
                user.chats_today = 0
                user.last_reset_date = today
                changed = True
            if changed:
                await session.commit()
                await session.refresh(user)
        return user


async def get_global_limits() -> BotSettings:
    async with async_session() as session:
        res = await session.execute(select(BotSettings).where(BotSettings.id == 1))
        cfg = res.scalar_one_or_none()
        if not cfg:
            cfg = BotSettings(id=1)
            session.add(cfg)
            await session.commit()
        return cfg


async def log_error(module: str, error_text: str, user_tg_id: Optional[int] = None):
    try:
        async with async_session() as session:
            session.add(ErrorLog(user_tg_id=user_tg_id, module=module, error_text=error_text[:2000]))
            await session.commit()
    except Exception:
        pass
