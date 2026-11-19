"""Stage: article parsing.

Cleans text, labels the content type (report / official / explainer /
analysis / opinion / promotional), and tags topics, a provisional
category and named entities. Rules are deliberately transparent; the
summariser can later refine the category with more context.
"""
from __future__ import annotations

import re

from ..config import registry
from ..models import Article

PROMO_MARKERS = [r"open (a )?free demat", r"\bsponsored\b", r"partner content", r"\bbuy now\b",
                 r"limited[- ]time offer", r"sign up (now|today)", r"\bbrandspotlight\b"]
OPINION_URL = [r"/opinion/", r"/opinions/", r"/dispatches/", r"/blogs?/", r"/columns?/", r"/editorial"]
EXPLAINER_TITLE = [r"^(what|why|how|who)\b.*\?$", r"\bexplained\b", r"^explainer\b"]
ANALYSIS_TITLE = [r'^["“].+["”]:\s.*\b(casts doubt|says|warns|argues)\b']

TOPIC_LEXICON: dict[str, list[str]] = {
    "geopolitics": [r"\bapec\b", r"\btrilateral\b", r"\bsummit\b", r"\bputin\b", r"\bxi\b", r"\bkremlin\b", r"\bnato\b"],
    "conflict": [r"\bwar\b", r"\bstrikes?\b", r"\bmissile\b", r"\bdrone\b", r"\btanker\b", r"\bblockade\b", r"\battacks?\b"],
    "diplomacy": [r"\btalks\b", r"\bbilateral\b", r"\bforeign minist", r"\bdiplomatic\b"],
    "gulf": [r"\bgulf\b", r"\bhormuz\b", r"\biran\w*\b", r"\bbahrain\b", r"\bkuwait\b", r"\buae\b"],
    "us": [r"\btrump\b", r"\bwhite house\b", r"\bsupreme court\b", r"\bcongress\b", r"\bfederal reserve\b", r"\bus\b"],
    "courts": [r"\bsupreme court\b", r"\bappeals? court\b", r"\bcircuit\b", r"\bjustices?\b", r"\bruling\b"],
    "politics": [r"\badministration\b", r"\bmidterms?\b", r"\bgop\b", r"\bpolicy\b"],
    "central-banks": [r"\bfed\b", r"\bfederal reserve\b", r"\bfomc\b", r"\brbi\b", r"\bmpc\b", r"\becb\b"],
    "rates": [r"\brate hikes?\b", r"\binterest rates?\b", r"\btightening\b", r"\brate cuts?\b"],
    "inflation": [r"\binflation\b", r"\bcpi\b", r"\bconsumer price"],
    "economy": [r"\beconom\w+\b", r"\bgdp\b", r"\binflation\b", r"\bfiscal\b"],
    "markets": [r"\bsensex\b", r"\bnifty\b", r"\bstock market\b", r"\bshares?\b", r"\bbrent\b", r"\bwti\b", r"\bbenchmark\b", r"\bper share\b", r"\bbond\b"],
    "oil": [r"\bcrude\b", r"\bbrent\b", r"\bwti\b", r"\boil prices?\b", r"\bbarrels?\b"],
    "energy": [r"\boil\b", r"\bfossil fuels?\b", r"\bgas\b", r"\benergy\b"],
    "earnings": [r"\brevenue\b", r"\bquarter\b", r"\bearnings\b", r"\bprofit\b", r"\bguidance\b"],
    "corporate": [r"\bceo\b", r"\bboard\b", r"\bchairman\b", r"\bresign\w*\b", r"\binterim\b"],
    "business": [r"\bcompany\b", r"\bfirm\b", r"\bcorporat\w+\b", r"\bnvidia\b"],
    "semiconductors": [r"\bnvidia\b", r"\bchips?\b", r"\bsemiconductor\w*\b", r"\bgpu\b", r"\bdata center\b"],
    "ai": [r"\bai\b", r"\bartificial intelligence\b", r"\bvera rubin\b", r"\bmachine learning\b"],
    "technology": [r"\btech\w*\b", r"\bsoftware\b", r"\bsaas\b", r"\bnvidia\b"],
    "startups": [r"\bstartups?\b", r"\bseed\b", r"\bseries [a-d]\b", r"\braised? \$", r"\bunicorn\b", r"\bqip\b", r"\bdrhp\b"],
    "venture": [r"\baccel\b", r"\bventure\b", r"\bfunding round\b", r"\braised?\b.*\bmillion\b"],
    "ipo": [r"\bipo\b", r"\bdrhp\b", r"\bpublic offer\b", r"\bfresh issue\b", r"\boffer for sale\b"],
    "climate": [r"\bcop\d+\b", r"\bclimate\b", r"\bemissions?\b", r"\bmethane\b", r"\bunfccc\b"],
    "pollution": [r"\baqi\b", r"\bair quality\b", r"\bpm2\.5\b", r"\bpollution\b", r"\bgrap\b"],
    "health": [r"\bhealth\b", r"\bebola\b", r"\bwho\b", r"\bdisease\b", r"\bobesity\b", r"\bdrugs?\b", r"\bantimicrobials?\b", r"\bepidemic\b", r"\boutbreak\b"],
    "public-health": [r"\boutbreak\b", r"\bepidemic\b", r"\bemergency committee\b", r"\bcases\b.*\bdeaths\b", r"\bpandemic\b"],
    "drug-regulation": [r"\bclinical trials rules\b", r"\bbulk drugs\b", r"\blicensing framework\b", r"\bantimicrobials?\b"],
    "education": [r"\buniversit\w+\b", r"\bschools?\b", r"\bexam\b", r"\bcat 20\d\d\b", r"\badmit card\b", r"\bresearchers?\b"],
    "universities": [r"\buniversity of\b", r"\buniversities\b", r"\bcampus\b"],
    "research": [r"\bresearch grants?\b", r"\bresearchers\b", r"\bstudy\b"],
    "careers": [r"\bjobs?\b", r"\bhiring\b", r"\blayoffs?\b", r"\brecruit\w+\b", r"\bexam\b"],
    "science": [r"\bscien\w+\b", r"\bnasa\b", r"\bisro\b", r"\bspace\b", r"\bsatellite\b"],
    "space": [r"\bnasa\b", r"\bisro\b", r"\blaunch vehicle\b", r"\borbit\b"],
    "india": [r"\bindia\w*\b", r"\bdelhi\b", r"\bsensex\b", r"\bnifty\b", r"\brbi\b", r"\bsebi\b", r"\bunion\b.*\bministry\b", r"₹", r"\bcrore\b", r"\bmumbai\b"],
    "delhi": [r"\bdelhi\b", r"\bncr\b"],
    "world": [r"\bunited nations\b", r"\bun\b", r"\bglobal\b", r"\bworldwide\b", r"\bcongo\b", r"\bturk\w+\b", r"\bchina\b", r"\brussia\b"],
    "trade": [r"\btariffs?\b", r"\bfree trade\b", r"\bexports?\b", r"\btrade truce\b"],
    "sports": [r"\bcricket\b", r"\bfootball\b", r"\bworld cup\b", r"\bt20\b"],
    "entertainment": [r"\bfilm\b", r"\bbox office\b", r"\bcelebrity\b", r"\bnetflix\b"],
}

