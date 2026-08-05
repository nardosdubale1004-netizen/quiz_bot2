from zoneinfo import ZoneInfo
from datetime import datetime, timezone as _dt_timezone
import difflib
import re as _re


# Primary IANA timezone per country — used to auto-derive a user's clock
# from the country they select, with no extra onboarding step.
COUNTRY_TZ_MAP = {
    "Afghanistan": "Asia/Kabul", "Albania": "Europe/Tirane", "Algeria": "Africa/Algiers",
    "Andorra": "Europe/Andorra", "Angola": "Africa/Luanda", "Argentina": "America/Argentina/Buenos_Aires",
    "Armenia": "Asia/Yerevan", "Australia": "Australia/Sydney", "Austria": "Europe/Vienna",
    "Azerbaijan": "Asia/Baku", "Bahamas": "America/Nassau", "Bahrain": "Asia/Bahrain",
    "Bangladesh": "Asia/Dhaka", "Barbados": "America/Barbados", "Belarus": "Europe/Minsk",
    "Belgium": "Europe/Brussels", "Belize": "America/Belize", "Benin": "Africa/Porto-Novo",
    "Bhutan": "Asia/Thimphu", "Bolivia": "America/La_Paz", "Bosnia and Herzegovina": "Europe/Sarajevo",
    "Botswana": "Africa/Gaborone", "Brazil": "America/Sao_Paulo", "Brunei": "Asia/Brunei",
    "Bulgaria": "Europe/Sofia", "Burkina Faso": "Africa/Ouagadougou", "Burundi": "Africa/Bujumbura",
    "Cambodia": "Asia/Phnom_Penh", "Cameroon": "Africa/Douala", "Canada": "America/Toronto",
    "Central African Republic": "Africa/Bangui", "Chad": "Africa/Ndjamena", "Chile": "America/Santiago",
    "China": "Asia/Shanghai", "Colombia": "America/Bogota", "Comoros": "Indian/Comoro",
    "Congo": "Africa/Brazzaville", "Costa Rica": "America/Costa_Rica", "Croatia": "Europe/Zagreb",
    "Cuba": "America/Havana", "Cyprus": "Asia/Nicosia", "Czech Republic": "Europe/Prague",
    "Denmark": "Europe/Copenhagen", "Djibouti": "Africa/Djibouti", "Dominica": "America/Dominica",
    "Dominican Republic": "America/Santo_Domingo", "Ecuador": "America/Guayaquil", "Egypt": "Africa/Cairo",
    "El Salvador": "America/El_Salvador", "Eritrea": "Africa/Asmara", "Estonia": "Europe/Tallinn",
    "Eswatini": "Africa/Mbabane", "Ethiopia": "Africa/Addis_Ababa", "Fiji": "Pacific/Fiji",
    "Finland": "Europe/Helsinki", "France": "Europe/Paris", "Gabon": "Africa/Libreville",
    "Gambia": "Africa/Banjul", "Georgia": "Asia/Tbilisi", "Germany": "Europe/Berlin",
    "Ghana": "Africa/Accra", "Greece": "Europe/Athens", "Grenada": "America/Grenada",
    "Guatemala": "America/Guatemala", "Guinea": "Africa/Conakry", "Guinea-Bissau": "Africa/Bissau",
    "Guyana": "America/Guyana", "Haiti": "America/Port-au-Prince", "Honduras": "America/Tegucigalpa",
    "Hungary": "Europe/Budapest", "Iceland": "Atlantic/Reykjavik", "India": "Asia/Kolkata",
    "Indonesia": "Asia/Jakarta", "Iran": "Asia/Tehran", "Iraq": "Asia/Baghdad",
    "Ireland": "Europe/Dublin", "Israel": "Asia/Jerusalem", "Italy": "Europe/Rome",
    "Ivory Coast": "Africa/Abidjan", "Jamaica": "America/Jamaica", "Japan": "Asia/Tokyo",
    "Jordan": "Asia/Amman", "Kazakhstan": "Asia/Almaty", "Kenya": "Africa/Nairobi",
    "Kiribati": "Pacific/Tarawa", "Kosovo": "Europe/Belgrade", "Kuwait": "Asia/Kuwait",
    "Kyrgyzstan": "Asia/Bishkek", "Laos": "Asia/Vientiane", "Latvia": "Europe/Riga",
    "Lebanon": "Asia/Beirut", "Lesotho": "Africa/Maseru", "Liberia": "Africa/Monrovia",
    "Libya": "Africa/Tripoli", "Liechtenstein": "Europe/Vaduz", "Lithuania": "Europe/Vilnius",
    "Luxembourg": "Europe/Luxembourg", "Madagascar": "Indian/Antananarivo", "Malawi": "Africa/Blantyre",
    "Malaysia": "Asia/Kuala_Lumpur", "Maldives": "Indian/Maldives", "Mali": "Africa/Bamako",
    "Malta": "Europe/Malta", "Mauritania": "Africa/Nouakchott", "Mauritius": "Indian/Mauritius",
    "Mexico": "America/Mexico_City", "Moldova": "Europe/Chisinau", "Monaco": "Europe/Monaco",
    "Mongolia": "Asia/Ulaanbaatar", "Montenegro": "Europe/Podgorica", "Morocco": "Africa/Casablanca",
    "Mozambique": "Africa/Maputo", "Myanmar": "Asia/Yangon", "Namibia": "Africa/Windhoek",
    "Nepal": "Asia/Kathmandu", "Netherlands": "Europe/Amsterdam", "New Zealand": "Pacific/Auckland",
    "Nicaragua": "America/Managua", "Niger": "Africa/Niamey", "Nigeria": "Africa/Lagos",
    "North Korea": "Asia/Pyongyang", "North Macedonia": "Europe/Skopje", "Norway": "Europe/Oslo",
    "Oman": "Asia/Muscat", "Pakistan": "Asia/Karachi", "Palau": "Pacific/Palau",
    "Palestine": "Asia/Gaza", "Panama": "America/Panama", "Papua New Guinea": "Pacific/Port_Moresby",
    "Paraguay": "America/Asuncion", "Peru": "America/Lima", "Philippines": "Asia/Manila",
    "Poland": "Europe/Warsaw", "Portugal": "Europe/Lisbon", "Qatar": "Asia/Qatar",
    "Romania": "Europe/Bucharest", "Russia": "Europe/Moscow", "Rwanda": "Africa/Kigali",
    "Saudi Arabia": "Asia/Riyadh", "Senegal": "Africa/Dakar", "Serbia": "Europe/Belgrade",
    "Seychelles": "Indian/Mahe", "Sierra Leone": "Africa/Freetown", "Singapore": "Asia/Singapore",
    "Slovakia": "Europe/Bratislava", "Slovenia": "Europe/Ljubljana", "Somalia": "Africa/Mogadishu",
    "South Africa": "Africa/Johannesburg", "South Korea": "Asia/Seoul", "South Sudan": "Africa/Juba",
    "Spain": "Europe/Madrid", "Sri Lanka": "Asia/Colombo", "Sudan": "Africa/Khartoum",
    "Suriname": "America/Paramaribo", "Sweden": "Europe/Stockholm", "Switzerland": "Europe/Zurich",
    "Syria": "Asia/Damascus", "Taiwan": "Asia/Taipei", "Tajikistan": "Asia/Dushanbe",
    "Tanzania": "Africa/Dar_es_Salaam", "Thailand": "Asia/Bangkok", "Togo": "Africa/Lome",
    "Tonga": "Pacific/Tongatapu", "Trinidad and Tobago": "America/Port_of_Spain", "Tunisia": "Africa/Tunis",
    "Turkey": "Europe/Istanbul", "Turkmenistan": "Asia/Ashgabat", "Uganda": "Africa/Kampala",
    "Ukraine": "Europe/Kyiv", "United Arab Emirates": "Asia/Dubai", "United Kingdom": "Europe/London",
    "United States": "America/New_York", "Uruguay": "America/Montevideo", "Uzbekistan": "Asia/Tashkent",
    "Vanuatu": "Pacific/Efate", "Vatican City": "Europe/Vatican", "Venezuela": "America/Caracas",
    "Vietnam": "Asia/Ho_Chi_Minh", "Yemen": "Asia/Aden", "Zambia": "Africa/Lusaka", "Zimbabwe": "Africa/Harare",
}

