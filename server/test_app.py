"""Run from the checkout with: python -m unittest discover -s server -v"""

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import subprocess
import sqlite3
import re
from unittest.mock import patch

import app


PNG = b"\x89PNG\r\n\x1a\n" + b"sample-image-data"
ORIGIN = "https://donomas.github.io"
NEWS_URL = ORIGIN + "/massage-site/news.html"
TEMPLATE = ('<!doctype html><html><head><meta name="robots" content="noindex">'
            f'<link rel="canonical" href="{NEWS_URL}"></head><body>'
            '<main><!-- NEWS:START -->empty<!-- NEWS:END --></main></body></html>')
SITEMAP = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f'<url><loc>{ORIGIN}/massage-site/</loc></url></urlset>')


def section_template(section):
    template = TEMPLATE.replace("news.html", section + ".html").replace("NEWS:", "CONTENT:")
    if section == "services":
        schema = {"@context": "https://schema.org", "@graph": [
            {"@type": "Person", "@id": ORIGIN + "/massage-site/#person", "name": "Александр Голованов"},
            {"@type": "ItemList", "name": "Виды массажа в Ангарске", "itemListElement": []},
            {"@type": "Service", "@id": "obsolete-service", "areaServed": {"@type": "City", "name": "Ангарск"}},
        ]}
        template = template.replace("</head>", '<script type="application/ld+json" id="services-schema">'
                                    + json.dumps(schema) + "</script></head>")
    return template


def card(**changes):
    return {"id": "news-1", "title": "Заголовок", "text": "Первая строка\nВторая строка",
            "date": "2026-11-20", "media": [], **changes}


