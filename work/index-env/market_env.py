#!/usr/bin/env python3
"""
Personal A-share index environment system.

Data source: Tencent daily K-line for CSI All Share, sh000985.
No third-party packages are required.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import os
import re
import socket
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WORK_DATA = ROOT / "data"
OUTPUTS = ROOT.parents[1] / "outputs"
RAW_JSON = WORK_DATA / "index_000985.json"
STATES_CSV = WORK_DATA / "states.csv"
REPORT_HTML = OUTPUTS / "index_env_report.html"
INDEX_HTML = OUTPUTS / "index.html"

SYMBOL = "sh000985"
INDEX_NAME = "中证全指"
INDEX_CODE = "sh000985"
DEFAULT_BEGIN = "2025-01-01"
STATE_COLORS = {
    "多": "#ef4444",
    "转": "#f59e0b",
    "空": "#16a34a",
}


def today_text() -> str:
    return dt.date.today().strftime("%Y-%m-%d")


def parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


def to_float(value: str | float | int) -> float:
    return float(value)


def fmt_num(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def moving_average(values: list[float], end_index: int, window: int) -> float | None:
    if end_index + 1 < window:
        return None
    sample = values[end_index + 1 - window : end_index + 1]
    return sum(sample) / window


def previous_trading_date(rows: list[dict], index: int) -> dict | None:
    if index <= 0:
        return None
    return rows[index - 1]


def fetch_tencent_kline(symbol: str, begin: str, end: str) -> list[dict]:
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
        f"param={symbol},day,{begin},{end},800,qfq"
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://gu.qq.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if payload.get("code") != 0:
        raise RuntimeError(f"Tencent API error: {payload!r}")

    data = payload.get("data", {}).get(symbol, {})
    raw_rows = data.get("day") or data.get("qfqday") or []
    rows = []
    for item in raw_rows:
        if len(item) < 6:
            continue
        date, open_, close, high, low, volume = item[:6]
        rows.append(
            {
                "date": date,
                "open": to_float(open_),
                "high": to_float(high),
                "low": to_float(low),
                "close": to_float(close),
                "volume": to_float(volume),
            }
        )
    rows.sort(key=lambda row: row["date"])
    return rows


def parse_ths_line_payload(text: str) -> list[dict]:
    """Parse common 10jqka line JSON/JSONP payload shapes.

    10jqka has changed this endpoint several times. The parser accepts plain
    JSON, JSONP wrappers, and compact `data` strings when the fields are
    comma/semicolon separated.
    """
    stripped = text.strip()
    if not stripped:
        return []

    json_text = stripped
    match = re.search(r"(\{.*\})", stripped, flags=re.S)
    if match:
        json_text = match.group(1)

    rows: list[dict] = []
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        payload = None

    candidates = []
    if isinstance(payload, dict):
        candidates.extend(
            value
            for value in (
                payload.get("data"),
                payload.get("line"),
                payload.get("day"),
                payload.get("klines"),
            )
            if value
        )
        if "880003" in payload:
            candidates.append(payload["880003"])
    else:
        candidates.append(stripped)

    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in ("data", "line", "day", "klines"):
                if candidate.get(key):
                    candidates.append(candidate[key])
        elif isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, list) and len(item) >= 6:
                    date, open_, high, low, close, volume = item[:6]
                    rows.append(
                        {
                            "date": normalize_date(str(date)),
                            "open": to_float(open_),
                            "high": to_float(high),
                            "low": to_float(low),
                            "close": to_float(close),
                            "volume": to_float(volume),
                        }
                    )
        elif isinstance(candidate, str):
            rows.extend(parse_compact_kline_string(candidate))

    dedup = {row["date"]: row for row in rows if row.get("date")}
    return [dedup[key] for key in sorted(dedup)]


def normalize_date(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value[:10]


def parse_compact_kline_string(text: str) -> list[dict]:
    rows = []
    for chunk in re.split(r"[;\n|]+", text):
        numbers = re.split(r"[,:\s]+", chunk.strip())
        if len(numbers) < 6:
            continue
        if not re.fullmatch(r"\d{4}-?\d{2}-?\d{2}", numbers[0]):
            continue
        try:
            date = normalize_date(numbers[0])
            values = [to_float(value) for value in numbers[1:6]]
        except ValueError:
            continue
        open_, high, low, close, volume = values
        rows.append(
            {
                "date": date,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
    return rows


def fetch_ths_average_price_kline(begin: str, end: str) -> list[dict]:
    urls = [
        "https://d.10jqka.com.cn/v6/line/hs_880003/01/last.js",
        "https://d.10jqka.com.cn/v6/line/hs_880003/01/all.js",
        "https://d.10jqka.com.cn/v2/line/hs_880003/01/last.js",
        "http://d.10jqka.com.cn/v6/line/hs_880003/01/last.js",
        "http://d.10jqka.com.cn/v2/line/hs_880003/01/last.js",
    ]
    errors = []
    for url in urls:
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://q.10jqka.com.cn/",
                    "Accept": "*/*",
                },
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                text = response.read().decode("utf-8", errors="ignore")
            rows = parse_ths_line_payload(text)
            if rows:
                begin_date = parse_date(begin)
                end_date = parse_date(end)
                return [
                    row
                    for row in rows
                    if begin_date <= parse_date(row["date"]) <= end_date
                ]
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError(
        "平均股价 880003 历史K线暂时无法从同花顺接口获取。"
        "请稍后重试，或提供可访问的数据接口/CSV。"
    )


def fetch_kline(begin: str, end: str) -> list[dict]:
    return fetch_tencent_kline(SYMBOL, begin, end)


def load_raw_rows() -> list[dict]:
    if not RAW_JSON.exists():
        return []
    return json.loads(RAW_JSON.read_text(encoding="utf-8"))


def save_raw_rows(rows: list[dict]) -> None:
    WORK_DATA.mkdir(parents=True, exist_ok=True)
    RAW_JSON.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def import_csv_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for raw in reader:
            normalized = {str(k).strip().lower(): v for k, v in raw.items() if k}
            date = (
                normalized.get("date")
                or normalized.get("日期")
                or normalized.get("时间")
                or normalized.get("交易日期")
            )
            close = normalized.get("close") or normalized.get("收盘") or normalized.get("收盘价")
            if not date or close in (None, ""):
                continue
            open_ = normalized.get("open") or normalized.get("开盘") or normalized.get("开盘价") or close
            high = normalized.get("high") or normalized.get("最高") or normalized.get("最高价") or close
            low = normalized.get("low") or normalized.get("最低") or normalized.get("最低价") or close
            volume = normalized.get("volume") or normalized.get("成交量") or normalized.get("vol") or 0
            rows.append(
                {
                    "date": normalize_date(str(date)),
                    "open": to_float(open_),
                    "high": to_float(high),
                    "low": to_float(low),
                    "close": to_float(close),
                    "volume": to_float(volume or 0),
                }
            )
    if not rows:
        raise RuntimeError("CSV 中没有识别到可导入的K线。至少需要 date/日期 和 close/收盘。")
    return merge_rows([], rows)


def merge_rows(old_rows: list[dict], new_rows: list[dict]) -> list[dict]:
    by_date = {row["date"]: row for row in old_rows}
    for row in new_rows:
        by_date[row["date"]] = row
    return [by_date[key] for key in sorted(by_date)]


def calculate_states(rows: list[dict]) -> list[dict]:
    closes = [row["close"] for row in rows]
    lows = [row["low"] for row in rows]
    volumes = [row["volume"] for row in rows]
    output = []

    for i, row in enumerate(rows):
        prev = previous_trading_date(rows, i)
        ma5 = moving_average(closes, i, 5)
        ma10 = moving_average(closes, i, 10)
        ma20 = moving_average(closes, i, 20)
        vol_ma5 = moving_average(volumes, i, 5)
        vol_ma20 = moving_average(volumes, i, 20)

        score = 15
        reasons: list[str] = ["风险缓冲 +15"]
        hard_below_ma20 = False
        hard_heavy_drop = False

        if ma5 is not None and row["close"] > ma5:
            score += 10
            reasons.append("收盘价站上MA5 +10")
        if ma10 is not None and row["close"] > ma10:
            score += 10
            reasons.append("收盘价站上MA10 +10")
        if ma20 is not None and row["close"] > ma20:
            score += 10
            reasons.append("收盘价站上MA20 +10")
        if None not in (ma5, ma10, ma20) and ma5 > ma10 > ma20:
            score += 10
            reasons.append("MA5>MA10>MA20 +10")

        if i >= 4 and row["close"] >= max(closes[i - 4 : i + 1]):
            score += 10
            reasons.append("创近5日收盘新高 +10")
        if i >= 2 and lows[i - 2] < lows[i - 1] < lows[i]:
            score += 5
            reasons.append("近3日低点抬高 +5")

        day_range = row["high"] - row["low"]
        close_position = 0.5 if day_range <= 0 else (row["close"] - row["low"]) / day_range
        if close_position >= 0.60:
            score += 5
            reasons.append("收盘位于日内上60% +5")

        pct_change = 0.0
        if prev is not None and prev["close"]:
            pct_change = (row["close"] / prev["close"] - 1.0) * 100
            if pct_change > 0:
                score += 5
                reasons.append("当日上涨 +5")

        volume_above_ma5 = vol_ma5 is not None and row["volume"] > vol_ma5
        volume_above_ma20 = vol_ma20 is not None and row["volume"] > vol_ma20
        if volume_above_ma5:
            score += 8
            reasons.append("成交量高于5日均量 +8")
        if volume_above_ma20:
            score += 6
            reasons.append("成交量高于20日均量 +6")
        if pct_change > 0 and volume_above_ma5:
            score += 6
            reasons.append("上涨日放量 +6")
        if pct_change < 0 and volume_above_ma5:
            score -= 8
            reasons.append("下跌日放量 -8")

        if row["high"] > row["open"] and row["close"] < row["open"] and close_position <= 0.40 and volume_above_ma5:
            score -= 10
            reasons.append("放量冲高回落 -10")

        if ma10 is not None and row["close"] < ma10:
            score -= 5
            reasons.append("收盘跌破MA10 -5")
        if ma20 is not None and row["close"] < ma20:
            score -= 10
            hard_below_ma20 = True
            reasons.append("收盘跌破MA20 -10")
        if pct_change <= -1.5 and volume_above_ma5:
            score -= 10
            hard_heavy_drop = True
            reasons.append("放量大跌 -10")
        if i >= 2 and rows[i - 2]["close"]:
            three_day_pct = (row["close"] / rows[i - 2]["close"] - 1.0) * 100
            if three_day_pct <= -3:
                score -= 10
                reasons.append("近3日累计跌幅<=-3% -10")

        if score < 0:
            score = 0
        if score > 100:
            score = 100

        if score < 40 or (hard_below_ma20 and ma5 is not None and ma10 is not None and ma5 < ma10):
            state = "空"
        elif score >= 70 and not hard_below_ma20 and not hard_heavy_drop:
            state = "多"
        else:
            state = "转"

        enriched = {
            **row,
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
            "vol_ma5": vol_ma5,
            "vol_ma20": vol_ma20,
            "pct_change": pct_change,
            "score": int(round(score)),
            "state": state,
            "phase": "",
            "reasons": "；".join(reasons) if reasons else "样本不足，默认观察",
        }
        output.append(enriched)

    for i, row in enumerate(output):
        row["phase"] = phase_for(output, i)

    return output


def phase_for(states: list[dict], index: int) -> str:
    recent5 = states[max(0, index - 4) : index + 1]
    recent3 = states[max(0, index - 2) : index + 1]
    labels5 = [row["state"] for row in recent5]
    labels3 = [row["state"] for row in recent3]
    latest = states[index]["state"]

    if labels5.count("多") >= 3:
        return "主升"
    if labels3 == ["转", "多", "多"]:
        return "主升"
    if len(labels3) == 3 and labels3.count("转") >= 2 and latest == "多":
        return "主升"
    if latest == "空":
        return "防守"
    if latest == "多":
        return "试攻"
    return "观察"


def position_advice(row: dict) -> str:
    if row["phase"] == "主升":
        return "60%-80%"
    if row["state"] == "多":
        return "40%-60%"
    if row["state"] == "转":
        return "20%-40%"
    return "0%-20%，不适合重仓"


def save_states_csv(rows: list[dict]) -> None:
    WORK_DATA.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "score",
        "state",
        "phase",
        "reasons",
    ]
    with STATES_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in fieldnames})


def load_states() -> list[dict]:
    raw_rows = load_raw_rows()
    if raw_rows:
        rows = calculate_states(raw_rows)
        save_states_csv(rows)
        return rows
    if not STATES_CSV.exists():
        return []
    with STATES_CSV.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for key in ("open", "high", "low", "close", "volume"):
            row[key] = float(row[key])
        row["score"] = int(row["score"])
    return rows


def update_data(begin: str = DEFAULT_BEGIN, end: str | None = None) -> list[dict]:
    end = end or today_text()
    old_rows = load_raw_rows()
    fetch_begin = begin
    if old_rows:
        last_date = parse_date(old_rows[-1]["date"])
        fetch_begin = (last_date - dt.timedelta(days=10)).strftime("%Y-%m-%d")
    new_rows = fetch_kline(fetch_begin, end)
    merged = merge_rows(old_rows, new_rows)
    if not merged:
        raise RuntimeError("No K-line data fetched.")
    save_raw_rows(merged)
    states = calculate_states(merged)
    save_states_csv(states)
    return states


def print_today(rows: list[dict]) -> None:
    if not rows:
        print("没有可用的中证全指数据。")
        print("可先尝试: python3 work/index-env/market_env.py update")
        return
    row = rows[-1]
    recent = rows[-5:]
    recent_text = " ".join(f"{item['date']}:{item['state']}" for item in recent)
    print(f"{INDEX_NAME} 指数环境日报")
    print(f"日期: {row['date']}")
    print(f"状态: {row['state']}  分数: {row['score']}  阶段: {row['phase']}")
    print(f"仓位建议: {position_advice(row)}")
    print(
        "K线: "
        f"开 {fmt_num(row['open'])} / 高 {fmt_num(row['high'])} / "
        f"低 {fmt_num(row['low'])} / 收 {fmt_num(row['close'])} / "
        f"涨跌 {fmt_num(row.get('pct_change', 0.0))}%"
    )
    print(f"最近5日: {recent_text}")
    print("触发原因:")
    for reason in str(row["reasons"]).split("；"):
        print(f"- {reason}")


def json_for_chart(rows: list[dict]) -> str:
    compact = []
    for row in rows:
        compact.append(
            {
                "d": row["date"],
                "o": row["open"],
                "h": row["high"],
                "l": row["low"],
                "c": row["close"],
                "v": row["volume"],
                "s": row["state"],
                "score": row["score"],
                "phase": row["phase"],
                "ma5": row.get("ma5"),
                "ma10": row.get("ma10"),
                "ma20": row.get("ma20"),
                "reasons": row["reasons"],
            }
        )
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def render_html(rows: list[dict]) -> Path:
    if not rows:
        raise RuntimeError("No rows to render.")
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    latest = rows[-1]
    data_json = json_for_chart(rows)
    state = latest["state"]
    color = STATE_COLORS[state]
    updated = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>{INDEX_NAME}指数环境</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fb;
      --panel: #ffffff;
      --ink: #172033;
      --muted: #667085;
      --line: #d9dee8;
      --red: #ef4444;
      --yellow: #f59e0b;
      --green: #16a34a;
      --blue: #2563eb;
      --purple: #7c3aed;
      --radius: 8px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 16px;
    }}
    header {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: end;
      margin-bottom: 12px;
    }}
    h1 {{
      font-size: clamp(22px, 5vw, 34px);
      line-height: 1.12;
      margin: 0 0 6px;
      letter-spacing: 0;
    }}
    .sub {{ color: var(--muted); font-size: 13px; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 54px;
      height: 46px;
      border-radius: var(--radius);
      background: {color};
      color: white;
      font-size: 28px;
      font-weight: 800;
    }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 10px 12px;
      min-height: 74px;
    }}
    .metric b {{
      display: block;
      font-size: 20px;
      line-height: 1.2;
      margin-top: 6px;
    }}
    .metric span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
    }}
    .chart-wrap {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 10px;
      overflow: hidden;
    }}
    .chart-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      padding: 2px 2px 10px;
    }}
    .chart-title {{
      font-size: 17px;
      font-weight: 800;
      line-height: 1.25;
    }}
    .chart-sub {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
    }}
    .range {{
      display: inline-flex;
      gap: 4px;
      background: #eef2f7;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 3px;
      flex: none;
    }}
    .range button {{
      border: 0;
      background: transparent;
      color: #344054;
      border-radius: 6px;
      min-width: 42px;
      height: 30px;
      padding: 0 8px;
      font: inherit;
      font-size: 12px;
    }}
    .range button.active {{
      background: #fff;
      color: var(--ink);
      box-shadow: 0 1px 2px rgba(16, 24, 40, .12);
      font-weight: 700;
    }}
    .chart-actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .zoom {{
      display: inline-flex;
      gap: 4px;
      background: #eef2f7;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 3px;
      flex: none;
    }}
    .zoom button {{
      border: 0;
      background: #fff;
      color: var(--ink);
      border-radius: 6px;
      width: 34px;
      height: 30px;
      padding: 0;
      font: inherit;
      font-size: 18px;
      font-weight: 800;
      box-shadow: 0 1px 2px rgba(16, 24, 40, .10);
    }}
    .toolbar {{
      display: flex;
      gap: 8px;
      align-items: center;
      justify-content: space-between;
      padding: 4px 4px 8px;
      color: var(--muted);
      font-size: 12px;
    }}
    .legend {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .dot {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      display: inline-block;
      margin-right: 4px;
      vertical-align: -1px;
    }}
    canvas {{
      display: block;
      width: 100%;
      height: min(72vh, 720px);
      min-height: 500px;
      touch-action: pan-y;
    }}
    .details {{
      margin-top: 12px;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 12px;
    }}
    .panel h2 {{
      font-size: 15px;
      margin: 0 0 10px;
    }}
    .reasons {{
      margin: 0;
      padding-left: 18px;
      color: #344054;
      line-height: 1.7;
      font-size: 14px;
    }}
    .history {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 6px;
    }}
    .day {{
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 8px;
      font-size: 12px;
      background: #fbfcff;
    }}
    .day strong {{
      display: inline-flex;
      min-width: 24px;
      height: 24px;
      align-items: center;
      justify-content: center;
      border-radius: 6px;
      color: #fff;
      margin-bottom: 6px;
    }}
    @media (max-width: 760px) {{
      main {{ padding: 10px; }}
      header {{ grid-template-columns: 1fr auto; }}
      .summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .chart-head {{ display: block; }}
      .chart-actions {{ margin-top: 10px; justify-content: stretch; }}
      .range {{ width: 100%; justify-content: space-between; }}
      .range button {{ flex: 1; }}
      .zoom {{ width: 100%; }}
      .zoom button {{ flex: 1; }}
      .details {{ grid-template-columns: 1fr; }}
      canvas {{ height: 68vh; min-height: 460px; }}
      .history {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>{INDEX_NAME}指数环境</h1>
      <div class="sub">主判指数 {html.escape(INDEX_NAME)} {html.escape(INDEX_CODE)} · 数据更新 {html.escape(updated)} · 收盘口径</div>
    </div>
    <div class="badge">{html.escape(state)}</div>
  </header>
  <section class="summary">
    <div class="metric"><span>日期</span><b>{html.escape(latest["date"])}</b></div>
    <div class="metric"><span>分数</span><b>{latest["score"]}</b></div>
    <div class="metric"><span>阶段</span><b>{html.escape(latest["phase"])}</b></div>
    <div class="metric"><span>仓位建议</span><b>{html.escape(position_advice(latest))}</b></div>
  </section>
  <section class="chart-wrap">
    <div class="chart-head">
      <div>
        <div class="chart-title">指数K线与每日环境标注</div>
        <div class="chart-sub">每根K线上方的字就是当天环境：多=进攻，转=观察，空=防守</div>
      </div>
      <div class="chart-actions">
        <div class="range" aria-label="切换K线显示范围">
          <button type="button" data-range="60">60日</button>
          <button type="button" data-range="120" class="active">120日</button>
          <button type="button" data-range="240">240日</button>
          <button type="button" data-range="all">全部</button>
        </div>
        <div class="zoom" aria-label="缩放K线">
          <button type="button" id="zoomOut" title="缩小">−</button>
          <button type="button" id="zoomIn" title="放大">+</button>
        </div>
      </div>
    </div>
    <div class="toolbar">
      <div class="legend">
        <span><i class="dot" style="background:var(--red)"></i>多</span>
        <span><i class="dot" style="background:var(--yellow)"></i>转</span>
        <span><i class="dot" style="background:var(--green)"></i>空</span>
        <span><i class="dot" style="background:var(--blue)"></i>MA5/10</span>
        <span><i class="dot" style="background:var(--purple)"></i>MA20</span>
      </div>
      <div id="tip">拖动查看历史，双击回到最新</div>
    </div>
    <canvas id="chart" aria-label="指数K线图"></canvas>
  </section>
  <section class="details">
    <div class="panel">
      <h2>今日触发原因</h2>
      <ul class="reasons">
        {"".join(f"<li>{html.escape(item)}</li>" for item in str(latest["reasons"]).split("；"))}
      </ul>
    </div>
    <div class="panel">
      <h2>最近5日</h2>
      <div class="history">
        {"".join(render_history_card(row) for row in rows[-5:])}
      </div>
    </div>
  </section>
</main>
<script>
const rows = {data_json};
const colors = {{"多":"#ef4444","转":"#f59e0b","空":"#16a34a"}};
const canvas = document.getElementById("chart");
const tip = document.getElementById("tip");
const ctx = canvas.getContext("2d");
let end = rows.length - 1;
let visible = Math.min(rows.length, window.innerWidth < 760 ? 80 : 120);
let dragging = false;
let lastX = 0;
let activeRange = "120";
let pointers = new Map();
let pinchStartDistance = 0;
let pinchStartVisible = visible;

function resize() {{
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.floor(rect.width * ratio);
  canvas.height = Math.floor(rect.height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  draw();
}}

function yFor(value, min, max, top, height) {{
  if (max === min) return top + height / 2;
  return top + (max - value) / (max - min) * height;
}}

function drawLine(data, key, start, count, min, max, left, top, width, height, color) {{
  ctx.beginPath();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.4;
  let started = false;
  for (let i = 0; i < count; i++) {{
    const row = data[start + i];
    const value = row[key];
    if (value === null || value === undefined) {{
      started = false;
      continue;
    }}
    const x = left + i * (width / count) + width / count / 2;
    const y = yFor(value, min, max, top, height);
    if (!started) {{
      ctx.moveTo(x, y);
      started = true;
    }} else {{
      ctx.lineTo(x, y);
    }}
  }}
  ctx.stroke();
}}

function draw() {{
  const rect = canvas.getBoundingClientRect();
  const w = rect.width;
  const h = rect.height;
  ctx.clearRect(0, 0, w, h);
  const pad = {{l: 44, r: 12, t: 32, b: 28}};
  const chartH = h - pad.t - pad.b;
  const start = Math.max(0, end - visible + 1);
  const slice = rows.slice(start, end + 1);
  const max = Math.max(...slice.flatMap(r => [r.h, r.ma5 || r.h, r.ma10 || r.h, r.ma20 || r.h]));
  const min = Math.min(...slice.flatMap(r => [r.l, r.ma5 || r.l, r.ma10 || r.l, r.ma20 || r.l]));
  const step = (w - pad.l - pad.r) / slice.length;
  const candleW = Math.max(2, Math.min(12, step * 0.58));
  const drawEveryLabel = step >= 7 ? 1 : Math.ceil(7 / step);

  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = "#e5e7ef";
  ctx.lineWidth = 1;
  ctx.font = "12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
  ctx.fillStyle = "#667085";
  for (let i = 0; i <= 4; i++) {{
    const y = pad.t + chartH / 4 * i;
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
    const value = max - (max - min) / 4 * i;
    ctx.fillText(value.toFixed(0), 4, y + 4);
  }}

  slice.forEach((r, i) => {{
    const x = pad.l + i * step + step / 2;
    const highY = yFor(r.h, min, max, pad.t, chartH);
    const lowY = yFor(r.l, min, max, pad.t, chartH);
    const openY = yFor(r.o, min, max, pad.t, chartH);
    const closeY = yFor(r.c, min, max, pad.t, chartH);
    const up = r.c >= r.o;
    const bodyTop = Math.min(openY, closeY);
    const bodyH = Math.max(1, Math.abs(closeY - openY));
    ctx.strokeStyle = up ? "#dc2626" : "#16a34a";
    ctx.fillStyle = up ? "#ef4444" : "#16a34a";
    ctx.beginPath();
    ctx.moveTo(x, highY);
    ctx.lineTo(x, lowY);
    ctx.stroke();
    ctx.fillRect(x - candleW / 2, bodyTop, candleW, bodyH);

    if (i % drawEveryLabel === 0 || i === slice.length - 1) {{
      const labelW = step < 10 ? 16 : 22;
      const labelH = step < 10 ? 16 : 18;
      const pointerH = 6;
      const fontSize = step < 10 ? 10 : 12;
      const labelY = Math.max(4, highY - labelH - pointerH - 8);
      ctx.fillStyle = colors[r.s];
      roundRect(ctx, x - labelW / 2, labelY, labelW, labelH, 4);
      ctx.fill();
      ctx.beginPath();
      ctx.moveTo(x - 5, labelY + labelH - 1);
      ctx.lineTo(x + 5, labelY + labelH - 1);
      ctx.lineTo(x, labelY + labelH + pointerH);
      ctx.closePath();
      ctx.fill();
      ctx.strokeStyle = "rgba(255,255,255,.78)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, labelY + labelH + pointerH);
      ctx.lineTo(x, Math.max(labelY + labelH + pointerH, highY - 1));
      ctx.stroke();
      ctx.fillStyle = "#fff";
      ctx.font = `bold ${{fontSize}}px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif`;
      ctx.textAlign = "center";
      ctx.fillText(r.s, x, labelY + labelH - 5);
      ctx.textAlign = "left";
    }}
  }});

  drawLine(rows, "ma5", start, slice.length, min, max, pad.l, pad.t, w - pad.l - pad.r, chartH, "#2563eb");
  drawLine(rows, "ma10", start, slice.length, min, max, pad.l, pad.t, w - pad.l - pad.r, chartH, "#0ea5e9");
  drawLine(rows, "ma20", start, slice.length, min, max, pad.l, pad.t, w - pad.l - pad.r, chartH, "#7c3aed");

  ctx.fillStyle = "#667085";
  ctx.font = "12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
  if (slice.length) {{
    ctx.fillText(slice[0].d, pad.l, h - 8);
    const last = slice[slice.length - 1];
    const lastText = last.d;
    ctx.fillText(lastText, w - pad.r - ctx.measureText(lastText).width, h - 8);
    tip.textContent = `${{last.d}}  状态:${{last.s}}  分数:${{last.score}}  收盘:${{last.c.toFixed(2)}}`;
  }}
}}

function setRange(value) {{
  activeRange = value;
  if (value === "all") {{
    visible = rows.length;
  }} else {{
    visible = Math.min(rows.length, Number(value));
  }}
  end = rows.length - 1;
  document.querySelectorAll(".range button").forEach(btn => {{
    btn.classList.toggle("active", btn.dataset.range === value);
  }});
  draw();
}}

function setVisible(nextVisible, anchorRatio = 1) {{
  const oldVisible = visible;
  const oldStart = Math.max(0, end - oldVisible + 1);
  const anchorIndex = oldStart + Math.floor(oldVisible * anchorRatio);
  visible = Math.max(20, Math.min(rows.length, Math.round(nextVisible)));
  end = Math.round(anchorIndex + visible * (1 - anchorRatio));
  end = Math.max(visible - 1, Math.min(rows.length - 1, end));
  activeRange = "";
  document.querySelectorAll(".range button").forEach(btn => btn.classList.remove("active"));
  draw();
}}

function zoom(factor, anchorRatio = 0.5) {{
  setVisible(visible * factor, anchorRatio);
}}

function roundRect(ctx, x, y, w, h, r) {{
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}}

canvas.addEventListener("pointerdown", e => {{
  pointers.set(e.pointerId, {{x: e.clientX, y: e.clientY}});
  if (pointers.size === 2) {{
    const pts = Array.from(pointers.values());
    pinchStartDistance = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
    pinchStartVisible = visible;
    dragging = false;
  }} else {{
    dragging = true;
    lastX = e.clientX;
  }}
  canvas.setPointerCapture(e.pointerId);
}});
canvas.addEventListener("pointermove", e => {{
  if (pointers.has(e.pointerId)) pointers.set(e.pointerId, {{x: e.clientX, y: e.clientY}});
  if (pointers.size === 2 && pinchStartDistance > 0) {{
    const pts = Array.from(pointers.values());
    const distance = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
    if (distance > 0) {{
      setVisible(pinchStartVisible * (pinchStartDistance / distance), 0.5);
    }}
    return;
  }}
  if (!dragging) return;
  const dx = e.clientX - lastX;
  if (Math.abs(dx) > 8) {{
    end = Math.max(visible - 1, Math.min(rows.length - 1, end - Math.sign(dx) * Math.ceil(Math.abs(dx) / 16)));
    lastX = e.clientX;
    draw();
  }}
}});
canvas.addEventListener("pointerup", e => {{ pointers.delete(e.pointerId); dragging = false; }});
canvas.addEventListener("pointercancel", e => {{ pointers.delete(e.pointerId); dragging = false; }});
canvas.addEventListener("wheel", e => {{
  e.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const anchor = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  zoom(e.deltaY < 0 ? 0.82 : 1.22, anchor);
}}, {{passive: false}});
canvas.addEventListener("dblclick", () => {{ end = rows.length - 1; draw(); }});
document.getElementById("zoomIn").addEventListener("click", () => zoom(0.75, 0.5));
document.getElementById("zoomOut").addEventListener("click", () => zoom(1.35, 0.5));
document.querySelectorAll(".range button").forEach(btn => {{
  btn.addEventListener("click", () => setRange(btn.dataset.range));
}});
window.addEventListener("resize", resize);
resize();
</script>
</body>
</html>
"""
    REPORT_HTML.write_text(html_text, encoding="utf-8")
    INDEX_HTML.write_text(html_text, encoding="utf-8")
    return REPORT_HTML


