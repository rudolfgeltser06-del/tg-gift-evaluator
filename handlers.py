import asyncio
import html
from datetime import datetime
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select, func, desc, delete

from config import settings
from database import (
    async_session, User, Evaluation, Favorite, ChatMessage, ErrorLog,
    BotSettings, get_or_create_user, get_global_limits, log_error
)
from market_parser import normalize_gift_url, parse_tme_nft_page, evaluate_market_data
from ai_service import recognize_gift_from_image, ask_ai_chat

router = Router()


class BotStates(StatesGroup):
    waiting_gift_input = State()
    in_ai_chat = State()
    admin_broadcast = State()
    admin_ban_id = State()
    admin_set_limits = State()


# --- КЛАВИАТУРЫ ---

def main_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔍 Оценить подарок", callback_data="menu:evaluate")],
        [InlineKeyboardButton(text="💬 AI-чат", callback_data="menu:chat")],
        [
            InlineKeyboardButton(text="⭐ Избранное", callback_data="menu:favorites"),
            InlineKeyboardButton(text="📊 История оценок", callback_data="menu:history"),
        ],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile"),
            InlineKeyboardButton(text="🆘 Поддержка", callback_data="menu:support"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ В главное меню", callback_data="menu:main")]
    ])


def evaluation_result_kb(eval_id: int, is_fav: bool = False) -> InlineKeyboardMarkup:
    fav_text = "💔 Убрать из избранного" if is_fav else "⭐ В избранное"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=fav_text, callback_data=f"eval:fav:{eval_id}"),
            InlineKeyboardButton(text="🔄 Оценить заново", callback_data=f"eval:recheck:{eval_id}"),
        ],
        [InlineKeyboardButton(text="🔍 Оценить другой подарок", callback_data="menu:evaluate")],
        [InlineKeyboardButton(text="◀️ В главное меню", callback_data="menu:main")],
    ])


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🚫 Бан / Разбан / VIP", callback_data="admin:ban")],
        [InlineKeyboardButton(text="⚙️ Настройка лимитов", callback_data="admin:limits")],
        [InlineKeyboardButton(text="🐞 Просмотр ошибок", callback_data="admin:errors")],
        [InlineKeyboardButton(text="◀️ В главное меню", callback_data="menu:main")],
    ])


# --- ФОРМАТИРОВАНИЕ КАРТОЧКИ ОЦЕНКИ ---

def format_evaluation_card(ev: Evaluation) -> str:
    def fmt_attr(name: str, rarity: Optional[str]) -> str:
        safe_name = html.escape(name or "Неизвестно")
        return f"{safe_name} ({html.escape(rarity)})" if rarity else safe_name

    def fmt_price(val: Optional[float]) -> str:
        return f"{val:.2f} TON" if val is not None else "Нет данных"

    note_block = f"\nℹ️ <i>{html.escape(ev.market_note)}</i>" if ev.market_note else ""

    return (
        f"🎁 <b>Название подарка:</b> {html.escape(ev.gift_title)}\n"
        f"📦 <b>Коллекция:</b> {html.escape(ev.collection_name)}\n"
        f"🎨 <b>Model:</b> {fmt_attr(ev.model_name, ev.model_rarity)}\n"
        f"🖼 <b>Backdrop:</b> {fmt_attr(ev.backdrop_name, ev.backdrop_rarity)}\n"
        f"✨ <b>Symbol:</b> {fmt_attr(ev.symbol_name, ev.symbol_rarity)}\n\n"
        f"💎 <b>Рыночная стоимость:</b>\n"
        f"• Минимальная цена: <b>{fmt_price(ev.min_price_ton)}</b>\n"
        f"• Средняя цена: <b>{fmt_price(ev.avg_price_ton)}</b>\n"
        f"• Максимальная цена: <b>{fmt_price(ev.max_price_ton)}</b>\n\n"
        f"📊 <b>Дополнительно:</b>\n"
        f"• Редкость: {html.escape(ev.overall_rarity)}\n"
        f"• Интерес коллекционеров: {html.escape(ev.collector_interest)}\n"
        f"• Достоверность оценки: {html.escape(ev.confidence)}\n"
        f"• Динамика стоимости: {html.escape(ev.price_dynamics)}"
        f"{note_block}"
    )


def is_user_admin(tg_id: int) -> bool:
    return tg_id in settings.admin_id_list


