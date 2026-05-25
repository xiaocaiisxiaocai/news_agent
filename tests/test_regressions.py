import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config_store as cfg
import storage
import web.app as webapp
from fetchers.fetch import fetch_url
from processors.batch import process_one


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="news-agent-regression-")
        self.old_cfg_db = cfg.DB_PATH
        self.old_storage_db = storage.DB_PATH
        self.old_briefs_dir = webapp._BRIEFS_DIR
        self.old_jobs = webapp._jobs
        self.old_secret = webapp.app.secret_key
        cfg.DB_PATH = os.path.join(self.tmp.name, "news_agent.db")
        storage.DB_PATH = cfg.DB_PATH
        storage._db_initialized = False
        webapp._BRIEFS_DIR = os.path.join(self.tmp.name, "briefs")
        webapp._jobs = {}
        cfg.init_config()
        storage.init_db()
        webapp.app.config.update(TESTING=True)
        webapp.app.secret_key = "test-secret"
        self.client = webapp.app.test_client()

    def tearDown(self):
        cfg.DB_PATH = self.old_cfg_db
        storage.DB_PATH = self.old_storage_db
        storage._db_initialized = False
        webapp._BRIEFS_DIR = self.old_briefs_dir
        webapp._jobs = self.old_jobs
        webapp.app.secret_key = self.old_secret
        self.tmp.cleanup()

    def save_article(self, title, url, category, importance, conclusion=""):
        return storage.save_article({
            "title": title,
            "url": url,
            "category": category,
            "importance": importance,
            "language": "中文",
            "keywords": [category],
            "summary_raw": conclusion,
            "summary": {"conclusion": conclusion, "points": [], "action": ""},
        })

    def test_brief_download_serves_existing_markdown_file(self):
        briefs = Path(webapp._BRIEFS_DIR)
        briefs.mkdir(parents=True, exist_ok=True)
        target = briefs / "brief-2026-05-25.md"
        target.write_text("# 简报\n", encoding="utf-8")

        resp = self.client.get("/briefs/brief-2026-05-25.md")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("# 简报", resp.get_data(as_text=True))
        resp.close()

    def test_briefs_page_lists_html_brief_download(self):
        briefs = Path(webapp._BRIEFS_DIR)
        briefs.mkdir(parents=True, exist_ok=True)
        (briefs / "brief-2026-05-25.md").write_text("# Markdown 简报\n", encoding="utf-8")
        (briefs / "brief-2026-05-25.html").write_text("<!doctype html><h1>HTML 简报</h1>", encoding="utf-8")

        resp = self.client.get("/briefs")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("/briefs/brief-2026-05-25.md", html)
        self.assertIn("/briefs/brief-2026-05-25.html", html)

    def test_article_page_total_uses_current_filters(self):
        self.save_article("AI A", "https://example.com/a", "科技/AI", 5, "AI news")
        self.save_article("Biz B", "https://example.com/b", "商业", 3, "Biz news")

        resp = self.client.get("/articles?cat=科技/AI")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("共 1 篇", resp.get_data(as_text=True))

    def test_setup_scheduler_registers_rss_and_brief_jobs(self):
        cfg.set("schedule.rss_time", "08:30")
        cfg.set("schedule.brief_time", "21:00")

        with patch("web.app.schedule.clear") as clear, \
             patch("web.app.schedule.every") as every:
            every.return_value.day.at.return_value.do.return_value.tag.return_value = None
            count = webapp.setup_scheduler(start_thread=False)

        clear.assert_called_once_with("news_agent")
        self.assertEqual(count, 2)
        self.assertEqual(every.return_value.day.at.call_count, 2)

    def test_run_persists_generated_secret_key(self):
        with patch("web.app.init_db"), \
             patch("web.app.setup_scheduler"), \
             patch.object(webapp.app, "run") as app_run:
            webapp.run()

        first = cfg.get("web.secret_key")
        self.assertTrue(first)

        with patch("web.app.init_db"), \
             patch("web.app.setup_scheduler"), \
             patch.object(webapp.app, "run") as app_run:
            webapp.run()

        self.assertEqual(cfg.get("web.secret_key"), first)
        self.assertEqual(webapp.app.secret_key, first)

    def test_saving_schedule_config_reloads_scheduler(self):
        with patch("web.app.setup_scheduler") as setup_scheduler:
            resp = self.client.post("/api/config", json={"schedule.rss_time": "09:30"})

        self.assertEqual(resp.status_code, 200)
        setup_scheduler.assert_called_once_with()

    def test_init_config_migrates_dead_geekpark_feed(self):
        cfg.set("rss.feeds", [
            "https://sspai.com/feed",
            "https://www.geekpark.net/rss",
        ])

        cfg.init_config()

        feeds = cfg.get_rss_feeds()
        self.assertNotIn("https://www.geekpark.net/rss", feeds)
        self.assertIn("https://www.infoq.cn/feed", feeds)

    def test_rss_sources_keep_legacy_feeds_and_enable_flags(self):
        cfg.set("rss.feeds", ["https://example.com/old.xml"])
        cfg.init_config()

        sources = cfg.get_rss_sources()
        old = [s for s in sources if s["url"] == "https://example.com/old.xml"][0]

        self.assertTrue(old["enabled"])
        self.assertEqual(old["category"], "其他")
        self.assertIn("rsshub.app/weibo/search/hot", "\n".join(s["url"] for s in sources))
        self.assertEqual(cfg.get_rss_feeds(), [s["url"] for s in sources if s["enabled"]])

    def test_rss_sources_are_persisted_in_database_table(self):
        cfg.init_config()

        with cfg._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM rss_sources").fetchone()[0]
            disabled_templates = conn.execute(
                "SELECT COUNT(*) FROM rss_sources WHERE enabled=0 AND url LIKE '%:%'"
            ).fetchone()[0]
            github = conn.execute(
                "SELECT category, source_type FROM rss_sources WHERE url=?",
                ("https://rsshub.app/github/trending/daily",),
            ).fetchone()

        self.assertGreaterEqual(total, 100)
        self.assertGreater(disabled_templates, 0)
        self.assertEqual(github["category"], "科技/AI")
        self.assertEqual(github["source_type"], "RSSHub")

    def test_fetch_all_rss_skips_disabled_sources_and_applies_category(self):
        cfg.set_rss_sources([
            {"name": "启用源", "url": "https://example.com/enabled.xml", "category": "科技/AI", "enabled": True},
            {"name": "停用源", "url": "https://example.com/disabled.xml", "category": "商业", "enabled": False},
        ])

        with patch("fetchers.fetch.fetch_rss_feed", return_value=[{"title": "A", "text": "正文", "url": "u"}]) as fetch:
            articles = webapp.fetch_all_rss()

        fetch.assert_called_once_with("https://example.com/enabled.xml", max_items=cfg.get_int("rss.max_per_feed", 5))
        self.assertEqual(articles[0]["source"], "启用源")
        self.assertEqual(articles[0]["category"], "科技/AI")

    def test_process_one_respects_rss_preset_category(self):
        article = {
            "title": "RSS 预设分类",
            "url": "https://example.com/preset",
            "text": "这是足够长的正文。" * 30,
            "category": "商业",
            "source": "测试源",
        }

        with patch("processors.summarize.process_article") as process_article:
            process_article.side_effect = lambda art: art
            result = process_one(article)

        self.assertEqual(result["category"], "商业")

    def test_rss_page_renders_category_and_enabled_controls(self):
        cfg.set_rss_sources([
            {"name": "测试源", "url": "https://example.com/feed.xml", "category": "科技/AI", "enabled": False},
        ])

        resp = self.client.get("/rss")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("https://example.com/feed.xml", html)
        self.assertIn("sources =", html)
        self.assertIn("type=\"checkbox\"", html)

    def test_rss_feeds_api_saves_structured_sources(self):
        resp = self.client.post("/api/rss-feeds", json={"sources": [
            {"name": "接口测试源", "url": "https://example.com/rss.xml", "category": "商业", "enabled": False}
        ]})

        sources = cfg.get_rss_sources()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["name"], "接口测试源")
        self.assertEqual(sources[0]["category"], "商业")
        self.assertFalse(sources[0]["enabled"])
        self.assertEqual(cfg.get_rss_feeds(), [])

    def test_rss_enable_ok_only_disables_failed_and_unknown_sources(self):
        cfg.set_rss_sources([
            {"name": "成功源", "url": "https://example.com/ok.xml", "category": "科技/AI", "enabled": True},
            {"name": "失败源", "url": "https://example.com/fail.xml", "category": "其他", "enabled": True},
            {"name": "未知源", "url": "https://example.com/unknown.xml", "category": "其他", "enabled": True},
        ])
        storage.record_rss_fetch("https://example.com/ok.xml", True, item_count=1)
        storage.record_rss_fetch("https://example.com/fail.xml", False, error="fail")

        resp = self.client.post("/api/rss-enable-ok-only", json={})
        sources = {s["url"]: s for s in cfg.get_rss_sources()}

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(sources["https://example.com/ok.xml"]["enabled"])
        self.assertFalse(sources["https://example.com/fail.xml"]["enabled"])
        self.assertFalse(sources["https://example.com/unknown.xml"]["enabled"])

    def test_run_rss_test_job_tests_enabled_sources_and_records_health(self):
        cfg.set_rss_sources([
            {"name": "成功源", "url": "https://example.com/ok.xml", "category": "科技/AI", "enabled": True},
            {"name": "失败源", "url": "https://example.com/fail.xml", "category": "其他", "enabled": True},
            {"name": "关闭源", "url": "https://example.com/off.xml", "category": "其他", "enabled": False},
        ])

        def fake_fetch(url, max_items=3):
            if "ok" in url:
                return [{"title": "OK", "text": "正文", "url": url}]
            return []

        with patch("web.app.fetch_rss_feed", side_effect=fake_fetch):
            jid = webapp.new_job("rss-test")
            webapp._run_rss_test_job(jid)

        job = webapp._jobs[jid]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["ok"], 1)
        self.assertEqual(job["result"]["failed"], 1)
        self.assertEqual(job["result"]["skipped"], 1)

        health = {h["feed_url"]: h for h in storage.get_rss_health()}
        self.assertEqual(health["https://example.com/ok.xml"]["status"], "ok")
        self.assertEqual(health["https://example.com/fail.xml"]["status"], "warning")
        self.assertNotIn("https://example.com/off.xml", health)

    def test_run_rss_test_job_keeps_fetcher_error_detail(self):
        cfg.set_rss_sources([
            {"name": "超时源", "url": "https://example.com/timeout.xml", "category": "其他", "enabled": True},
        ])
        storage.record_rss_fetch("https://example.com/timeout.xml", False, error="Connection timed out")

        with patch("web.app.fetch_rss_feed", return_value=[]):
            jid = webapp.new_job("rss-test")
            webapp._run_rss_test_job(jid)

        health = {h["feed_url"]: h for h in storage.get_rss_health()}
        self.assertIn("Connection timed out", health["https://example.com/timeout.xml"]["last_error"])

    def test_rss_page_shows_error_reason_category(self):
        cfg.set_rss_sources([
            {"name": "超时源", "url": "https://example.com/timeout.xml", "category": "其他", "enabled": True},
        ])
        storage.record_rss_fetch("https://example.com/timeout.xml", False, error="Connection timed out")

        resp = self.client.get("/rss")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("失败原因", html)
        self.assertIn("连接超时", html)


    def test_fetch_rss_feed_records_html_response_reason(self):
        class Resp:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            content = b"<!doctype html><html><body>blocked</body></html>"

        with patch("fetchers.fetch.requests.get", return_value=Resp()):
            from fetchers.fetch import fetch_rss_feed
            articles = fetch_rss_feed("https://example.com/feed.xml", max_items=3)

        health = {h["feed_url"]: h for h in storage.get_rss_health()}
        self.assertEqual(articles, [])
        self.assertIn("返回 HTML", health["https://example.com/feed.xml"]["last_error"])

    def test_rss_page_has_test_all_and_status_filter_controls(self):
        resp = self.client.get("/rss")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("测试全部", html)
        self.assertIn("status-filter", html)
        self.assertIn("只看成功", html)
        self.assertIn("只看失败", html)
        self.assertIn("未知", html)
        self.assertIn("仅启用成功源", html)
        self.assertIn("enableOnlySuccessful", html)

    def test_save_article_replaces_question_mark_garbled_title(self):
        storage.save_article({
            "title": "LLM ??????",
            "url": "https://example.com/garbled",
            "category": "科技/AI",
            "importance": 3,
            "summary": {
                "conclusion": "LLM 文本处理测试摘要",
                "points": [],
                "action": "",
            },
        })

        article = storage.get_articles(days=1, limit=1)[0]
        self.assertEqual(article["title"], "LLM 文本处理测试摘要")

    def test_site_specific_fetchers_extract_title_and_body(self):
        body = "这是一段用于站点抓取测试的正文内容，长度足够超过一百字。" * 8
        cases = [
            (
                "https://mp.weixin.qq.com/s/test",
                '<h1 class="rich_media_title">微信标题</h1><div id="js_content"><p>{}</p></div>',
                "微信标题",
                "微信公众号",
            ),
            (
                "https://juejin.cn/post/123",
                '<h1 class="article-title">掘金标题</h1><div class="markdown-body"><p>{}</p></div>',
                "掘金标题",
                "掘金",
            ),
            (
                "https://36kr.com/p/123",
                '<h1 class="article-title">36氪标题</h1><div class="articleDetailContent"><p>{}</p></div>',
                "36氪标题",
                "36氪",
            ),
        ]

        class Resp:
            status_code = 200
            headers = {}
            apparent_encoding = "utf-8"
            encoding = "utf-8"

            def __init__(self, html):
                self.text = f"<html><head><title>fallback</title></head><body>{html}</body></html>"

        for url, html_tpl, expected_title, expected_source in cases:
            with self.subTest(url=url), patch("fetchers.fetch.requests.get", return_value=Resp(html_tpl.format(body))):
                result = fetch_url(url)
                self.assertEqual(result["title"], expected_title)
                self.assertEqual(result["source"], expected_source)
                self.assertGreater(len(result["text"]), 100)
                self.assertNotIn("error", result)

    def test_site_fetcher_reports_403_as_access_denied(self):
        class Resp:
            status_code = 403
            text = ""
            apparent_encoding = "utf-8"

        with patch("fetchers.fetch.requests.get", return_value=Resp()):
            result = fetch_url("https://zhuanlan.zhihu.com/p/123")

        self.assertIn("拒绝访问", result["error"])

    def test_zhihu_url_is_not_advertised_as_special_fetcher(self):
        body = "这是一段用于知乎通用抓取测试的正文内容，长度足够超过一百字。" * 8

        class Resp:
            status_code = 200
            headers = {}
            apparent_encoding = "utf-8"
            encoding = "utf-8"
            text = f"<html><head><title>知乎通用标题</title></head><body><p>{body}</p></body></html>"

        with patch("fetchers.fetch.requests.get", return_value=Resp()):
            result = fetch_url("https://zhuanlan.zhihu.com/p/123")

        self.assertEqual(result["title"], "知乎通用标题")
        self.assertNotEqual(result.get("source"), "知乎")


if __name__ == "__main__":
    unittest.main()
