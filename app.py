from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

import pandas as pd
import streamlit as st

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PAGE_SIZE = 20
FIELDS = ["app_id", "country", "author", "rating", "title", "review", "date"]

APP_ID_IN_URL = re.compile(r"(?:id|/|^)(\d{5,})", re.IGNORECASE)
STOREFRONT_IN_URL = re.compile(r"https?://apps\.apple\.com/([a-z]{2})/", re.IGNORECASE)


# ---------- логика скрейпинга ----------

def fetch_page(app_id: str, country: str, offset: int) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {"platform": "web", "limit": PAGE_SIZE, "offset": offset, "sort": "recent"}
    )
    url = (
        "https://apps.apple.com/api/apps/v1/catalog/"
        f"{country}/apps/{app_id}/reviews?{query}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Origin": "https://apps.apple.com",
            "Referer": f"https://apps.apple.com/{country}/app/id{app_id}",
        },
    )
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise last_error  # type: ignore[misc]


def parse_reviews(payload: dict[str, Any], app_id: str, country: str) -> list[dict[str, str]]:
    reviews = []
    for item in payload.get("data") or []:
        attrs = item.get("attributes") or {}
        reviews.append(
            {
                "app_id": app_id,
                "country": country,
                "author": str(attrs.get("userName") or ""),
                "rating": str(attrs.get("rating") or ""),
                "title": str(attrs.get("title") or ""),
                "review": str(attrs.get("review") or ""),
                "date": str(attrs.get("date") or ""),
            }
        )
    return reviews


def collect_app_reviews(
    app_id: str,
    country: str,
    max_pages: int,
    on_page: Callable[[int], None] | None = None,
) -> list[dict[str, str]]:
    all_reviews: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for page in range(max_pages):
        try:
            payload = fetch_page(app_id, country, page * PAGE_SIZE)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                break
            raise
        page_reviews = parse_reviews(payload, app_id, country)
        if not page_reviews:
            break
        added = 0
        for review in page_reviews:
            key = (review["author"], review["date"], review["review"])
            if key in seen:
                continue
            seen.add(key)
            all_reviews.append(review)
            added += 1
        if on_page:
            on_page(len(all_reviews))
        if added == 0 or not payload.get("next"):
            break
        time.sleep(0.4)
    return all_reviews


def parse_app_ref(raw: str) -> tuple[str | None, str | None]:
    text = raw.strip()
    country = None
    storefront = STOREFRONT_IN_URL.search(text)
    if storefront:
        country = storefront.group(1).lower()
    if text.isdigit():
        return text, country
    match = APP_ID_IN_URL.search(text)
    if match:
        return match.group(1), country
    return None, country


# ---------- интерфейс Streamlit ----------

st.set_page_config(page_title="App Store Reviews", page_icon="📱", layout="wide")
st.title("📱 Сборщик отзывов из App Store")
st.caption("Вставь ссылки или ID приложений, получи таблицу отзывов и скачай её в CSV.")

with st.sidebar:
    st.header("Настройки")
    default_country = st.text_input(
        "Страна по умолчанию", value="us", max_chars=2,
        help="Используется, если в ссылке нет кода страны (/us/, /ru/ и т.д.)",
    ).lower()
    max_pages = st.slider(
        "Страниц на приложение", 1, 25, 5,
        help="Одна страница = 20 отзывов. 25 страниц ≈ 500 отзывов.",
    )
    keywords_raw = st.text_input(
        "Фильтр по словам (необязательно)",
        placeholder="persian, cuneiform, history",
        help="Через запятую. Останутся отзывы, где встречается хотя бы одно слово.",
    )

refs_raw = st.text_area(
    "Ссылки или ID приложений (по одному на строку)",
    value="https://apps.apple.com/us/app/duolingo/id570060128",
    height=140,
)

if st.button("Собрать отзывы", type="primary"):
    refs = [line.strip() for line in refs_raw.splitlines() if line.strip()]
    if not refs:
        st.warning("Добавь хотя бы одну ссылку или ID.")
    else:
        rows: list[dict[str, str]] = []
        progress = st.progress(0.0, text="Начинаю...")
        for i, raw in enumerate(refs):
            app_id, url_country = parse_app_ref(raw)
            if not app_id:
                st.warning(f"Пропуск: {raw!r} — не нашла числовой ID приложения.")
                continue
            store = url_country or default_country
            progress.progress(i / len(refs), text=f"Приложение {app_id} ({store})...")
            try:
                reviews = collect_app_reviews(app_id, store, max_pages)
            except urllib.error.HTTPError as exc:
                st.error(f"{app_id}: ошибка HTTP {exc.code}")
                continue
            except Exception as exc:  # noqa: BLE001
                st.error(f"{app_id}: {exc}")
                continue
            st.write(f"✅ app_id={app_id}, страна={store}: {len(reviews)} отзывов")
            rows.extend(reviews)
        progress.progress(1.0, text="Готово")
        st.session_state["df"] = pd.DataFrame(rows, columns=FIELDS)

df: pd.DataFrame | None = st.session_state.get("df")

if df is not None:
    if keywords_raw.strip():
        words = [w.strip().lower() for w in keywords_raw.split(",") if w.strip()]
        pattern = "|".join(re.escape(w) for w in words)
        text = (df["title"] + " " + df["review"]).str.lower()
        df = df[text.str.contains(pattern, regex=True, na=False)]
        df = df.reset_index(drop=True); df.index = df.index + 1
    st.subheader(f"Отзывов: {len(df)}")
    st.dataframe(df, use_container_width=True)
    st.download_button(
        "⬇️ Скачать CSV",
        data=df.to_csv(index=False).encode("utf-8-sig"),
        file_name="app_store_reviews.csv",
        mime="text/csv",
    )
