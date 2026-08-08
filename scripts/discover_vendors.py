#!/usr/bin/env python3
"""Deterministic vendor discovery from the deals intel feed (P2: auto-extend).

Reads site/data/deals.json (LEB + LET RSS intel, refreshed every live round),
extracts candidate vendor names that are NOT yet covered by providers.yaml,
and emits a candidates.json for the AI step to build target definitions.

No AI here: pure rules so the candidate list is stable and auditable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

# Terms that mark a deal as VPS/hosting related (title context).
VPS_MARKERS = (
    "vps", "kvm", "hosting", "host", "cloud", "server", "ryzen", "epyc",
    "xeon", "nvme", "sale", "promo", "coupon", "discount", "off", "annual",
    "year", "month", "rack", "vds",
)

# Terms that mark a deal as NOT a VPS vendor (dedicated/web/domain/others).
EXCLUDE_MARKERS = (
    "dedicated server", "bare metal", "web hosting", "shared hosting",
    "domain", "ssl", "cdn", "rdp", "vpn", "colocation", "email hosting",
    "windows", "office", "licen", "wordpress", "cloudflare", "oracle",
    "tribblix", "yabs", "lets encrypt", "proxmox", "whmcs", "cpanel",
)

# Generic tokens that are never vendor brands (geography, time, adjectives).
STOPWORDS = {
    "vps", "kvm", "the", "a", "an", "new", "big", "sale", "promo", "deal",
    "host", "cloud", "server", "this", "only", "from", "month", "year", "end",
    "top", "black", "friday", "let", "why", "is", "are", "your", "look", "need",
    "get", "free", "best", "full", "amd", "intel", "ryzen", "epyc", "xeon",
    "angeles", "los", "apac", "arizona", "chicago", "iowa", "tokyo", "hong",
    "kong", "singapore", "dallas", "newyork", "seattle", "frankfurt", "london",
    "paris", "amsterdam", "warsaw", "moscow", "russia", "uk", "usa", "eu",
    "taiwan", "vietnam", "india", "korea", "japan", "china", "canada",
    "australia", "germany", "france", "spain", "italy", "brazil", "mexico",
    "benchmark", "benchmarks", "love", "loving", "early", "coupons", "coupon",
    "deals", "deal", "double", "gbps", "monthly", "annual", "annually",
    "starting", "started", "starts", "ending", "ends", "expires", "expiring",
    "restock", "restocked", "restocking", "protected", "protection",
    "exclusive", "flash", "special", "promotion", "promotional", "introductory",
    "limited", "unlimited", "unmetered", "metered", "guaranteed", "sla",
    "uptime", "downtime", "location", "locations", "datacenter", "datacenters",
    "data-center", "network", "networks", "uplink", "downlink", "throughput",
    "ddos", "choose", "choosing", "jersey", "newjersey", "nvme", "ssd", "hdd",
    "16gb", "32gb", "64gb", "128gb", "self", "selfhosting", "yourself",
    "someone", "anybody", "nobody", "everybody", "ours", "yours", "theirs",
    "servers", "server", "services", "service", "miami", "4545p", "9950x",
    "7713", "9334", "9004", "ryzen9", "epyc", "xeon", "i9", "i7", "i5",
    "processors", "processor", "cores", "core", "ghz", "tb", "gb", "mb", "kb",
    "telegram", "urgent", "man", "men", "guy", "guys", "folks", "people",
    "utah", "vds", "with", "without", "within", "during", "before", "after",
    "back", "bonus", "budget", "category", "bandwidth", "b50", "black", "flash",
    "flashsale", "legacy", "offer", "offers", "special", "premium", "ultra",
    "standard", "basic", "pro", "max", "mini", "plus", "lite", "light", "heavy",
    "solid", "great", "exclusive", "anniversary", "returns", "return", "welcome",
    "hello", "intro", "introducing", "happy", "celebrate", "celebration",
    "important", "notice", "update", "updates", "status", "maintenance",
    "outage", "down", "issue", "issues", "problem", "problems", "error",
    "review", "reviews", "thread", "topic", "post", "question", "questions",
    "help", "request", "requests", "anyone", "everyone", "someone", "whose",
    "what", "which", "when", "where", "how", "who", "why", "does", "do", "did",
    "have", "has", "had", "can", "could", "should", "would", "will", "want",
    "wanted", "need", "needed", "looking", "searching", "find", "found",
    "using", "used", "use", "make", "made", "work", "works", "working",
    "good", "bad", "worst", "better", "best", "cheap", "cheapest", "expensive",
    "affordable", "reliable", "fast", "slow", "high", "low", "medium",
    "small", "large", "first", "second", "last", "next", "current", "old",
    "coming", "gone", "dead", "alive", "live", "test", "testing", "tested",
    "beta", "alpha", "final", "latest", "recent", "recently", "today",
    "tomorrow", "yesterday", "now", "soon", "later", "earlier", "lastly",
    "finally", "start", "started", "stop", "stopped", "running", "runs",
    "run", "again", "once", "twice", "never", "always", "sometimes", "often",
    "really", "actually", "just", "still", "already", "yet", "also", "even",
    "well", "much", "many", "more", "most", "less", "few", "little", "lot",
    "lots", "some", "any", "all", "each", "every", "other", "another", "both",
    "either", "neither", "none", "nothing", "everything", "something",
    "anything", "nothing", "total", "perfect", "amazing", "awesome", "crazy",
    "insane", "steal", "bargain", "value", "price", "prices", "pricing",
    "cost", "costs", "pay", "paid", "payment", "billing", "invoice", "bill",
    "order", "orders", "checkout", "cart", "buy", "bought", "purchase",
    "subscribe", "subscription", "renew", "renewal", "renewals", "expire",
    "expired", "expiration", "expiry", "plan", "plans", "package", "packages",
    "tier", "tiers", "level", "levels", "option", "options", "choice", "choices",
    "limit", "limits", "limited", "unlimited", "unmetered", "metered",
    "bandwidth", "traffic", "data", "storage", "disk", "memory", "ram", "cpu",
    "cores", "core", "threads", "thread", "ghz", "tb", "gb", "mb", "kb", "hz",
    "ip", "ips", "ipv4", "ipv6", "port", "ports", "speed", "speeds", "latency",
    "uptime", "downtime", "sla", "support", "ticket", "tickets", "response",
    "customer", "customers", "client", "clients", "user", "users", "member",
    "members", "vendor", "vendors", "provider", "providers", "company",
    "companies", "business", "businesses", "enterprise", "corporate", "b2b",
    "openvpn", "wireguard", "v2ray", "trojan", "ss", "ssr", "vmess", "vless",
    "cdp", "rdp", "vpn", "proxy", "proxies", "dns", "domain", "domains",
    "hosting", "webhosting", "web", "site", "sites", "website", "websites",
    "blog", "blogs", "forum", "forums", "wiki", "docs", "doc", "manual",
    "guide", "guides", "tutorial", "tutorials", "faq", "about", "contact",
    "privacy", "terms", "policy", "policies", "legal", "license", "licenses",
    "licensing", "copyright", "trademark", "brand", "brands", "logo", "logos",
    "name", "names", "title", "titles", "headline", "headlines", "tagline",
    "slogan", "slogans", "motto", "mission", "vision", "goal", "goals",
    "feature", "features", "benefit", "benefits", "advantage", "advantages",
    "disadvantage", "disadvantages", "pros", "cons", "comparison", "compare",
    "compared", "comparing", "versus", "vs", "against", "alternative",
    "alternatives", "replacement", "replacements", "substitute", "substitutes",
    "upgrade", "upgrades", "upgraded", "downgrade", "downgrades", "migration",
    "migrate", "migrated", "migrating", "transfer", "transfers", "transferred",
    "setup", "setups", "install", "installs", "installed", "installing",
    "config", "configs", "configuration", "configurations", "configure",
    "configured", "deploy", "deploys", "deployed", "deploying", "launch",
    "launches", "launched", "launching", "release", "releases", "released",
    "releasing", "version", "versions", "build", "builds", "builds",
    "update", "updated", "updating", "patch", "patches", "patched",
    "fix", "fixes", "fixed", "fixing", "bug", "bugs", "fix", "bugfix",
    "hotfix", "security", "secure", "secured", "vulnerability",
    "vulnerabilities", "exploit", "exploits", "exploited", "hack", "hacks",
    "hacked", "hacking", "attack", "attacks", "attacked", "breach", "breaches",
    "breached", "leak", "leaks", "leaked", "leaking", "data breach",
    "incident", "incidents", "event", "events", "alert", "alerts", "warning",
    "warnings", "notice", "notices", "announcement", "announcements",
    "announced", "news", "press", "media", "article", "articles", "story",
    "stories", "report", "reports", "reported", "reporting", "research",
    "researched", "study", "studies", "analysis", "analyses", "analytics",
    "data", "information", "info", "details", "detail", "spec", "specs",
    "specification", "specifications", "summary", "summaries", "overview",
    "general", "generic", "specific", "specifics", "particular", "particulars",
    "certain", "some", "such", "like", "including", "includes", "included",
    "including", "excluding", "excludes", "excluded", "except", "excepts",
    "besides", "beyond", "above", "below", "under", "over", "around", "about",
    "through", "throughout", "between", "among", "within", "without",
    "outside", "inside", "before", "after", "during", "since", "until",
    "unless", "while", "whereas", "although", "though", "even though",
    "however", "therefore", "thus", "hence", "consequently", "accordingly",
    "meanwhile", "instead", "rather", "otherwise", "furthermore", "moreover",
    "additionally", "besides", "in addition", "on the other hand", "in contrast",
    "in comparison", "similarly", "likewise", "equally", "notably", "particularly",
    "especially", "specifically", "mainly", "mostly", "primarily", "largely",
    "partly", "partially", "fully", "completely", "totally", "entirely",
    "absolutely", "definitely", "certainly", "surely", "probably", "possibly",
    "maybe", "perhaps", "likely", "unlikely", "apparently", "evidently",
    "obviously", "clearly", "plainly", "simply", "easily", "hardly", "barely",
    "scarcely", "merely", "purely", "strictly", "exactly", "precisely",
    "roughly", "approximately", "nearly", "almost", "practically", "virtually",
    "essentially", "basically", "fundamentally", "ultimately", "eventually",
    "finally", "initially", "originally", "previously", "subsequently",
    "consequently", "subsequently", "afterwards", "beforehand", "meanwhile",
    "contemporary", "current", "present", "previous", "following", "subsequent",
    "prior", "latter", "former", "later", "earlier", "sooner", "later",
    "delayed", "delays", "postponed", "postponing", "reschedule", "rescheduled",
    "cancelled", "canceled", "cancelling", "cancel", "cancels", "cancellation",
    "cancellations", "refund", "refunds", "refunded", "refunding", "chargeback",
    "chargebacks", "dispute", "disputes", "disputed", "complaint", "complaints",
    "complained", "feedback", "feedbacks", "testimonial", "testimonials",
    "praise", "praises", "praised", "criticism", "criticisms", "criticized",
    "critique", "critiques", "negative", "positive", "neutral", "mixed",
    "overall", "average", "below average", "above average", "excellent",
    "outstanding", "remarkable", "notable", "impressive", "impressed",
    "disappointed", "disappointing", "frustrated", "frustrating", "annoyed",
    "annoying", "angry", "mad", "upset", "happy", "glad", "pleased", "satisfied",
    "unsatisfied", "content", "discontent", "delighted", "thrilled", "excited",
    "excitement", "interest", "interested", "interesting", "boring", "bored",
    "confusing", "confused", "clear", "unclear", "ambiguous", "vague", "precise",
    "accurate", "inaccurate", "correct", "incorrect", "right", "wrong", "true",
    "false", "valid", "invalid", "verified", "unverified", "confirmed",
    "unconfirmed", "official", "unofficial", "formal", "informal", "casual",
    "serious", "light", "heavy", "hard", "soft", "easy", "difficult",
    "challenging", "complicated", "complex", "simple", "basic", "advanced",
    "intermediate", "beginner", "expert", "professional", "amateur", "novice",
    "rookie", "veteran", "senior", "junior", "middle", "entry", "entry-level",
    "high-end", "low-end", "mid-range", "top-tier", "bottom-tier", "best-selling",
    "top-rated", "highly-rated", "well-known", "famous", "popular", "unpopular",
    "common", "uncommon", "rare", "unusual", "typical", "atypical", "normal",
    "abnormal", "regular", "irregular", "standard", "non-standard", "custom",
    "customized", "customizable", "tailored", "bespoke", "made-to-order",
    "pre-made", "off-the-shelf", "out-of-the-box", "ready-made", "handmade",
    "homemade", "homegrown", "in-house", "outsourced", "offshore", "onshore",
    "domestic", "foreign", "international", "global", "worldwide", "local",
    "regional", "nationwide", "countrywide", "citywide", "statewide", "world",
    "planet", "globe", "universe", "galaxy", "cosmos", "space", "sky", "earth",
    "land", "sea", "ocean", "river", "mountain", "valley", "desert", "forest",
    "island", "continent", "country", "county", "province", "state", "city",
    "town", "village", "capital", "metropolis", "megalopolis", "suburb",
    "suburbs", "neighborhood", "district", "region", "area", "zone", "sector",
    "quarter", "precinct", "ward", "parish", "territory", "colony", "empire",
    "kingdom", "republic", "union", "federation", "confederation", "alliance",
    "league", "coalition", "partnership", "association", "society", "club",
    "group", "team", "crew", "staff", "faculty", "department", "division",
    "branch", "office", "headquarters", "base", "camp", "site", "location",
    "venue", "place", "spot", "point", "position", "post", "station", "terminal",
    "depot", "warehouse", "factory", "plant", "mill", "foundry", "workshop",
    "studio", "lab", "laboratory", "center", "centre", "institute", "academy",
    "school", "college", "university", "campus", "library", "museum", "gallery",
    "theater", "theatre", "cinema", "stadium", "arena", "field", "court",
    "track", "pool", "gym", "gymnasium", "park", "garden", "zoo", "aquarium",
    "farm", "ranch", "estate", "manor", "castle", "palace", "fort", "fortress",
    "tower", "bridge", "tunnel", "road", "street", "avenue", "boulevard",
    "highway", "freeway", "expressway", "interstate", "turnpike", "lane",
    "alley", "path", "trail", "route", "way", "direction", "course", "track",
    "orbit", "trajectory", "pathway", "corridor", "passage", "entrance", "exit",
    "gate", "door", "window", "wall", "floor", "ceiling", "roof", "basement",
    "attic", "garage", "porch", "patio", "deck", "balcony", "terrace", "courtyard",
    "hall", "lobby", "reception", "waiting room", "conference room", "meeting room",
    "boardroom", "classroom", "auditorium", "amphitheater", "chapel", "cathedral",
    "church", "temple", "mosque", "synagogue", "shrine", "monument", "memorial",
    "statue", "sculpture", "painting", "drawing", "photograph", "photo", "picture",
    "image", "icon", "symbol", "emblem", "insignia", "badge", "crest", "seal",
    "stamp", "mark", "marker", "sign", "signal", "signal", "flag", "banner",
    "poster", "billboard", "advertisement", "ad", "ads", "commercial", "commercials",
    "sponsor", "sponsors", "sponsored", "promotion", "promotions", "campaign",
    "campaigns", "marketing", "advertising", "publicity", "public relations", "pr",
    "social media", "facebook", "twitter", "instagram", "linkedin", "youtube",
    "tiktok", "snapchat", "pinterest", "reddit", "quora", "medium", "blogspot",
    "wordpress", "github", "gitlab", "bitbucket", "stackoverflow", "stackexchange",
    "wikipedia", "wikimedia", "google", "yahoo", "bing", "duckduckgo", "brave",
    "firefox", "chrome", "safari", "opera", "edge", "internet explorer", "netscape",
    "mosaic", "lynx", "wget", "curl", "telnet", "ssh", "ftp", "sftp", "scp",
    "rsync", "http", "https", "url", "uri", "link", "links", "hyperlink", "links",
    "webpage", "webpages", "webmaster", "webmasters", "webdesign", "webdesigner",
    "webdeveloper", "webdevelopment", "frontend", "backend", "fullstack", "full-stack",
    "devops", "sysadmin", "sysadmins", "administrator", "administrators", "admin",
    "admins", "moderator", "moderators", "mod", "mods", "owner", "owners", "founder",
    "founders", "co-founder", "cofounder", "ceo", "cto", "cfo", "coo", "cio", "cmo",
    "director", "directors", "manager", "managers", "supervisor", "supervisors",
    "coordinator", "coordinators", "assistant", "assistants", "secretary", "secretaries",
    "treasurer", "accountant", "accountants", "bookkeeper", "bookkeepers", "auditor",
    "auditors", "lawyer", "lawyers", "attorney", "attorneys", "consultant", "consultants",
    "advisor", "advisors", "analyst", "analysts", "specialist", "specialists", "expert",
    "experts", "professional", "professionals", "contractor", "contractors", "freelancer",
    "freelancers", "employee", "employees", "worker", "workers", "laborer", "laborers",
    "technician", "technicians", "engineer", "engineers", "developer", "developers",
    "programmer", "programmers", "coder", "coders", "architect", "architects", "designer",
    "designers", "artist", "artists", "writer", "writers", "author", "authors", "editor",
    "editors", "journalist", "journalists", "reporter", "reporters", "photographer",
    "photographers", "videographer", "videographers", "musician", "musicians", "singer",
    "singers", "actor", "actors", "actress", "actresses", "director", "directors",
    "producer", "producers", "broadcaster", "broadcasters", "anchor", "anchors", "host",
    "hosts", "co-host", "cohost", "guest", "guests", "participant", "participants",
    "attendee", "attendees", "member", "members", "subscriber", "subscribers", "follower",
    "followers", "fan", "fans", "supporter", "supporters", "donor", "donors", "sponsor",
    "sponsors", "partner", "partners", "collaborator", "collaborators", "competitor",
    "competitors", "rival", "rivals", "opponent", "opponents", "enemy", "enemies", "ally",
    "allies", "friend", "friends", "colleague", "colleagues", "associate", "associates",
    "acquaintance", "acquaintances", "relative", "relatives", "family", "families",
    "parent", "parents", "child", "children", "kid", "kids", "baby", "babies", "infant",
    "infants", "toddler", "toddlers", "teen", "teens", "teenager", "teenagers", "adult",
    "adults", "elder", "elders", "senior", "seniors", "elderly", "youth", "young",
    "old", "older", "oldest", "younger", "youngest", "middle-aged", "age", "ages",
    "generation", "generations", "era", "eras", "epoch", "epochs", "period", "periods",
    "time", "times", "moment", "moments", "instant", "instants", "second", "seconds",
    "minute", "minutes", "hour", "hours", "day", "days", "week", "weeks", "fortnight",
    "fortnights", "month", "months", "quarter", "quarters", "year", "years", "decade",
    "decades", "century", "centuries", "millennium", "millennia", "season", "seasons",
    "spring", "summer", "autumn", "fall", "winter", "morning", "afternoon", "evening",
    "night", "nights", "midnight", "noon", "dawn", "dusk", "sunrise", "sunset",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "weekday", "weekend", "holiday",
    "holidays", "vacation", "vacations", "break", "breaks", "festival", "festivals",
    "celebration", "celebrations", "anniversary", "anniversaries", "birthday",
    "birthdays", "new year", "new years", "christmas", "easter", "halloween",
    "thanksgiving", "valentine", "valentines", "st patrick", "independence day",
    "memorial day", "labor day", "columbus day", "veterans day", "remembrance day",
    "boxing day", "cyber monday", "black friday", "prime day", "singles day",
    "double eleven", "618", "1111", "999", "520", "214",
}

BRAND_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{2,23}\b")


def load_deals(site_dir: Path) -> list[dict[str, Any]]:
    deals_path = site_dir / "data" / "deals.json"
    if not deals_path.exists():
        return []
    try:
        data = json.loads(deals_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("entries", [])


def load_existing_providers(providers_path: Path) -> tuple[set[str], set[str]]:
    """Return (provider names, expected_domains) already tracked."""
    config = yaml.safe_load(providers_path.read_text(encoding="utf-8"))
    providers: set[str] = set()
    domains: set[str] = set()
    for target in config.get("targets", []):
        providers.add(str(target.get("provider", "")).casefold())
        for domain in target.get("expected_domains", []):
            domains.add(str(domain).casefold())
    return providers, domains


def extract_vendor_candidates(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Extract vendor brands from deal link slugs (high precision).

    LET discussion links embed the brand in the slug (/discussion/<id>/<brand>-...),
    LEB blog links too (/blog/<brand>-...). Title words are only a fallback.
    """
    candidates: dict[str, dict[str, str]] = {}
    for entry in entries:
        title = str(entry.get("title", ""))
        source = str(entry.get("source", "unknown"))
        link = str(entry.get("link", ""))
        lowered_title = title.casefold()
        if any(marker in lowered_title for marker in EXCLUDE_MARKERS):
            continue
        if not any(marker in lowered_title for marker in VPS_MARKERS):
            continue
        # Primary: brand from the link slug (first path segment after id/blog).
        slug_brand = _brand_from_slug(link)
        if slug_brand and slug_brand.casefold() not in STOPWORDS:
            key = slug_brand.casefold()
            if key not in candidates:
                candidates[key] = {
                    "vendor": slug_brand,
                    "source": source,
                    "title": title[:120],
                    "link": link[:200],
                }
            continue
        # Fallback: brand token within the first 3 title words, only when the
        # title carries a vendor-announcement signal.
        fallback_title = title
        first_three = " ".join(fallback_title.split()[:3])
        if not re.search(r"(?i)^[A-Z][A-Za-z0-9]{2,23}(®|™)?\s+(offers?|hosting|vps|kvm|cloud|is back|returns?|anniversary|thread|sale|promo|launch|arrives?)", fallback_title):
            if not re.search(r"(?i)^([A-Z][A-Za-z0-9]{2,23})", first_three):
                continue
        for match in BRAND_RE.finditer(title):
            brand = match.group(0)
            if brand.casefold() in STOPWORDS:
                continue
            key = brand.casefold()
            if key not in candidates:
                candidates[key] = {
                    "vendor": brand,
                    "source": source,
                    "title": title[:120],
                    "link": link[:200],
                }
            break
    return sorted(candidates.values(), key=lambda c: c["vendor"].casefold())


