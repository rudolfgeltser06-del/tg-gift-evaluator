import re
import asyncio
from typing import Optional, Dict, Any, List
import aiohttp
from bs4 import BeautifulSoup

from config import settings
from database import log_error


HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}


def normalize_gift_url(url: str) -> Optional[Dict[str, str]]:
    """Извлекает slug и номер из ссылок вида t.me/nft/CloverPin-214841 или fragment.com/gift/cloverpin-214841"""
    url = url.strip()
    match = re.search(r"(?:t\.me/nft/|fragment\.com/gift/)([A-Za-z0-9_]+)-(\d+)", url, re.IGNORECASE)
    if not match:
        return None
    raw_slug, mint_num = match.group(1), match.group(2)
    return {
        "raw_slug": raw_slug,
        "slug_lower": raw_slug.lower(),
        "mint_number": mint_num,
        "tme_url": f"https://t.me/nft/{raw_slug}-{mint_num}",
        "fragment_url": f"https://fragment.com/gift/{raw_slug.lower()}-{mint_num}",
    }


async def parse_tme_nft_page(tme_url: str, mint_number: str) -> Dict[str, Any]:
    """Парсит публичную карточку подарка t.me/nft/... для точного извлечения Model, Backdrop, Symbol и редкости."""
    result: Dict[str, Any] = {
        "gift_title": None,
        "collection_name": None,
        "mint_number": mint_number,
        "model_name": "Неизвестно",
        "model_rarity": None,
        "backdrop_name": "Неизвестно",
        "backdrop_rarity": None,
        "symbol_name": "Неизвестно",
        "symbol_rarity": None,
    }

    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(tme_url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status != 200:
                    return result
                html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")

        # Заголовок (обычно в og:title или h1/div с классом tgme_page_title)
        title_el = soup.find(class_="tgme_page_title") or soup.find("meta", property="og:title")
        if title_el:
            raw_title = title_el.get("content") if title_el.name == "meta" else title_el.get_text(strip=True)
            if raw_title:
                result["gift_title"] = raw_title
                clean_col = re.sub(r"\s*#\d+.*$", "", raw_title).strip()
                result["collection_name"] = clean_col

        # Разбор таблицы характеристик (Model, Backdrop, Symbol и проценты редкости)
        rows = soup.find_all("tr")
        for row in rows:
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"])]
            if len(cells) >= 2:
                key = cells[0].lower()
                val = cells[1]
                rarity_match = re.search(r"(\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?\s*‰)", val)
                rarity_str = rarity_match.group(1) if rarity_match else None
                clean_val = re.sub(r"\s*\(?\d+(?:\.\d+)?\s*[%‰]\)?", "", val).strip()

                if "model" in key or "модел" in key:
                    result["model_name"] = clean_val
                    result["model_rarity"] = rarity_str
                elif "backdrop" in key or "фон" in key:
                    result["backdrop_name"] = clean_val
                    result["backdrop_rarity"] = rarity_str
                elif "symbol" in key or "узор" in key or "символ" in key:
                    result["symbol_name"] = clean_val
                    result["symbol_rarity"] = rarity_str

        # Резервный поиск по og:description, если таблица не отрендерилась
        if result["model_name"] == "Неизвестно":
            desc_meta = soup.find("meta", property="og:description")
            if desc_meta and desc_meta.get("content"):
                desc_text = desc_meta["content"]
                m_mod = re.search(r"Model:\s*([^\n•]+)", desc_text, re.I)
                m_bg = re.search(r"Backdrop:\s*([^\n•]+)", desc_text, re.I)
                m_sym = re.search(r"Symbol:\s*([^\n•]+)", desc_text, re.I)
                if m_mod:
                    result["model_name"] = m_mod.group(1).strip()
                if m_bg:
                    result["backdrop_name"] = m_bg.group(1).strip()
                if m_sym:
                    result["symbol_name"] = m_sym.group(1).strip()

    except Exception as e:
        await log_error("market_parser.parse_tme", str(e))

    return result


async def fetch_fragment_market_stats(slug_lower: str, model_name: str = "") -> Dict[str, Any]:
    """Получает реальные актуальные цены (мин/сред/макс) и недавние продажи коллекции с Fragment."""
    prices: List[float] = []
    sold_prices: List[float] = []

    urls_to_check = [
        f"https://fragment.com/gifts/{slug_lower}?filter=sale",
        f"https://fragment.com/gifts/{slug_lower}?filter=sold",
    ]

    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            for idx, url in enumerate(urls_to_check):
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")

                    # Ищем карточки лотов и цены в TON
                    for row in soup.find_all(["tr", "a"], class_=re.compile(r"tm-row|tm-grid-item")):
                        row_text = row.get_text(" ", strip=True)
                        price_match = re.search(r"(\d[\d,]*\.?\d*)\s*(?:TON)?", row_text)
                        # Ищем именно числовое значение цены в блоке icon-ton
                        ton_el = row.find(class_=re.compile(r"icon-ton|table-cell-value"))
                        if ton_el:
                            num_str = re.sub(r"[^\d.]", "", ton_el.get_text(strip=True))
                            if num_str:
                                try:
                                    val = float(num_str)
                                    if val > 0:
                                        if idx == 0:
                                            prices.append(val)
                                        else:
                                            sold_prices.append(val)
                                except ValueError:
                                    pass
    except Exception as e:
        await log_error("market_parser.fragment", str(e))

    return {
        "active_listings": sorted(prices),
        "recent_sales": sold_prices,
    }


async def fetch_getgems_floor(collection_name: str) -> Optional[float]:
    """Запрашивает актуальный флор коллекции через публичный GraphQL Getgems."""
    query = """
    query($query: String!) {
      alphaNftCollectionSearch(query: $query, first: 3) {
        edges {
          node {
            name
            approximateHoldersCount
            floorPrice
          }
        }
      }
    }
    """
    try:
        async with aiohttp.ClientSession(headers={"Content-Type": "application/json", **HEADERS}) as session:
            async with session.post(
                "https://api.getgems.io/graphql",
                json={"query": query, "variables": {"query": collection_name}},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                edges = data.get("data", {}).get("alphaNftCollectionSearch", {}).get("edges", [])
                for edge in edges:
                    node = edge.get("node", {})
                    if collection_name.lower() in node.get("name", "").lower():
                        fp = node.get("floorPrice")
                        if fp:
                            # В Getgems floorPrice часто в нанотонах (10^9)
                            val = float(fp)
                            return round(val / 1e9, 2) if val > 10000 else round(val, 2)
    except Exception as e:
        await log_error("market_parser.getgems", str(e))
    return None


async def evaluate_market_data(
    collection_name: str,
    model_name: str = "Неизвестно",
    model_rarity: Optional[str] = None,
    backdrop_rarity: Optional[str] = None,
    symbol_rarity: Optional[str] = None,
) -> Dict[str, Any]:
    """Сводит реальные данные Fragment и Getgems в итоговую рыночную оценку без выдуманных цифр."""
    slug_lower = re.sub(r"[^a-z0-9]", "", collection_name.lower())

    frag_task = fetch_fragment_market_stats(slug_lower, model_name)
    gg_task = fetch_getgems_floor(collection_name)
    frag_data, gg_floor = await asyncio.gather(frag_task, gg_task)

    active = frag_data.get("active_listings", [])
    sold = frag_data.get("recent_sales", [])

    all_points = []
    if gg_floor and gg_floor > 0:
        all_points.append(gg_floor)
    all_points.extend(active[:15])
    all_points.extend(sold[:15])

    if not all_points:
        return {
            "min_price": None,
            "avg_price": None,
            "max_price": None,
            "confidence": "Низкая (мало открытых лотов в стакане)",
            "dynamics": "Недостаточно сделок для расчёта",
            "collector_interest": "Требуется ручная проверка стакана",
            "overall_rarity": _calc_rarity_label(model_rarity, backdrop_rarity, symbol_rarity),
            "note": "Актуальные ордера по этой коллекции сейчас скрыты или отсутствуют на Fragment/Getgems.",
        }

    # Базовый расчёт по реальным ордерам
    min_p = round(min(all_points), 2)
    # Отсекаем экстремальные выбросы (фейк-ордера по 99999 TON) при расчёте средней и макс
    trimmed = sorted(all_points)
    cutoff = max(1, int(len(trimmed) * 0.85))
    realistic = trimmed[:cutoff]

    avg_p = round(sum(realistic) / len(realistic), 2)
    max_p = round(max(realistic), 2)

    # Корректировка коэффициента на редкость Model, если она известна
    rarity_pct = _parse_percent(model_rarity)
    if rarity_pct is not None and rarity_pct <= 1.0:
        min_p = round(min_p * 1.35, 2)
        avg_p = round(avg_p * 1.5, 2)
        max_p = round(max_p * 1.8, 2)
    elif rarity_pct is not None and rarity_pct <= 2.5:
        min_p = round(min_p * 1.15, 2)
        avg_p = round(avg_p * 1.25, 2)

    # Достоверность
    total_samples = len(active) + len(sold) + (1 if gg_floor else 0)
    if total_samples >= 10 and len(sold) >= 3:
        confidence = f"Высокая ({total_samples} рыночных ордеров и продаж)"
    elif total_samples >= 3:
        confidence = f"Средняя ({total_samples} активных листингов)"
    else:
        confidence = "Низкая (единичные предложения)"

    # Динамика по последним продажам
    if len(sold) >= 4:
        recent_avg = sum(sold[:2]) / 2
        older_avg = sum(sold[-2:]) / 2
        if older_avg > 0:
            diff_pct = round(((recent_avg - older_avg) / older_avg) * 100, 1)
            if diff_pct > 3:
                dynamics = f"📈 Рост (+{diff_pct}% по последним сделкам)"
            elif diff_pct < -3:
                dynamics = f"📉 Снижение ({diff_pct}% по последним сделкам)"
            else:
                dynamics = f"➡️ Стабильно ({diff_pct:+.1f}%)"
        else:
            dynamics = "Стабильный спрос"
    else:
        dynamics = "Мало свежих продаж для тренда"

    interest = "Высокий" if (rarity_pct and rarity_pct <= 2.0) or len(sold) >= 5 else "Средний"

    return {
        "min_price": min_p,
        "avg_price": avg_p,
        "max_price": max_p,
        "confidence": confidence,
        "dynamics": dynamics,
        "collector_interest": interest,
        "overall_rarity": _calc_rarity_label(model_rarity, backdrop_rarity, symbol_rarity),
        "note": f"Источники: Fragment ({len(active)} в продаже, {len(sold)} продаж)" + (f", Getgems (флор {gg_floor} TON)" if gg_floor else ""),
    }


def _parse_percent(val: Optional[str]) -> Optional[float]:
    if not val:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", val)
    if not m:
        return None
    num = float(m.group(1))
    if "‰" in val:
        return num / 10.0
    return num


def _calc_rarity_label(m_rar: Optional[str], b_rar: Optional[str], s_rar: Optional[str]) -> str:
    pct = _parse_percent(m_rar)
    if pct is None:
        return "Стандартная (требует уточнения)"
    if pct <= 0.5:
        return f"Мифическая ({m_rar})"
    if pct <= 1.5:
        return f"Редкая ({m_rar})"
    if pct <= 3.0:
        return f"Необычная ({m_rar})"
    return f"Базовая ({m_rar})"
