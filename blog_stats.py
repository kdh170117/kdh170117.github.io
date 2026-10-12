#!/usr/bin/env python3
"""블로그 방문 통계 조회 (GoatCounter API, 읽기 전용 토큰) → stdout JSON.

  python3 blog_stats.py              # 최근 7일
  python3 blog_stats.py --days 30

출력: {"period":..., "total":..., "daily":[{day,count}], "top_pages":[{path,title,count}], "referrers":[{name,count}]}
토큰은 blog/.env 의 GOATCOUNTER_TOKEN (값은 출력·로그 금지). 실패는 stderr + 종료코드 1.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BLOG = Path(__file__).resolve().parent
SITE = "https://dbro.goatcounter.com"
KST = timezone(timedelta(hours=9))


def token():
    for line in (BLOG / ".env").read_text().splitlines():
        if line.startswith("GOATCOUNTER_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("GOATCOUNTER_TOKEN 없음 (blog/.env)")


def get(path, tok):
    req = urllib.request.Request(SITE + path, headers={"Authorization": f"Bearer {tok}",
                                                       "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    end = datetime.now(KST)
    start = (end - timedelta(days=a.days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # GoatCounter 는 RFC3339 시각을 받는다(날짜만 주면 일부 엔드포인트가 404).
    rng = f"start={start.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}&end={end.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
    try:
        tok = token()
        total = get(f"/api/v0/stats/total?{rng}", tok)
        hits = get(f"/api/v0/stats/hits?{rng}&limit=10", tok)
        refs = get(f"/api/v0/stats/toprefs?{rng}&limit=10", tok)
    except (urllib.error.URLError, RuntimeError, ValueError) as exc:
        print(f"[stats] 조회 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "period": f"{start:%Y-%m-%d}~{end:%Y-%m-%d}",
        "total": total.get("total", 0),
        "daily": [{"day": d["day"], "count": d.get("daily", sum(d.get("hourly", [])))} for d in total.get("stats", [])],
        "top_pages": [{"path": h.get("path"), "title": h.get("title"), "count": h.get("count")}
                      for h in hits.get("hits", [])],
        "referrers": [{"name": s.get("name") or "(직접 방문)", "count": s.get("count")}
                      for s in refs.get("stats", [])],
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