# Category picked from topics, first match wins (order = priority).
CATEGORY_RULES: list[tuple[str, set[str]]] = [
    ("Climate & Environment", {"climate", "pollution"}),
    ("Health", {"public-health", "drug-regulation"}),
    ("Markets & Finance", {"central-banks", "oil", "ipo"}),
    ("Startups", {"startups", "venture"}),
    ("Technology & AI", {"semiconductors", "ai"}),
    ("World", {"geopolitics", "conflict", "gulf"}),
    ("Politics & Policy", {"courts", "politics"}),
    ("Business & Economy", {"economy", "earnings", "corporate"}),
    ("Markets & Finance", {"markets"}),
    ("Education & Careers", {"education", "careers"}),
    ("Health", {"health"}),
    ("Science", {"science", "space"}),
    ("Sports", {"sports"}),
    ("Entertainment", {"entertainment"}),
]

STOP_CAPS = {"The", "A", "An", "In", "On", "At", "Of", "And", "But", "For", "With", "As", "By", "To", "Is",
             "It", "Its", "This", "That", "After", "Before", "While", "During", "From", "Over", "Under",
             "Live", "Updates", "Opinion", "Explainer", "What", "Why", "How", "Who", "Nov", "November",
             "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Stage", "Stages"}


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def content_type(a: Article) -> str:
    text = f"{a.title} {a.extract}".lower()
    src = registry().get(a.source_id)
    if any(re.search(p, text) for p in PROMO_MARKERS):
        return "promotional"
    if src and src.kind == "think_tank":
        return "opinion"
    if any(re.search(p, a.url.lower()) for p in OPINION_URL) or text.startswith("opinion"):
        return "opinion"
    if any(re.search(p, a.title.strip(), re.I) for p in ANALYSIS_TITLE):
        return "analysis"
    if src and src.kind in {"primary", "company"}:
        return "official"
    if any(re.search(p, a.title.strip().lower()) for p in EXPLAINER_TITLE) or a.extract.lower().startswith("explainer"):
        return "explainer"
    return "report"


def tag_topics(text: str) -> list[str]:
    low = text.lower()
    return [t for t, pats in TOPIC_LEXICON.items() if any(re.search(p, low) for p in pats)]


def category_for(topics: list[str]) -> str:
    ts = set(topics)
    for cat, keys in CATEGORY_RULES:
        if ts & keys:
            return cat
    return "World"


def entities(text: str) -> set[str]:
    found = set()
    for m in re.finditer(r"\b([A-Z][a-zA-Z\-]+(?:\s+[A-Z][a-zA-Z\-]+)*|[A-Z]{2,}\d*)\b", text):
        phrase = m.group(1)
        words = [w for w in phrase.split() if w not in STOP_CAPS]
        for w in words:
            if len(w) > 1:
                found.add(w.lower())
    return found


def parse(a: Article) -> Article:
    if a.rejected:
        return a
    a.title, a.extract = clean(a.title), clean(a.extract)
    a.content_type = content_type(a)
    body = f"{a.title}. {a.extract}"
    a.topics = tag_topics(body)
    a.category = category_for(a.topics)
    a.entities = entities(body)
    return a
