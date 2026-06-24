#!/usr/bin/env python3
"""
Albion Online — Black Market Enchanting Arbitrage Scanner v4
=============================================================
Запуск: python3 albion_bm_scanner_v4.py
Требует: pip install requests

Что нового в v4:
  ✅ Русские названия предметов (из items.json или встроенный словарь)
  ✅ Явный вывод: "купить нужно X Рун + Y Душ + Z Реликтов"
  ✅ Три сценария цены: BEST(min) / AVG(24h) / WORST(max)
  ✅ Все качества: Normal(1) Good(2) Outstanding(3) Excellent(4)
  ✅ Все зачарования: @1 @2 @3
  ✅ Тиры 5 / 6 / 7

items.json НЕ ОБЯЗАТЕЛЕН — если есть рядом, названия будут на русском;
если нет или сломан — используется встроенный словарь.

Выходные файлы:
  materials_prices.csv  — цены рун/душ/реликтов
  raw_prices.csv        — все сырые цены (предмет × качество × зачарование)
  profit_analysis.csv   — анализ прибыли, сортировка по profit_avg
"""

import requests
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────
#  НАСТРОЙКИ
# ─────────────────────────────────────────────────────────────
SERVER     = "europe"       # europe / americas / asia
CAERLEON   = "Caerleon"
BM         = "Black Market"

QUALITIES  = {1: "Normal", 2: "Good", 3: "Outstanding", 4: "Excellent"}

DELAY_SEC   = 0.8
BATCH_SIZE  = 10
MAX_RETRY   = 4
HISTORY_H   = 24   # часов для avg-цены материалов

# ─── TELEGRAM ─────────────────────────────────────────────────
import os as _os
TELEGRAM_TOKEN   = _os.environ.get("TELEGRAM_TOKEN",   "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = _os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
PROFIT_THRESHOLD = int(_os.environ.get("PROFIT_THRESHOLD", "500000"))

# ─── GITHUB GIST (необязательно) ─────────────────────────────
# Gist ID: последняя часть URL твоего gist (создать на gist.github.com)
# GitHub Token: Settings → Developer settings → Personal access tokens → gist scope
GIST_ID          = _os.environ.get("GIST_ID",      "")
GITHUB_TOKEN     = _os.environ.get("GIST_TOKEN",   _os.environ.get("GITHUB_TOKEN",""))

# ─── РЕЖИМ МОНИТОРИНГА ────────────────────────────────────────
LOOP_MODE        = _os.environ.get("LOOP_MODE", "false").lower() == "true"
SCAN_INTERVAL    = 20

BASE_URL = f"https://{SERVER}.albion-online-data.com/api/v2/stats/prices"
HIST_URL = f"https://{SERVER}.albion-online-data.com/api/v2/stats/history"
HEADERS  = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# ─────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    """Отправляет сообщение в Telegram. Возвращает True если успешно."""
    if TELEGRAM_TOKEN == "YOUR_BOT_TOKEN" or TELEGRAM_CHAT_ID == "YOUR_CHAT_ID":
        print("  [!] Telegram не настроен — заполни TELEGRAM_TOKEN и TELEGRAM_CHAT_ID")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"  [TG ERR] {e}")
        return False


# Человекочитаемые названия качества
QUALITY_RU = {1: "Обычное", 2: "Хорошее", 3: "Выдающееся", 4: "Отличное"}


def build_tg_message(alerts: list) -> str:
    """
    Компактное уведомление — одна карточка на позицию.
    Формат: T6.2 Название (Качество) | Профит | База | ЧР | Время
    """
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    header = (
        f"🚨 <b>Albion BM — найдено {len(alerts)} позиций > {PROFIT_THRESHOLD:,} сер.</b>  "
        f"({ts})"
    )
    cards = [header, ""]
    for r in alerts[:15]:
        tier_enc  = f"T{r['tier']}.{r['enchant']}"          # напр. T6.2
        quality   = QUALITY_RU.get(r["quality"], "?")        # Хорошее
        q_icon    = {1: "⚪", 2: "🟢", 3: "🔵", 4: "🟡"}.get(r["quality"], "⚪")

        parts = [
            q_icon + " <b>" + tier_enc + " " + r["name_ru"] + "</b> (" + quality + ")",
            "💰 Профит: <b>" + f"{r['profit_avg']:+,}" + "</b> сер.  ROI " + f"{r['roi_avg_pct']:+.1f}" + "%",
            "🏪 База: " + f"{r['base_sell_caerleon']:,}" + "  →  ЧР: " + f"{r['bm_buy_order']:,}",
            "⏰ " + r["bm_freshness"],
        ]
        line = "\n".join(parts)
        cards.append(line)
        cards.append("")

    if len(alerts) > 15:
        cards.append(f"<i>...и ещё {len(alerts) - 15} позиций в profit_analysis.csv</i>")

    return "\n".join(cards)



