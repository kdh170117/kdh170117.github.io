#!/usr/bin/env python3
"""오늘의 트렌드 TOP 5 카드 → 블로그 글 생성. (2026-10-12 밈 카드를 대체)

분야를 미리 정하지 않고 '그날 가장 뜨는 것'을 고른다 — 화장품 이슈가 큰 날은 뷰티가,
컴백 시즌엔 아이돌이 자연스럽게 올라온다.

1. 수집  분야별 시드 검색어로 네이버 뉴스·블로그 최근 2일 글을 모은다.
2. 추출  Claude(Sonnet)가 분야별 화제 키워드를 뽑는다 — 인용한 글에 키워드가 그대로 있어야 통과.
3. 검증  키워드별 (a) 블로그 최근 14일 언급 수 (b) 네이버 검색어 트렌드 급증비
         = 최근 3일 평균 ÷ 그 전 14일 평균(트렌드는 어제까지만 있다). 둘 다 기준을 넘어야 통과.
         (네이버는 '오늘의 인기 검색어 순위' API 가 없다 — 실시간 검색어는 2021년 폐지.)
4. 선정  분야 구분 없이 급증비 순 상위 5개. 한 분야 최대 2개, 최근 7일에 실은 키워드는
         급증비가 그때의 2배 이상일 때만 다시 싣는다.
5. 작성  Claude 가 '왜 뜨는지' 한 줄을 **검증 단계에서 모은 글 범위 안에서만** 새로 쓴다.
6. 렌더  HTML → Chrome headless → PNG, 글 파일 생성(커밋 안 함).

공개 글 안전장치: 아이돌·연예는 컴백·발매·공연 같은 공식 활동만. 열애·논란·루머·사건사고·정치 제외
(프롬프트 지시 + 결과 기계 검사 2중).

사용:
  python3 publish_trend.py --date 2026-10-12            # 글 파일만 생성
  python3 publish_trend.py --date 2026-10-12 --dry-run  # 후보·검증 결과만 출력(파일 안 만듦)

종료코드: 0 생성 · 1 실패 · 2 통과 키워드 부족(발행 생략) · 3 이미 있음
"""
import argparse
import html
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from PIL import Image

BLOG = Path(__file__).resolve().parent
sys.path.insert(0, str(Path.home() / "ai" / "projects" / "naver_trend" / "skills"))
from skill_search_docs import search_docs        # noqa: E402
from skill_search_trend import search_trend      # noqa: E402

import _llm_claude                                # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MODEL = "claude-sonnet-5"

# 분야: (칩 색, 시드 검색어). 시드는 '그 분야에서 요즘 뜨는 것'을 다루는 글이 걸리게 고른다.
CATEGORIES = {
    "뷰티": ("#e85d8a", ["화장품 신제품", "뷰티 트렌드", "올리브영 인기"]),
    "아이돌·연예": ("#7b5cff", ["컴백", "신곡 발매", "콘서트 티켓팅"]),
    "푸드": ("#f08c2e", ["편의점 신상", "디저트 유행", "맛집 웨이팅"]),
    "패션": ("#2f9e8f", ["패션 트렌드", "코디 유행", "품절 운동화"]),
    "테크·IT": ("#2b7bd6", ["신제품 출시", "AI 앱 인기", "스마트폰 출시"]),
    "소비·생활": ("#8a6d3b", ["품절 대란", "오픈런", "팝업스토어"]),
    "밈·유행어": ("#d1495b", ["유행어", "밈 뜻", "챌린지 유행"]),
}
TOP_N, MIN_N, PER_CATEGORY = 5, 3, 2
COLLECT_DAYS = 2
MIN_BLOG_14D = 5          # 블로그 최근 14일 언급이 이보다 적으면 '화제' 근거 부족
MIN_SURGE = 1.3           # 최근 3일 검색이 이전 2주 평균보다 30% 이상 높아야 '뜨는 중'
REPEAT_DAYS = 7
REPEAT_SURGE_FACTOR = 2.0

# 공개 글에 싣지 않는 것 — 프롬프트로 막고, 결과도 기계 검사한다.
BANNED = re.compile(r"한남|한녀|김치녀|틀딱|급식충|맘충|짱깨|쪽바리|흑형|장애|성기|섹스|자살|일베|메갈|좌좀|수꼴|대깨")
SENSITIVE = re.compile(r"열애|결별|이혼|논란|의혹|폭로|루머|사망|숨진|사고|참사|구속|체포|고소|고발|학폭|음주운전|"
                       r"마약|탈세|선거|대통령|국회|여당|야당|탄핵|전쟁|피해자")