def render_history_card(row: dict) -> str:
    state = row["state"]
    color = STATE_COLORS[state]
    return (
        '<div class="day">'
        f'<strong style="background:{color}">{html.escape(state)}</strong><br>'
        f'{html.escape(row["date"])}<br>'
        f'分数 {html.escape(str(row["score"]))}<br>'
        f'收 {html.escape(fmt_num(row["close"]))}'
        "</div>"
    )


def local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def command_update(args: argparse.Namespace) -> int:
    rows = update_data(begin=args.begin, end=args.end)
    print(f"已更新 {len(rows)} 个交易日。最新日期: {rows[-1]['date']} {rows[-1]['state']} {rows[-1]['score']}分")
    return 0


def command_today(_: argparse.Namespace) -> int:
    rows = load_states()
    print_today(rows)
    return 0


def command_render(_: argparse.Namespace) -> int:
    rows = calculate_states(load_raw_rows())
    if not rows:
        raise RuntimeError("没有可用的中证全指数据，请先运行 update。")
    if rows:
        save_states_csv(rows)
    path = render_html(rows)
    print(f"已生成网页: {path}")
    print(f"默认首页: {INDEX_HTML}")
    print(f"手机同一 Wi-Fi 访问: 先运行 python3 -m http.server 8765 -d {OUTPUTS}")
    print(f"然后打开: http://{local_ip()}:8765/")
    return 0


