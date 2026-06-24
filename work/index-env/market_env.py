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
import subprocess
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
WORK_DATA = ROOT / "data"
OUTPUTS = ROOT.parents[1] / "outputs"
RAW_JSON = WORK_DATA / "index_000985.json"
STATES_CSV = WORK_DATA / "states.csv"
MARKET_STRUCTURE_JSON = WORK_DATA / "market_structure.json"
SENSE_INDEX_JSON = WORK_DATA / "sense_average_price.json"
CONFIRMATION_JSON = WORK_DATA / "confirmation_indices.json"
REPORT_HTML = OUTPUTS / "index_env_report.html"
INDEX_HTML = OUTPUTS / "index.html"

SYMBOL = "sh000985"
INDEX_NAME = "中证全指"
INDEX_CODE = "sh000985"
SENSE_INDEX_NAME = "平均股价/体感指数"
DEFAULT_BEGIN = "2025-01-01"
STATE_COLORS = {
    "多": "#ef4444",
    "转": "#f59e0b",
    "空": "#16a34a",
}
ALL_A_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
INDUSTRY_FS = "m:90+t:2"
CONCEPT_FS = "m:90+t:3"
EMOTION_KEYWORDS = ("昨日", "打板", "连板", "涨停", "首板", "二板")
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json,text/plain,*/*",
    "Connection": "close",
}
CONFIRM_INDICES = [
    {"symbol": "sh000300", "name": "沪深300", "role": "权重风格"},
    {"symbol": "sh000852", "name": "中证1000", "role": "中小盘"},
    {"symbol": "sz399303", "name": "国证2000", "role": "小票题材"},
]
CHINA_TZ = ZoneInfo("Asia/Shanghai")


def china_now() -> dt.datetime:
    return dt.datetime.now(CHINA_TZ).replace(tzinfo=None)


def today_text() -> str:
    return china_now().strftime("%Y-%m-%d")


def now_text() -> str:
    return china_now().strftime("%Y-%m-%d %H:%M:%S")


def parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


def parse_quote_time(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y%m%d%H%M%S")


def is_trading_intraday(moment: dt.datetime) -> bool:
    if moment.weekday() >= 5:
        return False
    start = dt.time(9, 25)
    end = dt.time(15, 0)
    return start <= moment.time() < end


def is_live_trading_quote(quote_dt: dt.datetime) -> bool:
    current = china_now()
    return quote_dt.date() == current.date() and is_trading_intraday(current) and is_trading_intraday(quote_dt)


def elapsed_trading_minutes(moment: dt.datetime) -> int:
    sessions = (
        (dt.time(9, 30), dt.time(11, 30)),
        (dt.time(13, 0), dt.time(15, 0)),
    )
    elapsed = 0
    day = moment.date()
    for start_time, end_time in sessions:
        start = dt.datetime.combine(day, start_time)
        end = dt.datetime.combine(day, end_time)
        if moment <= start:
            continue
        elapsed += int((min(moment, end) - start).total_seconds() // 60)
    return max(0, min(240, elapsed))


def project_full_day_amount(amount: float, moment: dt.datetime) -> tuple[float, int]:
    elapsed = elapsed_trading_minutes(moment)
    if elapsed <= 0:
        return amount, elapsed
    return amount * 240 / elapsed, elapsed


def to_float(value: str | float | int) -> float:
    return float(value)


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def fmt_num(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def fmt_amount(value: float) -> str:
    if abs(value) >= 100_000_000:
        return f"{value / 100_000_000:.2f}亿"
    if abs(value) >= 10_000:
        return f"{value / 10_000:.2f}万"
    return f"{value:.0f}"


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


def fetch_tencent_realtime_row(symbol: str = SYMBOL) -> dict | None:
    url = f"https://qt.gtimg.cn/q={symbol}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://gu.qq.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        text = response.read().decode("gbk", errors="ignore")
    match = re.search(r'"([^"]+)"', text)
    if not match:
        return None
    fields = match.group(1).split("~")
    if len(fields) < 37 or not fields[30]:
        return None
    quote_dt = parse_quote_time(fields[30])
    current = safe_float(fields[3])
    if current <= 0:
        return None
    amount = safe_float(fields[57]) * 10000
    projected_amount, elapsed = project_full_day_amount(amount, quote_dt)
    return {
        "date": quote_dt.strftime("%Y-%m-%d"),
        "open": safe_float(fields[5], current),
        "high": safe_float(fields[33], current),
        "low": safe_float(fields[34], current),
        "close": current,
        "volume": safe_float(fields[36]),
        "amount": amount,
        "amount_projected": projected_amount,
        "elapsed_minutes": elapsed,
        "quote_time": quote_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "is_intraday": is_live_trading_quote(quote_dt),
        "prev_close": safe_float(fields[4]),
    }


def fetch_eastmoney_realtime_row() -> dict | None:
    url = (
        "https://push2.eastmoney.com/api/qt/stock/get?"
        "secid=1.000985&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f86,f169,f170"
    )
    payload = eastmoney_json(url)
    data = payload.get("data") or {}
    if payload.get("rc") != 0 or not data:
        return None
    quote_dt = dt.datetime.fromtimestamp(int(data.get("f86") or 0))
    current = safe_float(data.get("f43")) / 100
    if current <= 0:
        return None
    amount = safe_float(data.get("f48"))
    projected_amount, elapsed = project_full_day_amount(amount, quote_dt)
    return {
        "date": quote_dt.strftime("%Y-%m-%d"),
        "open": safe_float(data.get("f46")) / 100,
        "high": safe_float(data.get("f44")) / 100,
        "low": safe_float(data.get("f45")) / 100,
        "close": current,
        "volume": safe_float(data.get("f47")),
        "amount": amount,
        "amount_projected": projected_amount,
        "elapsed_minutes": elapsed,
        "quote_time": quote_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "is_intraday": is_live_trading_quote(quote_dt),
        "prev_close": safe_float(data.get("f60")) / 100,
    }


def append_intraday_row(rows: list[dict]) -> list[dict]:
    realtime = None
    try:
        realtime = fetch_eastmoney_realtime_row()
    except Exception as exc:
        print(f"东方财富实时指数数据暂时不可用: {exc}", file=sys.stderr)
    if realtime is None:
        try:
            realtime = fetch_tencent_realtime_row()
        except Exception as exc:
            print(f"腾讯实时指数数据暂时不可用: {exc}", file=sys.stderr)
            return rows
    if not realtime or not realtime.get("is_intraday"):
        return rows
    output = [dict(row) for row in rows]
    if output and realtime["date"] < output[-1]["date"]:
        return output
    realtime["intraday"] = True
    realtime["status_note"] = "交易中，盘中数据会波动，收盘后才会固化"
    if output and realtime["date"] == output[-1]["date"]:
        output[-1] = realtime
    else:
        output.append(realtime)
    return output


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


def eastmoney_json(url: str) -> dict:
    last_error: Exception | None = None
    for _ in range(3):
        try:
            request = urllib.request.Request(url, headers=REQUEST_HEADERS)
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
    try:
        result = subprocess.run(
            [
                "curl",
                "-L",
                "-A",
                REQUEST_HEADERS["User-Agent"],
                "-e",
                REQUEST_HEADERS["Referer"],
                "--max-time",
                "20",
                url,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)
    except Exception as exc:
        raise RuntimeError(f"Eastmoney request failed: {last_error}; curl fallback failed: {exc}") from exc


def eastmoney_clist(fs: str, fields: str, page_size: int = 100, sort_field: str = "f3") -> list[dict]:
    page_size = max(1, min(page_size, 100))
    rows: list[dict] = []
    total = None
    page = 1
    while total is None or len(rows) < total:
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get?"
            f"pn={page}&pz={page_size}&po=1&np=1&fltt=2&invt=2&fid={sort_field}"
            f"&fs={fs}&fields={fields}"
        )
        payload = eastmoney_json(url)
        if payload.get("rc") != 0:
            raise RuntimeError(f"Eastmoney API error: {payload!r}")
        data = payload.get("data", {}) or {}
        diff = data.get("diff") or []
        if total is None:
            total = int(data.get("total") or len(diff))
        if not diff:
            break
        rows.extend(diff)
        if len(diff) < page_size:
            break
        page += 1
    return rows[: total or len(rows)]


def limit_threshold(code: str) -> float:
    if code.startswith(("30", "68")):
        return 19.8
    if code.startswith(("4", "8")):
        return 29.8
    return 9.8


def is_limit_up(item: dict) -> bool:
    pct = item.get("f3")
    code = str(item.get("f12", ""))
    return isinstance(pct, (int, float)) and pct >= limit_threshold(code)


def is_limit_down(item: dict) -> bool:
    pct = item.get("f3")
    code = str(item.get("f12", ""))
    return isinstance(pct, (int, float)) and pct <= -limit_threshold(code)


def sector_signal(items: list[dict]) -> list[dict]:
    output = []
    for item in items:
        name = str(item.get("f14") or "")
        if not name:
            continue
        output.append(
            {
                "code": str(item.get("f12") or ""),
                "name": name,
                "pct": safe_float(item.get("f3")),
                "amount": safe_float(item.get("f6")),
                "net": safe_float(item.get("f62")),
            }
        )
    return output


def stock_signal(item: dict) -> dict:
    return {
        "code": str(item.get("f12") or ""),
        "name": str(item.get("f14") or ""),
        "pct": safe_float(item.get("f3")),
        "price": safe_float(item.get("f2")),
        "amount": safe_float(item.get("f6")),
        "net": safe_float(item.get("f62")),
    }


def assess_core_stocks(items: list[dict]) -> list[dict]:
    stocks = [
        stock_signal(item)
        for item in items
        if isinstance(item.get("f3"), (int, float))
        and safe_float(item.get("f6")) > 0
        and safe_float(item.get("f2")) > 0
    ]
    stocks = [
        item
        for item in stocks
        if item["pct"] > 0 and item["name"] and "ST" not in item["name"] and "退" not in item["name"]
    ]
    if not stocks:
        return []

    amount_rank = {item["code"]: rank for rank, item in enumerate(sorted(stocks, key=lambda x: x["amount"], reverse=True), 1)}
    net_rank = {item["code"]: rank for rank, item in enumerate(sorted(stocks, key=lambda x: x["net"], reverse=True), 1)}
    scored = []
    for item in stocks:
        score = 0.0
        reasons: list[str] = []
        pct = item["pct"]
        amount = item["amount"]
        net = item["net"]
        arank = amount_rank.get(item["code"], 9999)
        nrank = net_rank.get(item["code"], 9999)

        if pct >= limit_threshold(item["code"]):
            score += 3
            reasons.append("强势涨停/接近涨停")
        elif pct >= 7:
            score += 2
            reasons.append("涨幅主动")
        elif pct >= 4:
            score += 1
            reasons.append("明显强于市场")
        if arank <= 10:
            score += 3
            reasons.append("成交额全市场前10")
        elif arank <= 30:
            score += 2
            reasons.append("成交额全市场前30")
        elif amount >= 5_000_000_000:
            score += 1
            reasons.append("成交额有辨识度")
        if nrank <= 10 and net > 0:
            score += 3
            reasons.append("主力净流入前10")
        elif nrank <= 30 and net > 0:
            score += 2
            reasons.append("主力净流入前30")
        elif net >= 500_000_000:
            score += 1
            reasons.append("主动资金净流入")
        if pct >= 4 and amount >= 5_000_000_000:
            score += 1
            reasons.append("上涨时主动放量")
        if pct >= 4 and amount >= 5_000_000_000 and net > 0:
            score += 1
            reasons.append("有带动性")

        if score >= 7:
            scored.append(
                {
                    **item,
                    "score": round(score, 1),
                    "amount_rank": arank,
                    "net_rank": nrank,
                    "reasons": reasons[:5],
                }
            )
    scored.sort(key=lambda item: (item["score"], item["amount"], item["net"]), reverse=True)
    return scored[:2]


def non_emotion_sectors(items: list[dict]) -> list[dict]:
    return [
        item
        for item in items
        if not any(keyword in item["name"] for keyword in EMOTION_KEYWORDS)
    ]


def load_market_structures() -> list[dict]:
    if not MARKET_STRUCTURE_JSON.exists():
        return []
    rows = json.loads(MARKET_STRUCTURE_JSON.read_text(encoding="utf-8"))
    normalized: list[dict] = []
    for row in sorted(rows, key=lambda item: item.get("date", "")):
        if row.get("breadth") and (row.get("top_industries") or row.get("top_concepts")):
            row = dict(row)
            row["mainline"] = assess_market_structure(row, normalized)
        normalized.append(row)
    return normalized


def save_market_structures(rows: list[dict]) -> None:
    WORK_DATA.mkdir(parents=True, exist_ok=True)
    MARKET_STRUCTURE_JSON.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def assess_market_structure(snapshot: dict, history: list[dict]) -> dict:
    breadth = snapshot["breadth"]
    sectors = non_emotion_sectors(snapshot["top_industries"][:12] + snapshot["top_concepts"][:30])
    sector_names = [item["name"] for item in sectors[:8]]
    prev_names = {
        name
        for item in history[-2:]
        for name in item.get("mainline", {}).get("sector_names", [])
    }
    overlap = len(set(sector_names[:5]) & prev_names)
    top_pct = sectors[0]["pct"] if sectors else 0
    avg_top3 = sum(item["pct"] for item in sectors[:3]) / max(1, min(3, len(sectors)))
    mainline_title, core_branches, side_branches = classify_mainline(sectors)
    leadership_score, leadership_factors = score_mainline_leadership(
        sectors,
        breadth,
        overlap,
        top_pct,
        avg_top3,
        core_branches,
    )
    has_mainline = top_pct >= 3.5 and avg_top3 >= 2.5 and leadership_score >= 5.5

    emotion_boards = [
        item for item in snapshot["top_concepts"][:10] if any(keyword in item["name"] for keyword in EMOTION_KEYWORDS)
    ]
    emotion_active = breadth.get("limit_up", 0) >= 50 or any(item["pct"] >= 3 for item in emotion_boards)

    if has_mainline:
        status = "有主线"
        factor_text = "、".join(leadership_factors[:3])
        note = f"强势方向集中在 {mainline_title or ', '.join(sector_names[:3])}，{factor_text}"
    elif emotion_active:
        status = "妖股情绪"
        note = "主线不清晰，但涨停/连板情绪活跃"
    else:
        status = "无主线"
        factor_text = "、".join(leadership_factors[:2]) if leadership_factors else "缺少持续性/主动性确认"
        note = f"强势方向领导力不足（{factor_text}），适合降低预期"

    return {
        "status": status,
        "has_mainline": has_mainline,
        "emotion_active": emotion_active,
        "sector_names": sector_names[:8],
        "title": mainline_title or (sector_names[0] if sector_names else "暂无"),
        "core_branches": core_branches,
        "side_branches": side_branches,
        "overlap_3d": overlap,
        "top_pct": top_pct,
        "avg_top3_pct": avg_top3,
        "leadership_score": round(leadership_score, 1),
        "leadership_factors": leadership_factors,
        "note": note,
    }


def score_mainline_leadership(
    sectors: list[dict],
    breadth: dict,
    overlap: int,
    top_pct: float,
    avg_top3: float,
    core_branches: list[str],
) -> tuple[float, list[str]]:
    score = 0.0
    factors: list[str] = []
    if not sectors:
        return score, factors

    top_amount = max(safe_float(item.get("amount")) for item in sectors[:5])
    top3_amount = sum(safe_float(item.get("amount")) for item in sectors[:3])
    net_positive_count = sum(1 for item in sectors[:5] if safe_float(item.get("net")) > 0)
    net_sum = sum(safe_float(item.get("net")) for item in sectors[:5])
    up_ratio = safe_float(breadth.get("up_ratio"))
    limit_up = int(safe_float(breadth.get("limit_up")))

    if top_pct >= 5:
        score += 2
        factors.append("领涨强度高")
    elif top_pct >= 3.5:
        score += 1
        factors.append("领涨强度达标")
    if avg_top3 >= 3.5:
        score += 1.5
        factors.append("前三强度集中")
    elif avg_top3 >= 2.5:
        score += 1
        factors.append("前三强度达标")
    if overlap >= 2:
        score += 2
        factors.append("近3日方向延续")
    elif overlap >= 1:
        score += 1
        factors.append("有延续迹象")
    if top_amount >= 100_000_000_000 or top3_amount >= 200_000_000_000:
        score += 1.5
        factors.append("成交额有市场地位")
    elif top_amount >= 50_000_000_000:
        score += 1
        factors.append("成交额体量尚可")
    if net_positive_count >= 3 and net_sum > 0:
        score += 1.5
        factors.append("主动资金净流入")
    elif net_positive_count >= 2:
        score += 0.8
        factors.append("主动资金有承接")
    if len(core_branches) >= 4:
        score += 1.5
        factors.append("核心分支成簇")
    elif len(core_branches) >= 2:
        score += 0.8
        factors.append("有分支联动")
    if up_ratio >= 55 or limit_up >= 60:
        score += 1
        factors.append("市场带动较强")
    elif up_ratio <= 35 and limit_up < 40:
        score -= 1
        factors.append("市场带动偏弱")

    return score, factors


def classify_mainline(sectors: list[dict]) -> tuple[str, list[str], list[str]]:
    names = [item["name"] for item in sectors]
    resource_keywords = ("钼", "钨", "铜", "白银", "黄金", "小金属", "钴", "镍")
    ai, ai_subthemes = classify_ai_hardware(sectors)
    resource = [name for name in names if any(keyword in name for keyword in resource_keywords)]
    if len(ai) >= 2:
        visible_subthemes = ai_subthemes[:2]
        if len(ai_subthemes) >= 3 and ai_subthemes[2]["score"] >= max(ai_subthemes[0]["score"] - 4.0, 4.5):
            visible_subthemes = ai_subthemes[:3]
        subtheme_text = "+".join(item["label"] for item in visible_subthemes) if visible_subthemes else pick_subtheme(ai)
        return f"AI硬件 / {subtheme_text}", ai[:8], resource[:5]
    if len(resource) >= 3:
        return f"资源金属 / {pick_subtheme(resource)}", resource[:8], ai[:5]
    return (" / ".join(names[:2]) if names else "暂无", names[:6], [])


def classify_ai_hardware(sectors: list[dict]) -> tuple[list[str], list[dict]]:
    subthemes = [
        ("PCB/PET铜箔", ("PCB", "印制电路板", "PET铜箔", "复合集流体", "铜箔")),
        ("存储芯片/HBM", ("存储芯片", "高带宽内存", "HBM", "DRAM", "NAND", "存储器", "存储")),
        ("CPO", ("CPO", "光通信", "光纤", "铜缆", "通信线缆")),
        ("被动元件", ("MLCC", "被动元件", "元件")),
        ("先进封装", ("先进封装", "集成电路")),
        ("激光设备", ("激光",)),
        ("半导体材料", ("电子化学品", "分立器件", "AI芯片")),
    ]
    matched_names: list[str] = []
    scored: list[dict] = []
    for label, keywords in subthemes:
        matched = [item for item in sectors if any(keyword in item["name"] for keyword in keywords)]
        if not matched:
            continue
        names = [item["name"] for item in matched]
        matched_names.extend(name for name in names if name not in matched_names)
        best_pct = max(safe_float(item.get("pct")) for item in matched)
        amount = sum(safe_float(item.get("amount")) for item in matched)
        net = sum(safe_float(item.get("net")) for item in matched)
        score = best_pct + min(amount / 100_000_000_000, 2.5) + (1.0 if net > 0 else 0.0) + min(len(matched) * 0.4, 1.6)
        if label == "PCB/PET铜箔":
            score += 1.8
        if label == "存储芯片/HBM":
            score += 1.2
        if label == "CPO" and not any("CPO" in item["name"] for item in matched):
            score -= 1.2
        scored.append({"label": label, "score": score, "names": names})
    best_score = max((item["score"] for item in scored), default=0.0)
    narrow_labels = {"PCB/PET铜箔"}
    scored.sort(
        key=lambda item: (
            item["label"] in narrow_labels and item["score"] >= best_score - 3,
            item["score"],
        ),
        reverse=True,
    )
    return matched_names, scored


def pick_subtheme(names: list[str]) -> str:
    groups = [
        ("PCB/PET铜箔", ("PCB", "印制电路板", "PET铜箔", "复合集流体", "铜箔")),
        ("存储芯片/HBM", ("存储芯片", "高带宽内存", "HBM", "DRAM", "NAND", "存储器", "存储")),
        ("CPO", ("CPO", "光通信", "光纤", "铜缆")),
        ("被动元件", ("MLCC", "被动元件", "元件")),
        ("先进封装", ("先进封装", "集成电路")),
        ("激光设备", ("激光",)),
        ("钨钼", ("钨", "钼")),
        ("铜", ("铜",)),
        ("贵金属", ("白银", "黄金")),
        ("小金属", ("小金属", "钴", "镍")),
    ]
    for label, keywords in groups:
        if any(any(keyword in name for keyword in keywords) for name in names):
            return label
    return names[0] if names else "暂无"


def fetch_market_structure(trade_date: str, intraday_quote: dict | None = None) -> dict:
    partial = False
    try:
        stocks = eastmoney_clist(ALL_A_FS, "f12,f14,f3,f6,f2,f62", page_size=100)
    except Exception as exc:
        print(f"全A宽度数据暂时不可用: {exc}", file=sys.stderr)
        stocks = []
        partial = True
    try:
        industries = sector_signal(eastmoney_clist(INDUSTRY_FS, "f12,f14,f3,f6,f62", page_size=30))
    except Exception as exc:
        print(f"行业板块数据暂时不可用: {exc}", file=sys.stderr)
        industries = []
        partial = True
    try:
        concepts = sector_signal(eastmoney_clist(CONCEPT_FS, "f12,f14,f3,f6,f62", page_size=80))
    except Exception as exc:
        print(f"概念板块数据暂时不可用: {exc}", file=sys.stderr)
        concepts = []
        partial = True

    valid = [item for item in stocks if isinstance(item.get("f3"), (int, float))]
    up = sum(item["f3"] > 0 for item in valid)
    down = sum(item["f3"] < 0 for item in valid)
    flat = len(valid) - up - down
    limit_up = sum(is_limit_up(item) for item in valid)
    limit_down = sum(is_limit_down(item) for item in valid)
    big_drop = sum(item["f3"] <= -7 for item in valid)
    amount = sum(safe_float(item.get("f6")) for item in valid)
    if amount <= 0 and intraday_quote:
        amount = safe_float(intraday_quote.get("amount"))
    up_ratio = up / len(valid) * 100 if valid else 0
    quote_time = None
    is_intraday = False
    elapsed = 240
    amount_projected = amount
    if intraday_quote and intraday_quote.get("is_intraday"):
        quote_time = str(intraday_quote.get("quote_time") or "")
        is_intraday = True
        try:
            quote_dt = dt.datetime.strptime(quote_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            quote_dt = china_now()
        amount_projected, elapsed = project_full_day_amount(amount, quote_dt)

    snapshot = {
        "date": trade_date,
        "quote_time": quote_time or now_text(),
        "is_intraday": is_intraday,
        "partial": partial,
        "breadth": {
            "total": len(valid),
            "up": up,
            "down": down,
            "flat": flat,
            "up_ratio": up_ratio,
            "limit_up": limit_up,
            "limit_down": limit_down,
            "big_drop": big_drop,
            "amount": amount,
            "amount_projected": amount_projected,
            "elapsed_minutes": elapsed,
            "amount_note": "盘中按已交易分钟外推预计全天成交额" if is_intraday else "收盘后正式成交额",
        },
        "top_industries": industries[:12],
        "top_concepts": concepts[:30],
        "core_stocks": assess_core_stocks(valid),
    }
    history = [item for item in load_market_structures() if item.get("date") != trade_date]
    snapshot["mainline"] = assess_market_structure(snapshot, history)
    return snapshot


def update_market_structure(trade_date: str, intraday_quote: dict | None = None) -> dict | None:
    try:
        snapshot = fetch_market_structure(trade_date, intraday_quote=intraday_quote)
    except Exception as exc:
        print(f"市场结构数据暂时不可用: {exc}", file=sys.stderr)
        return None
    quote_time = str(snapshot.get("quote_time") or "")
    if quote_time[:10] and quote_time[:10] != trade_date:
        print(
            f"市场结构数据日期不匹配: trade_date={trade_date}, quote_time={quote_time}，保留已有缓存。",
            file=sys.stderr,
        )
        return None
    if not snapshot.get("top_industries") and not snapshot.get("top_concepts"):
        print("市场结构数据暂时不可用: 行业/概念板块为空，保留已有缓存。", file=sys.stderr)
        return None
    rows = [item for item in load_market_structures() if item.get("date") != trade_date]
    rows.append(snapshot)
    rows.sort(key=lambda item: item["date"])
    save_market_structures(rows)
    return snapshot


def attach_market_structure(rows: list[dict]) -> list[dict]:
    by_date = {item["date"]: item for item in load_market_structures()}
    for row in rows:
        market = by_date.get(row["date"])
        if market and market.get("is_intraday") and not row.get("intraday"):
            market = dict(market)
            breadth = dict(market.get("breadth") or {})
            if safe_float(breadth.get("amount_projected")) > 0:
                breadth["amount"] = breadth["amount_projected"]
            breadth["amount_note"] = "收盘后展示最近一次市场结构快照，等待正式宽度/板块数据刷新"
            market["breadth"] = breadth
            market["is_intraday"] = False
            market["snapshot_note"] = "市场结构来自盘中最近一次快照"
        row["market"] = market
    return rows


def fallback_intraday_market(row: dict) -> dict | None:
    amount = safe_float(row.get("amount"))
    if amount <= 0:
        return None
    return {
        "date": row["date"],
        "quote_time": row.get("quote_time"),
        "is_intraday": True,
        "partial": True,
        "breadth": {
            "total": 0,
            "up": 0,
            "down": 0,
            "flat": 0,
            "up_ratio": 0,
            "limit_up": 0,
            "limit_down": 0,
            "big_drop": 0,
            "amount": amount,
            "amount_projected": safe_float(row.get("amount_projected"), amount),
            "elapsed_minutes": int(row.get("elapsed_minutes") or 0),
            "amount_note": "市场宽度接口暂不可用；这里展示中证全指成交额，按已交易分钟外推预计全天",
        },
        "top_industries": [],
        "top_concepts": [],
        "mainline": {
            "status": "数据暂缺",
            "has_mainline": False,
            "emotion_active": False,
            "sector_names": [],
            "overlap_3d": 0,
            "top_pct": 0,
            "avg_top3_pct": 0,
            "note": "市场宽度/板块接口暂不可用，仅保留指数盘中成交额预测",
        },
    }


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


def load_sense_rows() -> list[dict]:
    if not SENSE_INDEX_JSON.exists():
        return []
    return json.loads(SENSE_INDEX_JSON.read_text(encoding="utf-8"))


def save_sense_rows(rows: list[dict]) -> None:
    WORK_DATA.mkdir(parents=True, exist_ok=True)
    SENSE_INDEX_JSON.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_confirmation_cache() -> dict:
    if not CONFIRMATION_JSON.exists():
        return {}
    return json.loads(CONFIRMATION_JSON.read_text(encoding="utf-8"))


def save_confirmation_cache(payload: dict) -> None:
    WORK_DATA.mkdir(parents=True, exist_ok=True)
    CONFIRMATION_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
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
        three_day_pct = 0.0

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

        transition_limited = False
        recent_after_bear = any(item["state"] == "空" for item in output[-3:])
        ma_structure_repaired = None not in (ma5, ma10, ma20) and ma5 > ma10 and ma5 > ma20
        strong_bull_confirm = score >= 75 and (volume_above_ma20 or ma_structure_repaired)
        previous_state = output[-1]["state"] if output else None
        close_below_ma10 = ma10 is not None and row["close"] < ma10
        severe_single_day_break = pct_change <= -2.5 and volume_above_ma5 and close_position <= 0.35
        severe_ma_break = hard_below_ma20 and (
            (ma5 is not None and ma10 is not None and ma5 < ma10)
            or (pct_change <= -1.5 and volume_above_ma5)
        )
        severe_multi_day_break = close_below_ma10 and three_day_pct <= -4.5
        severe_bear_confirm = severe_single_day_break or severe_ma_break or severe_multi_day_break
        if state == "多" and output and output[-1]["state"] == "空":
            state = "转"
            score = min(score, 69)
            transition_limited = True
            reasons.append("空头后首日修复，先按转处理，需次日确认 +0")
        elif state == "多" and recent_after_bear and not strong_bull_confirm:
            state = "转"
            score = min(score, 69)
            transition_limited = True
            reasons.append("空头修复后多头确认不足，需量能或均线结构确认 +0")
        elif state == "空" and previous_state == "多" and not severe_bear_confirm:
            state = "转"
            score = max(score, 40)
            reasons.append("多头后首日回撤，未出现严重破位，先按转处理 +0")

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
            "transition_limited": transition_limited,
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

    if latest == "空":
        return "防守"
    if latest == "多" and labels5.count("多") >= 3:
        return "主升"
    if latest == "多" and labels3 == ["转", "多", "多"]:
        return "主升"
    if len(labels3) == 3 and labels3.count("转") >= 2 and latest == "多":
        return "主升"
    if latest == "转" and labels5.count("多") >= 3:
        return "主升"
    if latest == "多":
        return "试攻"
    return "观察"


def latest_state_summary(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    state_rows = calculate_states(rows)
    latest = state_rows[-1]
    return {
        "date": latest["date"],
        "state": latest["state"],
        "score": latest["score"],
        "phase": latest["phase"],
        "close": latest["close"],
        "pct": latest.get("pct_change", 0.0),
    }


def update_confirmation_indices(begin: str = DEFAULT_BEGIN, end: str | None = None) -> dict:
    end = end or today_text()
    payload = {"updated_at": now_text(), "indices": []}
    for item in CONFIRM_INDICES:
        try:
            rows = fetch_tencent_kline(item["symbol"], begin, end)
            summary = latest_state_summary(rows)
            if summary:
                payload["indices"].append({**item, **summary})
        except Exception as exc:
            payload["indices"].append({**item, "error": str(exc)})
    save_confirmation_cache(payload)
    return payload


def build_confirmation(main_row: dict) -> dict:
    payload = load_confirmation_cache()
    indices = payload.get("indices", [])
    sense_summary = latest_state_summary(load_sense_rows())

    small_caps = [
        item
        for item in indices
        if item.get("name") in ("中证1000", "国证2000") and item.get("state")
    ]
    weight = next((item for item in indices if item.get("name") == "沪深300"), None)
    small_bull = sum(1 for item in small_caps if item.get("state") == "多")
    small_bear = sum(1 for item in small_caps if item.get("state") == "空")

    if sense_summary:
        sense_text = f"{SENSE_INDEX_NAME}{sense_summary['state']}（{sense_summary['score']}分）"
    else:
        sense_text = ""

    if small_bull >= 2:
        style_text = "中小盘/题材风格强"
    elif small_bear >= 2:
        style_text = "中小盘/题材风格弱"
    elif weight and weight.get("state") == "多":
        style_text = "权重风格相对强"
    else:
        style_text = "风格未共振"

    confirm_score = 0
    if sense_summary:
        confirm_score += {"多": 2, "转": 1, "空": -2}.get(sense_summary["state"], 0)
    for item in indices:
        confirm_score += {"多": 1, "转": 0, "空": -1}.get(item.get("state"), 0)

    if main_row["state"] == "多" and confirm_score >= 2:
        conclusion = "综合偏多，指数与体感/风格确认度较高"
    elif main_row["state"] == "多":
        conclusion = "指数偏多但确认不足，仓位宜打折"
    elif main_row["state"] == "转" and confirm_score >= 2:
        conclusion = "指数震荡但体感/风格修复，可试主线"
    elif main_row["state"] == "空":
        conclusion = "指数仍在防守区，确认层只作为修复观察"
    else:
        conclusion = "综合震荡，等待方向确认"

    return {
        "updated_at": payload.get("updated_at"),
        "sense": sense_summary,
        "indices": indices,
        "style": style_text,
        "sense_text": sense_text,
        "confirm_score": confirm_score,
        "conclusion": conclusion,
    }


def position_advice(row: dict) -> str:
    market = row.get("market")
    mainline = market.get("mainline", {}) if market else {}
    has_mainline = bool(mainline.get("has_mainline"))
    emotion_active = bool(mainline.get("emotion_active"))

    if row["state"] == "空":
        return "0%-20%，指数主跌/防守，不适合重仓"
    if row.get("transition_limited"):
        if has_mainline:
            return "20%-40%，空头后修复期，有主线也先等趋势确认"
        return "10%-30%，空头后修复期，先观察持续性"
    if row["phase"] == "主升" and has_mainline:
        return "70%-90%，指数主升且有主线，可重仓主线"
    if row["phase"] == "主升":
        return "60%-80%，指数主升但主线确认不足"
    if row["state"] in ("多", "转") and has_mainline:
        return "40%-60%，指数震荡/试攻，有主线可参与主线"
    if row["state"] in ("多", "转") and emotion_active:
        return "10%-30%，无清晰主线但情绪活跃，只适合小仓"
    if row["state"] == "多":
        return "30%-50%，指数偏强但缺少主线确认"
    if row["state"] == "转":
        return "0%-20%，指数震荡且无主线，低仓位观察"
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
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in fieldnames})


def load_states() -> list[dict]:
    raw_rows = load_raw_rows()
    if raw_rows:
        rows = calculate_states(raw_rows)
        save_states_csv(rows)
        return attach_market_structure(rows)
    if not STATES_CSV.exists():
        return []
    with STATES_CSV.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for key in ("open", "high", "low", "close", "volume"):
            row[key] = float(row[key])
        row["score"] = int(row["score"])
    return attach_market_structure(rows)


def update_data(begin: str = DEFAULT_BEGIN, end: str | None = None) -> list[dict]:
    end = end or today_text()
    old_rows = load_raw_rows()
    fetch_begin = begin
    if old_rows:
        last_date = parse_date(old_rows[-1]["date"])
        fetch_begin = (last_date - dt.timedelta(days=10)).strftime("%Y-%m-%d")
    new_rows = fetch_kline(fetch_begin, end)
    if is_trading_intraday(china_now()):
        new_rows = [row for row in new_rows if row["date"] != today_text()]
    merged = merge_rows(old_rows, new_rows)
    if not merged:
        raise RuntimeError("No K-line data fetched.")
    save_raw_rows(merged)
    states = calculate_states(merged)
    if not is_trading_intraday(china_now()) or states[-1]["date"] == today_text():
        update_market_structure(states[-1]["date"])
    attach_market_structure(states)
    save_states_csv(states)
    return states


def build_display_states(include_intraday: bool = True, refresh_market: bool = False) -> list[dict]:
    raw_rows = load_raw_rows()
    if include_intraday:
        raw_rows = append_intraday_row(raw_rows)
    states = calculate_states(raw_rows)
    if states and states[-1].get("intraday") and refresh_market:
        update_market_structure(states[-1]["date"], intraday_quote=states[-1])
    attach_market_structure(states)
    if states and states[-1].get("intraday") and not states[-1].get("market"):
        states[-1]["market"] = fallback_intraday_market(states[-1])
    if states:
        states[-1]["confirmation"] = build_confirmation(states[-1])
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
    if row.get("intraday"):
        print(f"日期: {row['date']}（交易中，盘中临时判断）")
        print(f"盘中更新时间: {row.get('quote_time', '-')}")
        print("提示: 盘中状态会随价格和成交额变化，收盘后才会固化为正式历史。")
    else:
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
    market = row.get("market")
    if market:
        breadth = market["breadth"]
        mainline = market["mainline"]
        if market.get("partial"):
            print("市场宽度: 暂不可用")
        else:
            print(
                "市场宽度: "
                f"上涨 {breadth['up']} / 下跌 {breadth['down']} / "
                f"上涨比例 {fmt_num(breadth['up_ratio'])}% / "
                f"涨停 {breadth['limit_up']} / 跌停 {breadth['limit_down']} / "
                f"大跌股 {breadth['big_drop']}"
            )
        if market.get("is_intraday"):
            print(
                "市场成交额: "
                f"当前 {fmt_amount(breadth['amount'])} / "
                f"预计全天 {fmt_amount(breadth.get('amount_projected', breadth['amount']))}"
            )
            print(f"成交额口径: {breadth.get('amount_note', '盘中预测')}")
        else:
            print(f"市场成交额: {fmt_amount(breadth['amount'])}")
        print(f"主线判断: {mainline['status']}，{mainline['note']}")
        if mainline.get("leadership_score") is not None:
            factors = " / ".join(mainline.get("leadership_factors") or [])
            print(f"主线领导力: {mainline['leadership_score']}分" + (f"（{factors}）" if factors else ""))
        if mainline.get("sector_names"):
            print(f"强势方向: {' / '.join(mainline['sector_names'][:5])}")
        core_stocks = market.get("core_stocks") or []
        if core_stocks:
            print("市场核心个股:")
            for item in core_stocks[:2]:
                reasons = " / ".join(item.get("reasons") or [])
                print(
                    f"- {item['name']}({item['code']}): "
                    f"{fmt_num(item['pct'])}% 成交额{fmt_amount(item['amount'])} "
                    f"净流入{fmt_amount(item['net'])}，{reasons}"
                )
    else:
        print("市场结构: 暂无当日涨跌家数/主线数据")
    confirmation = row.get("confirmation") or build_confirmation(row)
    if confirmation.get("sense_text"):
        print(f"体感确认: {confirmation['sense_text']}")
    print(f"风格确认: {confirmation['style']}")
    print(f"综合结论: {confirmation['conclusion']}")
    for item in confirmation.get("indices", []):
        if item.get("state"):
            print(f"- {item['name']}({item['role']}): {item['state']} {item['score']}分")
        elif item.get("error"):
            print(f"- {item['name']}({item['role']}): 数据暂不可用")
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
                "transitionLimited": bool(row.get("transition_limited")),
                "pct": row.get("pct_change", 0.0),
                "intraday": bool(row.get("intraday")),
                "quoteTime": row.get("quote_time"),
                "statusNote": row.get("status_note"),
                "amount": row.get("amount"),
                "amountProjected": row.get("amount_projected"),
                "ma5": row.get("ma5"),
                "ma10": row.get("ma10"),
                "ma20": row.get("ma20"),
                "reasons": row["reasons"],
                "market": row.get("market"),
                "confirmation": row.get("confirmation"),
            }
        )
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def render_mainline_section(row: dict) -> str:
    market = row.get("market")
    if not market or not market.get("mainline"):
        return (
            '<section class="mainline">'
            '<div class="mainline-head">'
            '<div><div class="mainline-title">今日最强主线</div>'
            '<div class="mainline-name">板块数据暂不可用</div></div>'
            '<div class="mainline-status">等待刷新</div>'
            "</div>"
            '<div class="mainline-body"><div>当前未获取到行业/概念强度数据，稍后刷新或重新运行 all。</div></div>'
            "</section>"
        )
    mainline = market["mainline"]
    title = mainline.get("title") or mainline.get("status") or "暂无"
    core = mainline.get("core_branches") or mainline.get("sector_names") or []
    side = mainline.get("side_branches") or []
    status = mainline.get("status") or "观察"
    core_text = " / ".join(core[:8]) if core else "暂无"
    side_text = " / ".join(side[:6]) if side else "暂无明显强支线"
    note = mainline.get("note") or ""
    leadership_score = mainline.get("leadership_score")
    leadership_factors = " / ".join(mainline.get("leadership_factors") or [])
    core_stocks = market.get("core_stocks") or []
    core_stock_text = " / ".join(
        f"{item.get('name')}({fmt_num(safe_float(item.get('pct')))}%)"
        for item in core_stocks[:2]
    ) or "暂无"
    return (
        '<section class="mainline">'
        '<div class="mainline-head">'
        '<div>'
        '<div class="mainline-title">今日最强主线</div>'
        f'<div class="mainline-name">{html.escape(title)}</div>'
        "</div>"
        f'<div class="mainline-status">{html.escape(status)}</div>'
        "</div>"
        '<div class="mainline-body">'
        f'<div><b>核心分支</b>{html.escape(core_text)}</div>'
        f'<div><b>强支线</b>{html.escape(side_text)}</div>'
        f'<div><b>领导力分</b>{html.escape(str(leadership_score if leadership_score is not None else "-"))}</div>'
        f'<div><b>依据</b>{html.escape(leadership_factors or "暂无")}</div>'
        f'<div><b>核心个股</b>{html.escape(core_stock_text)}</div>'
        f'<div><b>判断</b>{html.escape(note)}</div>'
        f'<div><b>更新时间</b>{html.escape(str(market.get("quote_time") or "-"))}</div>'
        "</div>"
        "</section>"
    )


def render_html(rows: list[dict]) -> Path:
    if not rows:
        raise RuntimeError("No rows to render.")
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    latest = rows[-1]
    data_json = json_for_chart(rows)
    state = latest["state"]
    color = STATE_COLORS[state]
    updated = now_text()
    is_intraday = bool(latest.get("intraday"))
    data_mode = "交易中 · 盘中临时判断" if is_intraday else "收盘口径"
    quote_time = latest.get("quote_time") or updated
    date_label = f"{latest['date']}（交易中）" if is_intraday else latest["date"]
    intraday_banner = ""
    if is_intraday:
        intraday_banner = (
            '<section class="live-banner">'
            '<strong>交易中</strong>'
            f'<span>当前为盘中实时分析，更新时间 {html.escape(str(quote_time))}。'
            "多/转/空和成交额预测会随盘面变化，收盘后才会固化为正式历史。</span>"
            "</section>"
        )
    mainline_section = render_mainline_section(latest)
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
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
    .live-banner {{
      display: flex;
      gap: 10px;
      align-items: center;
      border: 1px solid #f59e0b;
      background: #fff7ed;
      color: #9a3412;
      border-radius: var(--radius);
      padding: 10px 12px;
      margin-bottom: 12px;
      line-height: 1.55;
    }}
    .live-banner strong {{
      flex: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 64px;
      height: 30px;
      border-radius: 6px;
      background: #f97316;
      color: white;
      font-size: 15px;
    }}
    .live-banner span {{
      font-size: 14px;
    }}
    .live-banner.stale strong {{
      background: #dc2626;
    }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }}
    .mainline {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-left: 5px solid #2563eb;
      border-radius: var(--radius);
      padding: 12px;
      margin-bottom: 12px;
    }}
    .mainline-head {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      margin-bottom: 8px;
    }}
    .mainline-title {{
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 3px;
    }}
    .mainline-name {{
      font-size: 22px;
      font-weight: 850;
      line-height: 1.2;
    }}
    .mainline-status {{
      flex: none;
      border-radius: 6px;
      padding: 5px 8px;
      background: #eff6ff;
      color: #1d4ed8;
      font-size: 12px;
      font-weight: 700;
    }}
    .mainline-body {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      color: #344054;
      font-size: 13px;
      line-height: 1.65;
    }}
    .mainline-body b {{ display: block; color: var(--ink); margin-bottom: 2px; }}
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
    .detail-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }}
    .detail-item {{
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 8px;
      background: #fbfcff;
      min-height: 58px;
    }}
    .detail-item span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 4px;
    }}
    .detail-item b {{
      display: block;
      font-size: 16px;
      line-height: 1.25;
    }}
    .reasons {{
      margin: 0;
      padding-left: 18px;
      color: #344054;
      line-height: 1.7;
      font-size: 14px;
    }}
    .market-note {{
      border-top: 1px solid var(--line);
      margin-top: 10px;
      padding-top: 10px;
      color: #344054;
      font-size: 14px;
      line-height: 1.7;
    }}
    .confirm-list {{
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }}
    .confirm-row {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 8px 10px;
      background: #fbfcff;
      font-size: 13px;
    }}
    .confirm-row b {{ font-size: 14px; }}
    .confirm-row span {{ color: var(--muted); }}
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
      .mainline-head {{ display: block; }}
      .mainline-status {{ display: inline-flex; margin-top: 8px; }}
      .mainline-body {{ grid-template-columns: 1fr; }}
      .chart-head {{ display: block; }}
      .chart-actions {{ margin-top: 10px; justify-content: stretch; }}
      .range {{ width: 100%; justify-content: space-between; }}
      .range button {{ flex: 1; }}
      .zoom {{ width: 100%; }}
      .zoom button {{ flex: 1; }}
      .details {{ grid-template-columns: 1fr; }}
      .detail-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
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
      <div class="sub">主判指数 {html.escape(INDEX_NAME)} {html.escape(INDEX_CODE)} · 数据更新 {html.escape(updated)} · {html.escape(data_mode)}</div>
    </div>
    <div class="badge">{html.escape(state)}</div>
  </header>
{intraday_banner}
  <section class="summary">
    <div class="metric"><span>日期</span><b>{html.escape(date_label)}</b></div>
    <div class="metric"><span>分数</span><b>{latest["score"]}</b></div>
    <div class="metric"><span>阶段</span><b>{html.escape(latest["phase"])}</b></div>
    <div class="metric"><span>仓位建议</span><b>{html.escape(position_advice(latest))}</b></div>
  </section>
  {mainline_section}
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
      <div id="tip">拖动查看历史，电脑双击/手机双击轻触K线查看某日评分</div>
    </div>
    <canvas id="chart" aria-label="指数K线图"></canvas>
  </section>
  <section class="details">
    <div class="panel">
      <h2 id="detailTitle">今日择时评分明细</h2>
      <div class="detail-grid" id="detailGrid"></div>
      <ul class="reasons" id="detailReasons">
        {"".join(f"<li>{html.escape(item)}</li>" for item in str(latest["reasons"]).split("；"))}
      </ul>
      <div class="market-note" id="marketNote"></div>
    </div>
    <div class="panel">
      <h2>综合确认</h2>
      <div class="market-note" id="confirmSummary"></div>
      <div class="confirm-list" id="confirmList"></div>
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
const detailTitle = document.getElementById("detailTitle");
const detailGrid = document.getElementById("detailGrid");
const detailReasons = document.getElementById("detailReasons");
const marketNote = document.getElementById("marketNote");
const confirmSummary = document.getElementById("confirmSummary");
const confirmList = document.getElementById("confirmList");
const ctx = canvas.getContext("2d");
let end = rows.length - 1;
let visible = Math.min(rows.length, window.innerWidth < 760 ? 80 : 120);
let dragging = false;
let lastX = 0;
let activeRange = "120";
let selectedIndex = rows.length - 1;
let pointers = new Map();
let pinchStartDistance = 0;
let pinchStartVisible = visible;
let pointerDownPoint = null;
let lastTap = {{time: 0, x: 0, y: 0}};

function fmt(value, digits = 2) {{
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(digits);
}}

function fmtAmount(value) {{
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const n = Number(value);
  if (Math.abs(n) >= 100000000) return `${{(n / 100000000).toFixed(2)}}亿`;
  if (Math.abs(n) >= 10000) return `${{(n / 10000).toFixed(2)}}万`;
  return n.toFixed(0);
}}

function positionAdvice(row) {{
  const mainline = row.market && row.market.mainline ? row.market.mainline : {{}};
  const hasMainline = Boolean(mainline.has_mainline);
  const emotionActive = Boolean(mainline.emotion_active);
  if (row.s === "空") return "0%-20%，指数主跌/防守，不适合重仓";
  if (row.transitionLimited && hasMainline) return "20%-40%，空头后修复期，有主线也先等趋势确认";
  if (row.transitionLimited) return "10%-30%，空头后修复期，先观察持续性";
  if (row.phase === "主升" && hasMainline) return "70%-90%，指数主升且有主线，可重仓主线";
  if (row.phase === "主升") return "60%-80%，指数主升但主线确认不足";
  if ((row.s === "多" || row.s === "转") && hasMainline) return "40%-60%，指数震荡/试攻，有主线可参与主线";
  if ((row.s === "多" || row.s === "转") && emotionActive) return "10%-30%，无清晰主线但情绪活跃，只适合小仓";
  if (row.s === "多") return "30%-50%，指数偏强但缺少主线确认";
  if (row.s === "转") return "0%-20%，指数震荡且无主线，低仓位观察";
  return "0%-20%，不适合重仓";
}}

function escapeHtml(value) {{
  return String(value).replace(/[&<>"']/g, ch => ({{
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }}[ch]));
}}

function updateDetail(index) {{
  selectedIndex = Math.max(0, Math.min(rows.length - 1, index));
  const row = rows[selectedIndex];
  const market = row.market;
  const breadth = market ? market.breadth : null;
  const mainline = market ? market.mainline : null;
  const coreStocks = market && Array.isArray(market.core_stocks) ? market.core_stocks : [];
  const coreStockText = coreStocks.length
    ? coreStocks.slice(0, 2).map(item => `${{item.name}}(${{fmt(item.pct)}}%)`).join(" / ")
    : "-";
  const amountText = breadth
    ? (market && market.is_intraday
      ? `当前 ${{fmtAmount(breadth.amount)}} / 预计全天 ${{fmtAmount(breadth.amount_projected)}}`
      : fmtAmount(breadth.amount))
    : "-";
  detailTitle.textContent = `${{row.d}}${{row.intraday ? "（交易中）" : ""}} 择时评分明细`;
  const items = [
    ["状态", row.s],
    ["分数", `${{row.score}}分`],
    ["阶段", row.phase],
    ["趋势确认限制", row.transitionLimited ? "是，需继续确认" : "否"],
    ["仓位建议", positionAdvice(row)],
    ["开盘", fmt(row.o)],
    ["最高", fmt(row.h)],
    ["最低", fmt(row.l)],
    ["收盘", fmt(row.c)],
    ["涨跌幅", `${{fmt(row.pct)}}%`],
    ["成交量", fmt(row.v, 0)],
    ["MA5", fmt(row.ma5)],
    ["MA20", fmt(row.ma20)],
    ["上涨比例", breadth && !market.partial ? `${{fmt(breadth.up_ratio)}}%` : "-"],
    ["涨停/跌停", breadth && !market.partial ? `${{breadth.limit_up}} / ${{breadth.limit_down}}` : "-"],
    ["大跌股", breadth && !market.partial ? breadth.big_drop : "-"],
    ["全市场成交额", amountText],
    ["主线状态", mainline ? mainline.status : "-"],
    ["主线连续性", mainline ? `${{mainline.overlap_3d}}个重合方向` : "-"],
    ["主线领导力", mainline && mainline.leadership_score !== undefined ? `${{mainline.leadership_score}}分` : "-"],
    ["核心个股", coreStockText]
  ];
  detailGrid.innerHTML = items.map(([label, value]) =>
    `<div class="detail-item"><span>${{escapeHtml(label)}}</span><b>${{escapeHtml(value)}}</b></div>`
  ).join("");
  detailReasons.innerHTML = String(row.reasons || "样本不足，默认观察")
    .split("；")
    .map(item => `<li>${{escapeHtml(item)}}</li>`)
    .join("");
  if (market && mainline) {{
    const sectors = mainline.sector_names && mainline.sector_names.length
      ? mainline.sector_names.slice(0, 6).join(" / ")
      : "暂无";
    marketNote.innerHTML =
      `<b>市场结构：</b>${{escapeHtml(mainline.note)}}<br>` +
      `<b>强势方向：</b>${{escapeHtml(sectors)}}` +
      (coreStocks.length
        ? `<br><b>核心个股：</b>${{coreStocks.slice(0, 2).map(item => {{
            const reasons = Array.isArray(item.reasons) ? item.reasons.slice(0, 3).join(" / ") : "";
            return `${{item.name}}(${{item.code}}) ${{fmt(item.pct)}}%，成交额${{fmtAmount(item.amount)}}，净流入${{fmtAmount(item.net)}}${{reasons ? "，" + reasons : ""}}`;
          }}).map(escapeHtml).join("<br>")}}`
        : "") +
      (market.is_intraday && breadth && breadth.amount_note
        ? `<br><b>成交额口径：</b>${{escapeHtml(breadth.amount_note)}}`
        : "");
  }} else {{
    marketNote.innerHTML = "<b>市场结构：</b>暂无当日涨跌家数/主线数据";
  }}
  if (row.confirmation) {{
    const c = row.confirmation;
    confirmSummary.innerHTML =
      (c.sense_text ? `<b>体感确认：</b>${{escapeHtml(c.sense_text)}}<br>` : "") +
      `<b>风格确认：</b>${{escapeHtml(c.style || "暂无")}}<br>` +
      `<b>综合结论：</b>${{escapeHtml(c.conclusion || "暂无")}}`;
    confirmList.innerHTML = (c.indices || []).map(item => {{
      const value = item.state ? `${{item.state}} ${{item.score}}分` : "数据暂缺";
      return `<div class="confirm-row"><span>${{escapeHtml(item.name)}} · ${{escapeHtml(item.role)}}</span><b>${{escapeHtml(value)}}</b></div>`;
    }}).join("");
  }} else {{
    confirmSummary.innerHTML = "<b>综合确认：</b>仅最新交易日展示";
    confirmList.innerHTML = "";
  }}
  tip.textContent = `${{row.d}}  状态:${{row.s}}  分数:${{row.score}}  收盘:${{fmt(row.c)}}`;
}}

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
  const selectedVisibleIndex = selectedIndex >= start && selectedIndex <= end ? selectedIndex - start : -1;

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

  if (selectedVisibleIndex >= 0) {{
    const selectedX = pad.l + selectedVisibleIndex * step + step / 2;
    ctx.fillStyle = "rgba(37, 99, 235, .08)";
    ctx.fillRect(Math.max(pad.l, selectedX - step / 2), pad.t, Math.min(step, w - pad.r - pad.l), chartH);
    ctx.strokeStyle = "rgba(37, 99, 235, .55)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(selectedX, pad.t);
    ctx.lineTo(selectedX, pad.t + chartH);
    ctx.stroke();
    ctx.setLineDash([]);
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

    if (start + i === selectedIndex) {{
      ctx.strokeStyle = "#1d4ed8";
      ctx.lineWidth = 2;
      roundRect(ctx, x - candleW / 2 - 3, bodyTop - 3, candleW + 6, bodyH + 6, 4);
      ctx.stroke();
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
    const selected = rows[selectedIndex] || last;
    tip.textContent = `${{selected.d}}  状态:${{selected.s}}  分数:${{selected.score}}  收盘:${{fmt(selected.c)}}`;
  }}
}}

function indexFromClientX(clientX) {{
  const rect = canvas.getBoundingClientRect();
  const pad = {{l: 44, r: 12}};
  const start = Math.max(0, end - visible + 1);
  const sliceLength = end - start + 1;
  const chartWidth = rect.width - pad.l - pad.r;
  if (sliceLength <= 0 || chartWidth <= 0) return selectedIndex;
  const x = Math.max(pad.l, Math.min(rect.width - pad.r, clientX - rect.left));
  const offset = Math.floor((x - pad.l) / (chartWidth / sliceLength));
  return Math.max(start, Math.min(end, start + offset));
}}

function setRange(value) {{
  activeRange = value;
  if (value === "all") {{
    visible = rows.length;
  }} else {{
    visible = Math.min(rows.length, Number(value));
  }}
  end = rows.length - 1;
  selectedIndex = rows.length - 1;
  document.querySelectorAll(".range button").forEach(btn => {{
    btn.classList.toggle("active", btn.dataset.range === value);
  }});
  updateDetail(selectedIndex);
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

function selectByClientX(clientX) {{
  updateDetail(indexFromClientX(clientX));
  draw();
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
    pointerDownPoint = {{x: e.clientX, y: e.clientY}};
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
canvas.addEventListener("pointerup", e => {{
  const moved = pointerDownPoint
    ? Math.hypot(e.clientX - pointerDownPoint.x, e.clientY - pointerDownPoint.y)
    : 999;
  const now = Date.now();
  const closeToLastTap = Math.hypot(e.clientX - lastTap.x, e.clientY - lastTap.y) < 28;
  if (e.pointerType !== "mouse" && pointers.size === 1 && moved < 10 && now - lastTap.time < 320 && closeToLastTap) {{
    e.preventDefault();
    selectByClientX(e.clientX);
    lastTap = {{time: 0, x: 0, y: 0}};
  }} else if (e.pointerType !== "mouse" && moved < 10) {{
    lastTap = {{time: now, x: e.clientX, y: e.clientY}};
  }}
  pointers.delete(e.pointerId);
  dragging = false;
  pointerDownPoint = null;
}});
canvas.addEventListener("pointercancel", e => {{
  pointers.delete(e.pointerId);
  dragging = false;
  pointerDownPoint = null;
}});
canvas.addEventListener("wheel", e => {{
  e.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const anchor = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  zoom(e.deltaY < 0 ? 0.82 : 1.22, anchor);
}}, {{passive: false}});
canvas.addEventListener("dblclick", e => {{
  selectByClientX(e.clientX);
}});
document.getElementById("zoomIn").addEventListener("click", () => zoom(0.75, 0.5));
document.getElementById("zoomOut").addEventListener("click", () => zoom(1.35, 0.5));
document.querySelectorAll(".range button").forEach(btn => {{
  btn.addEventListener("click", () => setRange(btn.dataset.range));
}});

function chinaTimeParts() {{
  const parts = new Intl.DateTimeFormat("en-US", {{
    timeZone: "Asia/Shanghai",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }}).formatToParts(new Date());
  const map = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return {{
    weekday: map.weekday,
    minutes: Number(map.hour) * 60 + Number(map.minute)
  }};
}}

function isChinaTradingWindow() {{
  const t = chinaTimeParts();
  if (t.weekday === "Sat" || t.weekday === "Sun") return false;
  return (t.minutes >= 9 * 60 + 25 && t.minutes <= 11 * 60 + 35)
    || (t.minutes >= 13 * 60 && t.minutes <= 15 * 60 + 10);
}}

function refreshWithCacheBuster() {{
  const url = new URL(window.location.href);
  url.searchParams.set("live", String(Date.now()));
  window.location.replace(url.toString());
}}

function markStaleIntradayData() {{
  const latest = rows[rows.length - 1];
  if (!latest || !latest.intraday || !latest.quoteTime || !isChinaTradingWindow()) return;
  const quoteAt = new Date(String(latest.quoteTime).replace(" ", "T") + "+08:00").getTime();
  const lagMinutes = Math.floor((Date.now() - quoteAt) / 60000);
  if (!Number.isFinite(lagMinutes) || lagMinutes <= 15) return;
  const banner = document.querySelector(".live-banner");
  if (!banner) return;
  banner.classList.add("stale");
  banner.innerHTML = `<strong>数据滞后</strong><span>当前仍是交易时间，但页面行情已滞后约 ${{lagMinutes}} 分钟。页面会继续自动刷新；若长期不更新，说明 GitHub 自动部署或行情接口暂时不稳定。</span>`;
}}

if (isChinaTradingWindow()) {{
  setTimeout(refreshWithCacheBuster, 2 * 60 * 1000);
}}
window.addEventListener("resize", resize);
updateDetail(selectedIndex);
resize();
markStaleIntradayData();
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
    update_confirmation_indices(begin=args.begin, end=args.end)
    print(f"已更新 {len(rows)} 个交易日。最新日期: {rows[-1]['date']} {rows[-1]['state']} {rows[-1]['score']}分")
    return 0


def command_today(_: argparse.Namespace) -> int:
    rows = build_display_states(include_intraday=True, refresh_market=True)
    print_today(rows)
    return 0


def command_render(_: argparse.Namespace) -> int:
    rows = build_display_states(include_intraday=True, refresh_market=False)
    if not rows:
        raise RuntimeError("没有可用的中证全指数据，请先运行 update。")
    path = render_html(rows)
    print(f"已生成网页: {path}")
    print(f"默认首页: {INDEX_HTML}")
    print(f"手机同一 Wi-Fi 访问: 先运行 python3 -m http.server 8765 -d {OUTPUTS}")
    print(f"然后打开: http://{local_ip()}:8765/")
    return 0


def command_all(args: argparse.Namespace) -> int:
    update_data(begin=args.begin, end=args.end)
    update_confirmation_indices(begin=args.begin, end=args.end)
    display_rows = build_display_states(include_intraday=True, refresh_market=True)
    print_today(display_rows)
    path = render_html(display_rows)
    print("")
    print(f"网页: {path}")
    print(f"默认首页: {INDEX_HTML}")
    print(f"手机同一 Wi-Fi 访问: python3 -m http.server 8765 -d {OUTPUTS}")
    print(f"手机地址: http://{local_ip()}:8765/")
    return 0


def command_import_sense_csv(args: argparse.Namespace) -> int:
    rows = import_csv_rows(Path(args.path))
    save_sense_rows(rows)
    summary = latest_state_summary(rows)
    print(f"已导入 {len(rows)} 个交易日的{SENSE_INDEX_NAME}。")
    if summary:
        print(f"最新体感状态: {summary['date']} {summary['state']} {summary['score']}分")
    display_rows = build_display_states(include_intraday=True, refresh_market=False)
    path = render_html(display_rows)
    print(f"网页: {path}")
    return 0


def command_import_csv(args: argparse.Namespace) -> int:
    rows = import_csv_rows(Path(args.path))
    save_raw_rows(rows)
    states = calculate_states(rows)
    attach_market_structure(states)
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

    import_sense_cmd = sub.add_parser("import-sense-csv", help="导入平均股价/等权体感指数CSV，作为确认层")
    import_sense_cmd.add_argument("path", help="CSV路径，支持 date/open/high/low/close/volume 或 中文列名")
    import_sense_cmd.set_defaults(func=command_import_sense_csv)

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