EXTRACT_SYSTEM = """너는 한국 트렌드 편집자다. 입력은 분야별 최근 네이버 뉴스·블로그 글 목록(번호·분야·제목·요약)이다.
각 분야에서 '지금 사람들이 관심 갖고 찾아보는 것'인 키워드를 분야당 최대 5개 뽑아라.
- 키워드는 제품·브랜드 신상, 아이돌 그룹/앨범/곡명, 음식·메뉴, 패션 아이템, 앱·기기, 팝업·행사, 유행어·밈 등 구체적인 것.
- 키워드는 글 제목/요약에 **글자 그대로** 나온 형태(띄어쓰기 포함), 2~20자.
- 제외: 열애·결별·논란·의혹·루머·사건사고·범죄·소송, 정치·선거, 특정 일반인, 혐오·비하·성적 표현,
  분야 이름 자체('화장품','컴백' 같은 일반명사), 광고성 할인 문구.
- 아이돌·연예는 컴백·발매·공연·수상 같은 공식 활동만.
- 근거 글 번호(evidence)를 1개 이상 단다.
출력은 JSON 하나만: {"candidates":[{"category":"분야명","term":"","evidence":[번호]}]}"""

WRITE_SYSTEM = """너는 트렌드 카드 작가다. 입력은 키워드별로 모은 최근 글 요약이다.
각 키워드에 대해 why(왜 요즘 뜨는지, 40자 이내)를 **새 문장으로** 써라(입력 문장 베끼기 금지).
category 는 키워드의 실제 성격으로 다시 판정한다 — 반드시 아래 목록 중 하나, 맞는 게 없으면 "제외".
분야 목록: 뷰티, 아이돌·연예, 푸드, 패션, 테크·IT, 소비·생활, 밈·유행어 (의약품·의료는 "제외")
- 입력 글로 확인되는 사실만. 날짜·수치·인물·출처를 지어내지 않는다.
- 논란·루머·사생활·사건사고는 언급하지 않는다. 가볍고 정보 위주로.
- 명사형 또는 '~해서 화제' 같은 짧은 종결.
출력은 JSON 하나만: {"items":[{"term":"","category":"","why":""}]}"""


def log(msg):
    print(f"[trend] {msg}", file=sys.stderr)