# --- СТАРТ И ГЛАВНОЕ МЕНЮ ---

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    if user.is_banned:
        await message.answer("⛔ Ваш доступ к боту ограничен администратором.")
        return

    await message.answer(
        "👋 <b>Добро пожаловать в анализатор Telegram Gifts!</b>\n\n"
        "Отправьте ссылку на подарок (<code>t.me/nft/...</code>) или его скриншот, "
        "чтобы узнать точные характеристики, редкость и реальную рыночную цену в TON.\n\n"
        "Выберите действие в меню ниже:",
        reply_markup=main_menu_kb(is_user_admin(message.from_user.id)),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await get_or_create_user(callback.from_user.id)
    if user.is_banned:
        await callback.answer("Доступ ограничен", show_alert=True)
        return

    await callback.message.edit_text(
        "🏠 <b>Главное меню</b>\nВыберите нужный раздел:",
        reply_markup=main_menu_kb(is_user_admin(callback.from_user.id)),
        parse_mode="HTML",
    )
    await callback.answer()


# --- ОЦЕНКА ПОДАРКА (ПО ФОТО И ПО ССЫЛКЕ) ---

@router.callback_query(F.data == "menu:evaluate")
async def cb_evaluate_prompt(callback: CallbackQuery, state: FSMContext):
    user = await get_or_create_user(callback.from_user.id)
    if user.is_banned:
        await callback.answer("Доступ ограничен", show_alert=True)
        return

    await state.set_state(BotStates.waiting_gift_input)
    await callback.message.edit_text(
        "🔍 <b>Оценка подарка</b>\n\n"
        "Отправьте в этот чат:\n"
        "1️⃣ <b>Ссылку на подарок</b> (например: <code>https://t.me/nft/CloverPin-214841</code>)\n"
        "2️⃣ Или <b>фотографию / скриншот</b> карточки подарка.",
        reply_markup=back_to_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


async def _check_and_consume_eval_limit(tg_id: int) -> tuple[bool, str]:
    if is_user_admin(tg_id):
        return True, ""
    limits = await get_global_limits()
    async with async_session() as session:
        res = await session.execute(select(User).where(User.tg_id == tg_id))
        user = res.scalar_one_or_none()
        if not user:
            return False, "Ошибка профиля"
        if user.is_banned:
            return False, "⛔ Ваш доступ ограничен."
        if user.is_vip:
            return True, ""
        max_lim = user.custom_eval_limit if user.custom_eval_limit is not None else limits.daily_eval_limit
        if user.evals_today >= max_lim:
            return False, f"⚠️ Достигнут дневной лимит оценок ({max_lim}/{max_lim}). Попробуйте завтра или обратитесь в поддержку."
        user.evals_today += 1
        await session.commit()
        return True, ""


@router.message(BotStates.waiting_gift_input, F.photo)
@router.message(F.photo)
async def handle_gift_photo(message: Message, bot: Bot, state: FSMContext):
    user = await get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    ok, err_msg = await _check_and_consume_eval_limit(user.tg_id)
    if not ok:
        await message.answer(err_msg, reply_markup=back_to_menu_kb())
        return

    status_msg = await message.answer("⏳ Распознаю подарок по фото и проверяю рынок...")

    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        file_bytes_io = await bot.download_file(file_info.file_path)
        image_bytes = file_bytes_io.read()

        ai_data = await recognize_gift_from_image(image_bytes)
        if not ai_data.get("is_gift"):
            await status_msg.edit_text(
                "❌ Не удалось распознать Telegram Gift на этом изображении.\n"
                "Отправьте чёткий скриншот карточки подарка или прямую ссылку <code>t.me/nft/...</code>.",
                reply_markup=back_to_menu_kb(),
                parse_mode="HTML",
            )
            return

        col_name = ai_data.get("collection_name") or "Unknown Gift"
        gift_title = ai_data.get("gift_title") or col_name
        model_name = ai_data.get("model_name") or "Неизвестно"
        model_rarity = ai_data.get("model_rarity")
        backdrop_name = ai_data.get("backdrop_name") or "Неизвестно"
        backdrop_rarity = ai_data.get("backdrop_rarity")
        symbol_name = ai_data.get("symbol_name") or "Неизвестно"
        symbol_rarity = ai_data.get("symbol_rarity")

        market = await evaluate_market_data(
            collection_name=col_name,
            model_name=model_name,
            model_rarity=model_rarity,
            backdrop_rarity=backdrop_rarity,
            symbol_rarity=symbol_rarity,
        )

        async with async_session() as session:
            ev = Evaluation(
                user_tg_id=user.tg_id,
                source_type="photo",
                source_value=photo.file_id,
                gift_title=gift_title,
                collection_name=col_name,
                mint_number=str(ai_data.get("mint_number") or ""),
                model_name=model_name,
                model_rarity=model_rarity,
                backdrop_name=backdrop_name,
                backdrop_rarity=backdrop_rarity,
                symbol_name=symbol_name,
                symbol_rarity=symbol_rarity,
                min_price_ton=market["min_price"],
                avg_price_ton=market["avg_price"],
                max_price_ton=market["max_price"],
                overall_rarity=market["overall_rarity"],
                collector_interest=market["collector_interest"],
                confidence=market["confidence"],
                price_dynamics=market["dynamics"],
                market_note=market["note"],
            )
            session.add(ev)
            await session.commit()
            await session.refresh(ev)

        await state.clear()
        await status_msg.edit_text(
            format_evaluation_card(ev),
            reply_markup=evaluation_result_kb(ev.id, is_fav=False),
            parse_mode="HTML",
        )
    except Exception as e:
        await log_error("handlers.photo", str(e), user.tg_id)
        await status_msg.edit_text("⚠️ Ошибка при обработке изображения. Попробуйте ещё раз.", reply_markup=back_to_menu_kb())


async def _process_gift_link(message: Message, raw_url: str, state: FSMContext):
    parsed_url = normalize_gift_url(raw_url)
    if not parsed_url:
        await message.answer(
            "⚠️ Некорректная ссылка. Отправьте ссылку вида <code>https://t.me/nft/CloverPin-214841</code> или фото подарка.",
            reply_markup=back_to_menu_kb(),
            parse_mode="HTML",
        )
        return

    user = await get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    ok, err_msg = await _check_and_consume_eval_limit(user.tg_id)
    if not ok:
        await message.answer(err_msg, reply_markup=back_to_menu_kb())
        return

    status_msg = await message.answer("⏳ Загружаю характеристики подарка и рыночные ордера...")

    try:
        meta = await parse_tme_nft_page(parsed_url["tme_url"], parsed_url["mint_number"])
        col_name = meta.get("collection_name") or parsed_url["raw_slug"]
        gift_title = meta.get("gift_title") or f"{col_name} #{parsed_url['mint_number']}"

        market = await evaluate_market_data(
            collection_name=col_name,
            model_name=meta["model_name"],
            model_rarity=meta["model_rarity"],
            backdrop_rarity=meta["backdrop_rarity"],
            symbol_rarity=meta["symbol_rarity"],
        )

        async with async_session() as session:
            ev = Evaluation(
                user_tg_id=user.tg_id,
                source_type="link",
                source_value=parsed_url["tme_url"],
                gift_title=gift_title,
                collection_name=col_name,
                mint_number=parsed_url["mint_number"],
                model_name=meta["model_name"],
                model_rarity=meta["model_rarity"],
                backdrop_name=meta["backdrop_name"],
                backdrop_rarity=meta["backdrop_rarity"],
                symbol_name=meta["symbol_name"],
                symbol_rarity=meta["symbol_rarity"],
                min_price_ton=market["min_price"],
                avg_price_ton=market["avg_price"],
                max_price_ton=market["max_price"],
                overall_rarity=market["overall_rarity"],
                collector_interest=market["collector_interest"],
                confidence=market["confidence"],
                price_dynamics=market["dynamics"],
                market_note=market["note"],
            )
            session.add(ev)
            await session.commit()
            await session.refresh(ev)

        await state.clear()
        await status_msg.edit_text(
            format_evaluation_card(ev),
            reply_markup=evaluation_result_kb(ev.id, is_fav=False),
            parse_mode="HTML",
        )
    except Exception as e:
        await log_error("handlers.link", str(e), user.tg_id)
        await status_msg.edit_text("⚠️ Ошибка при проверке ссылки. Попробуйте позже.", reply_markup=back_to_menu_kb())


# --- ИЗБРАННОЕ И ПОВТОРНАЯ ОЦЕНКА ---

@router.callback_query(F.data.startswith("eval:fav:"))
async def cb_toggle_favorite(callback: CallbackQuery):
    eval_id = int(callback.data.split(":")[-1])
    tg_id = callback.from_user.id

    async with async_session() as session:
        res = await session.execute(
            select(Favorite).where(Favorite.user_tg_id == tg_id, Favorite.evaluation_id == eval_id)
        )
        fav = res.scalar_one_or_none()
        if fav:
            await session.delete(fav)
            await session.commit()
            is_fav = False
            await callback.answer("Удалено из избранного")
        else:
            session.add(Favorite(user_tg_id=tg_id, evaluation_id=eval_id))
            await session.commit()
            is_fav = True
            await callback.answer("Добавлено в ⭐ Избранное!")

    await callback.message.edit_reply_markup(reply_markup=evaluation_result_kb(eval_id, is_fav=is_fav))


@router.callback_query(F.data.startswith("eval:recheck:"))
async def cb_recheck_evaluation(callback: CallbackQuery):
    eval_id = int(callback.data.split(":")[-1])
    tg_id = callback.from_user.id

    ok, err_msg = await _check_and_consume_eval_limit(tg_id)
    if not ok:
        await callback.answer(err_msg, show_alert=True)
