import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import config_store as cfg
import llm_client
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
        llm_client._client_cache = {}
        llm_client._last_call_times = {}
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
        llm_client._client_cache = {}
        llm_client._last_call_times = {}
        webapp.app.secret_key = self.old_secret
        self.tmp.cleanup()

    def save_article(self, title, url, category, importance, conclusion="", keywords=None):
        return storage.save_article({
            "title": title,
            "url": url,
            "category": category,
            "importance": importance,
            "language": "中文",
            "keywords": keywords or [category],
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

    def test_article_filter_accepts_custom_rss_category_from_config(self):
        cfg.set_config("pref.cat_weight", {"科技/AI": 0, "自定义分类": 0})
        self.save_article("Custom C", "https://example.com/custom", "自定义分类", 4, "Custom news")
        self.save_article("AI A", "https://example.com/ai", "科技/AI", 4, "AI news")

        articles = storage.get_articles(days=1, category="自定义分类")

        self.assertEqual([a["title"] for a in articles], ["Custom C"])

    def test_article_feedback_changes_recommendation_order(self):
        self.save_article("AI liked", "https://example.com/liked", "科技/AI", 3, "AI")
        self.save_article("Biz hidden", "https://example.com/hidden", "商业", 5, "Biz")
        articles = {a["title"]: a for a in storage.get_articles(days=1, limit=10)}

        storage.record_article_event(articles["AI liked"]["id"], "favorite")
        storage.record_article_event(articles["Biz hidden"]["id"], "hide")

        recommended = storage.get_recommended_articles(days=1, limit=10)

        self.assertEqual(recommended[0]["title"], "AI liked")
        self.assertGreater(recommended[0]["recommend_score"], recommended[1]["recommend_score"])
        self.assertEqual(recommended[-1]["title"], "Biz hidden")

    def test_article_event_api_records_feedback(self):
        self.save_article("Feedback target", "https://example.com/feedback", "科技/AI", 3, "AI")
        article = storage.get_articles(days=1, limit=1)[0]

        resp = self.client.post(f"/api/articles/{article['id']}/event", json={"event": "open"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(storage.get_article_event_counts(article["id"])["open"], 1)

    def test_recommendations_api_returns_recommend_score(self):
        self.save_article("Recommended target", "https://example.com/reco", "科技/AI", 3, "AI")

        resp = self.client.get("/api/recommendations?days=1&limit=5")
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data[0]["title"], "Recommended target")
        self.assertIn("recommend_score", data[0])

    def test_find_similar_articles_uses_keywords_and_conclusion(self):
        self.save_article(
            "OpenAI 模型发布",
            "https://example.com/memory-a",
            "科技/AI",
            5,
            "OpenAI 发布新的 GPT 大模型",
            keywords=["OpenAI", "GPT", "模型"],
        )
        self.save_article(
            "GPT 推理能力提升",
            "https://example.com/memory-b",
            "科技/AI",
            4,
            "GPT 大模型推理能力明显提升",
            keywords=["GPT", "模型", "推理"],
        )
        self.save_article(
            "电商促销活动",
            "https://example.com/memory-c",
            "商业",
            4,
            "电商平台推出促销活动",
            keywords=["电商", "促销"],
        )
        articles = {a["title"]: a for a in storage.get_articles(days=1, limit=10)}

        similar = storage.find_similar_articles(articles["OpenAI 模型发布"]["id"], limit=5, days=1)

        self.assertEqual(similar[0]["title"], "GPT 推理能力提升")
        self.assertGreater(similar[0]["similarity"], 0)
        self.assertIn("GPT", similar[0]["overlap_keywords"])
        self.assertIn("memory_reason", similar[0])

    def test_recommendations_include_memory_score_from_positive_feedback(self):
        self.save_article(
            "收藏过的 GPT 文章",
            "https://example.com/memory-liked",
            "科技/AI",
            3,
            "GPT 模型推理新闻",
            keywords=["GPT", "模型", "推理"],
        )
        self.save_article(
            "新的 GPT 推理文章",
            "https://example.com/memory-new",
            "科技/AI",
            3,
            "新的 GPT 模型推理能力更新",
            keywords=["GPT", "模型", "推理"],
        )
        self.save_article(
            "无关商业文章",
            "https://example.com/memory-unrelated",
            "商业",
            3,
            "商业渠道调整",
            keywords=["商业", "渠道"],
        )
        articles = {a["title"]: a for a in storage.get_articles(days=1, limit=10)}
        storage.record_article_event(articles["收藏过的 GPT 文章"]["id"], "favorite")

        recommended = storage.get_recommended_articles(days=1, limit=10)
        target = next(a for a in recommended if a["title"] == "新的 GPT 推理文章")

        self.assertGreater(target["memory_score"], 0)
        self.assertIn("相似", target["recommend_reason"])
        self.assertIn("收藏", target["recommend_reason"])

    def test_recommendations_penalize_hidden_similar_articles(self):
        self.save_article(
            "隐藏过的营销文章",
            "https://example.com/memory-hidden",
            "商业",
            3,
            "广告营销活动",
            keywords=["广告", "营销"],
        )
        self.save_article(
            "新的广告营销文章",
            "https://example.com/memory-ad",
            "商业",
            3,
            "广告营销玩法",
            keywords=["广告", "营销"],
        )
        articles = {a["title"]: a for a in storage.get_articles(days=1, limit=10)}
        storage.record_article_event(articles["隐藏过的营销文章"]["id"], "hide")

        recommended = storage.get_recommended_articles(days=1, limit=10)
        target = next(a for a in recommended if a["title"] == "新的广告营销文章")

        self.assertLess(target["memory_score"], 0)
        self.assertIn("相似", target["recommend_reason"])
        self.assertIn("隐藏", target["recommend_reason"])

    def test_notification_channel_crud_and_send_webhook_records_log(self):
        channel_id = storage.save_notification_channel({
            "name": "测试 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        self.save_article("Webhook article", "https://example.com/webhook", "科技/AI", 5, "Webhook summary")
        article = storage.get_articles(days=1, limit=1)[0]

        with patch("web.app.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.text = "ok"
            result = webapp.send_article_notification(channel_id, article["id"])

        self.assertTrue(result["ok"])
        post.assert_called_once()
        logs = storage.get_notification_logs(channel_id=channel_id)
        self.assertEqual(logs[0]["status"], "ok")
        self.assertIn("Webhook article", logs[0]["payload"])

    def test_send_email_notification_uses_existing_smtp_config_and_records_log(self):
        cfg.set_many({
            "notify.email.enabled": "true",
            "notify.email.smtp_server": "smtp.example.com",
            "notify.email.smtp_port": "465",
            "notify.email.username": "sender@example.com",
            "notify.email.password": "secret",
            "notify.email.from": "sender@example.com",
        })
        channel_id = storage.save_notification_channel({
            "name": "测试邮件",
            "channel_type": "email",
            "target": "reader@example.com",
            "enabled": True,
        })
        self.save_article("Email article", "https://example.com/email", "科技/AI", 5, "Email summary")
        article = storage.get_articles(days=1, limit=1)[0]

        with patch("web.app.smtplib.SMTP_SSL") as smtp:
            result = webapp.send_article_notification(channel_id, article["id"])

        self.assertTrue(result["ok"])
        smtp.return_value.__enter__.return_value.login.assert_called_once_with("sender@example.com", "secret")
        smtp.return_value.__enter__.return_value.send_message.assert_called_once()
        self.assertEqual(storage.get_notification_logs(channel_id=channel_id)[0]["status"], "ok")

    def test_notification_api_excludes_telegram_channels(self):
        resp = self.client.post("/api/notification-channels", json={
            "name": "Telegram",
            "channel_type": "telegram",
            "target": "bot-token",
            "enabled": True,
        })

        self.assertEqual(resp.status_code, 400)

    def test_notification_channel_api_updates_toggles_deletes_and_validates_target(self):
        invalid = self.client.post("/api/notification-channels", json={
            "name": "无效 Webhook",
            "channel_type": "webhook",
            "target": "not-a-url",
            "enabled": True,
        })
        self.assertEqual(invalid.status_code, 400)

        created = self.client.post("/api/notification-channels", json={
            "name": "旧渠道",
            "channel_type": "webhook",
            "target": "https://example.com/old",
            "enabled": True,
        })
        channel_id = created.get_json()["id"]

        updated = self.client.put(f"/api/notification-channels/{channel_id}", json={
            "name": "新渠道",
            "channel_type": "webhook",
            "target": "https://example.com/new",
            "enabled": False,
        })
        channel = storage.get_notification_channel(channel_id)

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(channel["name"], "新渠道")
        self.assertEqual(channel["target"], "https://example.com/new")
        self.assertEqual(channel["enabled"], 0)

        deleted = self.client.delete(f"/api/notification-channels/{channel_id}")

        self.assertEqual(deleted.status_code, 200)
        self.assertIsNone(storage.get_notification_channel(channel_id))

    def test_notification_channels_include_last_test_status(self):
        channel_id = storage.save_notification_channel({
            "name": "体检 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        storage.record_notification_log(channel_id, None, "test_ok", "HTTP 200")

        channels = storage.get_notification_channels()

        self.assertEqual(channels[0]["last_test_status"], "test_ok")
        self.assertIn("HTTP 200", channels[0]["last_test_payload"])
        self.assertTrue(channels[0]["last_test_at"])

    def test_notification_health_summary_reports_config_and_recent_results(self):
        channel_id = storage.save_notification_channel({
            "name": "健康 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        storage.record_notification_log(channel_id, None, "ok", "payload")
        storage.record_notification_log(channel_id, None, "error", "payload", "失败原因")

        summary = webapp.get_notification_health_summary()

        self.assertEqual(summary["enabled_channels"], 1)
        self.assertFalse(summary["smtp_ready"])
        self.assertIn("SMTP", summary["smtp_status"])
        self.assertTrue(summary["last_success_at"])
        self.assertEqual(summary["last_failure_error"], "失败原因")

    def test_notification_test_all_enabled_channels_records_results(self):
        self.save_article("体检文章", "https://example.com/health-article", "科技/AI", 5, "Health")
        storage.save_notification_channel({
            "name": "启用 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        storage.save_notification_channel({
            "name": "停用 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/off",
            "enabled": False,
        })

        with patch("web.app.send_article_notification", return_value={"ok": True}) as send:
            resp = self.client.post("/api/notification-channels/test-all")

        data = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data["tested"], 1)
        self.assertEqual(data["ok"], 1)
        self.assertEqual(data["failed"], 0)
        send.assert_called_once()

    def test_notifications_page_renders_health_summary_and_test_all_control(self):
        storage.save_notification_channel({
            "name": "页面体检 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })

        resp = self.client.get("/notifications")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("配置健康", html)
        self.assertIn("启用渠道", html)
        self.assertIn("SMTP 状态", html)
        self.assertIn("测试全部启用渠道", html)
        self.assertIn("testAllChannels", html)

    def test_notification_test_send_validates_email_smtp_config(self):
        channel_id = storage.save_notification_channel({
            "name": "测试邮件",
            "channel_type": "email",
            "target": "reader@example.com",
            "enabled": True,
        })

        resp = self.client.post(f"/api/notification-channels/{channel_id}/test")
        data = resp.get_json()

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])
        self.assertIn("SMTP 配置不完整", data["error"])

    def test_send_recommended_without_enabled_channels_returns_clear_error(self):
        self.save_article("No channel recommendation", "https://example.com/no-channel", "科技/AI", 5, "No channel")

        resp = self.client.post("/api/notifications/send-recommended", json={"limit": 1, "days": 1})
        data = resp.get_json()

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])
        self.assertIn("没有启用的通知渠道", data["error"])

    def test_notification_logs_include_names_and_status_filter(self):
        channel_id = storage.save_notification_channel({
            "name": "日志 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        self.save_article("日志文章", "https://example.com/log", "科技/AI", 5, "Log summary")
        article = storage.get_articles(days=1, limit=1)[0]
        storage.record_notification_log(channel_id, article["id"], "ok", "ok payload")
        storage.record_notification_log(channel_id, article["id"], "error", "bad payload", "失败原因")

        logs = storage.get_notification_logs(status="error")

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["status"], "error")
        self.assertEqual(logs[0]["channel_name"], "日志 Webhook")
        self.assertEqual(logs[0]["article_title"], "日志文章")

    def test_notification_logs_api_filters_skipped_status(self):
        channel_id = storage.save_notification_channel({
            "name": "跳过 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        storage.record_notification_log(channel_id, None, "skipped", "payload", "近 7 天已成功推送")

        resp = self.client.get("/api/notification-logs?status=skipped")
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["status"], "skipped")

    def test_notification_logs_export_returns_csv_with_filters(self):
        channel_id = storage.save_notification_channel({
            "name": "导出 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        self.save_article("导出文章", "https://example.com/export", "科技/AI", 5, "Export summary")
        article = storage.get_articles(days=1, limit=1)[0]
        storage.record_notification_log(channel_id, article["id"], "ok", "ok payload")
        storage.record_notification_log(channel_id, article["id"], "error", "bad payload", "失败原因")

        resp = self.client.get("/api/notification-logs/export?status=error")
        csv_text = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers["Content-Type"])
        self.assertIn("attachment; filename=notification-logs.csv", resp.headers["Content-Disposition"])
        self.assertIn("created_at,channel_name,article_title,status,error,payload", csv_text)
        self.assertIn("导出 Webhook", csv_text)
        self.assertIn("导出文章", csv_text)
        self.assertIn("error", csv_text)
        self.assertIn("失败原因", csv_text)
        self.assertNotIn("ok payload", csv_text)

    def test_webhook_notification_full_smoke_flow_exports_and_tracks_click(self):
        channel_id = storage.save_notification_channel({
            "name": "闭环 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        self.save_article("闭环文章", "https://example.com/full-flow", "科技/AI", 5, "闭环摘要")
        article = storage.get_articles(days=1, limit=1)[0]

        with patch("web.app.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.text = "ok"
            send_resp = self.client.post("/api/notifications/send", json={
                "channel_id": channel_id,
                "article_id": article["id"],
            })

        export_resp = self.client.get("/api/notification-logs/export?status=ok")
        click_resp = self.client.get(f"/notifications/click/{article['id']}")
        payload = post.call_args.kwargs["json"]

        self.assertEqual(send_resp.status_code, 200)
        self.assertTrue(send_resp.get_json()["ok"])
        self.assertEqual(payload["title"], "闭环文章")
        self.assertIn(f"/notifications/click/{article['id']}", payload["url"])
        self.assertEqual(export_resp.status_code, 200)
        self.assertIn("闭环 Webhook", export_resp.get_data(as_text=True))
        self.assertIn("闭环文章", export_resp.get_data(as_text=True))
        self.assertEqual(click_resp.status_code, 302)
        self.assertEqual(click_resp.headers["Location"], "https://example.com/full-flow")
        self.assertEqual(storage.get_article_event_counts(article["id"])["click"], 1)

    def test_notification_access_token_blocks_risky_api_when_enabled(self):
        cfg.set_many({
            "web.access_token.enabled": "true",
            "web.access_token": "secret-token",
        })

        blocked = self.client.post("/api/notification-channels", json={
            "name": "受保护 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        allowed = self.client.post("/api/notification-channels", json={
            "name": "受保护 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        }, headers={"X-Access-Token": "secret-token"})

        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.get_json()["error"], "访问令牌无效")
        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(storage.get_notification_channel(allowed.get_json()["id"]))

    def test_notification_access_token_accepts_query_token_for_export(self):
        cfg.set_many({
            "web.access_token.enabled": "true",
            "web.access_token": "secret-token",
        })
        channel_id = storage.save_notification_channel({
            "name": "令牌导出",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        storage.record_notification_log(channel_id, None, "ok", "payload")

        blocked = self.client.get("/api/notification-logs/export")
        allowed = self.client.get("/api/notification-logs/export?token=secret-token")

        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertIn("令牌导出", allowed.get_data(as_text=True))

    def test_notifications_page_renders_channel_management_and_log_filter(self):
        channel_id = storage.save_notification_channel({
            "name": "管理 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        storage.record_notification_log(channel_id, None, "error", "payload", "失败原因")

        resp = self.client.get("/notifications?status=error")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("editChannel", html)
        self.assertIn("toggleChannel", html)
        self.assertIn("deleteChannel", html)
        self.assertIn("testChannel", html)
        self.assertIn("log-status", html)
        self.assertIn("失败原因", html)
        self.assertIn("导出 CSV", html)
        self.assertIn("exportLogsCsv", html)
        self.assertIn("当前筛选：失败", html)
        self.assertIn("badge b-err", html)

    def test_notifications_page_renders_channels_recommendations_and_logs(self):
        channel_id = storage.save_notification_channel({
            "name": "推荐 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        self.save_article("Push candidate", "https://example.com/push", "科技/AI", 5, "Push summary")
        article = storage.get_articles(days=1, limit=1)[0]
        storage.record_notification_log(channel_id, article["id"], "ok", "Push candidate")

        resp = self.client.get("/notifications")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("通知推送", html)
        self.assertIn("推荐 Webhook", html)
        self.assertIn("Push candidate", html)
        self.assertIn("发送记录", html)

    def test_notification_payload_uses_local_tracking_link(self):
        channel_id = storage.save_notification_channel({
            "name": "追踪 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        self.save_article("Tracked article", "https://example.com/original", "科技/AI", 5, "Tracked summary")
        article = storage.get_articles(days=1, limit=1)[0]

        with patch("web.app.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.text = "ok"
            result = webapp.send_article_notification(channel_id, article["id"])

        self.assertTrue(result["ok"])
        payload = post.call_args.kwargs["json"]
        self.assertIn(f"/notifications/click/{article['id']}", payload["url"])
        self.assertEqual(payload["original_url"], "https://example.com/original")

    def test_build_email_notification_content_uses_configured_template(self):
        cfg.set_many({
            "notify.template.email_subject": "今日资讯：{title}",
            "notify.template.email_body": "{title}\n{conclusion}\n追踪：{tracking_url}\n原文：{original_url}",
        })
        self.save_article("模板文章", "https://example.com/template", "科技/AI", 5, "模板摘要")
        article = storage.get_articles(days=1, limit=1)[0]
        channel = {"channel_type": "email"}

        content = webapp.build_notification_content(channel, article)

        self.assertEqual(content["subject"], "今日资讯：模板文章")
        self.assertIn("模板摘要", content["body"])
        self.assertIn(f"/notifications/click/{article['id']}", content["body"])
        self.assertIn("https://example.com/template", content["body"])

    def test_build_webhook_notification_content_can_hide_original_url(self):
        cfg.set_many({
            "notify.template.webhook_include_original_url": "false",
            "notify.template.webhook_include_summary": "false",
        })
        self.save_article("Webhook 模板", "https://example.com/webhook-template", "科技/AI", 4, "Webhook 摘要")
        article = storage.get_articles(days=1, limit=1)[0]
        channel = {"channel_type": "webhook"}

        content = webapp.build_notification_content(channel, article)

        self.assertNotIn("original_url", content["payload"])
        self.assertNotIn("conclusion", content["payload"])
        self.assertIn(f"/notifications/click/{article['id']}", content["payload"]["url"])

    def test_notification_preview_api_returns_channel_specific_content(self):
        channel_id = storage.save_notification_channel({
            "name": "预览邮件",
            "channel_type": "email",
            "target": "reader@example.com",
            "enabled": True,
        })
        self.save_article("预览文章", "https://example.com/preview", "科技/AI", 5, "预览摘要")
        article = storage.get_articles(days=1, limit=1)[0]

        resp = self.client.get(f"/api/notifications/preview?channel_id={channel_id}&article_id={article['id']}")
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data["channel"]["name"], "预览邮件")
        self.assertEqual(data["article"]["title"], "预览文章")
        self.assertIn("subject", data["content"])
        self.assertIn("tracking_url", data["content"])

    def test_notifications_page_renders_template_config_and_preview_controls(self):
        self.save_article("页面预览文章", "https://example.com/page-preview", "科技/AI", 5, "页面预览摘要")

        resp = self.client.get("/notifications")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("推送模板", html)
        self.assertIn("notify.template.email_subject", html)
        self.assertIn("推送预览", html)
        self.assertIn("loadNotificationPreview", html)
        self.assertIn("preview-article", html)
        self.assertIn("预览需要至少一个通知渠道和一篇推荐文章", html)

    def test_notification_click_route_records_click_and_redirects(self):
        self.save_article("Click tracked", "https://example.com/click-target", "科技/AI", 4, "Click summary")
        article = storage.get_articles(days=1, limit=1)[0]

        resp = self.client.get(f"/notifications/click/{article['id']}")

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "https://example.com/click-target")
        self.assertEqual(storage.get_article_event_counts(article["id"])["click"], 1)

    def test_notification_click_increases_recommendation_score(self):
        self.save_article("Clicked push", "https://example.com/clicked-push", "科技/AI", 3, "Clicked")
        article = storage.get_articles(days=1, limit=1)[0]

        self.client.get(f"/notifications/click/{article['id']}")
        recommended = storage.get_recommended_articles(days=1, limit=1)[0]

        self.assertEqual(recommended["feedback_score"], 2)
        self.assertEqual(recommended["recommend_score"], 32)

    def test_notification_stats_counts_recent_statuses_clicks_and_channel_rates(self):
        channel_id = storage.save_notification_channel({
            "name": "统计 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        self.save_article("Stats article", "https://example.com/stats", "科技/AI", 5, "Stats summary")
        article = storage.get_articles(days=1, limit=1)[0]
        storage.record_notification_log(channel_id, article["id"], "ok", "payload")
        storage.record_notification_log(channel_id, article["id"], "skipped", "payload", "重复")
        storage.record_notification_log(channel_id, article["id"], "error", "payload", "失败")
        storage.record_article_event(article["id"], "click")

        stats = storage.get_notification_stats(days=7)

        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["clicks"], 1)
        self.assertEqual(stats["channels"][0]["channel_name"], "统计 Webhook")
        self.assertEqual(stats["channels"][0]["success_rate"], 50)
        self.assertEqual(stats["channels"][0]["click_rate"], 100)

    def test_notifications_page_shows_notification_stats_panel(self):
        channel_id = storage.save_notification_channel({
            "name": "面板 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        storage.record_notification_log(channel_id, None, "ok", "payload")

        resp = self.client.get("/notifications")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("推送效果", html)
        self.assertIn("点击数", html)
        self.assertIn("点击率", html)

    def test_notifications_page_renders_smtp_config_form(self):
        cfg.set_many({
            "notify.email.enabled": "true",
            "notify.email.smtp_server": "smtp.example.com",
            "notify.email.smtp_port": "465",
            "notify.email.username": "sender@example.com",
            "notify.email.from": "sender@example.com",
        })

        resp = self.client.get("/notifications")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("SMTP 配置", html)
        self.assertIn("smtp.example.com", html)
        self.assertIn("sender@example.com", html)
        self.assertIn("saveSmtpConfig", html)

    def test_notifications_page_renders_auto_push_schedule_form(self):
        cfg.set_many({
            "notify.push.enabled": "true",
            "notify.push.time": "09:15",
            "notify.push.limit": "3",
        })

        resp = self.client.get("/notifications")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("自动推送计划", html)
        self.assertIn("notify.push.enabled", html)
        self.assertIn("09:15", html)
        self.assertIn("savePushSchedule", html)

    def test_notification_config_api_allows_notify_email_prefix(self):
        resp = self.client.post("/api/config", json={
            "notify.email.enabled": "true",
            "notify.email.smtp_server": "smtp.example.com",
        })

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(cfg.get("notify.email.smtp_server"), "smtp.example.com")

    def test_article_detail_page_has_single_article_push_controls(self):
        channel_id = storage.save_notification_channel({
            "name": "详情 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        self.save_article("Detail push", "https://example.com/detail-push", "科技/AI", 5, "Detail summary")
        article = storage.get_articles(days=1, limit=1)[0]

        resp = self.client.get(f"/article/{article['id']}")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("推送本文", html)
        self.assertIn("详情 Webhook", html)
        self.assertIn("sendCurrentArticle", html)

    def test_article_detail_push_can_send_access_token_from_query(self):
        cfg.set_many({
            "web.access_token.enabled": "true",
            "web.access_token": "secret-token",
        })
        storage.save_notification_channel({
            "name": "令牌详情 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        self.save_article("Token detail push", "https://example.com/token-detail", "科技/AI", 5, "Token detail")
        article = storage.get_articles(days=1, limit=1)[0]

        resp = self.client.get(f"/article/{article['id']}?token=secret-token")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("accessTokenFromUrl", html)
        self.assertIn("X-Access-Token", html)

    def test_article_detail_page_shows_push_empty_state_without_channels(self):
        self.save_article("No channel push", "https://example.com/no-channel-push", "科技/AI", 5, "No channel summary")
        article = storage.get_articles(days=1, limit=1)[0]

        resp = self.client.get(f"/article/{article['id']}")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("推送本文", html)
        self.assertIn("还没有启用通知渠道", html)
        self.assertIn("/notifications", html)

    def test_recommendations_include_explainable_score_parts(self):
        self.save_article("Explain score", "https://example.com/explain", "科技/AI", 4, "Explain summary")
        article = storage.get_articles(days=1, limit=1)[0]
        storage.record_article_event(article["id"], "favorite")
        storage.record_article_event(article["id"], "open")

        recommended = storage.get_recommended_articles(days=1, limit=1)[0]

        self.assertEqual(recommended["base_score"], 40)
        self.assertEqual(recommended["feedback_score"], 6)
        self.assertEqual(recommended["recommend_score"], 46)
        self.assertEqual(recommended["open_count"], 1)
        self.assertEqual(recommended["favorite_count"], 1)

    def test_notifications_page_shows_score_explanation_columns(self):
        self.save_article("Explain page", "https://example.com/explain-page", "科技/AI", 4, "Explain page summary")
        article = storage.get_articles(days=1, limit=1)[0]
        storage.record_article_event(article["id"], "favorite")

        resp = self.client.get("/notifications")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("基础分", html)
        self.assertIn("反馈分", html)
        self.assertIn("记忆分", html)
        self.assertIn("推荐理由", html)
        self.assertIn("打开/收藏/隐藏", html)

    def test_article_detail_page_shows_memory_similar_articles(self):
        self.save_article(
            "详情 GPT 文章",
            "https://example.com/detail-memory-a",
            "科技/AI",
            5,
            "GPT 模型推理新闻",
            keywords=["GPT", "模型", "推理"],
        )
        self.save_article(
            "详情相似文章",
            "https://example.com/detail-memory-b",
            "科技/AI",
            4,
            "GPT 模型能力更新",
            keywords=["GPT", "模型"],
        )
        articles = {a["title"]: a for a in storage.get_articles(days=1, limit=10)}

        resp = self.client.get(f"/article/{articles['详情 GPT 文章']['id']}")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("记忆相似文章", html)
        self.assertIn("详情相似文章", html)

    def test_send_recommended_notifications_sends_top_articles_to_enabled_channels(self):
        channel_id = storage.save_notification_channel({
            "name": "推荐 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        self.save_article("Top push", "https://example.com/top", "科技/AI", 5, "Top summary")
        self.save_article("Low push", "https://example.com/low", "商业", 1, "Low summary")

        with patch("web.app.send_article_notification", return_value={"ok": True}) as send:
            result = webapp.send_recommended_notifications(limit=1, days=1)

        self.assertEqual(result["sent"], 1)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], channel_id)

    def test_send_recommended_notifications_skips_recent_successfully_sent_articles(self):
        channel_id = storage.save_notification_channel({
            "name": "去重 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        self.save_article("Already sent", "https://example.com/already", "科技/AI", 5, "Already")
        self.save_article("Fresh send", "https://example.com/fresh", "科技/AI", 4, "Fresh")
        articles = {a["title"]: a for a in storage.get_articles(days=1, limit=10)}
        storage.record_notification_log(channel_id, articles["Already sent"]["id"], "ok", "Already sent")

        with patch("web.app.send_article_notification", return_value={"ok": True}) as send:
            result = webapp.send_recommended_notifications(limit=2, days=1, skip_sent=True, dedupe_days=7)

        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(send.call_args.args[1], articles["Fresh send"]["id"])
        logs = storage.get_notification_logs(status="skipped")
        self.assertEqual(logs[0]["article_id"], articles["Already sent"]["id"])

    def test_single_article_notification_still_allows_resending_sent_article(self):
        channel_id = storage.save_notification_channel({
            "name": "单篇 Webhook",
            "channel_type": "webhook",
            "target": "https://example.com/hook",
            "enabled": True,
        })
        self.save_article("Force single", "https://example.com/force", "科技/AI", 5, "Force")
        article = storage.get_articles(days=1, limit=1)[0]
        storage.record_notification_log(channel_id, article["id"], "ok", "old")

        with patch("web.app.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.text = "ok"
            result = webapp.send_article_notification(channel_id, article["id"])

        self.assertTrue(result["ok"])
        post.assert_called_once()

    def test_notifications_page_renders_push_dedupe_controls(self):
        cfg.set_many({
            "notify.push.skip_sent": "true",
            "notify.push.dedupe_days": "7",
        })

        resp = self.client.get("/notifications")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("跳过已推送文章", html)
        self.assertIn("notify.push.skip_sent", html)
        self.assertIn("push-dedupe-days", html)

    def test_notifications_page_renders_access_token_controls(self):
        cfg.set_many({
            "web.access_token.enabled": "true",
            "web.access_token": "secret-token",
        })

        resp = self.client.get("/notifications")
        html = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("访问令牌保护", html)
        self.assertIn("web.access_token.enabled", html)
        self.assertIn("saveAccessTokenConfig", html)
        self.assertIn("accessHeaders", html)

    def test_run_scheduled_push_uses_notify_push_config(self):
        cfg.set_many({
            "notify.push.limit": "4",
            "notify.push.days": "2",
            "notify.push.skip_sent": "true",
            "notify.push.dedupe_days": "7",
        })

        with patch("web.app.send_recommended_notifications", return_value={"ok": True, "sent": 4}) as send:
            webapp.run_scheduled_push()

        send.assert_called_once_with(limit=4, days=2, skip_sent=True, dedupe_days=7)

    def test_setup_scheduler_registers_notification_push_job(self):
        cfg.set_config("schedule.rss_time", "")
        cfg.set_config("schedule.brief_time", "")
        cfg.set_config("notify.push.enabled", "true")
        cfg.set_config("notify.push.time", "10:45")

        with patch("web.app.schedule.clear") as clear, \
             patch("web.app.schedule.every") as every:
            every.return_value.day.at.return_value.do.return_value.tag.return_value = None
            count = webapp.setup_scheduler(start_thread=False)

        clear.assert_called_once_with("news_agent")
        self.assertEqual(count, 1)
        every.return_value.day.at.assert_called_once_with("10:45")
        every.return_value.day.at.return_value.do.assert_called_once_with(webapp.run_scheduled_push)

    def test_readme_documents_notification_workflow(self):
        readme = Path("README.md").read_text(encoding="utf-8")

        for keyword in ["通知推送", "SMTP", "Webhook", "自动推送", "去重", "点击追踪", "CSV", "访问令牌"]:
            self.assertIn(keyword, readme)

    def test_setup_scheduler_registers_rss_and_brief_jobs(self):
        cfg.set_config("schedule.rss_time", "08:30")
        cfg.set_config("schedule.brief_time", "21:00")

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
        cfg.set_config("rss.feeds", [
            "https://sspai.com/feed",
            "https://www.geekpark.net/rss",
        ])

        cfg.init_config()

        feeds = cfg.get_rss_feeds()
        self.assertNotIn("https://www.geekpark.net/rss", feeds)
        self.assertIn("https://www.infoq.cn/feed", feeds)

    def test_rss_sources_keep_legacy_feeds_and_enable_flags(self):
        cfg.set_config("rss.feeds", ["https://example.com/old.xml"])
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

    def test_save_article_replaces_unicode_replacement_garbled_title(self):
        storage.save_article({
            "title": "LLM ������",
            "url": "https://example.com/garbled-replacement",
            "category": "科技/AI",
            "importance": 3,
            "summary": {
                "conclusion": "LLM Unicode 乱码测试摘要",
                "points": [],
                "action": "",
            },
        })

        article = storage.get_articles(days=1, limit=1)[0]
        self.assertEqual(article["title"], "LLM Unicode 乱码测试摘要")

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

    def test_llm_cache_key_uses_sha256_and_avoids_separator_collisions(self):
        first = llm_client._hash("a|||b", "c", "model")
        second = llm_client._hash("a", "b|||c", "model")

        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, second)

    def test_llm_cache_get_ignores_expired_rows_when_max_age_is_set(self):
        stale_at = (datetime.now() - timedelta(days=31)).isoformat()
        with storage._conn() as conn:
            conn.execute(
                "INSERT INTO llm_cache (cache_key,response,model,hit_count,created_at) VALUES (?,?,?,?,?)",
                ("stale-key", "old", "model", 0, stale_at),
            )

        self.assertIsNone(storage.cache_get("stale-key", max_age_days=30))

    def test_llm_rate_limit_is_scoped_by_base_url(self):
        sleeps = []
        times = iter([100.0, 100.1, 100.1])

        with patch("llm_client.time.time", side_effect=lambda: next(times)), \
             patch("llm_client.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
            llm_client._rate_limit("https://api-one.example", min_interval=0.5)
            llm_client._rate_limit("https://api-two.example", min_interval=0.5)

        self.assertEqual(sleeps, [])

    def test_llm_client_rebuilds_when_timeout_changes(self):
        created = []

        class FakeOpenAI:
            def __init__(self, api_key, base_url, timeout):
                self.api_key = api_key
                self.base_url = base_url
                self.timeout = timeout
                created.append(self)

        with patch("llm_client.OpenAI", FakeOpenAI):
            first = llm_client._get_client("https://api.example", "key", 30)
            second = llm_client._get_client("https://api.example", "key", 60)

        self.assertEqual([c.timeout for c in created], [30, 60])
        self.assertIsNot(first, second)

    def test_seed_rss_sources_do_not_include_openclaw_sample_feeds(self):
        seeded = cfg._seed_rss_sources()
        joined = "\n".join(f"{s['name']} {s['url']} {s['note']}" for s in seeded)

        self.assertNotIn("OpenClaw", joined)


if __name__ == "__main__":
    unittest.main()