def strip(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def item_date(it):
    if it.get("postdate"):                                     # 블로그 20261012
        return datetime.strptime(it["postdate"], "%Y%m%d").date()
    return datetime.strptime(it["pubDate"][:16], "%a, %d %b %Y").date()   # 뉴스 RFC822


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("JSON 없음")
    return json.loads(m.group(0))


def collect(today):
    seen, out = set(), []
    for cat, (_, queries) in CATEGORIES.items():
        for q in queries:
            for kind in ("news", "blog"):
                for it in search_docs(kind, q, display=40, sort="date").get("items", []):
                    link = it.get("originallink") or it.get("link")
                    if link in seen or (today - item_date(it)).days > COLLECT_DAYS:
                        continue
                    seen.add(link)
                    out.append({"cat": cat, "title": strip(it["title"]),
                                "desc": strip(it.get("description"))[:140]})
    return out


def extract(docs):
    listing = "\n".join(f"{i}. [{d['cat']}] {d['title']} — {d['desc']}" for i, d in enumerate(docs))
    cands = parse_json(_llm_claude.call(listing, system=EXTRACT_SYSTEM, model=MODEL, timeout=300))["candidates"]
    ok, terms = [], set()
    for c in cands:
        term, cat = c.get("term", "").strip(), c.get("category", "").strip()
        ev = [i for i in c.get("evidence", []) if isinstance(i, int) and 0 <= i < len(docs)]
        if cat not in CATEGORIES or not (2 <= len(term) <= 20) or term in terms:
            continue
        if BANNED.search(term) or SENSITIVE.search(term):
            continue
        # 기계 검사: 인용한 글에 키워드가 그대로 있어야 한다(지어낸 키워드 차단)
        if not any(term in docs[i]["title"] + docs[i]["desc"] for i in ev):
            log(f"근거 불일치 제외: {term}")
            continue
        terms.add(term)
        ok.append({"term": term, "cat": cat})
    return ok


def recent_scores(today):
    """최근 REPEAT_DAYS 일 안에 실은 키워드 → 그때 급증비."""
    scores = {}
    posts = list((BLOG / "_posts").glob("*-trend.md")) + list((BLOG / "_posts").glob("*-meme.md"))  # 구 밈 글 포함
    for p in posts:
        if (today - date.fromisoformat(p.name[:10])).days <= REPEAT_DAYS:
            m = re.search(r"^scores: (\{.*\})$", p.read_text(), re.M)
            if m:
                for k, v in json.loads(m.group(1)).items():
                    scores[k] = max(v, scores.get(k, 0))
    return scores


def verify(cands, today):
    end = today - timedelta(days=1)                       # 트렌드는 어제(D-1)까지만 있다
    start = end - timedelta(days=16)
    days = [start + timedelta(days=i) for i in range(17)]
    for c in cands:
        items = search_docs("blog", f'"{c["term"]}"', display=50, sort="date").get("items", [])
        recent = [it for it in items if (today - item_date(it)).days <= 14]
        c["blog14"] = len(recent)
        c["snips"] = [strip(it["title"]) + " — " + strip(it.get("description"))[:140] for it in recent[:8]]
    for i in range(0, len(cands), 5):                     # 트렌드 1요청 최대 5그룹
        chunk = cands[i:i + 5]
        res = search_trend([{"groupName": f"g{j}", "keywords": [c["term"]]} for j, c in enumerate(chunk)],
                           start, end)["results"]
        for c, r in zip(chunk, res):
            vals = {d["period"]: d["ratio"] for d in r["data"]}
            series = [vals.get(d.isoformat(), 0.0) for d in days]   # 검색 0인 날은 응답에서 빠진다
            base, last = sum(series[:14]) / 14, sum(series[14:]) / 3
            c["surge"] = round(last / base, 2) if base > 0 else (99.0 if last > 0 else 0.0)
    return cands


def select(cands, recent):
    picked, per_cat = [], {}
    for c in sorted(cands, key=lambda c: (-c["surge"], -c["blog14"])):
        if c["blog14"] < MIN_BLOG_14D or c["surge"] < MIN_SURGE:
            continue
        if c["term"] in recent and c["surge"] < recent[c["term"]] * REPEAT_SURGE_FACTOR:
            continue
        if per_cat.get(c["cat"], 0) >= PER_CATEGORY:
            continue
        picked.append(c)
        per_cat[c["cat"]] = per_cat.get(c["cat"], 0) + 1
    return picked


def write(picks):
    body = "\n\n".join(f"[{c['term']}] ({c['cat']})\n" + "\n".join(c["snips"]) for c in picks)
    items = parse_json(_llm_claude.call(body, system=WRITE_SYSTEM, model=MODEL, timeout=240))["items"]
    by_term = {i.get("term"): i for i in items}
    out = []
    for p in picks:
        it = by_term.get(p["term"], {})
        why, cat = it.get("why", "").strip(), it.get("category", "").strip()
        if cat not in CATEGORIES:
            log(f"분야 재판정 제외: {p['term']} ({cat!r})")
            continue
        if not why or len(why) > 50 or BANNED.search(why) or SENSITIVE.search(why):
            log(f"작성 검사 탈락: {p['term']} ({why[:30]!r})")
            continue
        out.append({**p, "cat": cat, "why": why})
    return out


def surge_label(s):
    return "새로 등장" if s >= 99 else f"검색 ×{s:.1f}"   # 99 = 이전 2주 검색 0


CARD_CSS = """
body{margin:0;width:1080px;background:#f6f4ef;font-family:"Apple SD Gothic Neo",sans-serif;color:#1f1d1a}
.wrap{padding:56px 56px 40px}
.top{display:flex;justify-content:space-between;align-items:baseline;color:#1f1d1a;font-weight:800;font-size:26px}
.top span{color:#8c8578;font-weight:600;font-size:22px}
h1{font-size:60px;line-height:1.15;margin:22px 0 8px;font-weight:900}
h1 em{font-style:normal;color:#d1495b}
.sub{font-size:24px;color:#8c8578;margin-bottom:30px}
.row{display:flex;gap:24px;align-items:flex-start;background:#fff;border-radius:24px;padding:28px 32px;margin-bottom:18px}
.rank{font-size:52px;font-weight:900;color:#d1495b;min-width:44px;line-height:1}
.body{flex:1}
.meta{display:flex;align-items:center;gap:12px;margin-bottom:8px}
.chip{color:#fff;font-size:20px;font-weight:800;border-radius:999px;padding:4px 14px}
.surge{margin-left:auto;font-size:21px;font-weight:800;color:#d1495b}
.term{font-size:40px;font-weight:900;margin-bottom:6px}
.why{font-size:26px;color:#4a453e}
.foot{font-size:19px;color:#a39c8f;margin-top:16px;line-height:1.5}
"""


def render(items, today, png_path):
    rows = "".join(
        f'<div class="row"><div class="rank">{i}</div><div class="body"><div class="meta">'
        f'<span class="chip" style="background:{CATEGORIES[c["cat"]][0]}">{html.escape(c["cat"])}</span>'
        f'<span class="surge">{surge_label(c["surge"])}</span></div>'
        f'<div class="term">{html.escape(c["term"])}</div><div class="why">{html.escape(c["why"])}</div></div></div>'
        for i, c in enumerate(items, 1))
    basis = today - timedelta(days=1)
    doc = (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>{CARD_CSS}</style></head><body><div class="wrap">'
           f'<div class="top">Dbro / 오늘의 트렌드<span>{today:%Y-%m-%d}</span></div>'
           f'<h1>지금 <em>뜨는 것</em> TOP {len(items)}</h1>'
           f'<div class="sub">분야 상관없이 네이버 검색이 가장 크게 늘어난 순 · {basis:%m/%d} 검색 기준</div>'
           f'{rows}<div class="foot">검색 ×N = 최근 3일 네이버 검색량 ÷ 직전 2주 평균 (네이버 검색어 트렌드 상대값, 어제까지 반영)<br>'
           f'설명은 최근 뉴스·블로그 글을 바탕으로 정리했습니다.</div></div></body></html>')
    src = png_path.with_suffix(".html")
    raw = png_path.with_name(png_path.stem + "_raw.png")
    src.write_text(doc, encoding="utf-8")
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                    "--window-size=1080,4000", f"--screenshot={raw}", src.as_uri()], capture_output=True, timeout=90)
    im = Image.open(raw).convert("RGB")
    w, h = im.size
    px, bg = im.load(), im.getpixel((5, h - 5))
    y = h - 1
    while y > 0 and all(sum(abs(p - q) for p, q in zip(px[x, y], bg)) < 12 for x in range(0, w, 4)):
        y -= 1
    im.crop((0, 0, w, min(h, y + 40))).save(png_path)
    raw.unlink(missing_ok=True)
    src.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--kind", default="trend", choices=["trend"])   # blog_approval 호환
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    today = date.fromisoformat(a.date)

    post = BLOG / "_posts" / f"{a.date}-trend.md"
    if post.exists():
        log(f"이미 있음: {post.name}")
        return 3
    try:
        docs = collect(today)
        log(f"수집 {len(docs)}건 ({', '.join(f'{k} {sum(d["cat"] == k for d in docs)}' for k in CATEGORIES)})")
        cands = verify(extract(docs), today)
    except Exception as exc:
        log(f"수집·검증 실패: {type(exc).__name__}: {exc}")
        return 1
    picks = select(cands, recent_scores(today))
    for c in sorted(cands, key=lambda c: -c["surge"]):
        log(f"  {'선정' if c in picks else '    '} [{c['cat']}] {c['term']}: ×{c['surge']} · 블로그14일 {c['blog14']}")
    if a.dry_run:
        return 0
    if len(picks) < MIN_N:
        log(f"선정 {len(picks)}개 < {MIN_N} — 발행 생략")
        return 2
    try:
        items = write(picks[:TOP_N + 2])[:TOP_N]
    except Exception as exc:
        log(f"작성 실패: {type(exc).__name__}: {exc}")
        return 1
    if len(items) < MIN_N:
        log(f"작성 통과 {len(items)}개 < {MIN_N} — 발행 생략")
        return 2

    img = BLOG / "assets" / "cards" / f"{a.date}_trend.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    render(items, today, img)
    names = " · ".join(c["term"] for c in items[:3])
    lines = "\n".join(f"{i}. **{c['term']}** ({c['cat']}, {surge_label(c['surge'])}) — {c['why']}"
                      for i, c in enumerate(items, 1))
    scores = json.dumps({c["term"]: c["surge"] for c in items}, ensure_ascii=False)
    post.parent.mkdir(exist_ok=True)
    post.write_text(
        "---\n"
        "layout: post\n"
        f'title: "[트렌드] 오늘 뜨는 것: {names}"\n'
        f"date: {a.date} 12:30:00 +0900\n"
        "categories: [trend]\n"
        f"scores: {scores}\n"
        "---\n"
        f"{lines}\n\n"
        f"![{a.date} 오늘의 트렌드 카드]({{{{ '/assets/cards/{img.name}' | relative_url }}}})\n\n"
        "※ 네이버 검색어 트렌드(상대값, 어제까지)와 최근 뉴스·블로그 글을 바탕으로 정리했습니다.\n"
    )
    print(f"[blog] 생성: {post.relative_to(BLOG)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
