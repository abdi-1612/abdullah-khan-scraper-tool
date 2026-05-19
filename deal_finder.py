#!/usr/bin/env python3
"""
Local deal finder for Kijiji + Facebook Marketplace.

Current goals:
- keep setup small and practical
- use workbook-driven pricing
- build a conservative text-first reasoning pipeline
- reduce false positives before valuation
- support future image analysis without pretending it is implemented today
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import hashlib
import html
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover - fallback when deps are missing
    fuzz = None


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
PRICES_PATH = BASE_DIR / "prices.csv"
STATE_PATH = BASE_DIR / "state.json"
SEEN_LISTINGS_PATH = BASE_DIR / "seen_listings.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

JSON_LD_PATTERN = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
NEXT_DATA_PATTERN = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
MONEY_PATTERN = re.compile(r"(?:CA\$|C\$|\$)\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
FREE_PRICE_PATTERN = re.compile(
    r"^(?:free|free pickup|free pick up|free obo|free o\.b\.o\.|free if picked up|free negotiable)$",
    re.IGNORECASE,
)
ID_PATTERN = re.compile(r"(\d{6,})/?$")
YEAR_PATTERN = re.compile(r"\b(201[4-9]|202[0-6])\b")
M_CHIP_PATTERN = re.compile(r"\bm\s*([1-9])(?:\s*(pro|max|ultra))?\b")
INTEL_PATTERN = re.compile(r"\b(?:intel|core\s*i[3579]|i[3579])\b")
SIZE_PATTERN = re.compile(r"\b(11|13|14|15|16|17|18|40|41|42|44|45|46|49)\s*(?:inch|in|mm)\b")
GENERIC_STORAGE_PATTERN = re.compile(r"\b(\d{1,4})\s*(gb|tb)\b")
BARE_STORAGE_PATTERN = re.compile(r"\b(64|128|256|512|1024)\b")
RAM_PATTERN = re.compile(r"\b(4|8|12|16|18|24|32|36|48|64|96)\s*gb\b")
NOISE_LINE_PATTERN = re.compile(
    r"\b("
    r"sponsored|marketplace|shipping|delivery|pickup|pick up|today|yesterday|"
    r"hour|hours|day|days|week|weeks|month|months|seller|save|hide|available|"
    r"listed|pending|sold|kms|km away"
    r")\b",
    re.IGNORECASE,
)

CARRIER_KEYWORDS = (
    "carrier locked",
    "locked to",
    "sim locked",
    "network locked",
    "at&t",
    "att",
    "verizon",
    "tmobile",
    "t-mobile",
    "sprint",
    "rogers",
    "bell",
    "telus",
    "fido",
    "koodo",
    "virgin",
    "xfinity",
    "boost",
    "metro pcs",
    "metropcs",
    "cricket",
    "tracfone",
)

GENERIC_MODEL_TOKENS = {
    "apple",
    "google",
    "samsung",
    "galaxy",
    "iphone",
    "ipad",
    "macbook",
    "pixel",
    "watch",
}

IMPORTANT_MODEL_TOKENS = {
    "a",
    "air",
    "edge",
    "fe",
    "flip",
    "fold",
    "max",
    "mini",
    "note",
    "pm",
    "pro",
    "plus",
    "s",
    "se",
    "ultra",
    "xl",
    "z",
}

EXCLUSIVE_VARIANT_TOKENS = {
    "iphone": {"mini", "plus", "pro", "max", "se"},
    "ipad": {"air", "mini", "pro"},
    "samsung": {"edge", "fe", "flip", "fold", "note", "plus", "ultra"},
    "pixel": {"a", "fold", "pro", "xl"},
    "apple_watch": {"se", "ultra"},
}

SEARCH_CATEGORY_HINTS = {
    "iphone": ["iphone"],
    "ipad": ["ipad"],
    "macbook": ["macbook", "mac book"],
    "samsung": ["samsung", "galaxy"],
    "pixel": ["pixel"],
    "apple_watch": ["apple watch", "watch"],
}

CATEGORY_CONTEXT_PATTERNS = {
    "iphone": [r"\biphone\b"],
    "ipad": [r"\bipad\b"],
    "macbook": [r"\bmacbook\b", r"\bmac book\b", r"\bmba\b", r"\bmbp\b"],
    "samsung": [r"\bsamsung\b", r"\bgalaxy\b", r"\bs\d{2}\b", r"\bnote\b", r"\bfold\b", r"\bflip\b"],
    "pixel": [r"\bpixel\b"],
    "apple_watch": [r"\bapple watch\b", r"\bwatch ultra\b", r"\bwatch se\b", r"\bwatch series\b"],
}

UNRELATED_HOME_TERMS = [
    "floor lamp",
    "lamp",
    "nightstand",
    "dresser",
    "sofa",
    "couch",
    "table",
    "coffee table",
    "chair",
    "desk",
    "bed frame",
    "bedframe",
    "mattress",
    "mirror",
    "cabinet",
    "bookshelf",
    "shelf",
    "curtain",
    "pillow",
]

ACCESSORY_TERMS = [
    "case",
    "cases",
    "cover",
    "folio",
    "smart folio",
    "smart cover",
    "holder",
    "screen protector",
    "charging cable",
    "cable",
    "adapter",
    "band",
    "strap",
    "keyboard",
    "keyboard case",
    "keyboard folio",
    "magic keyboard",
    "mouse",
    "dock",
    "mount",
    "apple pencil",
    "pencil",
    "stylus",
    "skin",
    "sleeve",
    "stand",
    "wallet",
]

TITLE_ACCESSORY_TERMS = [
    "smart folio",
    "smart cover",
    "folio",
    "case",
    "cover",
    "holder",
    "screen protector",
    "charger",
    "charging cable",
    "cable",
    "adapter",
    "keyboard case",
    "keyboard folio",
    "magic keyboard",
    "apple pencil",
    "pencil",
    "stylus",
    "mount",
    "stand",
    "dock",
    "skin",
    "sleeve",
    "band",
    "strap",
    "wallet",
]

BUNDLED_ACCESSORY_TERMS = (
    "accessories",
    "band",
    "bands",
    "box",
    "cable",
    "case",
    "cases",
    "charger",
    "chargers",
    "charging cable",
    "cover",
    "screen protector",
    "strap",
    "straps",
)

PARTS_ONLY_PATTERNS = {
    "for_parts": [
        r"\bfor parts\b",
        r"\bparts only\b",
        r"\bpart[s]?\s+only\b",
        r"\brepair only\b",
        r"\bfor repair\b",
        r"\bas is for parts\b",
    ],
    "icloud_locked": [
        r"\bicloud lock(?:ed)?\b",
        r"\bicloud locked\b",
        r"\blocked to icloud\b",
        r"\bfind my iphone on\b",
        r"\bfind my device on\b",
    ],
    "activation_locked": [
        r"\bactivation lock(?:ed)?\b",
        r"\bactivation locked\b",
        r"\bactivation lock is on\b",
    ],
    "water_damage": [r"\bwater damage\b", r"\bliquid damage\b", r"\bwater damaged\b"],
}

DAMAGE_PATTERNS = {
    "cracked_unspecified": [r"\bcracked\b", r"\bcrack\b", r"\bcracks\b"],
    "needs_new_screen": [
        r"needs? (?:a )?new screen",
        r"needs? screen repair",
        r"screen needs repair",
        r"needs? (?:a )?screen\b",
        r"needs? (?:the )?display\b",
    ],
    "cracked_screen": [
        r"crack(?:ed)? screen",
        r"screen crack",
        r"screen cracked",
        r"display crack",
        r"broken screen",
        r"shattered screen",
        r"front glass crack",
        r"front glass broken",
        r"front cracked",
        r"screen is cracked",
        r"display is cracked",
    ],
    "cracked_back": [
        r"crack(?:ed)? back",
        r"back cracked",
        r"back is cracked",
        r"back glass crack",
        r"back glass broken",
        r"cracked back glass",
        r"rear glass crack",
        r"rear glass broken",
        r"rear is cracked",
    ],
    "cracked_lens": [r"crack(?:ed)? lens", r"camera lens crack"],
    "screen_issue": [
        r"screen issue",
        r"dead pixel",
        r"ghost touch",
        r"touch issue",
        r"burn in",
        r"burn-in",
        r"line on (?:the )?screen",
        r"lines on (?:the )?screen",
        r"green line",
        r"pink line",
        r"vertical line",
        r"horizontal line",
        r"screen has line",
        r"line on side",
    ],
    "dead_pixels": [r"dead pixels?", r"dead pixel"],
    "ghost_touch": [r"ghost touch"],
    "rough_condition": [
        r"rough condition",
        r"poor condition",
        r"heav(?:y|ily) scratched",
        r"deep scratches?",
        r"rough shape",
        r"beat up",
    ],
    "heavy_scratching": [
        r"scratched badly",
        r"badly scratched",
        r"heavy scratches?",
        r"heavily scratched",
        r"scratched up",
        r"lots of scratches?",
        r"scratches all over",
    ],
    "bad_face_id": [
        r"bad face id",
        r"no face id",
        r"face id (?:broken|issue|issues)",
        r"face id (?:dont|don t|doesn t|doesnt|does not|not) work",
        r"face id (?:doesn t|doesnt|does not|not) work",
        r"unable to activate face id",
    ],
    "bad_touch_id": [
        r"bad touch id",
        r"no touch id",
        r"touch id (?:broken|issue|issues)",
        r"touch id (?:doesn t|doesnt|does not|not) work",
        r"unable to activate touch id",
    ],
    "bad_back_camera": [
        r"back camera (?:issue|broken|bad|not work)",
        r"camera issue",
        r"camera (?:is )?glitchy",
        r"camera glitch",
    ],
    "battery_under_80": [
        r"battery health\s*(?:under|below|less than)\s*80",
        r"battery under 80",
        r"battery health(?:\s+is)?(?:\s+at)?\s*7\d%?",
        r"battery health(?:\s+is)?(?:\s+at)?\s*6\d%?",
        r"battery health\s*7\d%",
        r"battery health\s*6\d%",
        r"battery health\s*7\d\b",
        r"battery health\s*6\d\b",
        r"\bbh\s*[:=]?\s*7\d\b",
        r"\bbh\s*[:=]?\s*6\d\b",
        r"maximum capacity\s*7\d\b",
        r"maximum capacity\s*6\d\b",
    ],
    "google_locked": [r"google locked", r"frp locked"],
    "kg_active": [r"\bkg active\b", r"\btrade in lock\b"],
    "repair_message": [r"repair message", r"non genuine", r"important battery message", r"important display message"],
    "demo": [r"\bdemo\b"],
    "missing_parts": [r"missing parts?", r"missing stylus", r"missing band", r"missing charger", r"no charger", r"no band"],
    "no_charger": [r"missing charger", r"no charger"],
    "no_band": [r"missing band", r"no band"],
    "missing_box": [r"missing box", r"no box"],
    "scratches": [
        r"\bscratches\b",
        r"\bscratched\b",
        r"\bscuffs?\b",
        r"\bwear on body\b",
        r"\bscratched body\b",
    ],
    "dented_corners": [
        r"dented corners?",
        r"\bdented\b",
        r"\bdinged up\b",
        r"\bdinged corners?\b",
        r"\bdings?\b",
        r"\bchipped frame\b",
        r"\bdented frame\b",
        r"\bbeat up edges\b",
        r"\bscuffed corners?\b",
    ],
    "bent_frame": [r"\bbent frame\b", r"\bframe bent\b", r"\bbent body\b"],
    "charger_issue": [
        r"charger doesn t work",
        r"charger does not work",
        r"charging issue",
        r"charging port issue",
        r"charger port issue",
        r"\bwon t charge\b",
        r"\bwont charge\b",
    ],
    "no_power": [r"\bno power\b"],
    "lcd_burn": [r"\blcd burn\b", r"\blcd burns\b", r"\bscreen shadow\b", r"\bshadow on screen\b"],
    "engraved": [r"\bengraved\b", r"\bengraving\b"],
    "missing_sim_tray": [r"missing sim tray", r"sim tray missing", r"no sim tray"],
}

WHOLE_DEVICE_FAILURE_PATTERNS = [
    r"\bnot working\b",
    r"\bno power\b",
    r"\bdead\b",
    r"\bdoes not power on\b",
    r"\bdoesn t power on\b",
    r"\bdoesnt power on\b",
    r"\bwon t turn on\b",
    r"\bwont turn on\b",
    r"\bdoesn t turn on\b",
    r"\bdoesnt turn on\b",
    r"\bwon t boot\b",
    r"\bwont boot\b",
]

WHOLE_DEVICE_FAILURE_HINTS = [
    "phone",
    "iphone",
    "ipad",
    "macbook",
    "watch",
    "pixel",
    "galaxy",
    "device",
    "tablet",
    "laptop",
]

COMPONENT_SCOPE_TERMS = [
    "face id",
    "touch id",
    "screen",
    "display",
    "front glass",
    "back glass",
    "camera",
    "camera lens",
    "lens",
    "battery",
    "charging port",
    "charger port",
    "port",
    "speaker",
    "microphone",
    "mic",
    "keyboard",
    "trackpad",
]

PART_LISTING_PATTERNS = [
    r"\breplacement screen\b",
    r"\bscreen only\b",
    r"\bdisplay only\b",
    r"\bscreen assembly\b",
    r"\bdisplay assembly\b",
    r"\breplacement part\b",
    r"\bpart only\b",
    r"\bhousing\b",
    r"\bframe\b",
    r"\bback glass only\b",
    r"\brear glass only\b",
    r"\bcamera lens\b",
    r"\bcamera glass\b",
    r"\blogic board\b",
    r"\bmotherboard\b",
    r"\bcharging port\b",
    r"\bport flex\b",
    r"\bdigitizer\b",
    r"\blcd\b",
    r"\boled\b",
    r"\bfor (?:iphone|ipad|macbook|apple watch|watch|galaxy|pixel)\b",
    r"\bcompatible with\b",
    r"\bfits (?:iphone|ipad|macbook|apple watch|watch|galaxy|pixel)\b",
]

WHOLE_DEVICE_STATE_TERMS = [
    "unlocked",
    "locked",
    "icloud",
    "activation",
    "battery health",
    "bh ",
    "maximum capacity",
    "used",
    "sealed",
    "open box",
    "opened",
    "face id",
    "touch id",
    "no sim",
    "carrier",
    "sim",
]

DEFECT_LABELS = {
    "for_parts": "for parts",
    "not_working": "not working",
    "icloud_locked": "iCloud locked",
    "activation_locked": "activation locked",
    "water_damage": "water damage",
    "cracked_unspecified": "cracked",
    "needs_new_screen": "cracked screen",
    "cracked_screen": "cracked screen",
    "cracked_back": "cracked back",
    "cracked_lens": "cracked lens",
    "screen_issue": "screen issue",
    "dead_pixels": "dead pixels",
    "ghost_touch": "ghost touch",
    "rough_condition": "scratched",
    "bad_face_id": "bad Face ID",
    "bad_touch_id": "bad Touch ID",
    "bad_back_camera": "camera issue",
    "battery_under_80": "battery health under 80 percent",
    "google_locked": "google locked",
    "kg_active": "KG/trade-in lock",
    "repair_message": "repair message",
    "demo": "demo unit",
    "missing_parts": "missing parts/accessories",
    "no_charger": "missing charger",
    "no_band": "missing band",
    "missing_box": "missing box",
    "scratches": "scratched",
    "dented_corners": "dented",
    "bent_frame": "bent frame",
    "charger_issue": "charger issue",
    "no_power": "no power",
    "beat_up_edges": "dented",
    "good_condition": "good condition",
    "heavy_scratching": "scratched",
    "lcd_burn": "screen shadow/burn",
    "engraved": "engraved",
    "missing_sim_tray": "missing SIM tray",
}

CONDITION_NOTE_ALIASES = {
    "battery health under 80 percent": "battery health under 80 percent",
    "missing parts/accessories": "missing parts",
    "needs screen": "cracked screen",
    "scratched badly": "scratched",
    "scratches": "scratched",
    "rough condition": "scratched",
    "dented corners": "dented",
    "beat up edges": "dented",
    "screen shadow/burn": "screen shadow/burn",
}

CONDITION_NOTE_PRIORITY = [
    "for parts",
    "not working",
    "no power",
    "iCloud locked",
    "activation locked",
    "google locked",
    "water damage",
    "carrier locked",
    "bad Face ID",
    "bad Touch ID",
    "cracked screen",
    "cracked back",
    "cracked lens",
    "screen issue",
    "dead pixels",
    "ghost touch",
    "battery health",
    "battery health under 80 percent",
    "charger issue",
    "repair message",
    "screen shadow/burn",
    "scratched",
    "dented",
    "bent frame",
    "missing parts",
    "missing SIM tray",
    "missing charger",
    "missing band",
    "missing box",
    "engraved",
    "opened box",
    "sealed",
    "new",
    "used",
    "good condition",
    "condition not specified",
]

STRONG_SEALED_PATTERNS = [r"\bsealed\b", r"\bbnib\b", r"\bunopened\b", r"\bbrand new sealed\b"]
NEW_PATTERNS = [r"\bbrand new\b", r"\bunused\b", r"\bnew in box\b", r"\bin box\b"]
OPENED_PATTERNS = [r"\bopen box\b", r"\bopened\b", r"\bbox opened\b", r"\bactivated\b"]
USED_PATTERNS = [r"\bused\b", r"\bpre owned\b", r"\bpre owned\b", r"\bpre-owned\b"]
GOOD_CONDITION_PATTERNS = [r"\bgood condition\b", r"\bexcellent condition\b", r"\blike new\b"]


def timestamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    print(f"[{timestamp()}] {message}")


def debug_log(config: dict, message: str) -> None:
    if config.get("debug", {}).get("enabled", False):
        log(f"DEBUG: {message}")


def clean_text(value: object) -> str:
    return html.unescape(str(value or "")).replace("\xa0", " ").strip()


def normalize_text(value: object) -> str:
    text = clean_text(value).lower()
    text = text.replace("&", " and ")
    text = text.replace('"', " inch ")
    text = text.replace("'", " ")
    text = text.replace("+", " plus ")
    text = text.replace("/", " / ")
    text = text.replace("se 2020", "se 2nd gen")
    text = text.replace("se 2022", "se 3rd gen")
    text = text.replace("second generation", "2nd gen")
    text = text.replace("third generation", "3rd gen")
    text = text.replace("second gen", "2nd gen")
    text = text.replace("third gen", "3rd gen")
    text = re.sub(r"([a-z])(\d)", r"\1 \2", text)
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text)
    text = text.replace("gb", " gb")
    text = text.replace("tb", " tb")
    text = re.sub(r"[^a-z0-9+/ ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalized_tokens(value: object) -> List[str]:
    return [token for token in normalize_text(value).split() if token]


def strip_bundled_accessory_tail(value: object) -> str:
    text = clean_text(value)
    if not text:
        return ""

    accessory_pattern = "|".join(re.escape(term) for term in sorted(BUNDLED_ACCESSORY_TERMS, key=len, reverse=True))
    patterns = (
        rf"\s+\+\s*(?:{accessory_pattern})\b.*$",
        rf"\s+(?:with|w/)\s+.*\b(?:{accessory_pattern})\b.*$",
        rf"\s+comes with\s+.*\b(?:{accessory_pattern})\b.*$",
    )
    stripped = text
    for pattern in patterns:
        stripped = re.sub(pattern, "", stripped, flags=re.IGNORECASE).strip(" -,+/")
    return stripped or text


def parse_money(value: object) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return None
    text = clean_text(value)
    if not text:
        return None
    match = MONEY_PATTERN.search(text)
    if match:
        text = match.group(1)
    text = text.replace(",", "").replace("$", "")
    try:
        return float(text)
    except ValueError:
        normalized = normalize_text(value)
        if FREE_PRICE_PATTERN.fullmatch(normalized):
            return 0.0
    return None


def is_free_price_text(value: object) -> bool:
    return bool(FREE_PRICE_PATTERN.fullmatch(normalize_text(value)))


def parse_listing_price_blob(value: object) -> Optional[float]:
    text = clean_text(value)
    if not text:
        return None

    lines = unique_non_empty_lines(text)
    for line in lines:
        price = parse_money(line)
        if price is not None:
            return price

    if any(is_free_price_text(line) for line in lines) or is_free_price_text(text):
        return 0.0

    return parse_money(text)


def normalize_url(url: str) -> str:
    parts = urlsplit(clean_text(url))
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def collect_text_variants(*values: object) -> List[str]:
    variants: List[str] = []
    seen = set()
    for value in values:
        raw = clean_text(value).lower()
        normalized = normalize_text(value)
        for candidate in (raw, normalized):
            if candidate and candidate not in seen:
                seen.add(candidate)
                variants.append(candidate)
    return variants


def contains_normalized_term(text: str, term: str) -> bool:
    normalized_text = normalize_text(text)
    normalized_term = normalize_text(term)
    if not normalized_text or not normalized_term:
        return False
    spaced_term = r"\s+".join(re.escape(piece) for piece in normalized_term.split())
    return bool(re.search(rf"(?<![a-z0-9]){spaced_term}(?![a-z0-9])", normalized_text))


def contains_any_normalized_term(text: str, terms: Iterable[str]) -> bool:
    return any(contains_normalized_term(text, term) for term in terms)


def update_query(url: str, **updates: object) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in updates.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = str(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def listing_id_from_url(url: str) -> str:
    normalized = normalize_url(url)
    match = ID_PATTERN.search(urlsplit(normalized).path)
    return match.group(1) if match else normalized


def storage_sort_key(storage: str) -> int:
    normalized = normalize_text(storage)
    match = re.match(r"(\d+)\s*(gb|tb|mm)", normalized)
    if not match:
        return 999999
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "tb":
        return amount * 1000
    return amount


def normalize_storage_option(value: str) -> str:
    normalized = normalize_text(value)
    return normalized.replace(" gb", "gb").replace(" tb", "tb").replace(" mm", "mm")


def split_storage_options(value: str) -> List[str]:
    if not value:
        return []
    options = []
    for part in clean_text(value).split("|"):
        cleaned = normalize_storage_option(part)
        if cleaned:
            options.append(cleaned)
    return sorted(set(options), key=storage_sort_key)


def lowest_storage_value(options: List[str]) -> str:
    if not options:
        return ""
    return sorted(options, key=storage_sort_key)[0]


def fuzzy_score(needle: str, haystack: str) -> float:
    if not needle or not haystack:
        return 0.0
    if fuzz is not None:
        return float(max(fuzz.token_set_ratio(needle, haystack), fuzz.partial_ratio(needle, haystack)))
    return difflib.SequenceMatcher(a=needle, b=haystack).ratio() * 100.0


def safe_json_dict(raw: object) -> Dict[str, dict]:
    text = clean_text(raw)
    if not text:
        return {}
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def extract_m_chip_family(text: object) -> str:
    match = M_CHIP_PATTERN.search(normalize_text(text))
    if not match:
        return ""
    chip = f"m {match.group(1)}"
    if match.group(2):
        chip = f"{chip} {match.group(2)}"
    return chip


def extract_ram_candidates(searchable_text: str) -> List[str]:
    found = {f"{match.group(1)}gb" for match in RAM_PATTERN.finditer(normalize_text(searchable_text))}
    return sorted(found, key=storage_sort_key)


def is_useful_alias(alias: str) -> bool:
    tokens = normalized_tokens(alias)
    if not tokens:
        return False
    if len(tokens) == 1 and tokens[0].isdigit():
        return False
    return len("".join(tokens)) >= 4


def sell_price_from_sheet_usd(value_usd: float, pricing: dict) -> float:
    return round(value_usd * float(pricing.get("sell_price_multiplier", 1.4)), 2)


def max_buy_price_from_sheet_usd(value_usd: float, pricing: dict) -> float:
    return round(value_usd * float(pricing.get("max_buy_multiplier", 1.12)), 2)


def max_listing_price_to_alert(max_buy_price: float, pricing: dict) -> float:
    return round(max_buy_price * float(pricing.get("negotiation_above_max_buy_ratio", 1.10)), 2)


def pricing_thresholds_from_sheet_usd(value_usd: float, pricing: dict) -> dict:
    sell_price = sell_price_from_sheet_usd(value_usd, pricing)
    max_buy_price = max_buy_price_from_sheet_usd(value_usd, pricing)
    return {
        "sell_price": sell_price,
        "max_buy_price": max_buy_price,
        "alert_ceiling": max_listing_price_to_alert(max_buy_price, pricing),
    }


def format_price(value: Optional[float]) -> str:
    if value is None:
        return "Unknown"
    if abs(float(value)) < 0.005:
        return "Free"
    rounded = round(float(value))
    if abs(float(value) - rounded) < 0.005:
        return f"${rounded:,}"
    return f"${float(value):,.2f}"


def platform_label(platform: str) -> str:
    return "Facebook" if clean_text(platform).lower() == "facebook" else "Kijiji"


def http_get_text(url: str, timeout_seconds: int = 30) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError(f"Rate limited by {urlsplit(url).netloc}. Try again later.") from exc
        raise RuntimeError(f"HTTP error {exc.code} while fetching {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error while fetching {url}: {exc.reason}") from exc


def html_meta_content(html_text: str, name: str) -> str:
    for pattern in (
        rf'<meta[^>]+property="{re.escape(name)}"[^>]+content="([^"]*)"',
        rf"<meta[^>]+property='{re.escape(name)}'[^>]+content='([^']*)'",
        rf'<meta[^>]+name="{re.escape(name)}"[^>]+content="([^"]*)"',
        rf"<meta[^>]+name='{re.escape(name)}'[^>]+content='([^']*)'",
    ):
        match = re.search(pattern, html_text, re.IGNORECASE)
        if match:
            return clean_text(html.unescape(match.group(1)))
    return ""


def facebook_http_listing_detail(url: str, timeout_seconds: int) -> dict:
    html_text = http_get_text(url, timeout_seconds=timeout_seconds)
    title = (
        html_meta_content(html_text, "og:title")
        or html_meta_content(html_text, "twitter:title")
        or ""
    )
    description = (
        html_meta_content(html_text, "og:description")
        or html_meta_content(html_text, "description")
        or ""
    )
    image_urls = [
        clean_text(html.unescape(match.group(1)))
        for match in re.finditer(
            r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"',
            html_text,
            re.IGNORECASE,
        )
    ]
    image_urls = [item for item in image_urls if item.startswith("http")]
    return {
        "title": title,
        "description": description,
        "image_urls": list(dict.fromkeys(image_urls)),
        "source": "facebook_http_meta",
    }


def useful_marketplace_detail_lines(text: str) -> List[str]:
    keywords = (
        "iphone",
        "ipad",
        "apple watch",
        "watch",
        "galaxy",
        "pixel",
        "macbook",
        "unlocked",
        "locked",
        "storage",
        "battery",
        "bh",
        "sim tray",
        "screen",
        "display",
        "back",
        "glass",
        "crack",
        "scratch",
        "dented",
        "ding",
        "face id",
        "touch id",
        "camera",
        "charge",
        "port",
        "frame",
        "working",
        "repair",
        "parts",
        "icloud",
        "activation",
        "box",
        "case",
    )
    useful: List[str] = []
    for line in unique_non_empty_lines(text):
        normalized = normalize_text(line)
        if len(normalized) < 4:
            continue
        if any(keyword in normalized for keyword in keywords) or GENERIC_STORAGE_PATTERN.search(normalized):
            useful.append(line)
    return useful[:12]


FACEBOOK_DESCRIPTION_START_LINES = {
    "description",
    "seller description",
    "seller s description",
    "item description",
    "details",
}

FACEBOOK_DESCRIPTION_STOP_PREFIXES = (
    "seller information",
    "condition",
    "pickup",
    "shipping",
    "location",
    "listed",
    "message seller",
    "meet up",
    "category",
)

FACEBOOK_DESCRIPTION_NOISE_LINES = {
    "see more",
    "see less",
    "more details",
    "is this available",
}


def truncate_debug_text(text: object, limit: int = 260) -> str:
    cleaned = clean_text(text)
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit].rstrip()}..."


def extract_facebook_description_lines(body_text: str, title: str = "", meta_description: str = "") -> List[str]:
    lines = unique_non_empty_lines(body_text)
    normalized_title = normalize_text(title)
    normalized_meta = normalize_text(meta_description)
    results: List[str] = []
    seen = set()
    collecting = False

    def add_line(line: str) -> None:
        normalized = normalize_text(line)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        results.append(clean_text(line))

    for line in lines:
        normalized = normalize_text(line)
        if not normalized or normalized in {normalized_title, normalized_meta}:
            continue
        if normalized in FACEBOOK_DESCRIPTION_NOISE_LINES:
            continue
        if normalized in FACEBOOK_DESCRIPTION_START_LINES:
            collecting = True
            continue
        if collecting:
            if any(normalized.startswith(prefix) for prefix in FACEBOOK_DESCRIPTION_STOP_PREFIXES):
                break
            if len(normalized) >= 3:
                add_line(line)

    if results:
        return results[:12]

    for line in useful_marketplace_detail_lines(body_text):
        normalized = normalize_text(line)
        if normalized in {normalized_title, normalized_meta}:
            continue
        add_line(line)

    if not results and meta_description:
        add_line(meta_description)

    return results[:12]


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Missing {path.name}. Edit the file in this folder before running the script.")

    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    config.setdefault("poll_minutes", 45)
    config.setdefault("alert_existing_on_first_run", False)
    config.setdefault("min_price", 0)
    config.setdefault("max_price", 100000)
    config.setdefault("blocked_words", [])
    config.setdefault("searches", [])
    config.setdefault("telegram", {})
    config.setdefault("facebook", {})
    config.setdefault("kijiji", {})
    config.setdefault("pricing", {})
    config.setdefault("validation", {})
    config.setdefault("dedupe", {})
    config.setdefault("debug", {})

    if "sell_price_multiplier" not in config["pricing"]:
        legacy_sell = float(config["pricing"].get("usd_to_cad", 1.4))
        config["pricing"]["sell_price_multiplier"] = legacy_sell
    if "max_buy_multiplier" not in config["pricing"]:
        legacy_sell = float(config["pricing"].get("usd_to_cad", 1.4))
        legacy_buy_ratio = float(config["pricing"].get("maximum_buy_ratio", 0.8))
        config["pricing"]["max_buy_multiplier"] = round(legacy_sell * legacy_buy_ratio, 4)
    config["pricing"].setdefault("negotiation_above_max_buy_ratio", 1.10)

    config["validation"].setdefault("general_min_score", 78)
    config["validation"].setdefault("macbook_direct_min_score", 88)
    config["validation"].setdefault("free_listing_score_relief", 6)
    config["validation"].setdefault("reject_home_terms", True)
    config["validation"].setdefault("reject_accessories", True)
    config["validation"].setdefault("reject_parts_only", True)
    config["validation"].setdefault("reject_locked_devices", True)
    config["validation"].setdefault("allow_non_sheet_macbooks", True)
    config["validation"].setdefault("reject_pre_2020_macbooks", True)
    config["validation"].setdefault("reject_intel_macbooks", True)
    config["validation"].setdefault("manual_deduction_max_steps", 2)

    config["dedupe"].setdefault("enabled", True)
    config["dedupe"].setdefault("history_file", "seen_listings.json")

    config["kijiji"].setdefault("pages_per_search", 2)
    config["kijiji"].setdefault("fetch_listing_details", True)
    config["kijiji"].setdefault("detail_fetch_limit", 12)
    config["kijiji"].setdefault("free_listing_detail_fetch_limit", 12)
    config["kijiji"].setdefault("timeout_seconds", 25)

    config["facebook"].setdefault("profile_dir", "fb_profile")
    config["facebook"].setdefault("headless", True)
    config["facebook"].setdefault("timeout_seconds", 30)
    config["facebook"].setdefault("scroll_rounds", 6)
    config["facebook"].setdefault("fetch_listing_details", True)
    config["facebook"].setdefault("detail_fetch_limit", 6)
    config["facebook"].setdefault("free_listing_detail_fetch_limit", 6)

    config["debug"].setdefault("enabled", False)
    config["debug"].setdefault("log_rejections", True)
    config["debug"].setdefault("log_dedupe", True)

    enabled_searches = [item for item in config["searches"] if item.get("enabled", True)]
    if not enabled_searches:
        raise SystemExit("No enabled searches found in config.json.")

    return config


def load_price_catalog(path: Path, pricing: dict) -> dict:
    if not path.exists():
        raise SystemExit(f"Missing {path.name}. Rebuild it before running the script.")

    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            model = normalize_text(raw.get("model", ""))
            if not model:
                continue

            storage_options = split_storage_options(raw.get("storage_options", ""))
            base_price_usd = parse_money(raw.get("base_price_usd"))
            if base_price_usd is None:
                continue

            thresholds = pricing_thresholds_from_sheet_usd(base_price_usd, pricing)

            category = clean_text(raw.get("category", "")).lower()
            device_line = normalize_text(raw.get("device_line", ""))
            chip_family = normalize_text(raw.get("chip_family", ""))
            notes = clean_text(raw.get("notes", ""))

            if category == "macbook":
                if not device_line:
                    if "macbook air" in model:
                        device_line = "macbook air"
                    elif "macbook pro" in model:
                        device_line = "macbook pro"
                if not chip_family:
                    chip_family = extract_m_chip_family(f"{model} {notes}")

            aliases = [
                normalize_text(item)
                for item in clean_text(raw.get("aliases", "")).split("|")
                if is_useful_alias(item)
            ]
            if category == "apple_watch":
                size_less_model = re.sub(r"\b(40|41|42|44|45|46|49)\s*mm\b", "", model).strip()
                size_less_model = re.sub(r"\s+", " ", size_less_model).strip()
                if is_useful_alias(size_less_model):
                    aliases.append(size_less_model)
                brandless_watch = size_less_model.replace("apple watch ", "").strip()
                if is_useful_alias(brandless_watch):
                    aliases.append(brandless_watch)
                watch_prefixed = size_less_model.replace("apple watch ", "watch ").strip()
                if is_useful_alias(watch_prefixed):
                    aliases.append(watch_prefixed)
            if model not in aliases:
                aliases.append(model)

            row = {
                "category": category,
                "brand": clean_text(raw.get("brand", "")).lower(),
                "family_key": clean_text(raw.get("family_key", "")) or model,
                "model": model,
                "model_year": clean_text(raw.get("model_year", "")),
                "device_line": device_line,
                "chip_family": chip_family,
                "storage_options": storage_options,
                "lowest_storage": lowest_storage_value(storage_options),
                "carrier_status": clean_text(raw.get("carrier_status", "")).lower() or "unknown",
                "default_condition": clean_text(raw.get("default_condition", "")).lower(),
                "source_sheet": clean_text(raw.get("source_sheet", "")),
                "base_price_usd": base_price_usd,
                "sell_price": thresholds["sell_price"],
                "max_buy_price": thresholds["max_buy_price"],
                "max_listing_price_to_alert": thresholds["alert_ceiling"],
                "fair_price_from_sheet_cad": thresholds["sell_price"],
                "sell_price_cad": thresholds["sell_price"],
                "maximum_buy_price_cad": thresholds["max_buy_price"],
                "max_listing_price_to_alert_cad": thresholds["alert_ceiling"],
                "condition_prices_usd": safe_json_dict(raw.get("condition_prices_usd", "")),
                "deduction_rules_usd": safe_json_dict(raw.get("deduction_rules_usd", "")),
                "aliases": sorted(set(filter(None, aliases))),
                "notes": notes,
            }
            rows.append(row)

    if not rows:
        raise SystemExit("prices.csv is empty or invalid.")

    families_by_key: Dict[str, dict] = {}
    for row in rows:
        family = families_by_key.setdefault(
            row["family_key"],
            {
                "family_key": row["family_key"],
                "category": row["category"],
                "brand": row["brand"],
                "model": row["model"],
                "model_year": row["model_year"],
                "device_line": row["device_line"],
                "chip_family": row["chip_family"],
                "carrier_status": row["carrier_status"],
                "aliases": set(),
                "rows": [],
                "support_model_key": "",
                "samsung_model_key": "",
            },
        )
        family["rows"].append(row)
        family["aliases"].update(row["aliases"])
        if not family.get("model_year") and row.get("model_year"):
            family["model_year"] = row["model_year"]
        if not family.get("device_line") and row.get("device_line"):
            family["device_line"] = row["device_line"]
        if not family.get("chip_family") and row.get("chip_family"):
            family["chip_family"] = row["chip_family"]

    families = []
    for family in families_by_key.values():
        family["rows"] = sorted(
            family["rows"],
            key=lambda item: (
                storage_sort_key(item.get("lowest_storage", "")),
                item.get("fair_price_from_sheet_cad", 0),
            ),
        )
        family["storage_options"] = sorted(
            {
                storage
                for row in family["rows"]
                for storage in row.get("storage_options", [])
            },
            key=storage_sort_key,
        )
        family["aliases"] = sorted(alias for alias in family["aliases"] if is_useful_alias(alias))
        family["model_tokens"] = [
            token for token in normalized_tokens(family["model"]) if token not in GENERIC_MODEL_TOKENS
        ]
        family["numeric_model_tokens"] = {token for token in family["model_tokens"] if token.isdigit()}
        family["alpha_model_tokens"] = {token for token in family["model_tokens"] if token in IMPORTANT_MODEL_TOKENS}
        family["support_model_key"] = extract_supported_model_key(family["category"], family["model"])
        if family["category"] == "samsung":
            family["samsung_model_key"] = extract_samsung_model_key(family["model"])
        size_match = SIZE_PATTERN.search(normalize_text(family["model"]))
        family["size_value"] = size_match.group(1) if size_match else ""
        families.append(family)

    supported_samsung_model_keys = {
        family["samsung_model_key"]
        for family in families
        if family["category"] == "samsung" and family.get("samsung_model_key")
    }

    return {
        "rows": rows,
        "families": families,
        "supported_samsung_model_keys": supported_samsung_model_keys,
        "supported_model_keys_by_category": {
            category: {
                family["support_model_key"]
                for family in families
                if family["category"] == category and family.get("support_model_key")
            }
            for category in {"iphone", "samsung", "apple_watch"}
        },
    }


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"first_run_completed": False, "items": {}}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(path: Path, state: dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)


def base_seen_history() -> dict:
    return {"version": 2, "records": {}, "key_map": {}}


def seen_status_priority(status: str) -> int:
    return {"messaged": 2, "returned": 1}.get(clean_text(status).lower(), 0)


def dedupe_identity_for_listing(listing: dict) -> str:
    keys = dedupe_keys_for_listing(listing)
    return keys[0] if keys else ""


def normalize_seen_record(record: dict, fallback_key: str = "") -> Optional[dict]:
    if not isinstance(record, dict):
        return None

    canonical_url = normalize_url(record.get("canonical_url", "") or record.get("url", ""))
    platform = clean_text(record.get("platform", ""))
    if not platform:
        if "facebook.com" in canonical_url:
            platform = "facebook"
        elif "kijiji.ca" in canonical_url:
            platform = "kijiji"

    listing_id = clean_text(record.get("listing_id", "") or record.get("id", ""))
    if not listing_id and canonical_url:
        derived_id = listing_id_from_url(canonical_url)
        if derived_id != canonical_url:
            listing_id = derived_id

    listing_stub = {
        "platform": platform,
        "url": canonical_url or clean_text(record.get("url", "")),
        "id": listing_id,
        "title": clean_text(record.get("title", "")),
        "description": "",
    }
    identity_key = dedupe_identity_for_listing(listing_stub) or clean_text(fallback_key)
    if not identity_key:
        return None

    status = clean_text(record.get("status", "")).lower()
    if status not in {"returned", "messaged"}:
        status = "messaged" if record.get("messaged_at") else "returned"

    normalized = {
        "identity_key": identity_key,
        "url": clean_text(record.get("url", "")) or canonical_url,
        "canonical_url": canonical_url,
        "listing_id": listing_id,
        "title": clean_text(record.get("title", "")),
        "platform": platform,
        "price": parse_money(record.get("price", 0)) or 0.0,
        "matched_label": clean_text(record.get("matched_label", "")),
        "status": status,
        "updated_at": clean_text(record.get("updated_at", "")) or dt.datetime.now().isoformat(timespec="seconds"),
    }
    if clean_text(record.get("alerted_at", "")):
        normalized["alerted_at"] = clean_text(record["alerted_at"])
    if clean_text(record.get("messaged_at", "")):
        normalized["messaged_at"] = clean_text(record["messaged_at"])
    return normalized


def merge_seen_records(existing: Optional[dict], incoming: dict) -> dict:
    if not existing:
        return dict(incoming)

    merged = dict(existing)
    for field in ("url", "canonical_url", "listing_id", "title", "platform", "matched_label"):
        if clean_text(incoming.get(field, "")):
            merged[field] = incoming[field]
    if incoming.get("price") not in (None, ""):
        merged["price"] = float(incoming["price"])

    existing_status = clean_text(existing.get("status", "")).lower()
    incoming_status = clean_text(incoming.get("status", "")).lower()
    if seen_status_priority(incoming_status) >= seen_status_priority(existing_status):
        merged["status"] = incoming_status
    else:
        merged["status"] = existing_status

    for timestamp_field in ("alerted_at", "messaged_at", "updated_at"):
        if clean_text(incoming.get(timestamp_field, "")):
            current_value = clean_text(merged.get(timestamp_field, ""))
            merged[timestamp_field] = max(current_value, clean_text(incoming[timestamp_field])) if current_value else clean_text(incoming[timestamp_field])

    return merged


def normalize_seen_history_state(raw: object) -> dict:
    if not isinstance(raw, dict):
        return base_seen_history()

    normalized = base_seen_history()
    sources = []
    for identity_key, record in (raw.get("records") or {}).items():
        sources.append((identity_key, record))
    for key, record in (raw.get("items") or {}).items():
        sources.append((key, record))

    for fallback_key, record in sources:
        normalized_record = normalize_seen_record(record, fallback_key=fallback_key)
        if not normalized_record:
            continue
        identity_key = normalized_record["identity_key"]
        normalized["records"][identity_key] = merge_seen_records(
            normalized["records"].get(identity_key),
            normalized_record,
        )

    for identity_key, record in normalized["records"].items():
        listing_stub = {
            "platform": record.get("platform", ""),
            "url": record.get("canonical_url", "") or record.get("url", ""),
            "id": record.get("listing_id", ""),
            "title": record.get("title", ""),
            "description": "",
        }
        for key in dedupe_keys_for_listing(listing_stub):
            normalized["key_map"][key] = identity_key

    return normalized


def ensure_seen_history_state(seen_history: dict) -> dict:
    normalized = normalize_seen_history_state(seen_history)
    if seen_history != normalized:
        seen_history.clear()
        seen_history.update(normalized)
    return seen_history


def load_seen_history(path: Path) -> dict:
    if not path.exists():
        return base_seen_history()
    with path.open("r", encoding="utf-8") as handle:
        return normalize_seen_history_state(json.load(handle))


def save_seen_history(path: Path, state: dict) -> None:
    normalized = normalize_seen_history_state(state)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(normalized, handle, indent=2, sort_keys=True)


def clear_seen_history(path: Path) -> None:
    save_seen_history(path, base_seen_history())


def unique_non_empty_lines(text: str) -> List[str]:
    seen = set()
    lines = []
    for raw in clean_text(text).splitlines():
        line = clean_text(raw)
        if not line:
            continue
        lowered = line.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        lines.append(line)
    return lines


def extract_next_data(html_text: str) -> dict:
    match = NEXT_DATA_PATTERN.search(html_text)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def extract_image_urls_from_payload(payload: object) -> List[str]:
    urls: List[str] = []

    def walk(node: object) -> None:
        if isinstance(node, str):
            if node.startswith("http") and any(token in node for token in ("image", "photo", "media")):
                urls.append(node)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in {"image", "images", "imageurl", "imageurls"}:
                    walk(value)
    walk(payload)
    deduped = []
    seen = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped[:12]


def choose_detail_targets(listings: List[dict], base_limit: int, free_limit: int, fetch_details: bool) -> List[dict]:
    targets: List[dict] = []
    seen_urls = set()

    if fetch_details:
        for listing in listings[: max(0, base_limit)]:
            url = listing.get("url")
            if url and url not in seen_urls:
                seen_urls.add(url)
                targets.append(listing)

    free_targets = [listing for listing in listings if float(listing.get("price", 0) or 0) == 0]
    for listing in free_targets[: max(0, free_limit)]:
        url = listing.get("url")
        if url and url not in seen_urls:
            seen_urls.add(url)
            targets.append(listing)

    return targets


def parse_kijiji_search_html(html_text: str, search_url: str) -> List[dict]:
    blocks = JSON_LD_PATTERN.findall(html_text)
    results = []
    seen_urls = set()

    for block in blocks:
        raw = clean_text(block)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        payloads = data if isinstance(data, list) else [data]
        for payload in payloads:
            if not isinstance(payload, dict) or payload.get("@type") != "ItemList":
                continue
            for element in payload.get("itemListElement", []):
                item = (element or {}).get("item", {})
                title = clean_text(item.get("name", ""))
                description = clean_text(item.get("description", ""))
                url = normalize_url(item.get("url", ""))
                price = parse_money((item.get("offers") or {}).get("price"))
                if not title or not url or price is None or url in seen_urls:
                    continue
                seen_urls.add(url)
                results.append(
                    {
                        "platform": "kijiji",
                        "source_url": search_url,
                        "url": url,
                        "id": listing_id_from_url(url),
                        "title": title,
                        "description": description,
                        "price": price,
                        "image_urls": [],
                    }
                )

    if results:
        return results

    next_data = extract_next_data(html_text)
    apollo_state = next_data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})
    for key, value in apollo_state.items():
        if not key.startswith("StandardListing:") or not isinstance(value, dict):
            continue
        title = clean_text(value.get("title", ""))
        description = clean_text(value.get("description", ""))
        url = normalize_url(value.get("url", ""))
        raw_amount = (value.get("price") or {}).get("amount")
        price = float(raw_amount) / 100.0 if isinstance(raw_amount, (int, float)) else parse_money(raw_amount)
        if not title or not url or price is None or url in seen_urls:
            continue
        seen_urls.add(url)
        results.append(
            {
                "platform": "kijiji",
                "source_url": search_url,
                "url": url,
                "id": listing_id_from_url(url),
                "title": title,
                "description": description,
                "price": price,
                "image_urls": [],
            }
        )

    return results


def fetch_kijiji_listing_detail(url: str, timeout_seconds: int) -> dict:
    html_text = http_get_text(url, timeout_seconds=timeout_seconds)
    description = ""
    image_urls: List[str] = []

    next_data = extract_next_data(html_text)
    apollo_state = next_data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})
    for key, value in apollo_state.items():
        if not key.startswith("StandardListing:") or not isinstance(value, dict):
            continue
        candidate_url = normalize_url(value.get("url", ""))
        if candidate_url and candidate_url != normalize_url(url):
            continue
        description = clean_text(value.get("description", "")) or description
        image_urls = extract_image_urls_from_payload(value) or image_urls
        break

    if not description or not image_urls:
        for block in JSON_LD_PATTERN.findall(html_text):
            raw = clean_text(block)
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not description and isinstance(payload, dict):
                description = clean_text(payload.get("description", "")) or description
            if not image_urls:
                image_urls = extract_image_urls_from_payload(payload)

    return {"description": description, "image_urls": image_urls}


def extract_kijiji_listings(
    search_url: str,
    timeout_seconds: int,
    max_pages: int,
    fetch_details: bool,
    detail_fetch_limit: int,
    free_detail_fetch_limit: int,
) -> List[dict]:
    listings = []
    seen_urls = set()

    for page_number in range(1, max_pages + 1):
        page_url = search_url if page_number == 1 else update_query(search_url, page=page_number)
        html_text = http_get_text(page_url, timeout_seconds=timeout_seconds)
        for listing in parse_kijiji_search_html(html_text, search_url):
            if listing["url"] in seen_urls:
                continue
            seen_urls.add(listing["url"])
            listings.append(listing)

    detail_targets = choose_detail_targets(
        listings,
        base_limit=detail_fetch_limit,
        free_limit=free_detail_fetch_limit,
        fetch_details=fetch_details,
    )
    for listing in detail_targets:
            try:
                detail = fetch_kijiji_listing_detail(listing["url"], timeout_seconds=timeout_seconds)
            except Exception:
                continue
            if detail.get("description"):
                listing["description"] = detail["description"]
            if detail.get("image_urls"):
                listing["image_urls"] = detail["image_urls"]

    return listings


def import_playwright():
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright is not installed.\n"
            "Run these commands first:\n"
            "python3 -m pip install -r requirements.txt\n"
            "python3 -m playwright install chromium"
        ) from exc
    return sync_playwright, PlaywrightTimeoutError


class FacebookSession:
    def __init__(self, profile_dir: Path, headless: bool, timeout_seconds: int, debug_enabled: bool = False) -> None:
        sync_playwright, playwright_timeout = import_playwright()
        self._playwright_timeout = playwright_timeout
        self._debug_enabled = debug_enabled
        self._manager = sync_playwright().start()
        self._context = self._manager.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            viewport={"width": 1400, "height": 1600},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context.set_default_timeout(timeout_seconds * 1000)

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            self._manager.stop()

    def setup_login(self) -> None:
        page = self._context.new_page()
        page.goto("https://www.facebook.com/marketplace/", wait_until="domcontentloaded")
        log("A browser window is open.")
        log("Log into Facebook, open Marketplace, then come back here and press Enter.")
        input()
        page.close()

    def _debug(self, message: str) -> None:
        if self._debug_enabled:
            log(f"[DEBUG] {message}")

    def expand_listing_description(self, page) -> None:
        for _ in range(3):
            clicked = page.evaluate(
                """
                () => {
                  const matches = Array.from(document.querySelectorAll('div[role="button"], button, span, a'))
                    .filter((element) => {
                      const text = (element.innerText || element.textContent || '').trim().toLowerCase();
                      const visible = !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
                      return visible && /^(see more|show more|more details|read more)$/i.test(text);
                    })
                    .slice(0, 10);
                  let count = 0;
                  for (const element of matches) {
                    try {
                      element.click();
                      count += 1;
                    } catch (error) {}
                  }
                  return count;
                }
                """
            )
            if not clicked:
                break
            page.wait_for_timeout(900)

    def extract_listing_detail(self, url: str) -> dict:
        http_detail = {}
        try:
            http_detail = facebook_http_listing_detail(url, timeout_seconds=15)
        except Exception:
            http_detail = {}

        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            page.mouse.wheel(0, 1400)
            page.wait_for_timeout(900)
            self.expand_listing_description(page)
            page.wait_for_timeout(1200)
            data = page.evaluate(
                """
                () => ({
                  metaTitle:
                    document.querySelector('meta[property="og:title"]')?.content ||
                    document.querySelector('meta[name="twitter:title"]')?.content ||
                    '',
                  metaDescription:
                    document.querySelector('meta[property="og:description"]')?.content ||
                    document.querySelector('meta[name="description"]')?.content ||
                    '',
                  text: (document.body && document.body.innerText) || '',
                  images: Array.from(document.images || [])
                    .map(img => img.src || '')
                    .filter(src => src.startsWith('http'))
                    .slice(0, 12)
                })
                """
            )
        finally:
            page.close()

        body_text = clean_text(data.get("text", ""))
        meta_title = clean_text(data.get("metaTitle", "")) or clean_text(http_detail.get("title", ""))
        meta_description = clean_text(data.get("metaDescription", "")) or clean_text(http_detail.get("description", ""))
        title = meta_title
        description_parts = extract_facebook_description_lines(body_text, title=title, meta_description=meta_description)
        if meta_description and not description_parts:
            description_parts = [meta_description]
        elif meta_description and normalize_text(meta_description) not in {
            normalize_text(part) for part in description_parts if clean_text(part)
        }:
            description_parts.insert(0, meta_description)

        image_urls = [
            clean_text(item)
            for item in (http_detail.get("image_urls", []) or []) + data.get("images", [])
            if clean_text(item)
        ]
        detail = {
            "title": title,
            "description": "\n".join(part for part in description_parts if clean_text(part)),
            "image_urls": list(dict.fromkeys(image_urls)),
            "source": clean_text(http_detail.get("source", "")) or "playwright_body",
        }
        self._debug(
            "Facebook detail "
            f"{listing_id_from_url(url)} | title={truncate_debug_text(detail['title'])!r} | "
            f"description={truncate_debug_text(detail['description'], 500)!r}"
        )
        return detail

    def extract_search_results(
        self,
        search_url: str,
        scroll_rounds: int,
        fetch_details: bool,
        detail_fetch_limit: int,
        free_detail_fetch_limit: int,
    ) -> List[dict]:
        page = self._context.new_page()

        try:
            page.goto(search_url, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)

            for _ in range(max(1, scroll_rounds)):
                page.mouse.wheel(0, 2200)
                page.wait_for_timeout(1200)

            current_url = page.url.lower()
            if "login" in current_url or "checkpoint" in current_url:
                raise RuntimeError(
                    "Facebook session is not logged in. Run: python3 deal_finder.py --facebook-login"
                )

            raw_items = page.evaluate(
                """
                () => {
                  const anchors = Array.from(document.querySelectorAll('a[href*="/marketplace/item/"]'));
                  const seen = new Set();
                  const results = [];

                  for (const anchor of anchors) {
                    let href = anchor.href || anchor.getAttribute('href') || '';
                    if (!href) continue;
                    if (!href.startsWith('http')) {
                      href = new URL(href, location.origin).href;
                    }
                    href = href.split('?')[0];
                    if (seen.has(href)) continue;
                    seen.add(href);

                    let container = anchor;
                    for (let i = 0; i < 6 && container.parentElement; i += 1) {
                      const next = container.parentElement;
                      const text = (next.innerText || '').trim();
                      container = next;
                      if (text.length >= 20 && text.split('\\n').length >= 3) {
                        break;
                      }
                    }

                    results.push({
                      href,
                      text: (container.innerText || anchor.innerText || '').trim(),
                      ariaLabel: anchor.getAttribute('aria-label') || ''
                    });
                  }

                  return results;
                }
                """
            )
        except self._playwright_timeout as exc:
            raise RuntimeError(
                "Facebook search timed out. Try again in headed mode by setting "
                '"headless": false in config.json.'
            ) from exc
        finally:
            page.close()

        listings = []
        seen_urls = set()
        for item in raw_items:
            url = normalize_url(item.get("href", ""))
            if not url or url in seen_urls:
                continue

            blob = clean_text(item.get("text", ""))
            lines = unique_non_empty_lines(blob)
            price = parse_listing_price_blob(blob)
            title = guess_facebook_title(lines)
            description = " | ".join(lines[:8])
            if not title or price is None:
                continue

            seen_urls.add(url)
            listings.append(
                {
                    "platform": "facebook",
                    "source_url": search_url,
                    "url": url,
                    "id": listing_id_from_url(url),
                    "title": title,
                    "description": description,
                    "price": price,
                    "image_urls": [],
                }
            )

        detail_targets = choose_detail_targets(
            listings,
            base_limit=detail_fetch_limit,
            free_limit=free_detail_fetch_limit,
            fetch_details=fetch_details,
        )
        for listing in detail_targets:
            try:
                detail = self.extract_listing_detail(listing["url"])
            except Exception:
                try:
                    detail = facebook_http_listing_detail(listing["url"], timeout_seconds=15)
                except Exception:
                    continue
            if detail.get("title"):
                listing["title"] = detail["title"]
            if detail.get("description"):
                listing["description"] = detail["description"]
            if detail.get("image_urls"):
                listing["image_urls"] = detail["image_urls"]
            self._debug(
                "Facebook listing stored "
                f"{listing['id']} | title={truncate_debug_text(listing['title'])!r} | "
                f"description={truncate_debug_text(listing.get('description', ''), 500)!r}"
            )

        return listings


def guess_facebook_title(lines: List[str]) -> str:
    if not lines:
        return ""
    price_index = None
    for index, line in enumerate(lines):
        if parse_listing_price_blob(line) is not None:
            price_index = index
            break
    candidate_lines = lines[price_index + 1 : price_index + 5] if price_index is not None else lines[:5]
    for line in candidate_lines:
        if parse_listing_price_blob(line) is not None:
            continue
        if NOISE_LINE_PATTERN.search(line):
            continue
        if not re.search(r"[A-Za-z]", line):
            continue
        return line
    for line in lines:
        if parse_listing_price_blob(line) is not None:
            continue
        if re.search(r"[A-Za-z]", line):
            return line
    return ""


def infer_allowed_categories(search: dict) -> Optional[set[str]]:
    search_text = normalize_text(f"{search.get('name', '')} {search.get('url', '')}")
    allowed = {
        category
        for category, hints in SEARCH_CATEGORY_HINTS.items()
        if any(hint in search_text for hint in hints)
    }
    return allowed or None


def joined_text(text_or_variants: object) -> str:
    if isinstance(text_or_variants, list):
        return " ".join(clean_text(item) for item in text_or_variants if clean_text(item))
    return clean_text(text_or_variants)


def split_text_segments(*values: object) -> List[str]:
    segments: List[str] = []
    seen = set()
    for value in values:
        raw_text = clean_text(value)
        if not raw_text:
            continue
        for segment in re.split(r"[\n\r|;,]+", raw_text):
            normalized = normalize_text(segment)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            segments.append(normalized)
    return segments


def extract_battery_health(text_variants: List[str]) -> Optional[int]:
    patterns = [
        r"\bbh\s*[:=]?\s*(\d{2,3})\s*%?\b",
        r"\bbattery health(?:\s+is)?(?:\s+at)?\s*[:=]?\s*(\d{2,3})\s*%?\b",
        r"\bmaximum capacity(?:\s+is)?(?:\s+at)?\s*[:=]?\s*(\d{2,3})\s*%?\b",
        r"\b(\d{2,3})\s*%\s*battery health\b",
        r"\b(\d{2,3})\s*%\s*maximum capacity\b",
    ]
    candidates: List[int] = []
    for text in text_variants:
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                value = int(match.group(1))
                if 40 <= value <= 100:
                    candidates.append(value)
    return min(candidates) if candidates else None


def detect_device_level_failures(segments: List[str]) -> List[str]:
    defects: List[str] = []
    for segment in segments:
        if any(re.search(pattern, segment) for pattern in WHOLE_DEVICE_FAILURE_PATTERNS):
            if any(term in segment for term in COMPONENT_SCOPE_TERMS) and not any(
                hint in segment for hint in WHOLE_DEVICE_FAILURE_HINTS
            ):
                continue
            defects.append("not_working")
            break
        if re.search(r"\b(?:doesn t|doesnt|does not) work\b", segment):
            if any(term in segment for term in COMPONENT_SCOPE_TERMS):
                continue
            defects.append("not_working")
            break
    return defects


def detect_part_listing_hits(
    title_text: str,
    full_text: str,
    storage_candidates: List[str],
    battery_health: Optional[int],
    defects: List[str],
    carrier_status: str,
) -> List[str]:
    hits = []
    whole_device_state = bool(
        storage_candidates
        or battery_health is not None
        or defects
        or carrier_status != "unknown"
        or contains_any_normalized_term(full_text, WHOLE_DEVICE_STATE_TERMS)
        or contains_any_normalized_term(full_text, ("phone", "tablet", "laptop", "fully functional", "works great"))
    )

    explicit_part_terms = [
        "replacement screen",
        "screen only",
        "display only",
        "screen assembly",
        "display assembly",
        "replacement part",
        "part only",
        "battery only",
        "housing",
        "frame",
        "back glass",
        "back glass only",
        "rear glass",
        "rear glass only",
        "replacement back",
        "camera lens",
        "camera glass",
        "logic board",
        "motherboard",
        "charging port",
        "port flex",
        "digitizer",
        "lcd",
        "oled",
    ]

    for term in explicit_part_terms:
        if contains_normalized_term(title_text, term):
            hits.append(term)

    if re.search(
        r"\b(?:for|fits|compatible with)\s+(?:iphone|ipad|macbook|apple watch|watch|galaxy|pixel)\b",
        title_text,
    ):
        hits.append("compatibility_listing")

    if any(contains_normalized_term(title_text, term) for term in TITLE_ACCESSORY_TERMS) and not whole_device_state:
        hits.append("accessory_title")

    if hits and ("replacement" in title_text or "only" in title_text or "compatibility_listing" in hits):
        return sorted(set(hits))

    if hits and not whole_device_state:
        return sorted(set(hits))

    if re.search(r"\b(?:for|fits|compatible with)\s+(?:iphone|ipad|macbook|apple watch|watch|galaxy|pixel)\b", full_text):
        if any(contains_normalized_term(full_text, term) for term in explicit_part_terms) and not whole_device_state:
            hits.append("compatibility_listing")

    return sorted(set(hits))


def detect_listing_carrier_status(searchable_text: object) -> str:
    combined = normalize_text(joined_text(searchable_text))
    if any(term in combined for term in ("icloud locked", "activation locked", "google locked", "frp locked", "kg active")):
        return "unknown"
    if "unlocked" in combined:
        return "unlocked"
    if "no sim" in combined or "locked to" in combined or "sim not supported" in combined:
        return "carrier_locked"
    if any(re.search(rf"\b{re.escape(normalize_text(keyword))}\b", combined) for keyword in CARRIER_KEYWORDS):
        return "carrier_locked"
    return "unknown"


def detect_pattern_hits(searchable_text: object, patterns: Dict[str, List[str]]) -> List[str]:
    variants = (
        [item for item in searchable_text if clean_text(item)]
        if isinstance(searchable_text, list)
        else [clean_text(searchable_text)]
    )
    found = []
    for key, group in patterns.items():
        if any(re.search(pattern, text) for pattern in group for text in variants):
            found.append(key)
    return found


def extract_storage_candidates(searchable_text: object) -> List[str]:
    combined = normalize_text(joined_text(searchable_text))
    found = set()
    for match in GENERIC_STORAGE_PATTERN.finditer(combined):
        amount = match.group(1)
        unit = match.group(2)
        storage = normalize_storage_option(f"{amount}{unit}")
        found.add(storage)

    for match in BARE_STORAGE_PATTERN.finditer(combined):
        amount = match.group(1)
        window_start = max(0, match.start() - 14)
        window_end = min(len(combined), match.end() + 14)
        window = combined[window_start:window_end]
        if re.search(r"(gb|tb|storage|ssd|rom|memory|ram)", window):
            found.add(normalize_storage_option(f"{amount}gb"))

    return sorted(found, key=storage_sort_key)


def choose_storage_from_catalog(context: dict, family: dict) -> tuple[str, str]:
    explicit = context["storage_candidates"]
    if explicit and family.get("storage_options"):
        for candidate in explicit:
            if candidate in family["storage_options"]:
                return candidate, "detected"
        lowest = lowest_storage_value(family.get("storage_options", []))
        if lowest:
            return lowest, "family_floor"
    lowest = lowest_storage_value(family.get("storage_options", []))
    if lowest:
        return lowest, "unspecified"
    return "", "unspecified"


def extract_year(searchable_text: str) -> str:
    years = sorted(set(YEAR_PATTERN.findall(searchable_text)))
    if len(years) == 1:
        return years[0]
    return ""


def extract_watch_size_value(text: str) -> str:
    normalized = normalize_text(text)
    match = re.search(r"\b(40|41|42|44|45|46|49)\s*mm\b", normalized)
    return match.group(1) if match else ""


def extract_iphone_model_key(text: str) -> str:
    normalized = normalize_text(text)
    if not normalized:
        return ""

    if "iphone se" in normalized:
        if re.search(r"\b2\s*nd\s*gen\b", normalized):
            return "iphone_se_2_nd_gen"
        if re.search(r"\b3\s*rd\s*gen\b", normalized):
            return "iphone_se_3_rd_gen"
        return "iphone_se"

    match = re.search(r"\biphone\s*(\d{1,2})(?:\s*(mini|plus|pro(?:\s+max)?|e))?\b", normalized)
    if not match:
        return ""

    key = f"iphone_{int(match.group(1))}"
    variant = clean_text(match.group(2)).replace(" ", "_")
    if variant:
        key = f"{key}_{variant}"
    return key


def extract_samsung_model_key(text: str) -> str:
    normalized = normalize_text(text)
    if not normalized:
        return ""

    compact = re.sub(r"\b(?:samsung|galaxy)\b", " ", normalized)
    compact = re.sub(r"\s+", " ", compact).strip()

    note_match = re.search(r"\bnote\s*(\d{1,2})(?:\s*(ultra|plus))?\b", compact)
    if note_match:
        key = f"galaxy_note_{int(note_match.group(1))}"
        if note_match.group(2):
            key = f"{key}_{note_match.group(2)}"
        return key

    z_flip_match = re.search(r"\b(?:z\s*)?flip\s*(\d)\b", compact)
    if z_flip_match:
        return f"galaxy_z_flip_{z_flip_match.group(1)}"

    z_fold_match = re.search(r"\b(?:z\s*)?fold\s*(\d)\b", compact)
    if z_fold_match:
        return f"galaxy_z_fold_{z_fold_match.group(1)}"

    s_match = re.search(r"\bs\s*(\d{1,2})(?:\s*(ultra|plus|edge|fe))?\b", compact)
    if s_match:
        key = f"galaxy_s_{int(s_match.group(1))}"
        if s_match.group(2):
            key = f"{key}_{s_match.group(2)}"
        return key

    a_match = re.search(r"\ba\s*(\d{1,2})\b", compact)
    if a_match:
        return f"galaxy_a_{int(a_match.group(1))}"

    return ""


def extract_apple_watch_model_key(text: str) -> str:
    normalized = normalize_text(text)
    if not normalized:
        return ""

    se_match = re.search(r"\bwatch\s*se\s*(2\s*nd\s*gen|3\s*rd\s*gen|\d(?:\s*(?:nd|rd)\s*gen)?)\b", normalized)
    if se_match:
        generation = normalize_text(se_match.group(1)).replace(" ", "_")
        if generation == "2":
            generation = "2_nd_gen"
        elif generation == "3":
            generation = "3_rd_gen"
        return f"apple_watch_se_{generation}"

    ultra_match = re.search(r"\bseries\s+ultra(?:\s+(\d+))?\b|\bwatch\s+ultra(?:\s+(\d+))?\b", normalized)
    if ultra_match:
        generation = ultra_match.group(1) or ultra_match.group(2) or ""
        return f"apple_watch_ultra_{generation}" if generation else "apple_watch_ultra"

    series_match = re.search(r"\bseries\s*(\d{1,2})\b", normalized)
    if series_match:
        return f"apple_watch_series_{int(series_match.group(1))}"

    watch_number_match = re.search(r"\bapple watch\s*(\d{1,2})\b", normalized)
    if watch_number_match:
        return f"apple_watch_series_{int(watch_number_match.group(1))}"

    return ""


def extract_supported_model_key(category: str, text: str) -> str:
    if category == "iphone":
        return extract_iphone_model_key(text)
    if category == "samsung":
        return extract_samsung_model_key(text)
    if category == "apple_watch":
        return extract_apple_watch_model_key(text)
    return ""


def extract_primary_device_number(category: str, text: str) -> str:
    normalized = normalize_text(text)
    if category == "iphone":
        if "iphone se" in normalized:
            return "se"
        match = re.search(r"\biphone\s*(\d{1,2})\b", normalized)
        return match.group(1) if match else ""
    if category == "samsung":
        match = re.search(r"\b(?:galaxy\s*)?(s|a|note|z)\s*(\d{1,2})\b", normalized)
        if match:
            return f"{match.group(1)}{match.group(2)}"
    if category == "pixel":
        match = re.search(r"\bpixel\s*(\d{1,2})(a|pro|xl|fold)?\b", normalized)
        if match:
            return f"{match.group(1)}{match.group(2) or ''}"
    if category == "apple_watch":
        ultra_match = re.search(r"\bultra\s*(\d+)\b", normalized)
        if ultra_match:
            return f"ultra{ultra_match.group(1)}"
        se_match = re.search(r"\bwatch\s*se\s*(\d)(?:\s*(?:nd|rd)\s*gen)?\b", normalized)
        if se_match:
            return f"se{se_match.group(1)}"
        series_match = re.search(r"\bseries\s*(\d{1,2})\b", normalized)
        if series_match:
            return f"series{series_match.group(1)}"
        watch_match = re.search(r"\bapple watch\s*(\d{1,2})\b", normalized)
        if watch_match:
            return f"series{watch_match.group(1)}"
    return ""


def extract_lock_status(text_variants: List[str], carrier_status: str, defect_keys: List[str]) -> str:
    combined = " ".join(text_variants)
    if "icloud_locked" in defect_keys or "icloud locked" in combined or "icloud lock" in combined:
        return "icloud_locked"
    if "activation_locked" in defect_keys or "activation locked" in combined or "activation lock" in combined:
        return "activation_locked"
    if "google_locked" in defect_keys or "google locked" in combined or "frp locked" in combined:
        return "google_locked"
    if carrier_status == "carrier_locked":
        return "carrier_locked"
    return "unknown"


def infer_condition_from_signals(text_variants: List[str], defect_keys: List[str]) -> str:
    joined = " ".join(text_variants)
    severe_parts = {
        "for_parts",
        "not_working",
        "icloud_locked",
        "activation_locked",
        "google_locked",
        "water_damage",
        "kg_active",
    }
    severe_damage = {
        "needs_new_screen",
        "cracked_screen",
        "cracked_unspecified",
        "screen_issue",
        "dead_pixels",
        "ghost_touch",
        "rough_condition",
        "lcd_burn",
    }
    if severe_parts & set(defect_keys):
        return "parts"
    if severe_damage & set(defect_keys):
        return "damaged"
    if any(re.search(pattern, joined) for pattern in STRONG_SEALED_PATTERNS):
        return "sealed"
    if any(re.search(pattern, joined) for pattern in NEW_PATTERNS):
        return "new"
    if any(re.search(pattern, joined) for pattern in OPENED_PATTERNS):
        return "opened"
    if any(re.search(pattern, joined) for pattern in GOOD_CONDITION_PATTERNS):
        return "used"
    if any(re.search(pattern, joined) for pattern in USED_PATTERNS) or defect_keys:
        return "used"
    return "unspecified"


def build_macbook_context(searchable_text: str) -> dict:
    line = ""
    if "macbook air" in searchable_text or "mba" in searchable_text:
        line = "macbook air"
    elif "macbook pro" in searchable_text or "mbp" in searchable_text:
        line = "macbook pro"
    elif "macbook" in searchable_text:
        line = "macbook"

    chip_family = extract_m_chip_family(searchable_text)
    chip_kind = "m_series" if chip_family else ""
    if not chip_family and INTEL_PATTERN.search(searchable_text):
        chip_family = "intel"
        chip_kind = "intel"

    size_match = SIZE_PATTERN.search(searchable_text)
    size_value = size_match.group(1) if size_match else ""

    return {
        "present": bool(line or "macbook" in searchable_text),
        "line": line,
        "chip_family": chip_family,
        "chip_kind": chip_kind,
        "size_value": size_value,
        "year": extract_year(searchable_text),
        "ram_candidates": extract_ram_candidates(searchable_text),
        "confidence": sum(
            1
            for value in (
                bool(line),
                bool(chip_family),
                bool(size_value),
                bool(extract_year(searchable_text)),
                bool(extract_ram_candidates(searchable_text)),
            )
            if value
        ),
    }


def build_listing_context(listing: dict, search: dict, allowed_categories: Optional[set[str]]) -> dict:
    raw_title_text = clean_text(listing["title"]).lower()
    raw_description_text = clean_text(listing.get("description", "")).lower()
    raw_full_text = clean_text(f"{listing['title']} {listing.get('description', '')}").lower()
    match_title_source = strip_bundled_accessory_tail(listing["title"])
    title_text = normalize_text(match_title_source)
    description_text = normalize_text(listing.get("description", ""))
    full_text = normalize_text(f"{match_title_source} {listing.get('description', '')}")
    text_variants = collect_text_variants(
        match_title_source,
        listing.get("description", ""),
        f"{match_title_source} {listing.get('description', '')}",
    )
    text_segments = split_text_segments(listing["title"], listing.get("description", ""))
    defect_variants = collect_text_variants(listing["title"], *text_segments)
    raw_description = clean_text(listing.get("description", "")).lower()
    if raw_description:
        defect_variants.append(raw_description)
    token_list = full_text.split()
    token_counts = Counter(token_list)
    token_set = set(token_list)

    parts_hits = detect_pattern_hits(defect_variants, PARTS_ONLY_PATTERNS)
    parts_hits.extend(detect_device_level_failures(text_segments))
    damage_hits = detect_pattern_hits(defect_variants, DAMAGE_PATTERNS)
    if "cracked_unspecified" in damage_hits and any(key in damage_hits for key in ("cracked_screen", "cracked_back")):
        damage_hits = [item for item in damage_hits if item != "cracked_unspecified"]
    battery_health = extract_battery_health(text_variants)
    if battery_health is not None and battery_health < 80 and "battery_under_80" not in damage_hits:
        damage_hits.append("battery_under_80")
    defects = []
    seen = set()
    for key in parts_hits + damage_hits:
        if key not in seen:
            seen.add(key)
            defects.append(key)

    condition = infer_condition_from_signals(text_variants, defects)
    if defects and condition in {"sealed", "new"}:
        condition = "damaged"

    category_signals = {
        category: any(re.search(pattern, variant) for pattern in patterns for variant in text_variants)
        for category, patterns in CATEGORY_CONTEXT_PATTERNS.items()
    }

    carrier_status = detect_listing_carrier_status(text_variants)
    part_listing_hits = detect_part_listing_hits(
        title_text,
        full_text,
        extract_storage_candidates(text_variants),
        battery_health,
        defects,
        carrier_status,
    )
    listing_kind = "part" if part_listing_hits else "core_device"
    storage_candidates = extract_storage_candidates(text_variants)

    return {
        "raw_title_text": raw_title_text,
        "raw_description_text": raw_description_text,
        "raw_full_text": raw_full_text,
        "title_text": title_text,
        "description_text": description_text,
        "full_text": full_text,
        "text_variants": text_variants,
        "text_segments": text_segments,
        "token_list": token_list,
        "token_counts": token_counts,
        "token_set": token_set,
        "interesting_tokens": {token for token in token_set if token in IMPORTANT_MODEL_TOKENS},
        "numeric_tokens": {token for token in token_set if token.isdigit()},
        "carrier_status": carrier_status,
        "storage_candidates": storage_candidates,
        "ram_candidates": extract_ram_candidates(full_text),
        "year": extract_year(full_text),
        "watch_size": extract_watch_size_value(full_text),
        "battery_health": battery_health,
        "condition": condition,
        "defects": defects,
        "defect_labels": [DEFECT_LABELS.get(item, item.replace("_", " ")) for item in defects],
        "parts_hits": parts_hits,
        "damage_hits": damage_hits,
        "part_listing_hits": part_listing_hits,
        "listing_kind": listing_kind,
        "macbook": build_macbook_context(full_text),
        "iphone_model_key": extract_iphone_model_key(title_text) or extract_iphone_model_key(full_text),
        "samsung_model_key": extract_samsung_model_key(title_text) or extract_samsung_model_key(full_text),
        "apple_watch_model_key": extract_apple_watch_model_key(title_text) or extract_apple_watch_model_key(full_text),
        "lock_status": extract_lock_status(text_variants, carrier_status, defects),
        "text_condition_signals": [DEFECT_LABELS.get(item, item.replace("_", " ")) for item in defects],
        "allowed_categories": allowed_categories or set(),
        "category_signals": category_signals,
        "is_free_listing": abs(float(listing.get("price", 0) or 0)) < 0.005,
    }


def blocked_word_hits(context: dict, config: dict) -> List[str]:
    hits = []
    title_text = context.get("title_text", "")
    full_text = context.get("full_text", "")
    strong_device_title = bool(
        re.search(r"\b(iphone|ipad|macbook|pixel|galaxy|apple watch)\b", title_text)
        and (re.search(r"\b\d{1,2}\b", title_text) or any(token in title_text for token in IMPORTANT_MODEL_TOKENS))
    )
    for blocked_word in [normalize_text(word) for word in config.get("blocked_words", []) if clean_text(word)]:
        if not blocked_word:
            continue
        if " " in blocked_word:
            if blocked_word in full_text:
                hits.append(blocked_word)
            continue
        if blocked_word in title_text and blocked_word in TITLE_ACCESSORY_TERMS and not strong_device_title:
            hits.append(blocked_word)
    return hits


def looks_like_accessory_listing(context: dict) -> bool:
    title = context.get("title_text", "")
    full_text = context.get("full_text", "")
    strong_device_title = bool(
        re.search(r"\b(iphone|ipad|macbook|pixel|galaxy|apple watch|watch)\b", title)
        and (re.search(r"\b\d{1,2}\b", title) or any(token in title for token in IMPORTANT_MODEL_TOKENS))
    )
    strong_hits = [term for term in TITLE_ACCESSORY_TERMS if contains_normalized_term(title, term)]
    device_state_hits = bool(
        strong_device_title
        or context.get("storage_candidates")
        or context.get("battery_health") is not None
        or context.get("carrier_status") != "unknown"
        or context.get("defects")
        or contains_any_normalized_term(full_text, WHOLE_DEVICE_STATE_TERMS)
        or contains_any_normalized_term(full_text, ("phone", "tablet", "laptop", "works", "working", "functional"))
    )
    if strong_hits and not device_state_hits:
        return True
    if strong_hits and re.search(r"\b(?:for|fits|compatible with)\b", full_text):
        return True
    if strong_device_title and not re.search(r"\b(?:only|replacement|compatible with|fits)\b", full_text):
        return False
    accessory_hits = [term for term in ACCESSORY_TERMS if contains_normalized_term(full_text, term)]
    core_hits = sum(1 for present in context.get("category_signals", {}).values() if present)
    return bool(accessory_hits and core_hits == 0)


def validate_listing_context(listing: dict, context: dict, config: dict, allowed_categories: Optional[set[str]]) -> Optional[str]:
    price = listing["price"]
    if price != 0 and price < float(config.get("min_price", 0)):
        return "below minimum price"
    if price > float(config.get("max_price", 999999)):
        return "above maximum price"

    full_text = context["full_text"]
    for blocked_word in blocked_word_hits(context, config):
        return f"blocked by keyword: {blocked_word}"

    if config["validation"].get("reject_home_terms", True):
        for term in UNRELATED_HOME_TERMS:
            if term in full_text:
                return f"rejected home/furniture term: {term}"

    if config["validation"].get("reject_accessories", True):
        if looks_like_accessory_listing(context):
            return "rejected accessory-only listing"

    if config["validation"].get("reject_parts_only", True) and context.get("listing_kind") == "part":
        return "rejected part-only listing"

    if not any(context.get("category_signals", {}).values()):
        return "missing supported device signal"

    if allowed_categories and len(allowed_categories) == 1:
        category = next(iter(allowed_categories))
        if not context["category_signals"].get(category):
            if category == "samsung" and re.search(r"\bgalaxy\s+a\b|\ba\d{2}\b", full_text):
                return "rejected non-target Samsung A-series listing"
            return f"missing strong {category} signal"

    if context["macbook"]["present"]:
        year = context["macbook"].get("year") or context.get("year")
        if year and int(year) < 2020 and config["validation"].get("reject_pre_2020_macbooks", True):
            return "rejected pre-2020 MacBook"
        if context["macbook"].get("chip_kind") == "intel" and config["validation"].get("reject_intel_macbooks", True):
            return "rejected Intel MacBook"

    return None


def choose_condition_price(row: dict, condition: str) -> tuple[str, Optional[float]]:
    prices = row.get("condition_prices_usd") or {}
    candidates_map = {
        "sealed": ["sealed", "new", "open", "activated", row.get("default_condition", ""), "a", "b"],
        "new": ["new", "sealed", "open", "activated", row.get("default_condition", ""), "a", "b"],
        "opened": ["open", "activated", "new", "sealed", row.get("default_condition", ""), "a", "b"],
        "used": [row.get("default_condition", ""), "b", "b_plus", "ab_grade", "a", "c", "d", "open"],
        "damaged": ["d", "c", row.get("default_condition", ""), "doa"],
        "parts": ["doa", "d", "c", row.get("default_condition", "")],
        "unspecified": [row.get("default_condition", ""), "b", "b_plus", "ab_grade", "a", "c", "d", "open"],
    }
    for key in candidates_map.get(condition, []):
        if key and key in prices:
            return key, float(prices[key])
    if row.get("base_price_usd") is not None:
        return row.get("default_condition", ""), float(row["base_price_usd"])
    return "", None


def validate_samsung_model_support(context: dict, catalog: dict, allowed_categories: Optional[set[str]]) -> Optional[str]:
    samsung_model_key = clean_text(context.get("samsung_model_key", ""))
    if not samsung_model_key:
        return None

    samsung_relevant = bool(
        context.get("category_signals", {}).get("samsung")
        or (allowed_categories and "samsung" in allowed_categories)
    )
    if not samsung_relevant:
        return None

    if samsung_model_key not in catalog.get("supported_samsung_model_keys", set()):
        return f"unsupported samsung model family: {samsung_model_key}"

    return None


def parsed_model_key_for_category(context: dict, category: str) -> str:
    if category == "iphone":
        return clean_text(context.get("iphone_model_key", ""))
    if category == "samsung":
        return clean_text(context.get("samsung_model_key", ""))
    if category == "apple_watch":
        return clean_text(context.get("apple_watch_model_key", ""))
    return ""


def validate_supported_model_family(context: dict, catalog: dict, allowed_categories: Optional[set[str]]) -> Optional[str]:
    for category in {"iphone", "apple_watch"}:
        if allowed_categories and category not in allowed_categories:
            continue
        if not context.get("category_signals", {}).get(category):
            continue
        parsed_key = parsed_model_key_for_category(context, category)
        if not parsed_key:
            continue
        supported = catalog.get("supported_model_keys_by_category", {}).get(category, set())
        if parsed_key not in supported:
            return f"unsupported {category.replace('_', ' ')} model family: {parsed_key}"
    return None


def validate_selected_price_row(
    context: dict,
    matched_family: dict,
    matched_row: dict,
    allowed_categories: Optional[set[str]],
) -> Optional[str]:
    category = matched_row.get("category", "")
    if allowed_categories and category not in allowed_categories:
        return f"selected row category mismatch: {category}"

    parsed_key = parsed_model_key_for_category(context, category)
    family_key = clean_text(matched_family.get("support_model_key", ""))
    if parsed_key and family_key and parsed_key != family_key:
        return f"selected row family mismatch: listing={parsed_key} row={family_key}"

    return None


def step_down_condition_price(
    row: dict,
    current_key: str,
    steps: int,
    floor_key: str = "",
) -> tuple[str, Optional[float]]:
    prices = row.get("condition_prices_usd") or {}
    order = ["sealed", "new", "open", "activated", "a", "ab_grade", "b_plus", "b", "c", "d", "doa"]
    available = [key for key in order if key in prices]
    if not available:
        return current_key, None
    if current_key not in available:
        if row.get("default_condition", "") in available:
            current_key = row.get("default_condition", "")
        else:
            current_key = available[0]
    index = min(len(available) - 1, available.index(current_key) + max(0, steps))
    if floor_key and floor_key in available:
        index = min(index, available.index(floor_key))
    chosen = available[index]
    return chosen, float(prices[chosen])


def resolve_condition_override_price(row: dict, target_key: str) -> tuple[str, Optional[float]]:
    prices = row.get("condition_prices_usd") or {}
    if target_key and target_key in prices:
        return target_key, float(prices[target_key])
    if not prices:
        return target_key, None

    order = ["sealed", "new", "open", "activated", "a", "ab_grade", "b_plus", "b", "c", "d", "doa"]
    available = [key for key in order if key in prices]
    if not available:
        fallback_key = next(iter(prices.keys()))
        return fallback_key, float(prices[fallback_key])

    target_index = order.index(target_key) if target_key in order else len(order) - 1
    for key in available:
        if order.index(key) >= target_index:
            return key, float(prices[key])

    fallback_key = available[-1]
    return fallback_key, float(prices[fallback_key])


def apply_excel_adjustments(
    row: dict,
    condition: str,
    condition_key: str,
    condition_price_usd: float,
    defect_keys: List[str],
    config: dict,
) -> dict:
    flat_deduction_usd = 0.0
    applied_labels: List[str] = []
    manual_labels: List[str] = []
    pricing_adjustments: List[str] = []
    force_parts = False
    step_downs = 0
    condition_overrides: List[str] = []

    for defect_key in defect_keys:
        rule = (row.get("deduction_rules_usd") or {}).get(defect_key)
        label = DEFECT_LABELS.get(defect_key, defect_key.replace("_", " "))
        if not isinstance(rule, dict):
            continue
        if rule.get("kind") == "flat_usd":
            amount = float(rule.get("value", 0))
            flat_deduction_usd += amount
            applied_labels.append(label)
            pricing_adjustments.append(f"{label}: -${amount:.0f} USD")
            continue
        if rule.get("kind") == "condition_override":
            override_key = clean_text(rule.get("value", "")).lower()
            if override_key:
                condition_overrides.append(override_key)
                applied_labels.append(label)
                pricing_adjustments.append(f"{label}: use sheet {override_key.upper()} condition")
            continue

        reason = clean_text(rule.get("reason", "")).lower()
        if reason == "parts":
            force_parts = True
            applied_labels.append(label)
            pricing_adjustments.append(f"{label}: forced parts pricing")
        elif reason in {"ask", "extra_deduction"}:
            step_downs += 1
            manual_labels.append(label)
            pricing_adjustments.append(f"{label}: stepped down one sheet condition tier")

    step_downs = min(step_downs, int(config["validation"].get("manual_deduction_max_steps", 2)))
    final_condition_key = condition_key
    adjusted_usd = float(condition_price_usd)

    if force_parts:
        forced_key, forced_price = choose_condition_price(row, "parts")
        if forced_price is not None:
            final_condition_key = forced_key or final_condition_key
            adjusted_usd = forced_price
    elif condition_overrides:
        override_order = ["sealed", "new", "open", "activated", "a", "ab_grade", "b_plus", "b", "c", "d", "doa"]
        chosen_override = max(
            condition_overrides,
            key=lambda item: override_order.index(item) if item in override_order else len(override_order),
        )
        override_key, override_price = resolve_condition_override_price(row, chosen_override)
        if override_price is not None:
            final_condition_key = override_key or final_condition_key
            adjusted_usd = override_price

    if not force_parts and step_downs:
        floor_key = ""
        if condition == "damaged":
            floor_key = "d"
        elif condition == "parts":
            floor_key = "doa"
        worse_key, worse_price = step_down_condition_price(row, final_condition_key, step_downs, floor_key=floor_key)
        if worse_price is not None:
            final_condition_key = worse_key or final_condition_key
            adjusted_usd = worse_price

    adjusted_usd = max(adjusted_usd - flat_deduction_usd, 0)
    return {
        "adjusted_usd": adjusted_usd,
        "final_condition_key": final_condition_key,
        "applied_defects": applied_labels,
        "manual_defects": manual_labels,
        "pricing_adjustments": pricing_adjustments,
    }


def family_match_score(family: dict, context: dict, config: dict) -> Optional[dict]:
    title_text = context["title_text"]
    full_text = context["full_text"]
    listing_tokens = context["token_set"]
    listing_token_counts = context["token_counts"]
    listing_alpha_tokens = context["interesting_tokens"]
    listing_carrier_status = context["carrier_status"]

    if family["category"] == "macbook":
        macbook = context["macbook"]
        if not macbook["present"]:
            return None
        if config["validation"].get("reject_intel_macbooks", True) and macbook["chip_kind"] == "intel":
            return None
        if config["validation"].get("reject_pre_2020_macbooks", True) and macbook.get("year") and int(macbook["year"]) < 2020:
            return None
        if macbook["chip_kind"] == "intel":
            return None
        if macbook["line"] and family.get("device_line") and macbook["line"] != family["device_line"]:
            return None
        if macbook["chip_family"] and family.get("chip_family") and macbook["chip_family"] != family["chip_family"]:
            return None
        if macbook["size_value"] and family.get("size_value") and macbook["size_value"] != family["size_value"]:
            return None
        if macbook["year"] and family.get("model_year") and macbook["year"] != family["model_year"]:
            return None
        if not macbook.get("year") and not macbook.get("chip_family"):
            return None
    elif family["category"] == "samsung":
        listing_samsung_model_key = context.get("samsung_model_key", "")
        family_samsung_model_key = family.get("samsung_model_key", "")
        if listing_samsung_model_key and family_samsung_model_key and listing_samsung_model_key != family_samsung_model_key:
            return None
    elif family["category"] in {"iphone", "apple_watch"}:
        parsed_key = parsed_model_key_for_category(context, family["category"])
        family_key = family.get("support_model_key", "")
        if parsed_key and family_key and parsed_key != family_key:
            return None

    best_alias = ""
    best_score = 0.0
    for alias in family.get("aliases", []):
        alias_tokens = set(normalized_tokens(alias))
        overlap = len(alias_tokens & listing_tokens)
        if alias_tokens and alias_tokens.issubset(listing_tokens):
            score = 104.0 + min(len(alias_tokens), 4)
        else:
            score = max(
                fuzzy_score(alias, title_text) + min(overlap * 4, 12),
                fuzzy_score(alias, full_text) + min(overlap * 2, 8),
            )
        if score > best_score:
            best_score = score
            best_alias = alias

    listing_watch_size = context.get("watch_size", "")
    model_token_list = list(family.get("model_tokens", []))
    if family["category"] == "apple_watch" and not listing_watch_size:
        size_tokens = {family.get("size_value", ""), "mm"}
        model_token_list = [token for token in model_token_list if token and token not in size_tokens]
    remaining_tokens = Counter(listing_token_counts)
    model_overlap = 0
    for token in model_token_list:
        if remaining_tokens[token] > 0:
            model_overlap += 1
            remaining_tokens[token] -= 1
    if model_overlap == 0 and best_score < 90:
        return None

    missing_model_tokens = len(model_token_list) - model_overlap
    model_bonus = (model_overlap * 4) - (missing_model_tokens * 7)

    family_numeric = set(family.get("numeric_model_tokens", set()))
    if family["category"] == "apple_watch" and not listing_watch_size and family.get("size_value"):
        family_numeric.discard(family["size_value"])
    if family_numeric:
        if family_numeric.issubset(context["numeric_tokens"]):
            model_bonus += 6
        else:
            model_bonus -= 18
            if family["category"] in {"samsung", "pixel"}:
                return None

    family_alpha = family.get("alpha_model_tokens", set())
    if family_alpha and listing_alpha_tokens and not (family_alpha & listing_alpha_tokens):
        if family["category"] in {"iphone", "samsung", "pixel"}:
            model_bonus -= 18
    exclusive_tokens = EXCLUSIVE_VARIANT_TOKENS.get(family["category"], set())
    if exclusive_tokens:
        listing_variant_tokens = {token for token in listing_alpha_tokens if token in exclusive_tokens}
        family_variant_tokens = {token for token in family_alpha if token in exclusive_tokens}
        if listing_variant_tokens != family_variant_tokens:
            if listing_variant_tokens or family_variant_tokens:
                return None
    if family["category"] in {"iphone", "samsung", "pixel"}:
        for strict_token in {"a", "edge", "fe", "se", "fold", "flip", "xl", "ultra", "mini", "plus", "max", "pro"}:
            if strict_token in listing_alpha_tokens and strict_token not in family_alpha:
                model_bonus -= 16

    if family["storage_options"] and context.get("storage_candidates"):
        if set(context["storage_candidates"]) & set(family["storage_options"]):
            model_bonus += 2
        else:
            model_bonus -= 4

    listing_primary_number = extract_primary_device_number(family["category"], full_text)
    family_primary_number = extract_primary_device_number(family["category"], family["model"])
    if listing_primary_number and family_primary_number and listing_primary_number != family_primary_number:
        if family["category"] in {"iphone", "samsung", "pixel", "apple_watch"}:
            return None

    if family["category"] == "apple_watch":
        family_watch_size = family.get("size_value", "")
        if listing_watch_size and family_watch_size and listing_watch_size != family_watch_size:
            return None

    carrier_bonus = 0.0
    if listing_carrier_status == "carrier_locked":
        if family["carrier_status"] == "carrier_locked":
            carrier_bonus += 4
        elif family["carrier_status"] == "unlocked":
            carrier_bonus -= 10
    elif listing_carrier_status == "unlocked":
        if family["carrier_status"] == "unlocked":
            carrier_bonus += 4
        elif family["carrier_status"] == "carrier_locked":
            carrier_bonus -= 10
    elif listing_carrier_status == "unknown" and family["carrier_status"] == "unlocked":
        carrier_bonus += 1

    score = best_score + model_bonus + carrier_bonus
    minimum_score = float(config["validation"].get("general_min_score", 78))
    if family["category"] == "macbook":
        minimum_score = float(config["validation"].get("macbook_direct_min_score", 88))
    if context.get("is_free_listing"):
        minimum_score -= float(config["validation"].get("free_listing_score_relief", 6))
    if score < minimum_score:
        return None

    return {
        "family": family,
        "score": score,
        "best_alias": best_alias,
        "model_overlap": model_overlap,
    }


def choose_best_family(context: dict, catalog: dict, config: dict, allowed_categories: Optional[set[str]]) -> Optional[dict]:
    candidates = []
    for family in catalog["families"]:
        if allowed_categories and family["category"] not in allowed_categories:
            continue
        scored = family_match_score(family, context, config)
        if scored:
            candidates.append(scored)
    if not candidates:
        return None
    if context["macbook"]["present"] and not context["macbook"].get("year"):
        top_score = max(item["score"] for item in candidates)
        close_candidates = [item for item in candidates if item["score"] >= top_score - 8]
        distinct_years = {item["family"].get("model_year", "") for item in close_candidates if item["family"].get("model_year", "")}
        if len(distinct_years) > 1:
            return None
    return max(candidates, key=lambda item: (item["score"], item["model_overlap"], len(item["best_alias"])))


def select_family_row(family: dict, chosen_storage: str) -> dict:
    if chosen_storage:
        for row in family["rows"]:
            if chosen_storage in row.get("storage_options", []):
                return row
    return family["rows"][0]


def estimate_non_sheet_macbook(
    context: dict,
    catalog: dict,
    config: dict,
    allowed_categories: Optional[set[str]],
) -> Optional[dict]:
    macbook = context["macbook"]
    if not macbook["present"]:
        return None
    if allowed_categories and "macbook" not in allowed_categories:
        return None
    if not context.get("category_signals", {}).get("macbook"):
        return None
    if any(
        present
        for category, present in (context.get("category_signals", {}) or {}).items()
        if category != "macbook" and present
    ):
        return None
    if not config["validation"].get("allow_non_sheet_macbooks", True):
        return None
    if config["validation"].get("reject_pre_2020_macbooks", True) and macbook.get("year") and int(macbook["year"]) < 2020:
        return None
    if config["validation"].get("reject_intel_macbooks", True) and macbook.get("chip_kind") == "intel":
        return None
    if macbook.get("chip_kind") not in {"m_series", ""}:
        return None
    if macbook.get("chip_kind") == "" and not macbook.get("year"):
        return None

    comparable_rows = [
        row
        for row in catalog["rows"]
        if row["category"] == "macbook"
        and row.get("device_line") == (macbook["line"] or row.get("device_line"))
    ]
    if macbook["size_value"]:
        same_size = [
            row for row in comparable_rows if SIZE_PATTERN.search(normalize_text(row["model"])) and SIZE_PATTERN.search(normalize_text(row["model"])).group(1) == macbook["size_value"]
        ]
        if same_size:
            comparable_rows = same_size
    if not comparable_rows:
        comparable_rows = [row for row in catalog["rows"] if row["category"] == "macbook"]
    if not comparable_rows:
        return None

    chip_family = macbook["chip_family"]
    if chip_family:
        suffix_rows = []
        if chip_family.endswith("pro"):
            suffix_rows = [row for row in comparable_rows if row.get("chip_family", "").endswith("pro")]
        elif chip_family.endswith("max"):
            suffix_rows = [row for row in comparable_rows if row.get("chip_family", "").endswith("max")]
        elif chip_family.startswith("m "):
            suffix_rows = [row for row in comparable_rows if row.get("chip_family", "").startswith(f"m {chip_family.split()[1]}")]
        if suffix_rows:
            comparable_rows = suffix_rows

    anchor = min(comparable_rows, key=lambda row: row["base_price_usd"])
    year = macbook["year"]
    factor = 0.45
    if macbook["chip_kind"] == "intel":
        if year:
            year_number = int(year)
            if year_number >= 2020:
                factor = 0.70
            elif year_number >= 2018:
                factor = 0.60
            elif year_number >= 2015:
                factor = 0.45
            else:
                factor = 0.35
        else:
            factor = 0.45
    elif chip_family:
        generation_match = re.search(r"\bm\s*([1-9])\b", chip_family)
        generation = int(generation_match.group(1)) if generation_match else 0
        factor_map = {1: 0.74, 2: 0.84, 3: 0.92, 4: 1.00, 5: 1.06}
        factor = factor_map.get(generation, 0.85)
        if chip_family.endswith("pro"):
            factor += 0.06
        if chip_family.endswith("max"):
            factor += 0.10
        if year and anchor.get("model_year"):
            try:
                year_gap = int(anchor["model_year"]) - int(year)
            except ValueError:
                year_gap = 0
            if year_gap > 0:
                factor *= max(0.65, 1 - (0.04 * year_gap))

    estimated_sheet_price_usd = round(float(anchor["base_price_usd"]) * factor, 2)
    if anchor.get("base_price_usd"):
        condition_key, condition_price = choose_condition_price(anchor, context.get("condition", "used"))
        if condition_price is not None:
            default_base = float(anchor["base_price_usd"])
            estimated_sheet_price_usd = round(
                estimated_sheet_price_usd * max(condition_price / max(default_base, 1), 0.2),
                2,
            )
    return {
        "pricing_match_type": "extrapolated_estimate",
        "matched_row": None,
        "matched_family": None,
        "matched_label": f"non-sheet {macbook['line'] or 'macbook'} estimate",
        "sheet_price_usd": estimated_sheet_price_usd,
        "anchor_row": anchor,
        "valuation_note": "Estimated from the nearest M-chip MacBook row because this MacBook is outside the price sheet.",
    }


def canonical_condition_note(note: str) -> str:
    cleaned = clean_text(note)
    if not cleaned:
        return ""
    normalized = CONDITION_NOTE_ALIASES.get(cleaned, cleaned)
    if normalized.lower() == "activation locked":
        return "activation locked"
    if normalized.lower() == "icloud locked":
        return "iCloud locked"
    return normalized


def battery_health_note(value: Optional[int]) -> str:
    if value is None:
        return ""
    return f"battery health {int(value)}%"


def build_condition_notes(context: dict) -> str:
    notes: List[str] = []
    text_defects = [canonical_condition_note(note) for note in context.get("defect_labels", [])]
    exact_battery_note = battery_health_note(context.get("battery_health"))

    if context.get("lock_status") == "icloud_locked" or "icloud locked" in context.get("full_text", ""):
        notes.append("iCloud locked")
    elif context.get("lock_status") == "activation_locked" or "activation locked" in context.get("full_text", ""):
        notes.append("activation locked")

    if context.get("carrier_status") == "carrier_locked":
        notes.append("carrier locked")

    full_text = context.get("raw_full_text", "")
    if any(re.search(pattern, full_text) for pattern in STRONG_SEALED_PATTERNS):
        notes.append("sealed")
    elif any(re.search(pattern, full_text) for pattern in NEW_PATTERNS):
        notes.append("new")
    elif any(re.search(pattern, full_text) for pattern in OPENED_PATTERNS):
        notes.append("opened box")

    if any(re.search(pattern, context.get("raw_full_text", "")) for pattern in GOOD_CONDITION_PATTERNS):
        notes.append("good condition")

    if exact_battery_note and context.get("battery_health") is not None and int(context["battery_health"]) < 80:
        notes.append(exact_battery_note)

    notes.extend(text_defects)

    ordered: List[str] = []
    seen = set()
    for target in CONDITION_NOTE_PRIORITY:
        for note in notes:
            canonical = canonical_condition_note(note)
            if not canonical or canonical.lower() in seen:
                continue
            if target == "battery health" and canonical.lower().startswith("battery health "):
                seen.add(canonical.lower())
                ordered.append(canonical)
                continue
            if canonical.lower() == target.lower():
                seen.add(canonical.lower())
                ordered.append(canonical)

    if not ordered:
        ordered.append("condition not specified")

    if any(note in {"for parts", "not working", "activation locked", "iCloud locked", "google locked"} for note in ordered):
        ordered = [note for note in ordered if note not in {"new", "sealed", "opened box", "good condition"}]
    elif any(note in {"cracked screen", "cracked back", "screen issue"} for note in ordered):
        ordered = [note for note in ordered if note not in {"new", "sealed", "good condition"}]
    if exact_battery_note and context.get("battery_health") is not None and int(context["battery_health"]) < 80:
        ordered = [note for note in ordered if note != "battery health under 80 percent" or note == exact_battery_note]
    specific_notes = [
        note
        for note in ordered
        if note
        not in {"new", "sealed", "opened box", "good condition", "condition not specified"}
    ]

    return ", ".join(ordered[:3])


def condition_requires_deduction(analysis: dict) -> bool:
    if analysis.get("defects"):
        return True
    battery_health = analysis.get("battery_health")
    return battery_health is not None and int(battery_health) < 80


def build_text_signal_summary(context: dict) -> dict:
    return {
        "parts_hits": list(context.get("parts_hits", [])),
        "damage_hits": list(context.get("damage_hits", [])),
        "part_listing_hits": list(context.get("part_listing_hits", [])),
        "category_signals": sorted(
            category for category, present in (context.get("category_signals", {}) or {}).items() if present
        ),
    }


def build_listing_analysis(
    listing: dict,
    search: dict,
    catalog: dict,
    config: dict,
    allowed_categories: Optional[set[str]],
) -> Optional[dict]:
    context = build_listing_context(listing, search, allowed_categories)
    if config.get("debug", {}).get("enabled", False):
        debug_log(
            config,
            "Pipeline input "
            f"{listing['platform']} {listing['id']} | title={truncate_debug_text(listing.get('title', ''))!r} | "
            f"description={truncate_debug_text(listing.get('description', ''), 500)!r}",
        )
    reject_reason = validate_listing_context(listing, context, config, allowed_categories)
    if reject_reason:
        debug_log(config, f"Rejected {listing['platform']} {listing['title']}: {reject_reason}")
        return None

    samsung_support_reject_reason = validate_samsung_model_support(context, catalog, allowed_categories)
    if samsung_support_reject_reason:
        debug_log(config, f"Rejected {listing['platform']} {listing['title']}: {samsung_support_reject_reason}")
        return None

    support_reject_reason = validate_supported_model_family(context, catalog, allowed_categories)
    if support_reject_reason:
        debug_log(config, f"Rejected {listing['platform']} {listing['title']}: {support_reject_reason}")
        return None

    match = choose_best_family(context, catalog, config, allowed_categories)
    pricing_match_type = ""

    if match:
        matched_family = match["family"]
        chosen_storage, storage_source = choose_storage_from_catalog(context, matched_family)
        matched_row = select_family_row(matched_family, chosen_storage)
        row_validation_error = validate_selected_price_row(context, matched_family, matched_row, allowed_categories)
        if row_validation_error:
            debug_log(config, f"Rejected {listing['platform']} {listing['title']}: {row_validation_error}")
            return None
        condition_key, condition_price_usd = choose_condition_price(matched_row, context["condition"])
        if condition_price_usd is None:
            return None

        adjustment_result = apply_excel_adjustments(
            matched_row,
            context["condition"],
            condition_key,
            condition_price_usd,
            context["defects"],
            config,
        )
        adjusted_usd = adjustment_result["adjusted_usd"]
        pricing_thresholds = pricing_thresholds_from_sheet_usd(adjusted_usd, config["pricing"])
        pricing_match_type = "exact_sheet_match" if storage_source == "detected" and context["condition"] in {"sealed", "new", "opened", "used"} else "inferred_sheet_match"
        if matched_family["category"] == "macbook" and not context["macbook"]["year"]:
            pricing_match_type = "inferred_sheet_match"
        resolved_year = context["macbook"].get("year") if matched_row["category"] == "macbook" else context["year"]
        chip_value = context["macbook"].get("chip_family", "") if matched_row["category"] == "macbook" else ""
        ram_value = context["macbook"].get("ram_candidates", [])[:1] if matched_row["category"] == "macbook" else []
        condition_notes = build_condition_notes(context)
        if config.get("debug", {}).get("enabled", False):
            debug_log(
                config,
                "Pipeline parsed "
                f"{listing['platform']} {listing['id']} | defects={context['defect_labels']} | "
                f"condition_notes={condition_notes!r}",
            )

        analysis = {
            "platform": listing["platform"],
            "listing_id": listing["id"],
            "canonical_url": normalize_url(listing["url"]),
            "category": matched_row["category"],
            "brand": matched_row["brand"],
            "model": matched_row["model"],
            "year": resolved_year or "",
            "chip": chip_value,
            "ram": ram_value[0] if ram_value else "",
            "sheet_year": matched_row.get("model_year", ""),
            "storage": chosen_storage,
            "storage_source": storage_source,
            "battery_health": context.get("battery_health"),
            "carrier_status": context["carrier_status"],
            "lock_status": context.get("lock_status", "unknown"),
            "listing_kind": context.get("listing_kind", "core_device"),
            "condition": context["condition"],
            "defects": context["defect_labels"],
            "text_signals": build_text_signal_summary(context),
            "text_condition_signals": context.get("text_condition_signals", []),
            "confidence_score": round(min(match["score"], 100.0), 1),
            "pricing_match_type": pricing_match_type,
            "matched_csv_row": matched_row["family_key"],
            "matched_label": matched_row["model"],
            "sheet_price_usd": adjusted_usd,
            "sell_price": pricing_thresholds["sell_price"],
            "max_buy_price": pricing_thresholds["max_buy_price"],
            "max_listing_price_to_alert": pricing_thresholds["alert_ceiling"],
            "fair_price_from_sheet_cad": pricing_thresholds["sell_price"],
            "sell_price_cad": pricing_thresholds["sell_price"],
            "maximum_buy_price_cad": pricing_thresholds["max_buy_price"],
            "max_listing_price_to_alert_cad": pricing_thresholds["alert_ceiling"],
            "condition_key": adjustment_result["final_condition_key"],
            "applied_defects": adjustment_result["applied_defects"],
            "manual_defects": adjustment_result["manual_defects"],
            "pricing_adjustments": adjustment_result["pricing_adjustments"],
            "within_maximum_buy_price": listing["price"] <= pricing_thresholds["max_buy_price"],
            "negotiation_required": (
                listing["price"] > pricing_thresholds["max_buy_price"]
                and listing["price"] <= pricing_thresholds["alert_ceiling"]
            ),
            "condition_notes": condition_notes,
            "valuation_note": matched_row.get("notes", ""),
            "extrapolated_estimate": False,
            "matched_price_row": matched_row,
            "dedupe_status": "unchecked",
            "return_decision": "candidate",
            "structured_reasoning": {
                "platform": listing["platform"],
                "listing_id": listing["id"],
                "canonical_url": normalize_url(listing["url"]),
                "category": matched_row["category"],
                "brand": matched_row["brand"],
                "model": matched_row["model"],
                "year": resolved_year or "",
                "chip": chip_value,
                "ram": ram_value[0] if ram_value else "",
                "storage": chosen_storage,
                "battery_health": context.get("battery_health"),
                "carrier_status": context["carrier_status"],
                "lock_status": context.get("lock_status", "unknown"),
                "listing_kind": context.get("listing_kind", "core_device"),
                "condition": context["condition"],
                "defects": context["defect_labels"],
                "text_signals": build_text_signal_summary(context),
                "text_condition_signals": context.get("text_condition_signals", []),
                "pricing_match_type": pricing_match_type,
                "matched_price_row": matched_row["family_key"],
                "sheet_price_usd": adjusted_usd,
                "fair_price": pricing_thresholds["sell_price"],
                "sell_price": pricing_thresholds["sell_price"],
                "max_buy_price": pricing_thresholds["max_buy_price"],
                "dedupe_status": "unchecked",
                "return_decision": "candidate",
            },
        }
        return analysis

    estimate = estimate_non_sheet_macbook(context, catalog, config, allowed_categories)
    if not estimate:
        return None

    anchor_row = estimate.get("anchor_row")
    condition_key = ""
    adjustment_result = {
        "applied_defects": [],
        "manual_defects": [],
        "pricing_adjustments": [],
        "final_condition_key": "",
    }
    estimated_sheet_price_usd = estimate["sheet_price_usd"]
    if anchor_row and anchor_row.get("base_price_usd"):
        base_anchor_usd = float(anchor_row["base_price_usd"])
        condition_key, condition_price_usd = choose_condition_price(anchor_row, context["condition"])
        if condition_price_usd is not None:
            adjustment_result = apply_excel_adjustments(
                anchor_row,
                context["condition"],
                condition_key,
                estimated_sheet_price_usd * (condition_price_usd / max(base_anchor_usd, 1)),
                context["defects"],
                config,
            )
            estimated_sheet_price_usd = adjustment_result["adjusted_usd"]

    pricing_thresholds = pricing_thresholds_from_sheet_usd(estimated_sheet_price_usd, config["pricing"])
    condition_notes = build_condition_notes(context)
    if config.get("debug", {}).get("enabled", False):
        debug_log(
            config,
            "Pipeline parsed "
            f"{listing['platform']} {listing['id']} | defects={context['defect_labels']} | "
            f"condition_notes={condition_notes!r}",
        )
    return {
        "platform": listing["platform"],
        "listing_id": listing["id"],
        "canonical_url": normalize_url(listing["url"]),
        "category": "macbook",
        "brand": "apple",
        "model": context["macbook"]["line"] or "macbook",
        "year": context["macbook"]["year"] or "",
        "chip": context["macbook"].get("chip_family", ""),
        "ram": context["macbook"].get("ram_candidates", [""])[0] if context["macbook"].get("ram_candidates") else "",
        "sheet_year": "",
        "storage": context["storage_candidates"][0] if context["storage_candidates"] else "",
        "storage_source": "detected" if context["storage_candidates"] else "unspecified",
        "battery_health": context.get("battery_health"),
        "carrier_status": "not_applicable",
        "lock_status": context.get("lock_status", "unknown"),
        "listing_kind": context.get("listing_kind", "core_device"),
        "condition": context["condition"],
        "defects": context["defect_labels"],
        "text_signals": build_text_signal_summary(context),
        "text_condition_signals": context.get("text_condition_signals", []),
        "confidence_score": 68.0,
        "pricing_match_type": estimate["pricing_match_type"],
        "matched_csv_row": "",
        "matched_label": estimate["matched_label"],
        "sheet_price_usd": estimated_sheet_price_usd,
        "sell_price": pricing_thresholds["sell_price"],
        "max_buy_price": pricing_thresholds["max_buy_price"],
        "max_listing_price_to_alert": pricing_thresholds["alert_ceiling"],
        "fair_price_from_sheet_cad": pricing_thresholds["sell_price"],
        "sell_price_cad": pricing_thresholds["sell_price"],
        "maximum_buy_price_cad": pricing_thresholds["max_buy_price"],
        "max_listing_price_to_alert_cad": pricing_thresholds["alert_ceiling"],
        "condition_key": adjustment_result.get("final_condition_key", condition_key),
        "applied_defects": adjustment_result.get("applied_defects", []),
        "manual_defects": adjustment_result.get("manual_defects", context["defect_labels"]),
        "pricing_adjustments": adjustment_result.get("pricing_adjustments", []),
        "within_maximum_buy_price": listing["price"] <= pricing_thresholds["max_buy_price"],
        "negotiation_required": (
            listing["price"] > pricing_thresholds["max_buy_price"]
            and listing["price"] <= pricing_thresholds["alert_ceiling"]
        ),
        "condition_notes": condition_notes,
        "valuation_note": estimate["valuation_note"],
        "extrapolated_estimate": True,
        "matched_price_row": anchor_row["family_key"] if anchor_row else "",
        "dedupe_status": "unchecked",
        "return_decision": "candidate",
        "structured_reasoning": {
            "platform": listing["platform"],
            "listing_id": listing["id"],
            "canonical_url": normalize_url(listing["url"]),
            "category": "macbook",
            "brand": "apple",
            "model": context["macbook"]["line"] or "macbook",
            "year": context["macbook"]["year"] or "",
            "chip": context["macbook"].get("chip_family", ""),
            "ram": context["macbook"].get("ram_candidates", [""])[0] if context["macbook"].get("ram_candidates") else "",
            "storage": context["storage_candidates"][0] if context["storage_candidates"] else "",
            "battery_health": context.get("battery_health"),
            "carrier_status": "not_applicable",
            "lock_status": context.get("lock_status", "unknown"),
            "listing_kind": context.get("listing_kind", "core_device"),
            "condition": context["condition"],
            "defects": context["defect_labels"],
            "text_signals": build_text_signal_summary(context),
            "text_condition_signals": context.get("text_condition_signals", []),
            "pricing_match_type": estimate["pricing_match_type"],
            "matched_price_row": anchor_row["family_key"] if anchor_row else "",
            "extrapolated_estimate": True,
            "sheet_price_usd": estimated_sheet_price_usd,
            "fair_price": pricing_thresholds["sell_price"],
            "sell_price": pricing_thresholds["sell_price"],
            "max_buy_price": pricing_thresholds["max_buy_price"],
            "dedupe_status": "unchecked",
            "return_decision": "candidate",
        },
    }


def build_alert_text(search_name: str, listing: dict, analysis: dict) -> str:
    if analysis.get("storage_source") == "detected" and analysis.get("storage"):
        storage_text = analysis["storage"]
    elif analysis.get("storage"):
        storage_text = f"unspecified (using {analysis['storage']} floor)"
    else:
        storage_text = "unspecified"

    condition_text = analysis.get("condition_notes") or "condition not specified"
    if condition_text != "condition not specified" and condition_requires_deduction(analysis):
        condition_text = f"{condition_text}, deduction required"

    lines = [
        "Deal Found",
        clean_text(listing["title"]),
        f"Storage - {storage_text}",
        f"{platform_label(listing['platform'])} | {format_price(listing['price'])}",
        f"Sell Price - {format_price(analysis.get('sell_price', analysis['sell_price_cad']))}",
        f"Max Buy - {format_price(analysis.get('max_buy_price', analysis['maximum_buy_price_cad']))}",
        f"Condition/Notes - {condition_text}",
    ]

    if analysis.get("negotiation_required"):
        lines.append("Negotiation required")

    lines.append(f"Link - {listing['url']}")
    return "\n".join(lines)


def telegram_is_configured(config: dict) -> bool:
    token = clean_text(config.get("telegram", {}).get("bot_token", ""))
    chat_id = clean_text(config.get("telegram", {}).get("chat_id", ""))
    return bool(token and chat_id and "PASTE_YOUR" not in token and "PASTE_YOUR" not in chat_id)


def send_telegram_message(config: dict, text: str) -> None:
    telegram = config.get("telegram", {})
    token = clean_text(telegram.get("bot_token", ""))
    chat_id = clean_text(telegram.get("chat_id", ""))

    payload = urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")

    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"User-Agent": USER_AGENT},
    )
    with urlopen(request, timeout=30) as response:
        data = json.load(response)
    if not data.get("ok"):
        raise RuntimeError(f"Telegram send failed: {data}")


def print_chat_ids(config: dict) -> None:
    token = clean_text(config.get("telegram", {}).get("bot_token", ""))
    if not token or "PASTE_YOUR" in token:
        raise SystemExit("Put your Telegram bot token into config.json first.")

    request = Request(
        f"https://api.telegram.org/bot{token}/getUpdates",
        headers={"User-Agent": USER_AGENT},
    )
    with urlopen(request, timeout=30) as response:
        data = json.load(response)
    if not data.get("ok"):
        raise SystemExit(f"Telegram getUpdates failed: {data}")

    results = data.get("result", [])
    if not results:
        raise SystemExit("No Telegram updates yet. Send a message to your bot first, then run this command again.")

    seen = {}
    for item in results:
        message = item.get("message") or item.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        title = chat.get("title") or chat.get("username") or chat.get("first_name") or "Unknown"
        if chat_id is not None:
            seen[str(chat_id)] = title

    print("\nTelegram chat IDs found:\n")
    for chat_id, title in seen.items():
        print(f"{chat_id}  ->  {title}")
    print("\nPut the correct chat ID into config.json.")


def dedupe_history_path(config: dict) -> Path:
    configured = clean_text(config.get("dedupe", {}).get("history_file", "seen_listings.json"))
    return BASE_DIR / configured if configured else SEEN_LISTINGS_PATH


def dedupe_keys_for_listing(listing: dict) -> List[str]:
    platform = clean_text(listing.get("platform", ""))
    normalized_url = normalize_url(listing.get("url", ""))
    raw_id = clean_text(listing.get("id", ""))
    if not raw_id and normalized_url:
        derived_id = listing_id_from_url(normalized_url)
        if derived_id != normalized_url:
            raw_id = derived_id
    keys: List[str] = []
    if raw_id and raw_id != normalized_url:
        keys.append(f"{platform}::id::{raw_id}")
    if normalized_url:
        digest = hashlib.sha1(normalized_url.encode("utf-8")).hexdigest()[:16]
        keys.append(f"{platform}::url::{digest}")
    if not keys:
        fallback = normalize_text(
            f"{platform} {listing.get('title', '')} {listing.get('description', '')} {listing.get('url', '')}"
        )
        if not fallback:
            return []
        digest = hashlib.sha1(fallback.encode("utf-8")).hexdigest()[:16]
        keys.append(f"{platform}::fp::{digest}")
    return list(dict.fromkeys(keys))


def lookup_seen_record(seen_history: dict, listing: dict) -> Optional[dict]:
    history = ensure_seen_history_state(seen_history)
    key_map = history.get("key_map", {})
    records = history.get("records", {})
    for key in dedupe_keys_for_listing(listing):
        identity_key = key_map.get(key)
        if identity_key and identity_key in records:
            record = dict(records[identity_key])
            record.setdefault("status", "returned")
            record["_matched_key"] = key
            return record
    return None


def listing_stub_from_reference(reference: str) -> dict:
    cleaned = clean_text(reference)
    normalized = normalize_url(cleaned)
    platform = "facebook" if "facebook.com" in normalized else "kijiji" if "kijiji.ca" in normalized else ""
    return {
        "platform": platform,
        "url": normalized or cleaned,
        "id": listing_id_from_url(normalized or cleaned),
        "title": "",
        "price": 0,
    }


def record_listing_state(items: dict, listing: dict, last_alert_price: Optional[float]) -> None:
    item_key = f"{listing['platform']}::{listing['id']}"
    items[item_key] = {
        "title": listing["title"],
        "url": listing["url"],
        "platform": listing["platform"],
        "last_price": listing["price"],
        "last_seen_at": dt.datetime.now().isoformat(timespec="seconds"),
        "last_alert_price": last_alert_price,
    }


def store_seen_record(seen_history: dict, listing: dict, analysis: dict, status: str) -> None:
    history = ensure_seen_history_state(seen_history)
    now = dt.datetime.now().isoformat(timespec="seconds")
    record = {
        "url": listing["url"],
        "canonical_url": normalize_url(listing["url"]),
        "listing_id": listing["id"],
        "title": listing["title"],
        "platform": listing["platform"],
        "price": listing["price"],
        "matched_label": analysis.get("matched_label", ""),
        "status": status,
        "updated_at": now,
    }
    if status == "returned":
        record["alerted_at"] = now
    elif status == "messaged":
        record["messaged_at"] = now

    normalized_record = normalize_seen_record(record)
    if not normalized_record:
        return
    identity_key = normalized_record["identity_key"]
    history["records"][identity_key] = merge_seen_records(history["records"].get(identity_key), normalized_record)
    for key in dedupe_keys_for_listing(listing):
        history["key_map"][key] = identity_key


def mark_listing_seen(seen_history: dict, listing: dict, analysis: dict) -> None:
    store_seen_record(seen_history, listing, analysis, status="returned")


def mark_listing_messaged(seen_history: dict, reference: str) -> bool:
    history = ensure_seen_history_state(seen_history)
    stub = listing_stub_from_reference(reference)
    matched = False
    for key in dedupe_keys_for_listing(stub):
        identity_key = history.get("key_map", {}).get(key)
        if identity_key and identity_key in history.get("records", {}):
            existing = history["records"][identity_key]
            updated = dict(existing)
            updated["status"] = "messaged"
            updated["messaged_at"] = dt.datetime.now().isoformat(timespec="seconds")
            updated["updated_at"] = updated["messaged_at"]
            history["records"][identity_key] = updated
            matched = True
    if matched:
        return True
    if stub.get("platform") and stub.get("url"):
        store_seen_record(history, stub, {"matched_label": ""}, status="messaged")
        return True
    return False


def process_search_results(
    search: dict,
    listings: List[dict],
    catalog: dict,
    config: dict,
    state: dict,
    seen_history: dict,
    dry_run: bool,
) -> Dict[str, int]:
    summary = {"checked": 0, "matched": 0, "alerts": 0, "skipped": 0, "duplicates": 0}
    first_run = not state.get("first_run_completed", False)
    items = state.setdefault("items", {})
    allowed_categories = infer_allowed_categories(search)
    dedupe_enabled = bool(config.get("dedupe", {}).get("enabled", True))

    for listing in listings:
        summary["checked"] += 1
        existing_record = lookup_seen_record(seen_history, listing) if dedupe_enabled else None
        if existing_record and existing_record.get("status") in {"returned", "messaged"}:
            summary["duplicates"] += 1
            record_listing_state(items, listing, None)
            if config.get("debug", {}).get("log_dedupe", True):
                debug_log(
                    config,
                    f"Suppressed duplicate ({existing_record.get('status')}): {listing['platform']} {listing['title']}",
                )
            continue

        analysis = build_listing_analysis(listing, search, catalog, config, allowed_categories)
        if not analysis:
            summary["skipped"] += 1
            continue

        analysis["dedupe_status"] = "new"
        analysis["structured_reasoning"]["dedupe_status"] = "new"
        summary["matched"] += 1
        if listing["price"] > analysis["max_listing_price_to_alert_cad"]:
            analysis["final_return_decision"] = "above_alert_threshold"
            analysis["return_decision"] = "above_alert_threshold"
            analysis["structured_reasoning"]["return_decision"] = "above_alert_threshold"
            record_listing_state(items, listing, None)
            continue

        item_key = f"{listing['platform']}::{listing['id']}"
        previous = items.get(item_key, {})
        should_alert = True

        if first_run and not config.get("alert_existing_on_first_run", False):
            should_alert = False
        elif not dedupe_enabled and previous.get("last_alert_price") is not None and listing["price"] >= float(previous["last_alert_price"]):
            should_alert = False

        if should_alert:
            analysis["final_return_decision"] = "return_new_listing"
            analysis["return_decision"] = "return_new_listing"
            analysis["structured_reasoning"]["return_decision"] = "return_new_listing"
            alert_text = build_alert_text(search["name"], listing, analysis)
            if config.get("debug", {}).get("enabled", False):
                debug_log(
                    config,
                    f"Alert payload {listing['platform']} {listing['id']} | {truncate_debug_text(alert_text, 700)!r}",
                )
            log(
                f"Alert: {listing['platform']} | {format_price(listing['price'])} | "
                f"{listing['title']} | fair=${analysis['fair_price_from_sheet_cad']:.0f} | "
                f"max-buy=${analysis['maximum_buy_price_cad']:.0f} | {listing['url']}"
            )
            if dry_run or not telegram_is_configured(config):
                log("Telegram send skipped (dry run or Telegram not configured).")
            else:
                send_telegram_message(config, alert_text)
            summary["alerts"] += 1
            record_listing_state(items, listing, listing["price"])
            if dedupe_enabled:
                mark_listing_seen(seen_history, listing, analysis)
        else:
            analysis["final_return_decision"] = "suppressed_existing_first_run"
            analysis["return_decision"] = "suppressed_existing_first_run"
            analysis["structured_reasoning"]["return_decision"] = "suppressed_existing_first_run"
            record_listing_state(items, listing, previous.get("last_alert_price"))

    return summary


def create_facebook_session(config: dict) -> FacebookSession:
    profile_dir = BASE_DIR / config["facebook"]["profile_dir"]
    profile_dir.mkdir(parents=True, exist_ok=True)
    return FacebookSession(
        profile_dir=profile_dir,
        headless=bool(config["facebook"].get("headless", False)),
        timeout_seconds=int(config["facebook"].get("timeout_seconds", 30)),
        debug_enabled=bool(config.get("debug", {}).get("enabled", False)),
    )


def run_once(
    config: dict,
    catalog: dict,
    state: dict,
    seen_history: dict,
    dry_run: bool,
    facebook_session: Optional[FacebookSession] = None,
) -> None:
    facebook_searches = [
        search for search in config["searches"] if search.get("enabled", True) and search.get("platform", "").lower() == "facebook"
    ]
    kijiji_searches = [
        search for search in config["searches"] if search.get("enabled", True) and search.get("platform", "").lower() == "kijiji"
    ]

    created_facebook_session = False
    if facebook_searches and facebook_session is None:
        facebook_session = create_facebook_session(config)
        created_facebook_session = True

    try:
        for search in kijiji_searches:
            try:
                log(f"Checking {search['name']} (Kijiji)")
                listings = extract_kijiji_listings(
                    search["url"],
                    timeout_seconds=int(config["kijiji"].get("timeout_seconds", 25)),
                    max_pages=int(search.get("pages", config["kijiji"].get("pages_per_search", 2))),
                    fetch_details=bool(search.get("fetch_details", config["kijiji"].get("fetch_listing_details", True))),
                    detail_fetch_limit=int(search.get("detail_fetch_limit", config["kijiji"].get("detail_fetch_limit", 12))),
                    free_detail_fetch_limit=int(
                        search.get(
                            "free_detail_fetch_limit",
                            config["kijiji"].get("free_listing_detail_fetch_limit", 12),
                        )
                    ),
                )
                summary = process_search_results(search, listings, catalog, config, state, seen_history, dry_run)
                log(
                    f"{search['name']}: checked={summary['checked']} matched={summary['matched']} "
                    f"alerts={summary['alerts']} skipped={summary['skipped']} duplicates={summary['duplicates']}"
                )
            except Exception as exc:
                log(f"{search['name']} failed: {exc}")

        for search in facebook_searches:
            try:
                log(f"Checking {search['name']} (Facebook Marketplace)")
                listings = facebook_session.extract_search_results(
                    search["url"],
                    scroll_rounds=int(search.get("scroll_rounds", config["facebook"].get("scroll_rounds", 6))),
                    fetch_details=bool(search.get("fetch_details", config["facebook"].get("fetch_listing_details", False))),
                    detail_fetch_limit=int(search.get("detail_fetch_limit", config["facebook"].get("detail_fetch_limit", 4))),
                    free_detail_fetch_limit=int(
                        search.get(
                            "free_detail_fetch_limit",
                            config["facebook"].get("free_listing_detail_fetch_limit", 6),
                        )
                    ),
                )
                summary = process_search_results(search, listings, catalog, config, state, seen_history, dry_run)
                log(
                    f"{search['name']}: checked={summary['checked']} matched={summary['matched']} "
                    f"alerts={summary['alerts']} skipped={summary['skipped']} duplicates={summary['duplicates']}"
                )
            except Exception as exc:
                log(f"{search['name']} failed: {exc}")
    finally:
        if created_facebook_session and facebook_session:
            facebook_session.close()

    state["first_run_completed"] = True
    save_state(STATE_PATH, state)
    save_seen_history(dedupe_history_path(config), seen_history)


def watch_loop(dry_run: bool) -> None:
    facebook_session = None
    try:
        while True:
            config = load_config(CONFIG_PATH)
            catalog = load_price_catalog(PRICES_PATH, config["pricing"])
            state = load_state(STATE_PATH)
            seen_history = load_seen_history(dedupe_history_path(config))
            has_facebook_searches = any(
                search.get("enabled", True) and search.get("platform", "").lower() == "facebook"
                for search in config["searches"]
            )

            if has_facebook_searches and facebook_session is None:
                facebook_session = create_facebook_session(config)
            if not has_facebook_searches and facebook_session is not None:
                facebook_session.close()
                facebook_session = None

            try:
                run_once(
                    config,
                    catalog,
                    state,
                    seen_history,
                    dry_run=dry_run,
                    facebook_session=facebook_session,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log(f"Run failed: {exc}")

            sleep_seconds = max(1, int(float(config.get("poll_minutes", 45)) * 60))
            log(f"Sleeping for {sleep_seconds} seconds...")
            time.sleep(sleep_seconds)
    finally:
        if facebook_session:
            facebook_session.close()


def test_telegram() -> None:
    config = load_config(CONFIG_PATH)
    if not telegram_is_configured(config):
        raise SystemExit("Telegram is not configured yet in config.json.")
    send_telegram_message(config, f"Deal finder test message\nSent at: {timestamp()}")
    log("Test Telegram message sent.")


def setup_facebook_login() -> None:
    config = load_config(CONFIG_PATH)
    session = FacebookSession(
        profile_dir=BASE_DIR / config["facebook"]["profile_dir"],
        headless=False,
        timeout_seconds=int(config["facebook"].get("timeout_seconds", 30)),
    )
    try:
        session.setup_login()
    finally:
        session.close()
    log("Facebook session should now be saved in the local profile folder.")


def mark_messaged_reference(reference: str) -> None:
    config = load_config(CONFIG_PATH)
    seen_history = load_seen_history(dedupe_history_path(config))
    if not mark_listing_messaged(seen_history, reference):
        raise SystemExit("Could not match that listing reference. Pass the full listing URL if possible.")
    save_seen_history(dedupe_history_path(config), seen_history)
    log("Listing marked as messaged and will be suppressed in future runs.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local deal finder")
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    parser.add_argument("--run-now", action="store_true", help="Run one manual scan and exit")
    parser.add_argument("--watch", action="store_true", help="Keep checking on a loop")
    parser.add_argument("--dry-run", action="store_true", help="Do not send Telegram messages")
    parser.add_argument("--facebook-login", action="store_true", help="Open a browser so you can log into Facebook Marketplace once")
    parser.add_argument("--get-telegram-chat-id", action="store_true", help="Show chat IDs for your Telegram bot")
    parser.add_argument("--test-telegram", action="store_true", help="Send a test Telegram message")
    parser.add_argument("--mark-messaged", metavar="REF", help="Mark a listing URL as already messaged")
    parser.add_argument("--clear-seen", action="store_true", help="Clear the persistent dedupe history")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.facebook_login:
        setup_facebook_login()
        return

    if args.get_telegram_chat_id:
        config = load_config(CONFIG_PATH)
        print_chat_ids(config)
        return

    if args.test_telegram:
        test_telegram()
        return

    if args.mark_messaged:
        mark_messaged_reference(args.mark_messaged)
        return

    if args.clear_seen:
        config = load_config(CONFIG_PATH)
        clear_seen_history(dedupe_history_path(config))
        log("Seen-listing history cleared.")
        return

    if args.once or args.run_now:
        config = load_config(CONFIG_PATH)
        catalog = load_price_catalog(PRICES_PATH, config["pricing"])
        state = load_state(STATE_PATH)
        seen_history = load_seen_history(dedupe_history_path(config))
        run_once(config, catalog, state, seen_history, dry_run=args.dry_run)
        return

    if args.watch or not any(vars(args).values()):
        watch_loop(dry_run=args.dry_run)
        return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
