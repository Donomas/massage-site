"""Private news drafts and atomic publication to GitHub Pages, Python 3.11+."""

from __future__ import annotations

import argparse
import base64
import binascii
import errno
from collections import OrderedDict
from contextlib import contextmanager
from datetime import date
import getpass
import hashlib
import hmac
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen
import uuid
import xml.etree.ElementTree as ET


MAX_FILE = 20 * 1024 * 1024
MAX_BODY = 30 * 1024 * 1024
DEFAULT_MEDIA_QUOTA = 512 * 1024 * 1024
SESSION_SECONDS = 8 * 60 * 60
MEDIA_ID = re.compile(r"[a-f0-9]{32}\.(?:jpg|png|webp|gif|mp4|webm)\Z")
CARD_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
MIME = {
    "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    "gif": "image/gif", "mp4": "video/mp4", "webm": "video/webm",
}
START = "<!-- NEWS:START -->"
END = "<!-- NEWS:END -->"
SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
SECTIONS = {"news", "offers", "services", "programs"}


def require_section(section="news"):
    if not isinstance(section, str) or section not in SECTIONS:
        raise APIError(400, "invalid_section", "Неизвестный раздел редактора.")
    return section


def query_section(query):
    values = parse_qs(query, keep_blank_values=True).get("section", ["news"])
    if len(values) != 1:
        raise APIError(400, "invalid_section", "Укажите один раздел редактора.")
    return require_section(values[0])


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algorithm, n, r, p, salt, digest = stored.split("$")
        if (algorithm, n, r, p) != ("scrypt", "16384", "8", "1"):
            return False
        salt_bytes, expected = bytes.fromhex(salt), bytes.fromhex(digest)
        if len(salt_bytes) != 16 or len(expected) != 32:
            return False
        actual = hashlib.scrypt(password.encode(), salt=salt_bytes, n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def detect_media(data: bytes):
    if len(data) < 12:
        raise APIError(400, "invalid_media", "Файл не распознан.")
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image"
    if data[4:8] == b"ftyp":
        box_size = int.from_bytes(data[:4], "big")
        if 16 <= box_size <= min(len(data), 4096):
            brands = [data[8:12]] + [data[i:i + 4] for i in range(16, box_size, 4)]
            if any(brand in (b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ") for brand in brands):
                return "mp4", "video"
    if data[:4] == b"\x1aE\xdf\xa3" and re.search(b"\x42\x82\x84webm", data[:4096]):
        return "webm", "video"
    raise APIError(400, "invalid_media", "Поддерживаются JPEG, PNG, WebP, GIF, MP4 и WebM.")


class Store:
    def __init__(self, directory: Path, media_quota_bytes=DEFAULT_MEDIA_QUOTA):
        if type(media_quota_bytes) is not int or media_quota_bytes <= 0:
            raise ValueError("Media quota must be a positive integer")
        self.media_quota_bytes = media_quota_bytes
        self.upload_lock = threading.Lock()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.media_dir = self.directory / "media"
        self.media_dir.mkdir(exist_ok=True, mode=0o700)
        self.database = self.directory / "news.sqlite3"
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    cards TEXT NOT NULL DEFAULT '[]', revision INTEGER NOT NULL DEFAULT 0,
                    published_commit TEXT, published_url TEXT, published_revision INTEGER
                );
                INSERT OR IGNORE INTO state(singleton) VALUES(1);
                CREATE TABLE IF NOT EXISTS section_state (
                    section TEXT PRIMARY KEY,
                    cards TEXT NOT NULL DEFAULT '[]', revision INTEGER NOT NULL DEFAULT 0,
                    published_commit TEXT, published_url TEXT, published_revision INTEGER
                );
                INSERT OR IGNORE INTO section_state
                    SELECT 'news',cards,revision,published_commit,published_url,published_revision
                    FROM state WHERE singleton=1;
                CREATE TABLE IF NOT EXISTS media (
                    id TEXT PRIMARY KEY, extension TEXT NOT NULL,
                    kind TEXT NOT NULL, name TEXT NOT NULL
                );
                COMMIT;
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def seed_section(self, section, cards):
        section = require_section(section)
        with self.connection() as db:
            if db.execute("SELECT 1 FROM section_state WHERE section=?", (section,)).fetchone():
                return False
        cards = self.validate_cards(cards, section)
        with self.connection() as db:
            result = db.execute("INSERT OR IGNORE INTO section_state(section,cards) VALUES(?,?)",
                                (section, json.dumps(cards, ensure_ascii=False)))
        return result.rowcount == 1

    def draft(self, section="news"):
        section = require_section(section)
        self.seed_section(section, [])
        with self.connection() as db:
            state = db.execute("SELECT cards, revision FROM section_state WHERE section=?", (section,)).fetchone()
        return {"cards": json.loads(state["cards"]), "revision": state["revision"]}

    def save(self, cards, revision, section="news"):
        section = require_section(section)
        self.seed_section(section, [])
        with self.connection() as db:
            result = db.execute(
                "UPDATE section_state SET cards=?,revision=revision+1 WHERE section=? AND revision=?",
                (json.dumps(cards, ensure_ascii=False), section, revision),
            )
            if result.rowcount != 1:
                raise APIError(409, "stale_revision", "Черновик изменён. Обновите страницу и повторите сохранение.")
        return {"cards": cards, "revision": revision + 1}

    def media(self, media_id):
        with self.connection() as db:
            result = db.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
        if result is None:
            raise APIError(400, "unknown_media", "Файл не найден в хранилище редактора.")
        return dict(result)

    def upload(self, name, data):
        if not isinstance(name, str) or not name.strip() or len(name) > 240 or any(ord(c) < 32 for c in name):
            raise APIError(400, "invalid_name", "Укажите имя файла длиной до 240 символов.")
        if not isinstance(data, str) or len(data) > (MAX_FILE + 2) // 3 * 4:
            raise APIError(413, "file_too_large", "Файл должен быть не больше 20 МБ.")
        try:
            content = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            raise APIError(400, "invalid_base64", "Неверная кодировка файла.") from None
        if len(content) > MAX_FILE:
            raise APIError(413, "file_too_large", "Файл должен быть не больше 20 МБ.")
        extension, kind = detect_media(content)
        media_id = uuid.uuid4().hex
        filename = f"{media_id}.{extension}"
        path = self.media_dir / filename
        with self.upload_lock:
            used_bytes = sum(item.stat().st_size for item in self.media_dir.iterdir() if item.is_file())
            if used_bytes + len(content) > self.media_quota_bytes:
                raise APIError(507, "storage_full", "Хранилище файлов заполнено. Обратитесь к администратору для освобождения места.")
            try:
                with path.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                with self.connection() as db:
                    db.execute("INSERT INTO media VALUES(?,?,?,?)", (media_id, extension, kind, name))
            except Exception as error:
                path.unlink(missing_ok=True)
                if isinstance(error, OSError) and error.errno == errno.ENOSPC:
                    raise APIError(507, "storage_full", "На сервере закончилось место для файлов.") from None
                raise
        return {"id": media_id, "url": f"/media/{filename}", "kind": kind, "name": name}

    def validate_cards(self, cards, section="news"):
        section = require_section(section)
        if not isinstance(cards, list) or len(cards) > 50:
            raise APIError(400, "invalid_cards", "Допускается не больше 50 карточек.")
        result = []
        card_ids = set()
        for card in cards:
            required = {"id", "title", "text", "date", "media"}
            optional = {"duration"} if section == "services" else set()
            if not isinstance(card, dict) or not required <= card.keys() or card.keys() - required - optional:
                raise APIError(400, "invalid_card", "Неверные поля карточки.")
            if "duration" in card and (not isinstance(card["duration"], str) or len(card["duration"]) > 80):
                raise APIError(400, "invalid_duration", "Длительность должна быть строкой до 80 символов.")
            card_id = card["id"]
            if not isinstance(card_id, str) or not CARD_ID.fullmatch(card_id) or card_id in card_ids:
                raise APIError(400, "invalid_card_id", "Карточкам нужны разные буквенно-цифровые ID.")
            card_ids.add(card_id)
            if not isinstance(card["title"], str) or not card["title"].strip() or len(card["title"]) > 160:
                raise APIError(400, "invalid_title", "Заголовок должен содержать от 1 до 160 символов.")
            if not isinstance(card["text"], str) or len(card["text"]) > 10000:
                raise APIError(400, "invalid_text", "Текст должен содержать не больше 10000 символов.")
            try:
                if not isinstance(card["date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", card["date"]):
                    raise ValueError
                date.fromisoformat(card["date"])
            except ValueError:
                raise APIError(400, "invalid_date", "Дата должна иметь формат ГГГГ-ММ-ДД.") from None
            if not isinstance(card["media"], list) or len(card["media"]) > 5:
                raise APIError(400, "invalid_media", "В карточке допускается не больше 5 файлов.")
            files = []
            seen = set()
            for media in card["media"]:
                if not isinstance(media, dict) or set(media) != {"id", "url", "kind", "name"}:
                    raise APIError(400, "invalid_media", "Неверные поля файла.")
                if not isinstance(media["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", media["id"]):
                    raise APIError(400, "invalid_media", "Неверный ID файла.")
                stored = self.media(media["id"])
                expected = {"id": stored["id"], "url": f"/media/{stored['id']}.{stored['extension']}",
                            "kind": stored["kind"], "name": stored["name"]}
                if media != expected or media["id"] in seen:
                    raise APIError(400, "invalid_media", "Файл должен соответствовать загруженному оригиналу.")
                seen.add(media["id"])
                files.append(expected)
            result.append({**card, "title": card["title"].strip(), "media": files})
        return result

    def mark_published(self, commit, url, revision, section="news"):
        section = require_section(section)
        self.seed_section(section, [])
        with self.connection() as db:
            db.execute("UPDATE section_state SET published_commit=?,published_url=?,published_revision=? WHERE section=?",
                       (commit, url, revision, section))

    def publish_status(self, section="news"):
        section = require_section(section)
        self.seed_section(section, [])
        with self.connection() as db:
            row = db.execute("SELECT published_commit,published_url,published_revision FROM section_state WHERE section=?", (section,)).fetchone()
        return {"commit": row[0], "url": row[1], "revision": row[2], "status": "submitted" if row[0] else "never"}


def public_cards(cards, section="news"):
    section = require_section(section)
    return [{**card, "media": [{**media, "url": f"./assets/{section}/{media['url'].rsplit('/', 1)[1]}"}
                              for media in card["media"]]} for card in cards]


def render_cards(cards, section="news"):
    section = require_section(section)
    if not cards:
        messages = {"news": "Пока новостей нет.", "offers": "Пока акций нет.",
                    "services": "Услуги пока не опубликованы.", "programs": "Программы пока не опубликованы."}
        return f'<section class="section empty-state" aria-label="Содержание раздела"><p>{messages[section]}</p></section>'
    rendered = []
    for card in cards:
        published_date = date.fromisoformat(card["date"])
        paragraphs = "".join("<p>" + escape(paragraph).replace("\n", "<br>") + "</p>"
                             for paragraph in card["text"].split("\n\n") if paragraph.strip())
        media_html = []
        for media in card["media"]:
            url, name = escape(media["url"], quote=True), escape(media["name"], quote=True)
            if media["kind"] == "image":
                media_html.append(f'<img src="{url}" alt="{name}" loading="lazy" decoding="async">')
            else:
                media_html.append(f'<video src="{url}" controls preload="metadata" playsinline aria-label="{name}">'
                                  f'<a href="{url}">Скачать видео</a></video>')
        media_block = '<div class="news-media">' + "".join(media_html) + '</div>' if media_html else ""
        card_class = "service-card" if section == "services" else "news-card"
        article_id = card["id"] if section == "services" else section + "-" + card["id"]
        date_html = (f'<time class="news-date" datetime="{card["date"]}">{published_date:%d.%m.%Y}</time>'
                     if section in ("news", "offers") else "")
        duration_html = (f'<p class="service-duration">{escape(card["duration"])}</p>'
                         if section == "services" and card.get("duration") else "")
        rendered.append(f'<article class="{card_class}" id="{escape(article_id, quote=True)}">'
                        f'{date_html}{duration_html}<h2>{escape(card["title"])}</h2>'
                        f'<div class="news-body">{paragraphs}</div>{media_block}</article>')
    wrapper = "service-grid" if section == "services" else "news-feed"
    return f'<div class="{wrapper}">\n' + "\n".join(rendered) + '\n</div>'


def update_service_schema(template, cards):
    pattern = re.compile(r'(<script\b[^>]*\btype=[\'"]application/ld\+json[\'"][^>]*>)(.*?)(</script\s*>)', re.I | re.S)
    matches = list(pattern.finditer(template))
    if len(matches) != 1:
        raise APIError(502, "invalid_template", "Для услуг нужен один JSON-LD с графом Person и Service.")
    match = matches[0]
    try:
        payload = json.loads(match[2])
        graph = payload["@graph"]
        if not isinstance(graph, list) or not all(isinstance(node, dict) for node in graph):
            raise ValueError
        person = next(node for node in graph if node.get("@type") == "Person" and node.get("@id"))
    except (ValueError, KeyError, TypeError, StopIteration):
        raise APIError(502, "invalid_template", "Для услуг нужен JSON-LD с данными массажиста.") from None
    service_nodes = [node for node in graph if node.get("@type") == "Service"]
    area = next((node["areaServed"] for node in service_nodes if "areaServed" in node),
                {"@type": "City", "name": "Ангарск"})
    list_name = next((node.get("name") for node in graph if node.get("@type") == "ItemList" and node.get("name")),
                     "Виды массажа в Ангарске")
    updated = [node for node in graph if node.get("@type") not in ("Service", "ItemList")]
    page_url = canonical_url(template)
    updated.append({"@type": "ItemList", "name": list_name, "itemListElement": [
        {"@type": "ListItem", "position": index, "item": {"@id": page_url + "#" + card["id"]}}
        for index, card in enumerate(cards, 1)]})
    for card in cards:
        description = card["text"]
        if card.get("duration"):
            description = description.rstrip() + " Длительность: " + card["duration"] + "."
        updated.append({"@type": "Service", "@id": page_url + "#" + card["id"], "name": card["title"],
                        "description": description, "url": page_url + "#" + card["id"],
                        "provider": {"@id": person["@id"]}, "areaServed": area})
    payload["@graph"] = updated
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return template[:match.start()] + match[1] + "\n" + encoded + "\n" + match[3] + template[match.end():]


def prepare_page(template, cards, section="news"):
    section = require_section(section)
    start, end = (START, END) if section == "news" else ("<!-- CONTENT:START -->", "<!-- CONTENT:END -->")
    if template.count(start) != 1 or template.count(end) != 1 or template.index(start) > template.index(end):
        raise APIError(502, "invalid_template", f"В {section}.html нужны единственные маркеры {start} и {end}.")
    before, remainder = template.split(start)
    _, after = remainder.split(end)
    result = before + start + "\n" + render_cards(cards, section) + "\n" + end + after
    robots = 'index, follow' if cards else 'noindex'
    result, count = re.subn(r'<meta\s+name=[\'"]robots[\'"]\s+content=[\'"][^\'"]*[\'"]\s*/?>',
                            f'<meta name="robots" content="{robots}">', result, flags=re.I)
    if count != 1:
        raise APIError(502, "invalid_template", f"В {section}.html нужен один meta robots.")
    if section == "services":
        result = update_service_schema(result, cards)
    return result


def prepare_sitemap(template, news_url, has_cards):
    try:
        root = ET.fromstring(template)
    except ET.ParseError:
        raise APIError(502, "invalid_sitemap", "Карта сайта содержит неверный XML.") from None
    if root.tag != f"{{{SITEMAP_NS}}}urlset":
        raise APIError(502, "invalid_sitemap", "Неверный формат sitemap.xml.")
    for entry in list(root):
        location = entry.find(f"{{{SITEMAP_NS}}}loc")
        if location is not None and location.text == news_url:
            root.remove(entry)
    if has_cards:
        entry = ET.SubElement(root, f"{{{SITEMAP_NS}}}url")
        ET.SubElement(entry, f"{{{SITEMAP_NS}}}loc").text = news_url
    ET.register_namespace("", SITEMAP_NS)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def canonical_url(template):
    canonical = re.search(r'<link\s+rel=[\'"]canonical[\'"]\s+href=[\'"]([^\'"]+)[\'"]', template, re.I)
    if not canonical or not canonical[1].startswith("https://"):
        raise APIError(502, "invalid_template", "У страницы нужен канонический HTTPS-адрес.")
    return canonical[1]


def matches_publication(content, cards, digest, section="news"):
    try:
        remote = json.loads(content)
    except (ValueError, TypeError):
        raise APIError(502, "invalid_published_json", "Не удалось проверить опубликованные новости.") from None
    if not isinstance(remote, dict):
        raise APIError(502, "invalid_published_json", "Неверный формат опубликованных новостей.")
    return remote.get("section", "news") == section and remote.get("digest") == digest and remote.get("cards") == cards


def publication_files(store, snapshot, cards, digest, template, sitemap, existing_paths):
    section = require_section(snapshot.get("section", "news"))
    news_url = canonical_url(template)
    files = {
        f"{section}.html": prepare_page(template, cards, section).encode("utf-8"),
        "sitemap.xml": prepare_sitemap(sitemap, news_url, bool(cards)).encode("utf-8"),
        f"content/{section}.json": json_bytes({"cards": cards, "revision": snapshot["revision"], "digest": digest, "section": section}),
    }
    for card in cards:
        for media in card["media"]:
            filename = media["url"].rsplit("/", 1)[1]
            path = f"assets/{section}/" + filename
            if path not in existing_paths:
                files[path] = (store.media_dir / filename).read_bytes()
    return files


class GitHub:
    def __init__(self, token, repository, branch):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid GITHUB_REPO")
        self.token = token
        self.base = "https://api.github.com/repos/" + repository
        self.branch = branch

    def request(self, method, path, payload=None):
        if not self.token:
            raise APIError(503, "github_not_configured", "Публикация ещё не настроена на сервере.")
        request = Request(self.base + path, data=json_bytes(payload) if payload is not None else None,
                          method=method, headers={"Authorization": f"Bearer {self.token}",
                          "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Massage-News-Editor/1.0"})
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code in (409, 422):
                raise APIError(409, "github_conflict", "Ветка GitHub изменилась. Повторите публикацию.") from None
            raise APIError(502, "github_error", f"GitHub отклонил операцию (HTTP {error.code}).") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise APIError(502, "github_unavailable", "GitHub недоступен. Черновик сохранён. Повторите публикацию.") from None

    def read_file(self, path, commit):
        response = self.request("GET", f"/contents/{quote(path, safe='/')}?ref={quote(commit, safe='')}")
        if response.get("encoding") != "base64":
            raise APIError(502, "invalid_template", "GitHub вернул неподдерживаемый формат файла.")
        try:
            return base64.b64decode(response["content"]).decode("utf-8")
        except (KeyError, ValueError, UnicodeError):
            raise APIError(502, "invalid_template", "Не удалось прочитать файл публикации.") from None

    def publish(self, store, snapshot):
        section = require_section(snapshot.get("section", "news"))
        cards = public_cards(snapshot["cards"], section)
        digest = hashlib.sha256(json_bytes({"section": section, "cards": cards})).hexdigest()
        reference = self.request("GET", "/git/ref/heads/" + quote(self.branch, safe="/"))
        head = reference["object"]["sha"]
        commit = self.request("GET", "/git/commits/" + head)
        tree = self.request("GET", "/git/trees/" + commit["tree"]["sha"] + "?recursive=1")
        if tree.get("truncated"):
            raise APIError(502, "tree_too_large", "GitHub вернул неполное дерево репозитория.")
        paths = {item["path"] for item in tree["tree"]}
        template = self.read_file(f"{section}.html", head)
        news_url = canonical_url(template)
        data_path = f"content/{section}.json"
        if data_path in paths:
            if matches_publication(self.read_file(data_path, head), cards, digest, section):
                return {"commit": head, "url": news_url}
        files = publication_files(store, snapshot, cards, digest, template, self.read_file("sitemap.xml", head), paths)
        entries = []
        for path, content in files.items():
            blob = self.request("POST", "/git/blobs", {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"})
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        new_tree = self.request("POST", "/git/trees", {"base_tree": commit["tree"]["sha"], "tree": entries})
        new_commit = self.request("POST", "/git/commits", {"message": f"Publish {section}", "tree": new_tree["sha"], "parents": [head]})
        self.request("PATCH", "/git/refs/heads/" + quote(self.branch, safe="/"), {"sha": new_commit["sha"], "force": False})
        return {"commit": new_commit["sha"], "url": news_url}


class GitSSH:
    """Publish git objects with a temporary index, preserving checkout files."""

    def __init__(self, directory, branch="main"):
        self.directory = Path(directory).resolve()
        self.branch = branch
        self.run("check-ref-format", "refs/heads/" + branch)

    def run(self, *arguments, content=None, environment=None):
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "News Editor",
               "GIT_AUTHOR_EMAIL": "news-editor@localhost", "GIT_COMMITTER_NAME": "News Editor",
               "GIT_COMMITTER_EMAIL": "news-editor@localhost", **(environment or {})}
        try:
            result = subprocess.run(["git", *arguments], input=content, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=self.directory, env=env, timeout=90)
        except (OSError, subprocess.TimeoutExpired):
            raise APIError(502, "git_unavailable", "Не удалось связаться с репозиторием. Повторите публикацию.") from None
        if result.returncode:
            if arguments[0] == "push" and any(marker in result.stderr.lower() for marker in (b"rejected", b"non-fast-forward", b"fetch first")):
                raise APIError(409, "github_conflict", "Ветка GitHub изменилась. Повторите публикацию.")
            raise APIError(502, "git_error", "Операция Git не завершилась. Черновик сохранён, повторите публикацию.")
        return result.stdout

    def publish(self, store, snapshot):
        section = require_section(snapshot.get("section", "news"))
        self.run("fetch", "--no-tags", "origin", f"+refs/heads/{self.branch}:refs/remotes/origin/{self.branch}")
        head = self.run("rev-parse", "refs/remotes/origin/" + self.branch).decode().strip()
        paths = set(self.run("ls-tree", "-r", "--name-only", "-z", head).decode("utf-8").rstrip("\0").split("\0"))
        template = self.run("show", head + f":{section}.html").decode("utf-8")
        cards = public_cards(snapshot["cards"], section)
        digest = hashlib.sha256(json_bytes({"section": section, "cards": cards})).hexdigest()
        news_url = canonical_url(template)
        data_path = f"content/{section}.json"
        if data_path in paths:
            if matches_publication(self.run("show", head + ":" + data_path), cards, digest, section):
                return {"commit": head, "url": news_url}
        sitemap = self.run("show", head + ":sitemap.xml").decode("utf-8")
        files = publication_files(store, snapshot, cards, digest, template, sitemap, paths)
        with tempfile.TemporaryDirectory(prefix="publish-", dir=store.directory) as temporary:
            environment = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
            self.run("read-tree", head, environment=environment)
            for path, content in files.items():
                sha = self.run("hash-object", "-w", "--stdin", content=content).decode().strip()
                self.run("update-index", "--add", "--cacheinfo", f"100644,{sha},{path}", environment=environment)
            tree = self.run("write-tree", environment=environment).decode().strip()
            commit = self.run("commit-tree", tree, "-p", head, "-m", f"Publish {section}").decode().strip()
        # The parent is the fetched remote head. Concurrent branch changes reject this non-force push.
        self.run("push", "--porcelain", "origin", f"{commit}:refs/heads/{self.branch}")
        return {"commit": commit, "url": news_url}


class Application:
    def __init__(self, store, password_hash, username="admin", allowed_origin="https://donomas.github.io", github=None):
        self.store = store
        self.password_hash = password_hash
        self.username = username
        self.allowed_origin = allowed_origin
        self.github = github
        self.sessions = {}
        self.login_attempts = OrderedDict()
        self.global_attempts = []
        self.auth_lock = threading.Lock()
        self.publish_lock = threading.Lock()

    def authenticated(self, authorization):
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        key = hashlib.sha256(token.encode()).hexdigest()
        with self.auth_lock:
            self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > time.monotonic()}
            return bool(token) and key in self.sessions

    def login(self, username, password, peer):
        now = time.monotonic()
        with self.auth_lock:
            self.global_attempts = [stamp for stamp in self.global_attempts if now - stamp < 60]
            attempts = [stamp for stamp in self.login_attempts.pop(peer, []) if now - stamp < 300]
            self.login_attempts[peer] = attempts
            while len(self.login_attempts) > 256:
                self.login_attempts.popitem(last=False)
            if len(self.global_attempts) >= 30 or len(attempts) >= 5:
                raise APIError(429, "rate_limited", "Слишком много попыток. Повторите вход через 5 минут.")
            attempts.append(now)
            self.global_attempts.append(now)
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 160 or len(password) > 1024:
            raise APIError(401, "invalid_credentials", "Неверное имя пользователя или пароль.")
        valid_password = check_password(password, self.password_hash)
        if not hmac.compare_digest(username.encode(), self.username.encode()) or not valid_password:
            raise APIError(401, "invalid_credentials", "Неверное имя пользователя или пароль.")
        token = secrets.token_urlsafe(32)
        with self.auth_lock:
            self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
            if len(self.sessions) >= 128:
                self.sessions.pop(next(iter(self.sessions)))
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = now + SESSION_SECONDS
        return {"token": token}

    def logout(self, authorization):
        token = authorization.removeprefix("Bearer ")
        with self.auth_lock:
            self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    def publish(self, revision, section="news"):
        section = require_section(section)
        if not self.publish_lock.acquire(blocking=False):
            raise APIError(409, "publish_in_progress", "Публикация уже выполняется.")
        try:
            snapshot = self.store.draft(section)
            if snapshot["revision"] != revision:
                raise APIError(409, "stale_revision", "Сначала сохраните текущую версию черновика.")
            snapshot["cards"] = self.store.validate_cards(snapshot["cards"], section)
            snapshot["section"] = section
            if self.github is None:
                raise APIError(503, "github_not_configured", "Публикация ещё не настроена на сервере.")
            result = self.github.publish(self.store, snapshot)
            self.store.mark_published(result["commit"], result["url"], revision, section)
            return result
        finally:
            self.publish_lock.release()


def require_revision(data):
    revision = data.get("revision")
    if type(revision) is not int or revision < 0:
        raise APIError(400, "invalid_revision", "Нужна сохранённая версия черновика.")
    return revision


def make_handler(application):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NewsEditor"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, format, *args):
            # Do not log request bodies, credentials, bearer tokens, or headers.
            pass

        def finish_headers(self):
            origin = self.headers.get("Origin")
            if origin == application.allowed_origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()

        def respond(self, status, value):
            body = json_bytes(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.finish_headers()
            self.wfile.write(body)

        def check_origin(self):
            origin = self.headers.get("Origin")
            if origin is not None and origin != application.allowed_origin:
                raise APIError(403, "origin_denied", "Этот адрес сайта не разрешён.")

        def body(self):
            if self.headers.get_content_type() != "application/json":
                raise APIError(415, "invalid_content_type", "Ожидается application/json.")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise APIError(411, "length_required", "Нужна длина запроса.") from None
            if not 0 < length <= MAX_BODY:
                raise APIError(413, "request_too_large", "Запрос слишком большой.")
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                raise APIError(400, "invalid_json", "Неверный JSON.") from None
            if not isinstance(body, dict):
                raise APIError(400, "invalid_json", "Ожидается JSON-объект.")
            return body

        def do_OPTIONS(self):
            try:
                self.check_origin()
                self.send_response(204)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Content-Length", "0")
                self.finish_headers()
            except APIError as error:
                self.respond(error.status, {"error": str(error), "code": error.code})

        def peer(self):
            peer = self.client_address[0]
            if peer in ("127.0.0.1", "::1"):
                try:
                    return str(ipaddress.ip_address(self.headers.get("X-Real-IP", peer)))
                except ValueError:
                    pass
            return peer

        def dispatch(self):
            try:
                self.check_origin()
                route = urlsplit(self.path)
                path = route.path
                if self.command == "GET" and path == "/health":
                    return self.respond(200, {"ok": True})
                if self.command == "POST" and path == "/login":
                    data = self.body()
                    return self.respond(200, application.login(data.get("username"), data.get("password"), self.peer()))
                authorization = self.headers.get("Authorization", "")
                if not application.authenticated(authorization):
                    raise APIError(401, "unauthorized", "Войдите в редактор.")
                if self.command == "GET" and path == "/draft":
                    return self.respond(200, application.store.draft(query_section(route.query)))
                if self.command == "GET" and path == "/publish-status":
                    return self.respond(200, application.store.publish_status(query_section(route.query)))
                if self.command == "GET" and path.startswith("/media/"):
                    return self.serve_media(path.removeprefix("/media/"))
                if self.command == "POST" and path == "/logout":
                    application.logout(authorization)
                    return self.respond(200, {"ok": True})
                if self.command == "PUT" and path == "/draft":
                    data = self.body()
                    revision = require_revision(data)
                    section = require_section(data.get("section", "news"))
                    cards = application.store.validate_cards(data.get("cards"), section)
                    return self.respond(200, application.store.save(cards, revision, section))
                if self.command == "POST" and path == "/upload":
                    data = self.body()
                    return self.respond(201, application.store.upload(data.get("name"), data.get("data")))
                if self.command == "POST" and path == "/publish":
                    data = self.body()
                    return self.respond(200, application.publish(require_revision(data), require_section(data.get("section", "news"))))
                raise APIError(404, "not_found", "Адрес не найден.")
            except APIError as error:
                self.respond(error.status, {"error": str(error), "code": error.code})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.respond(500, {"error": "Ошибка сервера. Повторите операцию.", "code": "server_error"})

        def serve_media(self, filename):
            if not MEDIA_ID.fullmatch(filename):
                raise APIError(404, "not_found", "Файл не найден.")
            media_id, extension = filename.rsplit(".", 1)
            media = application.store.media(media_id)
            path = application.store.media_dir / filename
            if media["extension"] != extension or not path.is_file():
                raise APIError(404, "not_found", "Файл не найден.")
            content = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", MIME[extension])
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
            self.finish_headers()
            self.wfile.write(content)

        do_GET = dispatch
        do_POST = dispatch
        do_PUT = dispatch

    return Handler


class NewsHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, handler):
        self.request_slots = threading.BoundedSemaphore(8)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        self.request_slots.acquire()
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-password", action="store_true", help="Print a scrypt hash after a private password prompt")
    arguments = parser.parse_args()
    if arguments.set_password:
        password = getpass.getpass("Пароль: ")
        confirm = getpass.getpass("Повторите пароль: ")
        if not password or password != confirm:
            parser.error("Пароли должны совпадать и быть непустыми.")
        print(hash_password(password))
        return
    password_hash = os.environ.get("NEWS_PASSWORD_HASH", "")
    if not re.fullmatch(r"scrypt\$16384\$8\$1\$[0-9a-f]{32}\$[0-9a-f]{64}", password_hash):
        parser.error("Задайте NEWS_PASSWORD_HASH через --set-password.")
    data_dir = Path(os.environ.get("NEWS_DATA_DIR", "~/.local/share/massage-news")).expanduser().resolve()
    checkout = Path(__file__).resolve().parent.parent
    if data_dir == checkout or checkout in data_dir.parents:
        parser.error("NEWS_DATA_DIR должен находиться за пределами checkout.")
    origin = os.environ.get("ALLOWED_ORIGIN", "https://donomas.github.io")
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        parser.error("ALLOWED_ORIGIN должен содержать только схему, домен и порт.")
    branch = os.environ.get("GITHUB_BRANCH", "main")
    try:
        media_quota_mb = int(os.environ.get("NEWS_MEDIA_QUOTA_MB", "512"))
        if media_quota_mb <= 0:
            raise ValueError
    except ValueError:
        parser.error("NEWS_MEDIA_QUOTA_MB должен быть положительным целым числом.")
    repository_directory = os.environ.get("NEWS_REPO_DIR")
    github = GitSSH(repository_directory, branch) if repository_directory else GitHub(
        os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPO", "Donomas/massage-site"), branch)
    store = Store(data_dir, media_quota_mb * 1024 * 1024)
    for section in sorted(SECTIONS - {"news"}):
        seed_path = checkout / "content" / f"{section}.json"
        if seed_path.is_file():
            try:
                seed = json.loads(seed_path.read_text(encoding="utf-8"))
                store.seed_section(section, seed["cards"])
            except (OSError, ValueError, KeyError, TypeError, APIError):
                parser.error(f"Не удалось подготовить начальное содержимое раздела {section}.")
    application = Application(store, password_hash,
                              os.environ.get("NEWS_USERNAME", "admin"), origin, github)
    server = NewsHTTPServer(("127.0.0.1", 8787), make_handler(application))
    print("News API listening on 127.0.0.1:8787", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
