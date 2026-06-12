# 指数环境判断系统

主判指数：中证全指 `sh000985`。

## 项目上下文

旧对话内容已整理到：

- [项目上下文与决策记录](/Users/zhouyang24/Documents/Codex/多空系统建设/docs/project-context.md)
- [旧对话归档](/Users/zhouyang24/Documents/Codex/多空系统建设/docs/conversation-archive.md)

## 常用命令

```bash
python3 work/index-env/market_env.py all
```

更新数据、输出今日状态、生成网页报告：

- 数据缓存：`work/index-env/data/index_000985.json`
- 状态历史：`work/index-env/data/states.csv`
- 网页报告：`outputs/index_env_report.html`
- 默认首页：`outputs/index.html`

只看命令行日报：

```bash
python3 work/index-env/market_env.py today
```

只重新生成网页：

```bash
python3 work/index-env/market_env.py render
```

导入自定义日线 CSV：

```bash
python3 work/index-env/market_env.py import-csv /path/to/index.csv
```

CSV 支持这些列名：

- `date/open/high/low/close/volume`
- `日期/开盘/最高/最低/收盘/成交量`

如果只有日期和收盘价，也可以导入，系统会用收盘价补齐开高低。

## 手机访问

先生成网页：

```bash
python3 work/index-env/market_env.py all
```

再启动本地服务：

```bash
python3 work/index-env/market_env.py serve
```

手机和电脑连接同一个 Wi-Fi 后，打开命令行里显示的局域网地址。

## 远端部署

推荐使用 GitHub Pages。仓库里已经包含 `.github/workflows/deploy-index-env.yml`：

- 手动触发：GitHub 仓库页面进入 `Actions`，选择 `Deploy Index Environment`，点 `Run workflow`
- 自动更新：工作日北京时间 18:10 自动更新一次
- 发布目录：`outputs`

首次使用步骤：

1. 把当前目录提交并推送到 GitHub 仓库。
2. 在 GitHub 仓库 `Settings -> Pages` 中把 `Build and deployment` 设置为 `GitHub Actions`。
3. 进入 `Actions -> Deploy Index Environment` 手动运行一次。
4. 运行成功后，手机打开 Pages 给出的固定网址。

以后只要打开这个固定网址即可查看最新指数环境。