class FakeGitHub(app.GitHub):
    def __init__(self):
        super().__init__("unused-test-token", "Donomas/massage-site", "main")
        self.files = {"news.html": TEMPLATE.encode(), "sitemap.xml": SITEMAP.encode()}
        self.files.update({section + ".html": section_template(section).encode()
                           for section in ("offers", "services", "programs")})
        self.head = "original-commit"
        self.blobs = {}
        self.calls = []
        self.commits = 0
        self.fail_ref = False
        self.timeout_after_ref = False

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET" and path.startswith("/git/ref/"):
            return {"object": {"sha": self.head}}
        if method == "GET" and path.startswith("/git/commits/"):
            return {"tree": {"sha": "current-tree"}}
        if method == "GET" and path.startswith("/git/trees/"):
            return {"tree": [{"path": path} for path in self.files]}
        if method == "POST" and path == "/git/blobs":
            content = base64.b64decode(payload["content"])
            sha = hashlib.sha256(content).hexdigest()
            self.blobs[sha] = content
            return {"sha": sha}
        if method == "POST" and path == "/git/trees":
            self.pending_files = self.files.copy()
            for entry in payload["tree"]:
                self.pending_files[entry["path"]] = self.blobs[entry["sha"]]
            return {"sha": "pending-tree"}
        if method == "POST" and path == "/git/commits":
            self.commits += 1
            return {"sha": f"published-commit-{self.commits}"}
        if method == "PATCH":
            if self.fail_ref:
                raise app.APIError(409, "github_conflict", "test conflict")
            if payload["force"] is not False:
                raise AssertionError("force update is forbidden")
            self.files = self.pending_files
            self.head = payload["sha"]
            if self.timeout_after_ref:
                self.timeout_after_ref = False
                raise app.APIError(502, "github_unavailable", "simulated lost response")
            return {"object": {"sha": self.head}}
        raise AssertionError((method, path))

    def read_file(self, path, commit):
        return self.files[path].decode()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = app.Store(Path(self.temp.name))

    def test_password_hash_and_verification(self):
        encoded = app.hash_password("strong test password")
        self.assertTrue(app.check_password("strong test password", encoded))
        self.assertFalse(app.check_password("wrong", encoded))
        self.assertFalse(app.check_password("wrong", "scrypt$999999$8$1$bad$bad"))

    def test_concurrent_save_allows_only_one_writer(self):
        gate = threading.Barrier(2)

        def write(title):
            gate.wait()
            try:
                return self.store.save([card(title=title)], 0)["revision"]
            except app.APIError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, ["Первый", "Второй"]))
        self.assertCountEqual(results, [1, 409])
        self.assertEqual(self.store.draft()["revision"], 1)

    def test_media_upload_and_card_reference(self):
        media = self.store.upload("photo.png", base64.b64encode(PNG).decode())
        value = self.store.validate_cards([card(media=[media])])
        self.assertEqual(value[0]["media"][0], media)
        filename = media["url"].rsplit("/", 1)[1]
        self.assertEqual((self.store.media_dir / filename).read_bytes(), PNG)
        wrong_url = {**media, "url": "https://example.org/track.png"}
        with self.assertRaises(app.APIError):
            self.store.validate_cards([card(media=[wrong_url])])

    def test_invalid_uploads_are_not_saved(self):
        for data in ["not-base64", base64.b64encode(b"<svg>untrusted</svg>").decode(), ""]:
            with self.subTest(data=data), self.assertRaises(app.APIError):
                self.store.upload("image.png", data)
        self.assertEqual(list(self.store.media_dir.iterdir()), [])

    def test_quota_rejects_upload_without_orphan_and_serializes_concurrency(self):
        store = app.Store(Path(self.temp.name) / "quota", media_quota_bytes=len(PNG))
        gate = threading.Barrier(2)

        def upload(_):
            gate.wait()
            try:
                store.upload("photo.png", base64.b64encode(PNG).decode())
                return 201
            except app.APIError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(upload, range(2)))
        self.assertCountEqual(results, [201, 507])
        self.assertEqual(len(list(store.media_dir.iterdir())), 1)
        with store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM media").fetchone()[0], 1)
        with self.assertRaises(app.APIError) as error:
            store.upload("photo.png", base64.b64encode(PNG).decode())
        self.assertEqual((error.exception.status, error.exception.code), (507, "storage_full"))
        self.assertEqual(len(list(store.media_dir.iterdir())), 1)

    def test_individual_file_limit_is_preserved_with_quota(self):
        with patch.object(app, "MAX_FILE", 12):
            with self.assertRaises(app.APIError) as error:
                self.store.upload("photo.png", base64.b64encode(PNG).decode())
        self.assertEqual((error.exception.status, error.exception.code), (413, "file_too_large"))
        self.assertEqual(list(self.store.media_dir.iterdir()), [])

    def test_invalid_cards(self):
        for value in [[card(date="2026-02-31")], [card(title="")], [card(text="x" * 10001)],
                      [card(), card()], [card(media=[{}] * 6)], [card(id="<script>")], [card()] * 51]:
            with self.subTest(value=str(value)[:50]), self.assertRaises(app.APIError):
                self.store.validate_cards(value)

    def test_render_escapes_content_and_marks_media(self):
        cards = [card(title='<img src=x onerror="alert(1)">', text="<script>bad()</script>\nline",
                      media=[{"id": "abc", "url": "./assets/news/abc.png", "kind": "image", "name": 'x" onload="bad'},
                             {"id": "def", "url": "./assets/news/def.mp4", "kind": "video", "name": "clip"}])]
        html = app.render_cards(cards)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn('datetime="2026-11-20"', html)
        self.assertIn('loading="lazy"', html)
        self.assertIn('controls preload="metadata"', html)
        self.assertIn('class="news-body"', html)

    def test_page_and_sitemap_toggle_when_news_removed(self):
        populated = app.prepare_page(TEMPLATE, [card()])
        self.assertIn('content="index, follow"', populated)
        empty = app.prepare_page(populated, [])
        self.assertIn('content="noindex"', empty)
        self.assertNotIn("Заголовок", empty)
        sitemap = app.prepare_sitemap(SITEMAP, NEWS_URL, True)
        self.assertEqual(sitemap.count(NEWS_URL), 1)
        self.assertEqual(app.prepare_sitemap(sitemap, NEWS_URL, True).count(NEWS_URL), 1)
        self.assertNotIn(NEWS_URL, app.prepare_sitemap(sitemap, NEWS_URL, False))
        with self.assertRaises(app.APIError):
            app.prepare_page("<main></main>", [card()])

    def test_atomic_publication_omits_unattached_drafts(self):
        media = self.store.upload("public.png", base64.b64encode(PNG).decode())
        unused = self.store.upload("private.png", base64.b64encode(PNG).decode())
        self.store.save([card(media=[media])], 0)
        github = FakeGitHub()
        application = app.Application(self.store, "", github=github)
        result = application.publish(1)
        self.assertEqual(self.store.publish_status()["commit"], result["commit"])
        data = json.loads(github.files["content/news.json"])
        self.assertEqual(data["revision"], 1)
        self.assertTrue(data["cards"][0]["media"][0]["url"].startswith("./assets/news/"))
        self.assertTrue(any(path.endswith(media["id"] + ".png") for path in github.files))
        self.assertFalse(any(unused["id"] in path for path in github.files))
        self.assertEqual(sum(method == "PATCH" for method, _, _ in github.calls), 1)
        self.assertEqual(self.store.draft()["cards"][0]["media"][0]["url"], media["url"])

    def test_publish_conflict_preserves_draft_and_last_publication(self):
        self.store.save([card()], 0)
        self.store.mark_published("previous", NEWS_URL, 0)
        github = FakeGitHub()
        github.fail_ref = True
        application = app.Application(self.store, "", github=github)
        with self.assertRaises(app.APIError):
            application.publish(1)
        self.assertEqual(github.head, "original-commit")
        self.assertEqual(self.store.publish_status()["commit"], "previous")
        self.assertEqual(self.store.draft()["revision"], 1)

    def test_timeout_after_git_commit_recovers_without_second_commit(self):
        self.store.save([card()], 0)
        github = FakeGitHub()
        github.timeout_after_ref = True
        application = app.Application(self.store, "", github=github)
        with self.assertRaises(app.APIError):
            application.publish(1)
        self.assertEqual(self.store.publish_status()["status"], "never")
        result = application.publish(1)
        self.assertEqual(result["commit"], "published-commit-1")
        self.assertEqual(github.commits, 1)
        self.assertEqual(self.store.publish_status()["commit"], result["commit"])

    def test_stale_publish_does_not_touch_github(self):
        github = FakeGitHub()
        with self.assertRaises(app.APIError) as error:
            app.Application(self.store, "", github=github).publish(1)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(github.calls, [])

    def test_rate_limiter_memory_stays_bounded_after_global_limit(self):
        application = app.Application(self.store, "invalid-test-hash")
        for index in range(400):
            with self.assertRaises(app.APIError):
                application.login("admin", "wrong", f"peer-{index}")
        self.assertEqual(len(application.login_attempts), 256)
        self.assertEqual(len(application.global_attempts), 30)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = app.hash_password("test password")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.application = app.Application(app.Store(Path(self.temp.name)), self.password_hash, allowed_origin=ORIGIN)
        self.server = app.NewsHTTPServer(("127.0.0.1", 0), app.make_handler(self.application))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.stop_server, thread)

    def stop_server(self, thread):
        self.server.shutdown()
        self.server.server_close()
        thread.join()

    def request(self, method, path, data=None, token=None, origin=ORIGIN):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Origin": origin}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            connection.request(method, path, body=app.json_bytes(data) if data is not None else None, headers=headers)
            response = connection.getresponse()
            body = response.read()
            if response.getheader("Content-Type", "").startswith("application/json"):
                body = json.loads(body)
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    def login(self):
        status, _, body = self.request("POST", "/login", {"username": "admin", "password": "test password"})
        self.assertEqual(status, 200)
        return body["token"]

    def test_auth_origin_logout_and_expiry(self):
        self.assertEqual(self.request("GET", "/health")[0], 200)
        self.assertEqual(self.request("GET", "/draft")[0], 401)
        self.assertEqual(self.request("POST", "/login", {"username": "admin", "password": "wrong"})[0], 401)
        token = self.login()
        status, headers, body = self.request("GET", "/draft", token=token)
        self.assertEqual((status, body), (200, {"cards": [], "revision": 0}))
        self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)
        denied = self.request("GET", "/draft", token=token, origin="https://attacker.test")
        self.assertEqual(denied[0], 403)
        self.assertNotIn("Access-Control-Allow-Origin", denied[1])
        self.assertEqual(self.request("POST", "/logout", {}, token=token)[0], 200)
        self.assertEqual(self.request("GET", "/draft", token=token)[0], 401)
        token = self.login()
        self.application.sessions[hashlib.sha256(token.encode()).hexdigest()] = time.monotonic() - 1
        self.assertEqual(self.request("GET", "/draft", token=token)[0], 401)

    def test_draft_upload_protection_and_optimistic_api(self):
        token = self.login()
        upload = self.request("POST", "/upload", {"name": "photo.png", "data": base64.b64encode(PNG).decode()}, token)[2]
        self.assertEqual(self.request("GET", upload["url"])[0], 401)
        self.assertEqual(self.request("GET", upload["url"], token=token)[2], PNG)
        data = {"cards": [card(media=[upload])], "revision": 0}
        self.assertEqual(self.request("PUT", "/draft", data, token)[2]["revision"], 1)
        self.assertEqual(self.request("PUT", "/draft", data, token)[0], 409)
        self.assertEqual(self.request("POST", "/publish", {"revision": 0}, token)[0], 409)
        self.assertEqual(self.request("POST", "/publish", {"revision": 1}, token)[0], 503)
        self.assertEqual(self.request("GET", "/publish-status", token=token)[2]["status"], "never")

    def test_login_rate_limit_and_preflight(self):
        for _ in range(5):
            self.assertEqual(self.request("POST", "/login", {"username": "admin", "password": "wrong"})[0], 401)
        self.assertEqual(self.request("POST", "/login", {"username": "admin", "password": "test password"})[0], 429)
        status, headers, _ = self.request("OPTIONS", "/draft")
        self.assertEqual(status, 204)
        self.assertIn("Authorization", headers["Access-Control-Allow-Headers"])
        self.assertNotIn("Access-Control-Allow-Credentials", headers)

    def test_section_api_isolation_and_invalid_section(self):
        token = self.login()
        services = [card(id="classic-full", duration="90 минут")]
        for section in ("offers", "services", "programs"):
            body = {"cards": services if section == "services" else [card()], "revision": 0, "section": section}
            self.assertEqual(self.request("PUT", "/draft", body, token)[0], 200)
            response = self.request("GET", "/draft?section=" + section, token=token)
            self.assertEqual(response[2]["cards"], body["cards"])
            self.assertEqual(self.request("GET", "/publish-status?section=" + section, token=token)[2]["status"], "never")
        self.assertEqual(self.request("GET", "/draft", token=token)[2], {"cards": [], "revision": 0})
        for section in ("unknown", "", "services&section=news", "../news"):
            self.assertEqual(self.request("GET", "/draft?section=" + section, token=token)[0], 400)
        for section in ("unknown", "", None, [], {}):
            self.assertEqual(self.request("PUT", "/draft", {"cards": [], "revision": 0, "section": section}, token)[0], 400)
            self.assertEqual(self.request("POST", "/publish", {"revision": 0, "section": section}, token)[0], 400)


class SectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = app.Store(self.directory / "private")

    def test_populated_legacy_database_migrates_once_without_data_loss(self):
        legacy = self.directory / "legacy"
        legacy.mkdir()
        with sqlite3.connect(legacy / "news.sqlite3") as db:
            db.executescript("""
                CREATE TABLE state(singleton INTEGER PRIMARY KEY,cards TEXT,revision INTEGER,
                    published_commit TEXT,published_url TEXT,published_revision INTEGER);
                CREATE TABLE media(id TEXT PRIMARY KEY,extension TEXT,kind TEXT,name TEXT);
            """)
            db.execute("INSERT INTO state VALUES(1,?,?,?,?,?)", (json.dumps([card()]), 7, "legacy-commit", NEWS_URL, 6))
            db.execute("INSERT INTO media VALUES(?,?,?,?)", ("a" * 32, "png", "image", "legacy.png"))
        db.close()
        store = app.Store(legacy)
        self.assertEqual(store.draft(), {"cards": [card()], "revision": 7})
        self.assertEqual(store.publish_status()["commit"], "legacy-commit")
        self.assertEqual(store.media("a" * 32)["name"], "legacy.png")
        store.save([card(title="Updated")], 7)
        reopened = app.Store(legacy)
        self.assertEqual(reopened.draft()["revision"], 8)
        self.assertEqual(reopened.draft()["cards"][0]["title"], "Updated")

    def test_seed_once_and_independent_revisions_and_status(self):
        services = [card(id="first", duration="90 минут"), card(id="second", duration="30 минут")]
        self.assertTrue(self.store.seed_section("services", services))
        self.assertFalse(self.store.seed_section("services", []))
        self.store.save([card()], 0)
        self.store.save([card(title="Акция")], 0, "offers")
        self.store.save(list(reversed(services)), 0, "services")
        self.assertEqual(self.store.draft("services")["cards"][0]["id"], "second")
        self.assertEqual(self.store.draft("offers")["revision"], 1)
        self.assertEqual(self.store.draft("programs")["revision"], 0)
        self.store.mark_published("offer-commit", "https://example.test/offers.html", 1, "offers")
        self.assertEqual(self.store.publish_status("offers")["commit"], "offer-commit")
        self.assertEqual(self.store.publish_status()["status"], "never")

    def test_duration_is_optional_only_for_services_and_price_is_rejected(self):
        self.assertEqual(self.store.validate_cards([card()], "services"), [card()])
        self.assertEqual(self.store.validate_cards([card(duration="40–60 минут")], "services")[0]["duration"], "40–60 минут")
        for section in ("news", "offers", "programs"):
            with self.assertRaises(app.APIError):
                self.store.validate_cards([card(duration="30 минут")], section)
        for value in (None, 90, "x" * 81):
            with self.assertRaises(app.APIError):
                self.store.validate_cards([card(duration=value)], "services")
        for section in app.SECTIONS:
            with self.assertRaises(app.APIError):
                self.store.validate_cards([card(price=100)], section)

    def test_service_render_and_schema_match_visible_cards(self):
        services = [card(id="classic", duration="90 минут", title="Тест </script><script>bad()</script>"),
                    card(id="other", duration="30 минут")]
        rendered = app.prepare_page(section_template("services"), services, "services")
        self.assertIn('class="service-grid"', rendered)
        self.assertIn('class="service-card" id="classic"', rendered)
        self.assertIn('<p class="service-duration">90 минут</p>', rendered)
        self.assertNotIn("<time", rendered)
        self.assertNotIn("obsolete-service", rendered)
        self.assertNotIn("<script>bad()", rendered)
        match = re.search(r'<script[^>]+id="services-schema">(.*?)</script>', rendered, re.S)
        graph = json.loads(match[1])["@graph"]
        self.assertEqual(graph[0]["@type"], "Person")
        nodes = [node for node in graph if node["@type"] == "Service"]
        page = NEWS_URL.replace("news.html", "services.html")
        self.assertEqual([node["@id"] for node in nodes], [page + "#classic", page + "#other"])
        self.assertIn("90 минут", nodes[0]["description"])
        removed = app.prepare_page(rendered, [], "services")
        self.assertIn('content="noindex"', removed)
        self.assertNotIn(page + "#classic", removed)
        self.assertNotIn("<time", app.render_cards([card()], "programs"))
        self.assertIn("<time", app.render_cards([card()], "offers"))

    def test_rest_publish_changes_only_selected_page_and_keeps_other_sitemap_urls(self):
        github = FakeGitHub()
        application = app.Application(self.store, "", github=github)
        self.store.save([card()], 0)
        application.publish(1)
        before = github.files.copy()
        media = self.store.upload("offer.png", base64.b64encode(PNG).decode())
        self.store.save([card(media=[media])], 0, "offers")
        result = application.publish(1, "offers")
        changed = {path for path in github.files if before.get(path) != github.files[path]}
        filename = media["url"].rsplit("/", 1)[1]
        self.assertEqual(changed, {"offers.html", "content/offers.json", "sitemap.xml", "assets/offers/" + filename})
        self.assertIn(NEWS_URL, github.files["sitemap.xml"].decode())
        self.assertIn(NEWS_URL.replace("news.html", "offers.html"), github.files["sitemap.xml"].decode())
        self.assertEqual(self.store.publish_status("offers")["commit"], result["commit"])
        repeated = application.publish(1, "offers")
        self.assertEqual(repeated, result)
        self.assertEqual(github.commits, 2)
        self.store.save([], 1, "offers")
        application.publish(2, "offers")
        self.assertIn(NEWS_URL, github.files["sitemap.xml"].decode())
        self.assertNotIn(NEWS_URL.replace("news.html", "offers.html"), github.files["sitemap.xml"].decode())

    def test_current_service_seed_and_template_remain_renderable(self):
        root = Path(__file__).resolve().parent.parent
        seed = json.loads((root / "content/services.json").read_text(encoding="utf-8"))["cards"]
        self.store.seed_section("services", seed)
        self.assertEqual(len(self.store.draft("services")["cards"]), 7)
        template = (root / "services.html").read_text(encoding="utf-8")
        rendered = app.prepare_page(template, seed, "services")
        self.assertEqual(rendered.count('class="service-card"'), 7)
        self.assertNotIn("<time", rendered)


class GitSSHTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.repository = self.root / "repository"
        self.git("init", "--bare", "--initial-branch=main", str(self.remote), cwd=self.root)
        self.git("clone", str(self.remote), str(self.repository), cwd=self.root)
        self.git("config", "user.name", "Test Editor")
        self.git("config", "user.email", "editor@example.test")
        (self.repository / "news.html").write_text(TEMPLATE, encoding="utf-8")
        (self.repository / "sitemap.xml").write_text(SITEMAP, encoding="utf-8")
        (self.repository / "keep.txt").write_text("preserve", encoding="utf-8")
        for section in ("offers", "services", "programs"):
            (self.repository / (section + ".html")).write_text(section_template(section), encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "Initial site")
        self.git("push", "origin", "main")
        self.store = app.Store(self.root / "private")
        self.store.save([card()], 0)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repository, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def test_publish_uses_only_intended_git_objects_and_retries(self):
        (self.repository / "keep.txt").write_text("local edit must stay private", encoding="utf-8")
        publisher = app.GitSSH(self.repository)
        application = app.Application(self.store, "", github=publisher)
        result = application.publish(1)
        published = self.git("--git-dir=" + str(self.remote), "show", result["commit"] + ":content/news.json")
        self.assertEqual(json.loads(published)["cards"][0]["title"], "Заголовок")
        kept = self.git("--git-dir=" + str(self.remote), "show", result["commit"] + ":keep.txt")
        self.assertEqual(kept, b"preserve")
        self.assertEqual((self.repository / "keep.txt").read_text(encoding="utf-8"), "local edit must stay private")
        repeated = application.publish(1)
        self.assertEqual(repeated["commit"], result["commit"])

    def test_nonforce_push_rejects_concurrent_change(self):
        repository, git = self.repository, self.git

        class ConcurrentPublisher(app.GitSSH):
            def run(self, *args, **kwargs):
                if args[0] == "push":
                    (repository / "keep.txt").write_text("concurrent change", encoding="utf-8")
                    git("add", "keep.txt")
                    git("commit", "-m", "Concurrent edit")
                    git("push", "origin", "main")
                return super().run(*args, **kwargs)

        application = app.Application(self.store, "", github=ConcurrentPublisher(self.repository))
        with self.assertRaises(app.APIError) as error:
            application.publish(1)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.store.publish_status()["status"], "never")
        content = self.git("--git-dir=" + str(self.remote), "show", "main:keep.txt")
        self.assertEqual(content, b"concurrent change")

    def test_push_timeout_retry_recovers_head(self):
        class TimeoutPublisher(app.GitSSH):
            lost_response = True

            def run(self, *args, **kwargs):
                result = super().run(*args, **kwargs)
                if args[0] == "push" and self.lost_response:
                    self.lost_response = False
                    raise app.APIError(502, "git_unavailable", "simulated timeout")
                return result

        publisher = TimeoutPublisher(self.repository)
        application = app.Application(self.store, "", github=publisher)
        with self.assertRaises(app.APIError):
            application.publish(1)
        previous_head = self.git("--git-dir=" + str(self.remote), "rev-parse", "main").decode().strip()
        self.assertEqual(self.store.publish_status()["status"], "never")
        recovered = application.publish(1)
        self.assertEqual(recovered["commit"], previous_head)
        self.assertEqual(self.store.publish_status()["commit"], previous_head)

    def test_ssh_section_commit_is_isolated_and_preserves_news(self):
        application = app.Application(self.store, "", github=app.GitSSH(self.repository))
        news = application.publish(1)
        self.store.save([card(id="classic", duration="90 минут")], 0, "services")
        services = application.publish(1, "services")
        changed = self.git("--git-dir=" + str(self.remote), "diff", "--name-only", news["commit"], services["commit"]).decode().splitlines()
        self.assertCountEqual(changed, ["services.html", "content/services.json", "sitemap.xml"])
        published = self.git("--git-dir=" + str(self.remote), "show", services["commit"] + ":content/services.json")
        data = json.loads(published)
        self.assertEqual(data["section"], "services")
        self.assertEqual(data["cards"][0]["duration"], "90 минут")
        self.assertEqual(self.store.publish_status()["commit"], news["commit"])
        self.assertEqual(application.publish(1, "services")["commit"], services["commit"])


if __name__ == "__main__":
    unittest.main()
