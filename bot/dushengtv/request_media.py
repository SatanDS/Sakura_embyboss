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


def image_url(value, size="w780"):
    # MoviePilot's Douban people use avatar.normal; TMDB people keep a path.
    if isinstance(value, dict):
        value = value.get("normal") or value.get("large") or value.get("medium") or value.get("url")
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    if value.startswith("/") and not value.startswith("//"):
        if not re.fullmatch(r"/[\w-]+\.(?:jpg|jpeg|png|webp)", value, re.I):
            return ""
        value = f"https://image.tmdb.org/t/p/{size}" + value
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        if url.scheme != "https" or url.username or url.password or url.fragment or url.port not in (None, 443):
            return ""
        # A configured TMDB image mirror has the same immutable /t/p/ path.
        # Canonicalize that path, never grant the mirror's arbitrary host.
        tmdb = re.fullmatch(r"/t/p/(original|w\d+(?:_and_h\d+_bestv2)?|h\d+)/([\w-]+\.(?:jpg|jpeg|png|webp))", url.path, re.I)
        if tmdb and not url.query:
            selected = size if tmdb[1] == "original" or host != "image.tmdb.org" else tmdb[1]
            return f"https://image.tmdb.org/t/p/{selected}/{tmdb[2]}"
        if host.endswith(".doubanio.com") and url.path.startswith(("/view/", "/img/")):
            return value
    except ValueError:
        pass
    return ""


def search_query(value):
    if not isinstance(value, str) or len(value) > 128 or any(ord(char) < 32 for char in value):
        raise TVError("INVALID_REQUEST", "搜索词应为 128 字以内的影片名称", 400)
    return " ".join(value.split())


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
    if raw_kind and (not isinstance(raw_kind, str) or raw_kind not in ({"电影", "movie", "Movie"} if kind == "movie" else {"电视剧", "tv", "TV", "Series"})):
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
    season_rows = row.get("seasons") if isinstance(row.get("seasons"), list) else row.get("season_info")
    for season in (season_rows if isinstance(season_rows, list) else [])[:1000]:
        if not isinstance(season, dict):
            continue
        number = season.get("season_number")
        if type(number) is int and 0 <= number <= 999:
            seasons.append({"number": number, "name": text(season.get("name")), "episodeCount": season.get("episode_count") if type(season.get("episode_count")) is int else None})
    def people(field):
        return [{"name": text(person.get("name")), "role": text(person.get("character") or person.get("job")),
                 "photo": image_url(person.get("profile_path") or person.get("avatar"), "w185")}
                for person in (row.get(field) if isinstance(row.get(field), list) else [])[:30] if isinstance(person, dict) and person.get("name")]
    countries = row.get("origin_country") or row.get("production_countries")
    studios = row.get("production_companies")
    return {"key": f"{source}:{kind}:{media_id}", "source": source, "id": media_id, "type": kind,
            "title": title, "originalTitle": text(row.get("original_title") or row.get("original_name")),
            "year": int(year) if re.fullmatch(r"(?:18|19|20|21)\d\d", year) else None,
            "overview": text(row.get("overview"), 16000),
            "poster": image_url(row.get("poster_path") or row.get("poster"), "w500"),
            "backdrop": image_url(row.get("backdrop_path") or row.get("backdrop"), "w1280"),
            "rating": rating, "providerIds": ids, "seasons": seasons,
            "tagline": text(row.get("tagline"), 512), "releaseDate": text(row.get("release_date") or row.get("first_air_date"), 40),
            "status": text(row.get("status"), 80), "language": text(row.get("original_language"), 40),
            "countries": [text(value.get("name") if isinstance(value, dict) else value, 80) for value in (countries if isinstance(countries, list) else [])[:20]],
            "studios": [text(value.get("name") if isinstance(value, dict) else value, 120) for value in (studios if isinstance(studios, list) else [])[:20]],
            "cast": people("actors"), "directors": people("directors"),
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