def command_all(args: argparse.Namespace) -> int:
    rows = update_data(begin=args.begin, end=args.end)
    print_today(rows)
    path = render_html(rows)
    print("")
    print(f"网页: {path}")
    print(f"默认首页: {INDEX_HTML}")
    print(f"手机同一 Wi-Fi 访问: python3 -m http.server 8765 -d {OUTPUTS}")
    print(f"手机地址: http://{local_ip()}:8765/")
    return 0


def command_import_csv(args: argparse.Namespace) -> int:
    rows = import_csv_rows(Path(args.path))
    save_raw_rows(rows)
    states = calculate_states(rows)
    save_states_csv(states)
    path = render_html(states)
    print(f"已导入 {len(rows)} 个交易日的{INDEX_NAME}K线。")
    print_today(states)
    print("")
    print(f"网页: {path}")
    return 0


def command_serve(args: argparse.Namespace) -> int:
    os.chdir(OUTPUTS)
    print(f"服务目录: {OUTPUTS}")
    print(f"本机访问: http://127.0.0.1:{args.port}/")
    print(f"手机同一 Wi-Fi 访问: http://{local_ip()}:{args.port}/")
    from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

    server = ThreadingHTTPServer(("0.0.0.0", args.port), SimpleHTTPRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止服务。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="中证全指 多/转/空 指数环境判断系统")
    sub = parser.add_subparsers(dest="command", required=True)

    update = sub.add_parser("update", help="拉取/更新历史K线并计算状态")
    update.add_argument("--begin", default=DEFAULT_BEGIN)
    update.add_argument("--end", default=None)
    update.set_defaults(func=command_update)

    today = sub.add_parser("today", help="输出最新交易日状态")
    today.set_defaults(func=command_today)

    render = sub.add_parser("render", help="生成网页K线报告")
    render.set_defaults(func=command_render)

    all_cmd = sub.add_parser("all", help="更新数据、输出日报、生成网页")
    all_cmd.add_argument("--begin", default=DEFAULT_BEGIN)
    all_cmd.add_argument("--end", default=None)
    all_cmd.set_defaults(func=command_all)

    serve = sub.add_parser("serve", help="启动本地网页服务，便于手机访问")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=command_serve)

    import_csv_cmd = sub.add_parser("import-csv", help="导入平均股价日线CSV并生成状态/网页")
    import_csv_cmd.add_argument("path", help="CSV路径，支持 date/open/high/low/close/volume 或 中文列名")
    import_csv_cmd.set_defaults(func=command_import_csv)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