def build_summary_md(profit_rows: list, ts: str) -> str:
    """
    Markdown-отчёт для GitHub Actions Summary и Gist.
    Показывает все прибыльные позиции в таблице.
    """
    q_name = {1: "Normal", 2: "Good", 3: "Outstanding", 4: "Excellent"}
    good = [r for r in profit_rows if r["profitable_avg"] == "YES"]

    lines = [
        f"# 🏆 Albion BM Scan — {ts}",
        f"",
        f"**Сервер:** {SERVER} | **Порог:** {PROFIT_THRESHOLD:,} сер. | "
        f"**Прибыльных:** {len(good)} из {len(profit_rows)}",
        f"",
    ]

    if not good:
        lines.append("_Прибыльных позиций не найдено._")
        return "\n".join(lines)

    lines += [
        "| Вещь | T.Enc | Качество | База Caerleon | ЧР Buy | Профит AVG | ROI | "
        "Матер. | Обновлено |",
        "|------|-------|----------|--------------|--------|------------|-----|"
        "--------|----------|",
    ]
    for r in good:
        tier_enc = f"T{r['tier']}.{r['enchant']}"
        qual     = q_name.get(r["quality"], str(r["quality"]))
        lines.append(
            f"| {r['name_ru']} | {tier_enc} | {qual} "
            f"| {r['base_sell_caerleon']:,} | {r['bm_buy_order']:,} "
            f"| **{r['profit_avg']:+,}** | {r['roi_avg_pct']:+.1f}% "
            f"| {r['mat_buy_desc']} | {r['bm_freshness']} |"
        )

    lines += [
        "",
        "<details><summary>📊 Все позиции с данными</summary>",
        "",
        "| Вещь | T.Enc | Q | База | ЧР | Профит BEST | Профит AVG | Профит WORST |",
        "|------|-------|---|------|-----|------------|------------|-------------|",
    ]
    for r in profit_rows[:100]:
        tier_enc = f"T{r['tier']}.{r['enchant']}"
        lines.append(
            f"| {r['name_ru']} | {tier_enc} | {r['quality']} "
            f"| {r['base_sell_caerleon']:,} | {r['bm_buy_order']:,} "
            f"| {r['profit_best']:+,} | {r['profit_avg']:+,} | {r['profit_worst']:+,} |"
        )
    lines += ["", "</details>", "", f"_Следующий скан через ~20 минут_"]
    return "\n".join(lines)


def write_github_summary(markdown: str):
    """Записывает markdown в GitHub Actions Summary (переменная GITHUB_STEP_SUMMARY)."""
    summary_file = _os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_file:
        try:
            with open(summary_file, "w", encoding="utf-8") as f:
                f.write(markdown)
            print("  GitHub Summary: записан")
        except Exception as e:
            print(f"  GitHub Summary: ошибка — {e}")
    else:
        # Локальный запуск — выводим в файл
        with open("report.md", "w", encoding="utf-8") as f:
            f.write(markdown)
        print("  report.md сохранён (локальный режим)")


