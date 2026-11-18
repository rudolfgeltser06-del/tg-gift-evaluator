import json
import re
from typing import Dict, Any, List
from google import genai
from google.genai import types
from sqlalchemy import select, desc

from config import settings
from database import async_session, ChatMessage, log_error

client = genai.Client(api_key=settings.GEMINI_API_KEY)
MODEL_NAME = "gemini-2.5-flash"


VISION_PROMPT = """Ты эксперт по коллекционным NFT-подаркам Telegram (Telegram Gifts).
Проанализируй это изображение (скриншот подарка Telegram) и извлеки с него точные данные.
Верни ответ СТРОГО в формате JSON без markdown-обёрток и лишнего текста:
{
  "is_gift": true,
  "gift_title": "Полное название с номером, например Clover Pin #214841",
  "collection_name": "Название коллекции на английском, например Clover Pin",
  "mint_number": "Номер без решётки, например 214841 или null",
  "model_name": "Название модели на английском, например Sell High, или Неизвестно",
  "model_rarity": "Процент редкости модели если виден на скрине, например 3% или null",
  "backdrop_name": "Название фона если видно или определено, иначе Неизвестно",
  "backdrop_rarity": "Процент редкости фона если виден, иначе null",
  "symbol_name": "Название узора/символа если видно, иначе Неизвестно",
  "symbol_rarity": "Процент редкости символа если виден, иначе null"
}
Если на фото вообще не подарок Telegram, верни {"is_gift": false}.
Не выдумывай цены — цены подставит рыночный модуль."""


async def recognize_gift_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> Dict[str, Any]:
    """Распознаёт характеристики Telegram Gift по скриншоту через Gemini Vision."""
    try:
        response = await client.aio.models.generate_content(
            model=MODEL_NAME,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                VISION_PROMPT,
            ],
            config=types.GenerateContentConfig(temperature=0.1),
        )
        raw = (response.text or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        return data
    except Exception as e:
        await log_error("ai_service.vision", str(e))
        return {"is_gift": False, "error": str(e)}


SYSTEM_CHAT_PROMPT = """Ты — встроенный AI-аналитик рынка Telegram Gifts (NFT-подарков в Telegram и TON).
Помогай пользователю оценивать редкость коллекций, сравнивать подарки между собой, понимать ликвидность моделей (Model), фонов (Backdrop), узоров (Symbol) и красивых номеров.
Правила:
1. Отвечай чётко, по делу, структурированно и на русском языке.
2. Не выдумывай точные текущие цены до копейки, если у тебя нет свежей сводки в контексте — честно указывай, от каких факторов (редкость модели, сочетание цветов, сапплай коллекции, минт-номер) зависит премия к флору.
3. Помни предыдущие сообщения пользователя в этом диалоге и опирайся на них при сравнении подарков."""


async def ask_ai_chat(user_tg_id: int, user_text: str) -> str:
    """Ведёт диалог с сохранением истории последних 12 сообщений в PostgreSQL."""
    try:
        async with async_session() as session:
            res = await session.execute(
                select(ChatMessage)
                .where(ChatMessage.user_tg_id == user_tg_id)
                .order_by(desc(ChatMessage.id))
                .limit(12)
            )
            history_rows: List[ChatMessage] = list(reversed(res.scalars().all()))

            contents = []
            for row in history_rows:
                contents.append(
                    types.Content(
                        role=row.role,
                        parts=[types.Part.from_text(text=row.content)],
                    )
                )
            contents.append(
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=user_text)],
                )
            )

            response = await client.aio.models.generate_content(
                model=MODEL_NAME,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_CHAT_PROMPT,
                    temperature=0.5,
                    max_output_tokens=1000,
                ),
            )
            reply = (response.text or "Не удалось сформировать ответ. Попробуй переформулировать вопрос.").strip()

            # Сохраняем вопрос и ответ в БД
            session.add(ChatMessage(user_tg_id=user_tg_id, role="user", content=user_text))
            session.add(ChatMessage(user_tg_id=user_tg_id, role="model", content=reply))
            await session.commit()

            return reply
    except Exception as e:
        await log_error("ai_service.chat", str(e), user_tg_id=user_tg_id)
        return "⚠️ Произошла временная ошибка при обращении к AI. Попробуй отправить запрос через пару секунд."
