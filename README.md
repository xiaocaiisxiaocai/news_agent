# news_agent_final

本项目是一个本地资讯 Agent：从 RSS、网页 URL 或手动文本获取内容，调用 LLM 做分类、摘要、评分、关键词提取和话题聚合，并通过 Flask Web 控制台查看文章、RSS 健康状态、简报和配置。

## 环境准备

建议使用 Python 3.12。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 启动

```powershell
python run.py
```

默认访问地址：

```text
http://127.0.0.1:5000
```

首次启动会自动创建 SQLite 数据库：

```text
data/news_agent.db
```

## 配置

进入 Web 控制台的“设置”页面配置：

- LLM 服务商、Base URL、API Key、模型名、超时和重试。
- RSS 源、每源抓取数量和 RSS 健康检查。
- 摘要质检、重要性评分、关键词、话题聚合等功能开关。
- 个性化加分词、扣分词、屏蔽词和分类权重。
- 定时 RSS 拉取和简报生成时间。

配置保存在 SQLite 中，页面保存后实时生效。

## 通知推送

进入 Web 控制台的“通知”页面，可以管理邮件和 Webhook 推送；当前阶段不启用 Telegram。

- SMTP：在页面填写 `notify.email.*` 配置后，可添加邮件渠道并测试发送。
- Webhook：添加 Webhook URL 后，可对单篇文章、推荐文章或自动任务推送 JSON 载荷。
- 渠道管理：支持新增、编辑、启用、停用、删除、单渠道测试和“测试全部启用渠道”。
- 手动推送：文章详情页可推送本文；通知页可按推荐分批量推送推荐文章。
- 自动推送：通过 `notify.push.enabled`、`notify.push.time`、`notify.push.limit`、`notify.push.days` 配置每日推荐推送。
- 去重：`notify.push.skip_sent` 与 `notify.push.dedupe_days` 可跳过近期已成功推送的文章，并记录 skipped 日志。
- 点击追踪：推送内容使用 `/notifications/click/<article_id>` 跳转链接，点击后记录行为并重定向到原文。
- 模板预览：邮件标题、邮件正文和 Webhook 字段可配置，并可在页面生成推送预览。
- CSV：发送记录支持按状态筛选，并通过“导出 CSV”下载日志。
- 访问令牌：开启 `web.access_token.enabled` 并设置 `web.access_token` 后，通知渠道变更、测试、发送、CSV 导出和配置保存需携带 `X-Access-Token` 请求头或 `token` 查询参数。

常见问题：

- SMTP 配置不完整：检查是否启用邮件通知、SMTP 服务器、端口和发件人。
- 没有启用渠道：先添加并启用邮件或 Webhook 渠道。
- 重复文章被跳过：检查去重窗口和 skipped 发送记录。
- Webhook 失败：查看发送记录中的 HTTP 状态或错误信息。

## 测试

```powershell
python -m unittest tests.test_regressions
```

## 主要目录

```text
config_store.py      配置和 RSS 源持久化
storage.py           SQLite 数据持久层
llm_client.py        LLM 客户端、缓存和限速
fetchers/            RSS、网页和文本抓取
processors/          分类、摘要、评分、关键词和聚合
outputs/             简报生成
web/                 Flask Web 控制台
tests/               回归测试
```
