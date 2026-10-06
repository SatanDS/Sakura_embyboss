"""Normalize untrusted MP metadata. Titles are never a subscription identity."""
import re
import unicodedata
from urllib.parse import urlsplit

from .service import TVError

SOURCES = {"tmdb": "themoviedb", "douban": "douban"}


def parse_key(key):
    if not isinstance(key, str) or not re.fullmatch(r"(tmdb|douban):(movie|tv):[1-9][0-9]{0,15}", key):
        raise TVError("INVALID_REQUEST", "影片编号无效", 400)
    return key.split(":")


def image_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    if value.startswith("/") and not value.startswith("//"):
        value = "https://image.tmdb.org/t/p/w780" + value
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        if (url.scheme == "https" and not url.username and not url.password and not url.fragment
                and url.port in (None, 443) and (host == "image.tmdb.org" or host.endswith(".doubanio.com"))):
            return value
    except ValueError:
        pass
    return ""


def text(value, limit=256):
    return str(value or "").replace("\x00", "")[:limit]


def numeric_id(value):
    return str(value) if re.fullmatch(r"[1-9][0-9]{0,15}", str(value or "")) else None


def normalize(row, source, kind):
    if not isinstance(row, dict):
        return None
    ids = {}
    for provider, keys in (("Tmdb", ("tmdb_id", "tmdbid")), ("Douban", ("douban_id", "doubanid")), ("Imdb", ("imdb_id", "imdbid"))):
        value = next((row[k] for k in keys if row.get(k)), None)
        if provider == "Imdb":
            if re.fullmatch(r"tt[0-9]{1,16}", str(value or "")):
                ids[provider] = str(value)
        elif numeric_id(value):
            ids[provider] = str(value)
    provider = "Tmdb" if source == "tmdb" else "Douban"
    media_id = ids.get(provider)
    if not media_id and (row.get("source") or row.get("media_source")) in (source, SOURCES[source]):
        media_id = numeric_id(row.get("media_id"))
        if media_id:
            ids[provider] = media_id
    if not media_id:
        return None
    raw_kind = row.get("type") or row.get("media_type")
    if raw_kind and raw_kind not in ({"电影", "movie", "Movie"} if kind == "movie" else {"电视剧", "tv", "TV", "Series"}):
        return None
    title = text(row.get("title") or row.get("name"))
    if not title:
        return None
    year = text(row.get("year") or row.get("release_date") or row.get("first_air_date"), 4)
    rating = row.get("vote_average") or row.get("rating") or 0
    try:
        rating = round(max(0, min(10, float(rating))), 1)
    except (ValueError, TypeError):
        rating = 0
    seasons = []
    for season in (row.get("seasons") if isinstance(row.get("seasons"), list) else [])[:1000]:
        if not isinstance(season, dict):
            continue
        number = season.get("season_number")
        if type(number) is int and 0 <= number <= 999:
            seasons.append({"number": number, "name": text(season.get("name")), "episodeCount": season.get("episode_count") if type(season.get("episode_count")) is int else None})
    return {"key": f"{source}:{kind}:{media_id}", "source": source, "id": media_id, "type": kind,
            "title": title, "originalTitle": text(row.get("original_title") or row.get("original_name")),
            "year": int(year) if re.fullmatch(r"(?:18|19|20|21)\d\d", year) else None,
            "overview": text(row.get("overview"), 16000),
            "poster": image_url(row.get("poster_path") or row.get("poster")),
            "backdrop": image_url(row.get("backdrop_path") or row.get("backdrop")),
            "rating": rating, "providerIds": ids, "seasons": seasons,
            "genres": [text(v.get("name") if isinstance(v, dict) else v, 60) for v in (row.get("genres") if isinstance(row.get("genres"), list) else [])[:20]]}


def same_identity(a, b):
    if a["type"] != b["type"]:
        return False
    left, right = a["providerIds"], b["providerIds"]
    shared = left.keys() & right.keys()
    return bool(shared) and all(left[k] == right[k] for k in shared)


def deduplicate(primary, supplement):
    """TMDB metadata wins. Title/year are ONLY used to collapse display cards."""
    result = []
    norm = lambda value: re.sub(r"\W", "", unicodedata.normalize("NFKC", value).casefold())
    for item in [*primary, *supplement]:
        if any(same_identity(item, other) or (
                item["type"] == other["type"] and item["year"] and item["year"] == other["year"]
                and norm(item["title"]) == norm(other["title"])) for other in result):
            continue
        result.append(item)
    return result
