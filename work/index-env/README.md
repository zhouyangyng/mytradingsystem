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
- 市场结构快照：`work/index-env/data/market_structure.json`
- 网页报告：`outputs/index_env_report.html`
- 默认首页：`outputs/index.html`

系统会在指数K线评分之外，自动抓取当日市场结构：

- 涨跌家数 / 上涨比例
- 涨停 / 跌停 / 大跌股数量
- 行业与概念强度
- 主线板块持续性
- 妖股/情绪行情识别

网页中双击某日K线，可以查看当天的指数评分明细和市场结构快照。

系统还会生成综合确认层：

- 稳定主判：中证全指
- 风格确认：沪深300 / 中证1000 / 国证2000
- 体感确认：平均股价或等权类 CSV，导入后参与判断
- 综合结论：判断指数信号是否被体感与风格确认

交易时间内运行时，系统会额外拉取盘中实时行情：

- 用实时开高低现价生成当天临时K线。
- 醒目标识“交易中”，提示盘中状态会变化。
- 全市场成交额同时展示当前已成交额和预计全天成交额。
- 盘中临时K线不写入正式历史，收盘后才固化。

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

导入平均股价/等权体感指数 CSV：

```bash
python3 work/index-env/market_env.py import-sense-csv /path/to/average_price.csv
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

### 国内服务器部署（推荐用于盘中）

如果希望交易时间稳定刷新，推荐使用国内 Linux 服务器或轻量云服务器，服务器本机定时拉行情并用 Nginx 提供页面：

- 交易时段：每 2 分钟刷新一次
- 收盘后：15:30 和 18:10 各补一次
- 页面目录：`/var/www/mytradingsystem`
- 更新日志：`/var/log/mytradingsystem/update.log`

首次部署，在服务器上执行：

```bash
sudo REPO_URL=https://github.com/zhouyangyng/mytradingsystem.git \
  APP_DIR=/opt/mytradingsystem \
  WEB_DIR=/var/www/mytradingsystem \
  bash deploy/domestic-server/install.sh
```

如果服务器还没有仓库文件，也可以先 clone：

```bash
git clone https://github.com/zhouyangyng/mytradingsystem.git /opt/mytradingsystem
cd /opt/mytradingsystem
sudo bash deploy/domestic-server/install.sh
```

部署完成后，手机打开：

```text
http://服务器公网IP/
```

如果绑定了已备案域名，可以把域名解析到服务器 IP，并在 Nginx 配置里把 `server_name _;` 改成域名。

### GitHub Pages 部署（备用）

仓库里也保留 `.github/workflows/deploy-index-env.yml`：

- 手动触发：GitHub 仓库页面进入 `Actions`，选择 `Deploy Index Environment`，点 `Run workflow`
- 自动更新：工作日交易时间与收盘后自动更新
- 发布目录：`outputs`

首次使用步骤：

1. 把当前目录提交并推送到 GitHub 仓库。
2. 在 GitHub 仓库 `Settings -> Pages` 中把 `Build and deployment` 设置为 `GitHub Actions`。
3. 进入 `Actions -> Deploy Index Environment` 手动运行一次。
4. 运行成功后，手机打开 Pages 给出的固定网址。

GitHub Pages 是静态站，定时任务可能延迟或漏跑；盘中使用优先选择国内服务器部署。