def _brand_from_slug(link: str) -> str:
    """Extract the brand token from a discussion/blog slug.

    Examples:
      lowendtalk.com/discussion/219854/digirdp-7th-anniversary  -> DigiRDP
      lowendtalk.com/discussion/219830/robovps-offers-full      -> RoboVPS
      lowendbox.com/blog/end-of-an-era-luxvps-closes-its-spec   -> Luxvps
    """
    match = re.search(r"/(?:discussion/\d+|blog)/([a-z0-9]+(?:-[a-z0-9]+)*)", link)
    if not match:
        return ""
    parts = match.group(1).split("-")
    # A slug is typically <brand>-<rest> but may start with filler words
    # ("end-of-an-era-luxvps-closes" -> luxvps). Scan tokens left to right
    # for the first plausible brand: length >= 4 and not a stopword.
    for token in parts:
        if token.isdigit():
            continue
        if len(token) >= 4 and token.casefold() not in STOPWORDS:
            return token
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-dir", type=Path, default=ROOT / "site")
    parser.add_argument("--providers", type=Path, default=ROOT / "providers.yaml")
    parser.add_argument("--output", type=Path, default=ROOT / "state" / "vendor_candidates.json")
    parser.add_argument("--max-candidates", type=int, default=12)
    args = parser.parse_args()

    entries = load_deals(args.site_dir)
    candidates = extract_vendor_candidates(entries)
    providers, domains = load_existing_providers(args.providers)

    fresh: list[dict[str, str]] = []
    for candidate in candidates:
        vendor = candidate["vendor"].casefold()
        if vendor in providers:
            continue
        # Skip when the brand already appears in any tracked domain.
        if any(vendor in domain for domain in domains):
            continue
        fresh.append(candidate)
        if len(fresh) >= args.max_candidates:
            break

    payload = {
        "schema_version": 1,
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(timespec="seconds"),
        "total_deals": len(entries),
        "candidate_count": len(fresh),
        "candidates": fresh,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[vendor-discovery] deals={len(entries)} existing_providers={len(providers)} "
          f"new_candidates={len(fresh)} -> {args.output}")
    for candidate in fresh:
        print(f"  - {candidate['vendor']} ({candidate['source']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