def update_gist(markdown: str, gist_id: str, github_token: str):
    """Обновляет GitHub Gist с отчётом (один и тот же файл, новый контент)."""
    if not gist_id or not github_token:
        return
    try:
        r = requests.patch(
            f"https://api.github.com/gists/{gist_id}",
            headers={
                "Authorization": f"token {github_token}",
                "Accept": "application/vnd.github+json",
            },
            json={"files": {"albion_bm_report.md": {"content": markdown}}},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            print(f"  Gist обновлён: {data.get('html_url','')}")
        else:
            print(f"  Gist: ошибка {r.status_code}")
    except Exception as e:
        print(f"  Gist: ошибка — {e}")


# ─────────────────────────────────────────────────────────────
#  КОЛИЧЕСТВО МАТЕРИАЛОВ НА 1 ШАГ ЗАЧАРОВАНИЯ
# ─────────────────────────────────────────────────────────────
ENCHANT_QTY = {
    "2H":      384,   # двуручное оружие
    "1H":      288,   # одноручное
    "offhand":  96,   # щит / факел / книга
    "armor":   192,   # нагрудник
    "head":     96,   # шлем
    "shoes":    96,   # сапоги
    "cape":     96,   # плащ
    "bag":     192,   # сумка
}

# Материалы зачарования (по тиру)
MATERIAL_IDS = {
    5: {"R": "T5_RUNE", "S": "T5_SOUL", "RE": "T5_RELIC"},
    6: {"R": "T6_RUNE", "S": "T6_SOUL", "RE": "T6_RELIC"},
    7: {"R": "T7_RUNE", "S": "T7_SOUL", "RE": "T7_RELIC"},
}
# Русские названия материалов
MAT_RU = {
    "T5_RUNE": "Руна (эксперт)",   "T5_SOUL": "Душа (эксперт)",   "T5_RELIC": "Реликт (эксперт)",
    "T6_RUNE": "Руна (мастер)",    "T6_SOUL": "Душа (мастер)",    "T6_RELIC": "Реликт (мастер)",
    "T7_RUNE": "Руна (магистр)",   "T7_SOUL": "Душа (магистр)",   "T7_RELIC": "Реликт (магистр)",
}

# ─────────────────────────────────────────────────────────────
#  ЗАГРУЗКА РУССКИХ НАЗВАНИЙ ИЗ items.json
# ─────────────────────────────────────────────────────────────
def load_names(path: str = "items.json") -> dict:
    """
    Возвращает {item_id: {"ru": str, "en": str}}.
    Пробует несколько стратегий парсинга — не упадёт на битом файле.
    """
    names: dict = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except FileNotFoundError:
        print(f"  [!] {path} не найден — используется встроенный словарь")
        return {}

    # Стратегия 1: полный JSON
    data = None
    try:
        data = json.loads(text)
        print(f"  items.json загружен полностью ({len(data)} записей)")
    except json.JSONDecodeError:
        pass

    # Стратегия 2: построчный NDJSON
    if data is None:
        data = []
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if line.startswith("{") and line.endswith("}"):
                try:
                    data.append(json.loads(line))
                except Exception:
                    pass
        if data:
            print(f"  items.json загружен построчно ({len(data)} записей)")

    # Стратегия 3: regex извлечение объектов
    if not data:
        for m in re.finditer(r'\{"UniqueName"[^{}]{10,2000}\}', text):
            try:
                data.append(json.loads(m.group()))
            except Exception:
                pass
        if data:
            print(f"  items.json частично распознан через regex ({len(data)} записей)")

    if not data:
        print("  [!] Не удалось распознать items.json — используется встроенный словарь")
        return {}

    for item in data:
        if not item:
            continue
        un = item.get("UniqueName", "") or ""
        if not un:
            continue
        locs = item.get("LocalizedNames") or {}
        ru   = locs.get("RU-RU", "") or ""
        en   = locs.get("EN-US", "") or ""
        names[un] = {"ru": ru, "en": en}

    print(f"  Загружено названий: {len(names)}")
    return names


# ─────────────────────────────────────────────────────────────
#  ВСТРОЕННЫЙ РЕЗЕРВНЫЙ СЛОВАРЬ (если items.json нет)
# ─────────────────────────────────────────────────────────────
FALLBACK_RU: dict[str, str] = {
    # Тир-префиксы добавляем при генерации
    "2H_AXE": "Большой топор",       "2H_BOW": "Лук",
    "2H_WARBOW": "Военный лук",       "2H_LONGBOW": "Длинный лук",
    "2H_CROSSBOW": "Арбалет",         "2H_CROSSBOWLARGE": "Тяжёлый арбалет",
    "2H_HAMMER": "Большой молот",     "2H_POLEHAMMER": "Шест-молот",
    "2H_QUARTERSTAFF": "Шест",        "2H_IRONCLADEDSTAFF": "Железный шест",
    "2H_DOUBLEBLADEDSTAFF": "Острый шест",
    "2H_SPEAR": "Пика",               "2H_GLAIVE": "Глефа",
    "2H_DAGGERPAIR": "Парные кинжалы","2H_CLAWPAIR": "Когти",
    "2H_CLAYMORE": "Клеймор",         "2H_DUALSWORD": "Парные мечи",
    "2H_FIRESTAFF": "Большой посох огня",   "2H_INFERNOSTAFF": "Адский посох",
    "2H_FROSTSTAFF": "Большой посох льда",  "2H_GLACIALSTAFF": "Ледяной посох",
    "2H_HOLYSTAFF": "Большой посох света",  "2H_DIVINESTAFF": "Божественный посох",
    "2H_NATURESTAFF": "Большой посох природы","2H_WILDSTAFF": "Дикий посох",
    "2H_ARCANESTAFF": "Большой мистический посох","2H_ENIGMATICSTAFF": "Загадочный посох",
    "2H_CURSEDSTAFF": "Большой проклятый посох","2H_DEMONICSTAFF": "Демонический посох",
    "2H_HALBERD": "Алебарда",         "2H_MACE": "Утренняя звезда",
    "2H_FLAIL": "Цеп",
    "2H_KNUCKLES_SET1": "Кулаки Авалона","2H_KNUCKLES_SET2": "Парные галатины",
    "2H_KNUCKLES_SET3": "Кулаки справедливости",
    "MAIN_SWORD": "Меч",              "MAIN_AXE": "Боевой топор",
    "MAIN_SPEAR": "Копьё",            "MAIN_HAMMER": "Молот",
    "MAIN_MACE": "Булава",            "MAIN_DAGGER": "Кинжал",
    "MAIN_1HCROSSBOW": "Арбалет (одноруч.)",
    "MAIN_FIRESTAFF": "Посох огня",   "MAIN_FROSTSTAFF": "Посох льда",
    "MAIN_HOLYSTAFF": "Посох света",  "MAIN_NATURESTAFF": "Посох природы",
    "MAIN_ARCANESTAFF": "Мистический посох","MAIN_CURSEDSTAFF": "Проклятый посох",
    "ARMOR_CLOTH_SET1": "Мантия учёного",  "ARMOR_CLOTH_SET2": "Мантия клирика",
    "ARMOR_CLOTH_SET3": "Мантия мага",
    "ARMOR_LEATHER_SET1": "Куртка наёмника","ARMOR_LEATHER_SET2": "Куртка охотника",
    "ARMOR_LEATHER_SET3": "Куртка убийцы",
    "ARMOR_PLATE_SET1": "Броня солдата","ARMOR_PLATE_SET2": "Броня рыцаря",
    "ARMOR_PLATE_SET3": "Броня стража",
    "HEAD_CLOTH_SET1": "Колпак учёного", "HEAD_CLOTH_SET2": "Колпак клирика",
    "HEAD_CLOTH_SET3": "Колпак мага",
    "HEAD_LEATHER_SET1": "Капюшон наёмника","HEAD_LEATHER_SET2": "Капюшон охотника",
    "HEAD_LEATHER_SET3": "Капюшон убийцы",
    "HEAD_PLATE_SET1": "Шлем солдата", "HEAD_PLATE_SET2": "Шлем рыцаря",
    "HEAD_PLATE_SET3": "Шлем стража",
    "SHOES_CLOTH_SET1": "Сандалии учёного","SHOES_CLOTH_SET2": "Сандалии клирика",
    "SHOES_CLOTH_SET3": "Сандалии мага",
    "SHOES_LEATHER_SET1": "Ботинки наёмника","SHOES_LEATHER_SET2": "Ботинки охотника",
    "SHOES_LEATHER_SET3": "Ботинки убийцы",
    "SHOES_PLATE_SET1": "Сапоги солдата","SHOES_PLATE_SET2": "Сапоги рыцаря",
    "SHOES_PLATE_SET3": "Сапоги стража",
    "OFFHAND_SHIELD": "Большой щит",  "OFFHAND_TORCH": "Факел",
    "OFFHAND_BOOK": "Книга заклинаний","OFFHAND_HORN": "Боевой рог",
    # Плащи (cape, 96 за шаг)
    "CAPE":                    "Плащ",
    "CAPEITEM_FW_BRIDGEWATCH": "Накидка Bridgewatch",
    "CAPEITEM_FW_FORTSTERLING":"Накидка Fort Sterling",
    "CAPEITEM_FW_LYMHURST":    "Накидка Lymhurst",
    "CAPEITEM_FW_MARTLOCK":    "Накидка Martlock",
    "CAPEITEM_FW_THETFORD":    "Накидка Thetford",
    "CAPEITEM_FW_CAERLEON":    "Накидка Caerleon",
    "CAPEITEM_HERETIC":        "Плащ Еретиков",
    "CAPEITEM_UNDEAD":         "Плащ Нежити",
    "CAPEITEM_KEEPER":         "Плащ Хранителей",
    "CAPEITEM_MORGANA":        "Плащ Морганы",
    "CAPEITEM_DEMON":          "Плащ Демонов",
    # Сумки (bag, 192 за шаг)
    "BAG":         "Сумка",
    "BAG_INSIGHT": "Кошель интуиции",
}
TIER_RU = {5: "(эксперт)", 6: "(мастер)", 7: "(магистр)"}


def get_name(uid: str, names_db: dict) -> dict:
    """Возвращает {"ru": str, "en": str} для item_id."""
    if uid in names_db:
        return names_db[uid]
    # Fallback: reconstruct from suffix
    tier = int(uid[1]) if uid[1].isdigit() else 0
    suffix = uid[3:]  # remove "T5_"
    ru_base = FALLBACK_RU.get(suffix, suffix)
    ru = f"{ru_base} {TIER_RU.get(tier,'')}"
    return {"ru": ru.strip(), "en": uid}


# ─────────────────────────────────────────────────────────────
#  СПИСОК ВЕЩЕЙ T5/T6/T7
# ─────────────────────────────────────────────────────────────
def build_items(names_db: dict) -> dict:
    items = {}

    def add(uid: str, slot: str):
        n = get_name(uid, names_db)
        items[uid] = {
            "slot": slot,
            "tier": int(uid[1]),
            "name_ru": n["ru"],
            "name_en": n["en"],
        }

    for t in [5, 6, 7]:
        p = f"T{t}_"
        for w in [
            "2H_AXE","2H_BOW","2H_WARBOW","2H_LONGBOW","2H_CROSSBOW","2H_CROSSBOWLARGE",
            "2H_HAMMER","2H_POLEHAMMER","2H_QUARTERSTAFF","2H_IRONCLADEDSTAFF",
            "2H_DOUBLEBLADEDSTAFF","2H_SPEAR","2H_GLAIVE","2H_DAGGERPAIR","2H_CLAWPAIR",
            "2H_CLAYMORE","2H_DUALSWORD",
            "2H_FIRESTAFF","2H_INFERNOSTAFF","2H_FROSTSTAFF","2H_GLACIALSTAFF",
            "2H_HOLYSTAFF","2H_DIVINESTAFF","2H_NATURESTAFF","2H_WILDSTAFF",
            "2H_ARCANESTAFF","2H_ENIGMATICSTAFF","2H_CURSEDSTAFF","2H_DEMONICSTAFF",
            "2H_HALBERD","2H_MACE","2H_FLAIL",
            "2H_KNUCKLES_SET1","2H_KNUCKLES_SET2","2H_KNUCKLES_SET3",
        ]:
            add(p + w, "2H")

        for w in [
            "MAIN_SWORD","MAIN_AXE","MAIN_SPEAR","MAIN_HAMMER","MAIN_MACE",
            "MAIN_DAGGER","MAIN_1HCROSSBOW",
            "MAIN_FIRESTAFF","MAIN_FROSTSTAFF","MAIN_HOLYSTAFF",
            "MAIN_NATURESTAFF","MAIN_ARCANESTAFF","MAIN_CURSEDSTAFF",
        ]:
            add(p + w, "1H")

        for s, slot in [
            ("ARMOR_CLOTH_SET1","armor"),("ARMOR_CLOTH_SET2","armor"),("ARMOR_CLOTH_SET3","armor"),
            ("ARMOR_LEATHER_SET1","armor"),("ARMOR_LEATHER_SET2","armor"),("ARMOR_LEATHER_SET3","armor"),
            ("ARMOR_PLATE_SET1","armor"),("ARMOR_PLATE_SET2","armor"),("ARMOR_PLATE_SET3","armor"),
            ("HEAD_CLOTH_SET1","head"),("HEAD_CLOTH_SET2","head"),("HEAD_CLOTH_SET3","head"),
            ("HEAD_LEATHER_SET1","head"),("HEAD_LEATHER_SET2","head"),("HEAD_LEATHER_SET3","head"),
            ("HEAD_PLATE_SET1","head"),("HEAD_PLATE_SET2","head"),("HEAD_PLATE_SET3","head"),
            ("SHOES_CLOTH_SET1","shoes"),("SHOES_CLOTH_SET2","shoes"),("SHOES_CLOTH_SET3","shoes"),
            ("SHOES_LEATHER_SET1","shoes"),("SHOES_LEATHER_SET2","shoes"),("SHOES_LEATHER_SET3","shoes"),
            ("SHOES_PLATE_SET1","shoes"),("SHOES_PLATE_SET2","shoes"),("SHOES_PLATE_SET3","shoes"),
            ("OFFHAND_SHIELD","offhand"),("OFFHAND_TORCH","offhand"),
            ("OFFHAND_BOOK","offhand"),("OFFHAND_HORN","offhand"),
        ]:
            add(p + s, slot)


        # ── Плащи (cape, qty=96) ──────────────────────────────────
        for uid, slot in [
            (p + "CAPE",                    "cape"),
            (p + "CAPEITEM_FW_BRIDGEWATCH", "cape"),
            (p + "CAPEITEM_FW_FORTSTERLING","cape"),
            (p + "CAPEITEM_FW_LYMHURST",    "cape"),
            (p + "CAPEITEM_FW_MARTLOCK",    "cape"),
            (p + "CAPEITEM_FW_THETFORD",    "cape"),
            (p + "CAPEITEM_FW_CAERLEON",    "cape"),
            (p + "CAPEITEM_HERETIC",        "cape"),
            (p + "CAPEITEM_UNDEAD",         "cape"),
            (p + "CAPEITEM_KEEPER",         "cape"),
            (p + "CAPEITEM_MORGANA",        "cape"),
            (p + "CAPEITEM_DEMON",          "cape"),
        ]:
            add(uid, slot)

        # ── Сумки (bag, qty=192) ──────────────────────────────────
        for uid, slot in [
            (p + "BAG",         "bag"),
            (p + "BAG_INSIGHT", "bag"),
        ]:
            add(uid, slot)

    return items


# ─────────────────────────────────────────────────────────────
#  API
# ─────────────────────────────────────────────────────────────
def _get(url: str, params: dict) -> list | None:
    for attempt in range(1, MAX_RETRY + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = 5 * attempt
                print(f"\n  [429] Rate limit — жду {wait}s...")
                time.sleep(wait)
            else:
                print(f"\n  [HTTP {r.status_code}] попытка {attempt}/{MAX_RETRY}")
                time.sleep(2)
        except Exception as e:
            print(f"\n  [ERR] {e} — попытка {attempt}/{MAX_RETRY}")
            time.sleep(2)
    return None

def age_minutes(ts: str, now_utc: datetime) -> int | None:
    """Сколько минут прошло с момента обновления цены (UTC timestamp из API)."""
    if not ts or ts.startswith("0001"):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z","")).replace(tzinfo=timezone.utc)
        diff = now_utc - dt
        return int(diff.total_seconds() / 60)
    except Exception:
        return None

def age_label(minutes: int | None) -> str:
    if minutes is None:
        return "нет данных"
    if minutes < 20:
        return f"✅ {minutes} мин назад"
    if minutes < 60:
        return f"⚠ {minutes} мин назад"
    if minutes < 1440:
        h = minutes // 60
        return f"❌ {h}ч назад"
    d = minutes // 1440
    return f"❌ {d}д назад"


def fetch_prices(ids: list, location: str, qualities: list) -> dict:
    """→ {(item_id, quality): {sell_min, sell_max, buy_max, buy_max_date}}"""
    raw = _get(
        f"{BASE_URL}/{','.join(ids)}.json",
        {"locations": location, "qualities": ",".join(map(str, qualities))},
    )
    if not raw:
        return {}
    result = {}
    for e in raw:
        key = (e.get("item_id",""), e.get("quality", 1))
        sm   = e.get("sell_price_min", 0) or 0
        sx   = e.get("sell_price_max", 0) or 0
        bx   = e.get("buy_price_max",  0) or 0
        bxd  = e.get("buy_price_max_date", "") or ""  # timestamp обновления BM цены
        smd  = e.get("sell_price_min_date","") or ""  # timestamp Sell Order в Caerleon
        if key not in result:
            result[key] = {"sell_min": 0, "sell_max": 0, "buy_max": 0,
                           "buy_max_date": "", "sell_min_date": ""}
        if sm > 0 and (result[key]["sell_min"] == 0 or sm < result[key]["sell_min"]):
            result[key]["sell_min"]      = sm
            result[key]["sell_min_date"] = smd
        if sx > result[key]["sell_max"]:
            result[key]["sell_max"] = sx
        if bx > result[key]["buy_max"]:
            result[key]["buy_max"]      = bx
            result[key]["buy_max_date"] = bxd
    return result


def fetch_history_avg(ids: list, location: str) -> dict:
    """→ {item_id: {avg_price, item_count}}"""
    raw = _get(
        f"{HIST_URL}/{','.join(ids)}.json",
        {"locations": location, "time-scale": HISTORY_H},
    )
    if not raw:
        return {}
    result = {}
    for e in raw:
        iid  = e.get("item_id", "")
        data = e.get("data") or []
        if not data:
            continue
        latest = sorted(data, key=lambda x: x.get("timestamp",""), reverse=True)[0]
        avg = latest.get("avg_price", 0) or 0
        cnt = latest.get("item_count", 0) or 0
        if avg > 0:
            result[iid] = {"avg_price": avg, "item_count": cnt}
    return result


def fetch_all(ids: list, location: str, qualities: list, label: str) -> dict:
    out = {}
    n   = len(ids)
    for i in range(0, n, BATCH_SIZE):
        batch = ids[i:i + BATCH_SIZE]
        done  = min(i + BATCH_SIZE, n)
        pct   = done * 100 // n
        print(f"  [{done:>4}/{n}] {pct:3}%  {label}: {batch[0][:30]}      ", end="\r")
        sys.stdout.flush()
        out.update(fetch_prices(batch, location, qualities))
        if done < n:
            time.sleep(DELAY_SEC)
    print()
    return out


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def scan_once(seen_alerts: set) -> set:
    """
    Один прогон сканирования.
    seen_alerts — set ключей уже отправленных уведомлений (item+enc+quality).
    Возвращает обновлённый seen_alerts.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("=" * 70)
    print("  Albion Online — BM Enchanting Arbitrage Scanner v4")
    print(f"  Время: {ts}  |  Сервер: {SERVER}")
    print("=" * 70)

    TAX    = 0.04
    q_list = list(QUALITIES.keys())

    # 1. Загружаем названия
    print("\n[1/6] Загружаем названия предметов из items.json...")
    names_db = load_names("items.json")

    # 2. Список вещей
    print("\n[2/6] Строим список вещей...")
    items    = build_items(names_db)
    base_ids = list(items.keys())
    enc_ids  = [f"{iid}@{e}" for iid in base_ids for e in [1, 2, 3]]
    mat_ids  = [mid for tm in MATERIAL_IDS.values() for mid in tm.values()]
    print(f"  Вещей: {len(items)} | Зачарованных ID: {len(enc_ids)} | Материалов: {len(mat_ids)}")

    # 3. Материалы: цены и история
    print(f"\n[3/6] Материалы (Руны/Души/Реликты) — Caerleon, Sell Order...")
    mat_price_raw: dict = {}
    for i in range(0, len(mat_ids), BATCH_SIZE):
        mat_price_raw.update(fetch_prices(mat_ids[i:i+BATCH_SIZE], CAERLEON, [1]))
        time.sleep(DELAY_SEC)

    mat_hist_raw: dict = {}
    for i in range(0, len(mat_ids), BATCH_SIZE):
        mat_hist_raw.update(fetch_history_avg(mat_ids[i:i+BATCH_SIZE], CAERLEON))
        time.sleep(DELAY_SEC)

    # Собираем mat[tier][key] = {sell_min, sell_max, avg, id, ru_name}
    mat: dict = {}
    mat_csv_rows = []
    for tier, tier_mats in MATERIAL_IDS.items():
        mat[tier] = {}
        for key, mid in tier_mats.items():
            pr  = mat_price_raw.get((mid, 1), {})
            hr  = mat_hist_raw.get(mid, {})
            sm  = pr.get("sell_min", 0)
            sx  = pr.get("sell_max", 0)
            avg = hr.get("avg_price", 0)
            cnt = hr.get("item_count", 0)
            avg_use = avg if avg > 0 else sm   # fallback если нет истории
            mat[tier][key] = {
                "sell_min": sm, "sell_max": sx, "avg": avg_use,
                "id": mid, "ru": MAT_RU.get(mid, mid),
            }
            mat_csv_rows.append({
                "item_id": mid, "name_ru": MAT_RU.get(mid, mid),
                "tier": tier,
                "sell_price_min": sm, "avg_price_24h": avg,
                "sell_price_max": sx, "volume_24h": cnt,
                "примечание": "avg=sell_min (нет истории)" if avg == 0 else "",
            })

    # ── Таблица материалов в терминале ──
    print()
    print("  ┌──────────────────────────┬───────────────────────────────────────────────────────────────┐")
    print("  │ Материал                 │  sell_min    avg_24h   sell_max   объём_24h │ Примечание      │")
    print("  ├──────────────────────────┼───────────────────────────────────────────────────────────────┤")
    for r in mat_csv_rows:
        note = r["примечание"] or ""
        print(f"  │ {r['name_ru']:<24} │ {r['sell_price_min']:>9,}  {r['avg_price_24h']:>9,}"
              f"  {r['sell_price_max']:>9,}  {r['volume_24h']:>10,} │ {note:<15} │")
    print("  └──────────────────────────┴───────────────────────────────────────────────────────────────┘")

    # ── Сколько нужно купить по слотам ──
    print()
    print("  ┌─────────────────────────────────────────────────────────────────────┐")
    print("  │  НУЖНО КУПИТЬ МАТЕРИАЛОВ (кол-во на 1 шаг зачарования):            │")
    print("  ├──────────────────┬────────┬─────────────────────────────────────────┤")
    print("  │ Тип слота        │  Кол-во│ Примеры предметов                       │")
    print("  ├──────────────────┼────────┼─────────────────────────────────────────┤")
    slot_examples = {
        "2H":      "Большой топор, Лук, Клеймор, посохи",
        "1H":      "Меч, Копьё, Кинжал, 1Н посохи",
        "armor":   "Нагрудная броня, куртки, мантии",
        "head":    "Шлемы, колпаки, капюшоны",
        "shoes":   "Сапоги, ботинки, сандалии",
        "offhand": "Щит, факел, книга, рог",
    }
    for slot, ex in slot_examples.items():
        q = ENCHANT_QTY.get(slot, 96)
        print(f"  │ {slot:<16} │ {q:>6} │ {ex:<39} │")
    print("  └──────────────────┴────────┴─────────────────────────────────────────┘")

    # ── Примеры затрат на T5/T6/T7 ──
    print()
    print("  ┌───────────────────────────────────────────────────────────────────────────────────────────┐")
    print("  │  ПРИМЕР ЗАТРАТ: Большой топор / Нагрудник (кол-во × цена = итого)                        │")
    print("  │  Три сценария: BEST (sell_min, если хватит объёма) / AVG (реально) / WORST (sell_max)     │")
    print("  ├────────────┬────────────────────────────────────────────────────────────────────────────────┤")
    for tier in [5, 6, 7]:
        tm = mat.get(tier, {})
        r = tm.get("R", {}); s = tm.get("S", {}); re = tm.get("RE", {})
        print(f"  │ T{tier} 2H оружие (384 на шаг):                                                              │")
        for enc_label, mats_used in [
            ("@0→@1 (покупаешь Руны)", [("R", 384)]),
            ("@0→@2 (Руны + Души)",    [("R", 384), ("S", 384)]),
            ("@0→@3 (+Реликты)",       [("R", 384), ("S", 384), ("RE", 384)]),
        ]:
            cost_b = sum(tm.get(k,{}).get("sell_min",0)*q for k,q in mats_used)
            cost_a = sum(tm.get(k,{}).get("avg",0)*q for k,q in mats_used)
            cost_w = sum(tm.get(k,{}).get("sell_max",0)*q for k,q in mats_used)
            parts  = [f"{q}×{tm.get(k,{}).get('ru',k)} @ {tm.get(k,{}).get('sell_min',0)}" for k,q in mats_used]
            detail = " + ".join(parts)
            print(f"  │   {enc_label:<25} {detail[:42]:<42}  BEST={cost_b:>9,}  AVG={cost_a:>9,}  WORST={cost_w:>9,} │")
        print(f"  ├────────────┬────────────────────────────────────────────────────────────────────────────────┤")
    print("  └────────────┴────────────────────────────────────────────────────────────────────────────────┘")

    # 4. Базовые вещи
    print(f"\n[4/6] Базовые вещи (без зачарования) → Sell Order Caerleon (все качества)...")
    base_p = fetch_all(base_ids, CAERLEON, q_list, "базовые")
    print(f"  Записей с ценой: {sum(1 for v in base_p.values() if v['sell_min']>0)}")

    # 5. Зачарованные вещи на ЧР
    print(f"\n[5/6] Зачарованные вещи → Buy Order ЧР (все качества)...")
    enc_p = fetch_all(enc_ids, BM, q_list, "зачарованные")
    print(f"  Записей с ценой: {sum(1 for v in enc_p.values() if v['buy_max']>0)}")

    # 6. Расчёт
    print("\n[6/6] Считаем профит...")
    raw_rows    = []
    profit_rows = []

    now_utc = datetime.now(timezone.utc)
    for iid, info in items.items():
        tier    = info["tier"]
        slot    = info["slot"]
        qty     = ENCHANT_QTY.get(slot, 192)
        name_ru = info["name_ru"]
        name_en = info["name_en"]
        tm      = mat.get(tier, {})

        r_min  = tm.get("R",{}).get("sell_min",0);  r_avg  = tm.get("R",{}).get("avg",0);  r_max  = tm.get("R",{}).get("sell_max",0)
        s_min  = tm.get("S",{}).get("sell_min",0);  s_avg  = tm.get("S",{}).get("avg",0);  s_max  = tm.get("S",{}).get("sell_max",0)
        re_min = tm.get("RE",{}).get("sell_min",0); re_avg = tm.get("RE",{}).get("avg",0); re_max = tm.get("RE",{}).get("sell_max",0)

        for quality, q_name in QUALITIES.items():
            base_sell = base_p.get((iid, quality), {}).get("sell_min", 0)

            for enc in [1, 2, 3]:
                enc_entry = enc_p.get((f"{iid}@{enc}", quality), {})
                bm_buy      = enc_entry.get("buy_max", 0)
                bm_upd_date = enc_entry.get("buy_max_date", "")
                bm_age_min  = age_minutes(bm_upd_date, now_utc)
                bm_age_str  = age_label(bm_age_min)

                # Стоимость материалов (все 3 сценария)
                if enc == 1:
                    mc_b  = qty * r_min;  mc_a  = qty * r_avg;  mc_w  = qty * r_max
                    mat_desc = f"{qty}×Руна"
                    mat_buy_desc = f"{qty} шт. (Руна: {r_avg:,} сер.)"
                elif enc == 2:
                    mc_b  = qty*r_min + qty*s_min;  mc_a = qty*r_avg + qty*s_avg;  mc_w = qty*r_max + qty*s_max
                    mat_desc = f"{qty}×Руна + {qty}×Душа"
                    mat_buy_desc = f"{qty} шт. (Руна: {r_avg:,}, Душа: {s_avg:,} сер.)"
                else:
                    mc_b  = qty*r_min + qty*s_min + qty*re_min
                    mc_a  = qty*r_avg + qty*s_avg + qty*re_avg
                    mc_w  = qty*r_max + qty*s_max + qty*re_max
                    mat_desc = f"{qty}×Руна + {qty}×Душа + {qty}×Реликт"
                    mat_buy_desc = f"{qty} шт. (Руна: {r_avg:,}, Душа: {s_avg:,}, Реликт: {re_avg:,} сер.)"

                # RAW
                raw_rows.append({
                    "item_id":iid,"name_ru":name_ru,"name_en":name_en,
                    "tier":tier,"slot":slot,"quality":quality,"quality_name":q_name,"enchant":enc,
                    "base_sell_caerleon":base_sell,"bm_buy_order":bm_buy,
                    "mat_qty":qty,"mat_desc":mat_desc,
                    "rune_sell_min":r_min,"rune_avg_24h":r_avg,"rune_sell_max":r_max,
                    "soul_sell_min":s_min,"soul_avg_24h":s_avg,"soul_sell_max":s_max,
                    "relic_sell_min":re_min,"relic_avg_24h":re_avg,"relic_sell_max":re_max,
                    "mat_cost_best":int(mc_b),"mat_cost_avg":int(mc_a),"mat_cost_worst":int(mc_w),
                    "bm_updated_at":bm_upd_date,"bm_age_minutes":bm_age_min,"bm_freshness":bm_age_str,
                })

                # ПРОФИТ
                if base_sell == 0 or bm_buy == 0: continue
                if r_min == 0: continue
                if enc >= 2 and s_min == 0: continue
                if enc == 3 and re_min == 0: continue

                rev = bm_buy * (1 - TAX)

                def calc(mc):
                    total = base_sell + mc
                    profit = rev - total
                    roi = profit / total * 100 if total > 0 else 0
                    return int(total), int(profit), round(roi, 1)

                total_b, profit_b, roi_b = calc(mc_b)
                total_a, profit_a, roi_a = calc(mc_a)
                total_w, profit_w, roi_w = calc(mc_w)

                profit_rows.append({
                    "profitable_avg":  "YES" if profit_a > 0 else "NO",
                    "profit_avg":      profit_a, "roi_avg_pct": roi_a,
                    "profitable_best": "YES" if profit_b > 0 else "NO",
                    "profit_best":     profit_b, "roi_best_pct": roi_b,
                    "profitable_worst":"YES" if profit_w > 0 else "NO",
                    "profit_worst":    profit_w, "roi_worst_pct": roi_w,
                    "item_id":iid,"name_ru":name_ru,"name_en":name_en,
                    "tier":tier,"slot":slot,
                    "quality":quality,"quality_name":q_name,
                    "enchant":enc,
                    "base_sell_caerleon":base_sell,
                    "mat_qty":qty,
                    "mat_buy_desc":mat_buy_desc,
                    "mat_cost_best":int(mc_b),"mat_cost_avg":int(mc_a),"mat_cost_worst":int(mc_w),
                    "mat_desc":mat_desc,
                    "total_best":total_b,"total_avg":total_a,"total_worst":total_w,
                    "bm_buy_order":bm_buy,"revenue_after_tax":int(rev),
                    "bm_updated_at":bm_upd_date,"bm_age_minutes":bm_age_min,"bm_freshness":bm_age_str,
                })

    profit_rows.sort(key=lambda x: (x["profitable_avg"]!="YES", -x["profit_avg"]))

    # Сохраняем CSV
    with open("materials_prices.csv","w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["item_id","name_ru","tier","sell_price_min",
                                          "avg_price_24h","sell_price_max","volume_24h","примечание"])
        w.writeheader(); w.writerows(mat_csv_rows)

    raw_fields = ["item_id","name_ru","name_en","tier","slot","quality","quality_name","enchant",
                  "base_sell_caerleon","bm_buy_order","mat_qty","mat_desc",
                  "rune_sell_min","rune_avg_24h","rune_sell_max",
                  "soul_sell_min","soul_avg_24h","soul_sell_max",
                  "relic_sell_min","relic_avg_24h","relic_sell_max",
                  "mat_cost_best","mat_cost_avg","mat_cost_worst",
                  "bm_updated_at","bm_freshness","bm_age_minutes"]
    with open("raw_prices.csv","w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=raw_fields)
        w.writeheader(); w.writerows(raw_rows)

    prof_fields = ["profitable_avg","profit_avg","roi_avg_pct",
                   "profitable_best","profit_best","roi_best_pct",
                   "profitable_worst","profit_worst","roi_worst_pct",
                   "item_id","name_ru","name_en","tier","slot",
                   "quality","quality_name","enchant",
                   "base_sell_caerleon","mat_qty","mat_buy_desc","mat_desc",
                   "mat_cost_best","mat_cost_avg","mat_cost_worst",
                   "total_best","total_avg","total_worst",
                   "bm_buy_order","revenue_after_tax",
                   "bm_updated_at","bm_freshness","bm_age_minutes"]
    with open("profit_analysis.csv","w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=prof_fields)
        w.writeheader(); w.writerows(profit_rows)

    # ── Топ-30 прибыльных в терминале ──
    good = [r for r in profit_rows if r["profitable_avg"] == "YES"]

    # ── GitHub Summary + Gist ──────────────────────────────────
    summary_md = build_summary_md(profit_rows, ts)
    write_github_summary(summary_md)
    if GIST_ID:
        update_gist(summary_md, GIST_ID, GITHUB_TOKEN)

    print()
    print(f"  ├ raw_prices.csv       {len(raw_rows):>6,} строк")
    print(f"  ├ profit_analysis.csv  {len(profit_rows):>6,} строк  (прибыльных по AVG: {len(good)})")
    print(f"  └ materials_prices.csv {len(mat_csv_rows):>6,} строк")

    if good:
        print()
        W = 175
        print("=" * W)
        print(f"  ТОП ПРИБЫЛЬНЫХ  |  {ts}  |  Сортировка по profit_avg (реалистичный сценарий)")
        print("=" * W)
        print(f"  {'Предмет (RU)':<38} T Q @  {'База':>9}  {'Mat(avg)':>10}  "
              f"{'Итого':>10}  {'ЧР Buy':>10}  {'Profit AVG':>11}  {'ROI':>6}  Нужно купить")
        print("-" * W)
        for r in good[:30]:
            print(
                f"  ✅ {r['name_ru']:<36} "
                f"{r['tier']} {r['quality']} @{r['enchant']}  "
                f"{r['base_sell_caerleon']:>9,}  "
                f"{r['mat_cost_avg']:>10,}  "
                f"{r['total_avg']:>10,}  "
                f"{r['bm_buy_order']:>10,}  "
                f"{r['profit_avg']:>+11,}  "
                f"{r['roi_avg_pct']:>+5.1f}%  "
                f"{r['mat_buy_desc']}  "
                f"{r['bm_freshness']}  ({r['bm_age_minutes']} мин)"
            )

    print()
    print("  Легенда качества в столбце Q:")
    for k,v in QUALITIES.items():
        print(f"    {k} = {v}")
    print()
    print("  ⚠ profit_avg  — реалистично (средняя цена торгов за 24ч)")
    print("  ⚠ profit_best — оптимистично (минимальный ордер, если хватит объёма)")
    print("  ⚠ profit_worst— пессимистично (максимальный ордер в стакане)")
    print("  ⚠ BM цены меняются каждые ~20 мин — перезапускай перед операцией!")
    print("  ⚠ @2 = ДВА шага: @0→@1 (руны), затем @1→@2 (души).")

    # ── Telegram-уведомления ──────────────────────────────────
    new_alerts = []
    for r in good:
        if r["profit_avg"] < PROFIT_THRESHOLD:
            continue
        alert_key = f"{r['item_id']}_{r['enchant']}_{r['quality']}"
        if alert_key not in seen_alerts:
            new_alerts.append(r)
            seen_alerts.add(alert_key)

    if new_alerts:
        print(f"  НОВЫХ УВЕДОМЛЕНИЙ: {len(new_alerts)} (профит > {PROFIT_THRESHOLD:,})")
        for r in new_alerts:
            print(f"     → {r['name_ru']} @{r['enchant']} Q{r['quality']}: "
                  f"profit_avg={r['profit_avg']:+,}")
        msg = build_tg_message(new_alerts)
        ok = send_telegram(msg)
        print(f"  Telegram: {'OK отправлено' if ok else 'ОШИБКА отправки'}")
    else:
        print(f"  Новых позиций выше {PROFIT_THRESHOLD:,} нет.")

    return seen_alerts




def main():
    if LOOP_MODE:
        print("Режим мониторинга: сканирую каждые " + str(SCAN_INTERVAL) + " минут")
        print("Порог уведомления: " + str(PROFIT_THRESHOLD) + " сер.")
        print("Нажми Ctrl+C для остановки")
        seen = set()
        scan_num = 0
        while True:
            scan_num += 1
            sep = "=" * 70
            print()
            print(sep)
            print("  СКАН #" + str(scan_num) + "  |  " +
                  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
            print(sep)
            try:
                seen = scan_once(seen)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print("[!] Ошибка в скане #" + str(scan_num) + ": " + str(e))
                print("Жду " + str(SCAN_INTERVAL) + " мин до следующего скана...")
            next_h = datetime.now(timezone.utc).strftime("%H:%M")
            print("Следующий скан через " + str(SCAN_INTERVAL) + " мин (сейчас " + next_h + " UTC)")
            try:
                time.sleep(SCAN_INTERVAL * 60)
            except KeyboardInterrupt:
                print("Остановлено пользователем.")
                break
    else:
        scan_once(set())


if __name__ == "__main__":
    main()