COUNTRY_NAMES = sorted(COUNTRY_TZ_MAP.keys())

def get_timezone_for_country(country: str) -> str:
    return COUNTRY_TZ_MAP.get((country or "").strip(), "UTC")


def format_local_time(dt, tz_name: str = "UTC", fmt: str = "%b %d, %Y · %H:%M") -> str:
    """Renders a UTC-aware/naive datetime in the viewer's local timezone, DM-facing only.
    Channel-facing timestamps (tournament cards, etc.) intentionally keep showing UTC/EAT
    side-by-side and must NOT call this."""
    if not dt:
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except Exception:
            return str(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt_timezone.utc)
    try:
        local_dt = dt.astimezone(ZoneInfo(tz_name or "UTC"))
    except Exception:
        local_dt = dt.astimezone(_dt_timezone.utc)
    tz_label = local_dt.strftime("%Z") or (tz_name or "UTC")
    return f"{local_dt.strftime(fmt)} {tz_label}"

def normalize_location_name(name: str) -> str:
    """Lowercase, strip punctuation/whitespace so 'Addis-Ababa' == 'addis ababa' == 'ADDIS_ABABA'."""
    if not name:
        return ""
    cleaned = _re.sub(r'[^\w\s]', '', name.lower()).strip()
    return _re.sub(r'\s+', ' ', cleaned)


def find_close_match(candidate: str, known_list: list, cutoff: float = 0.82) -> str or None:
    """Returns the known_list entry closest to `candidate`, or None if nothing is close
    enough to be confident it's the same place rather than a genuinely new one."""
    if not candidate or not known_list:
        return None
    norm_candidate = normalize_location_name(candidate)
    norm_map = {normalize_location_name(k): k for k in known_list}
    if norm_candidate in norm_map:
        return norm_map[norm_candidate]
    matches = difflib.get_close_matches(norm_candidate, list(norm_map.keys()), n=1, cutoff=cutoff)
    return norm_map[matches[0]] if matches else None

def normalize_country_input(raw: str):
    """Matches freely-typed country text against the canonical COUNTRY_NAMES list.
    Returns (normalized_name, is_exact): is_exact=True only when the input matched a
    known country exactly (after normalization). On a close-but-not-exact match, returns
    the canonical name with is_exact=False so the caller can show a 'matched to...' note.
    If nothing is close enough, returns the cleaned, title-cased input as a new candidate."""
    if not raw:
        return "", False
    cleaned = raw.strip()
    norm_input = normalize_location_name(cleaned)
    norm_map = {normalize_location_name(c): c for c in COUNTRY_NAMES}

    if norm_input in norm_map:
        return norm_map[norm_input], True

    matches = difflib.get_close_matches(norm_input, list(norm_map.keys()), n=1, cutoff=0.78)
    if matches:
        return norm_map[matches[0]], False

    return cleaned.title(), False