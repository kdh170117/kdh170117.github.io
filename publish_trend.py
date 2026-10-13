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
import tempfile
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
TOP_N, MIN_N, PER_CATEGORY = 3, 3, 1   # 2026-10-12 도형님: TOP 3 로 간략하게
COLLECT_DAYS = 2
MIN_BLOG_14D = 5          # 블로그 최근 14일 언급이 이보다 적으면 '화제' 근거 부족
MIN_SURGE = 1.3           # 최근 3일 검색이 이전 2주 평균보다 30% 이상 높아야 '뜨는 중'
# 2026-10-13 도형님: "언급된 건 제외하고 다음 순으로" → 한 번 실은 키워드는 예외 없이 30일 제외.
# (구 규칙: 7일 내라도 급증비가 그때의 2배면 재등장 허용 → 쿠가세·I SPY 가 이틀 연속 실렸다)
REPEAT_DAYS = 30
HOT_N = 2                 # '여전히 화제' — 이미 소개했지만 오늘도 기준을 넘은 키워드(한 줄 근황만)

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

WRITE_SYSTEM = """너는 트렌드 해설 카드 작가다. 입력은 키워드별로 모은 최근 뉴스·블로그 요약이다.
처음 듣는 사람도 '그게 뭔데?'가 풀리게, 각 키워드를 아래 형식으로 **새 문장으로** 정리하라(입력 문장 베끼기 금지).
- category: 키워드의 실제 성격으로 다시 판정. 반드시 목록 중 하나, 맞는 게 없으면 "제외".
  목록: 뷰티, 아이돌·연예, 푸드, 패션, 테크·IT, 소비·생활, 밈·유행어 (의약품·의료는 "제외")
- emoji: 키워드를 나타내는 이모지 1개
- headline: 한 줄 정의(무엇인지), 30자 이내. 예) "쿠팡이 1년에 2번 여는 가전·디지털 할인 행사"
- facts: 핵심 정보 2~4개. label 은 무엇/언제/어디/얼마/누가/특징 중 하나, text 는 40자 이내.
  입력에 있는 사실만. 날짜·가격·수량·인원 등 숫자는 입력에 그대로 있는 것만 쓴다. 모르면 그 항목을 빼라.
- why: 왜 지금 뜨는지, 40자 이내.
- 키워드 머리에 (여전히 화제) 표시가 있으면 이미 소개했던 것이다: headline 과 why(=지금 근황, 새로 바뀐 점 위주)만 쓰고
  facts 는 빈 배열, scene 은 빈 문자열로 둔다.
- scene: 대표 이미지로 쓸 '일반적인 장면' 묘사, 한국어 60자 이내. 실존 인물·아이돌·브랜드·로고·실제 상품명·글자 금지.
  예) 할인 행사 → "밝은 매장에 냉장고·TV·노트북이 진열된 장면", 아이돌 신곡 → "보라색 조명이 비치는 빈 콘서트 무대",
      디저트 → "나무 쟁반 위 알록달록한 찹쌀떡 클로즈업", 게임 → "판타지 마을 풍경 일러스트"
규칙: 논란·루머·사생활·사건사고·부고는 언급하지 않는다. 광고 문구처럼 쓰지 않는다. 담백하고 정보 위주.
- 참여 업체 한 곳의 정보(그 브랜드의 할인 기간·가격)를 행사·제품 전체의 정보처럼 쓰지 않는다.
  여러 글에 공통으로 나오는 사실을 우선하고, 글마다 다르면 그 항목은 뺀다.
출력은 JSON 하나만:
{"items":[{"term":"","category":"","emoji":"","headline":"","facts":[{"label":"","text":""}],"why":"","scene":""}]}"""


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


def norm(term):
    return re.sub(r"\s+", "", term).lower()


def recent_terms(today, skip_today=False):
    """최근 REPEAT_DAYS 일 안에 실은 키워드(정규화). skip_today: 오늘 글을 새로 뽑아 교체할 때 오늘 글은 빼고 본다."""
    terms = set()
    posts = list((BLOG / "_posts").glob("*-trend.md")) + list((BLOG / "_posts").glob("*-meme.md"))  # 구 밈 글 포함
    for p in posts:
        d = date.fromisoformat(p.name[:10])
        if (skip_today and d == today) or not 0 <= (today - d).days <= REPEAT_DAYS:
            continue
        m = re.search(r"^scores: (\{.*\})$", p.read_text(), re.M)
        if m:
            terms |= {norm(k) for k in json.loads(m.group(1))}
    return terms


def already_used(term, used):
    """띄어쓰기·대소문자 무시, 한쪽이 다른 쪽을 포함해도 같은 키워드로 본다('쿠가세' ↔ '쿠팡 쿠가세')."""
    t = norm(term)
    return any(t == u or (len(min(t, u, key=len)) >= 2 and (t in u or u in t)) for u in used)


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


def select(cands, used):
    """반환: (신규 TOP 후보, 여전히 화제). TOP 은 처음 등장한 키워드만, 이미 소개한 건 '여전히 화제'로."""
    picked, hot, per_cat = [], [], {}
    for c in sorted(cands, key=lambda c: (-c["surge"], -c["blog14"])):
        if c["blog14"] < MIN_BLOG_14D or c["surge"] < MIN_SURGE:
            continue
        if already_used(c["term"], used):
            if len(hot) < HOT_N and not already_used(c["term"], {norm(x["term"]) for x in hot}):
                hot.append(c)
            continue
        if already_used(c["term"], {norm(x["term"]) for x in picked}):
            continue
        if per_cat.get(c["cat"], 0) >= PER_CATEGORY:
            continue
        picked.append(c)
        per_cat[c["cat"]] = per_cat.get(c["cat"], 0) + 1
    return picked, hot


def gather_sources(c):
    """선정된 키워드의 설명 재료: 뉴스(정확도순) + 블로그(최신순). 키워드가 실제로 들어간 글만."""
    texts = []
    for kind, sort, n in (("news", "sim", 10), ("blog", "date", 6)):
        for it in search_docs(kind, f'"{c["term"]}"', display=20, sort=sort).get("items", []):
            t = strip(it["title"]) + " — " + strip(it.get("description"))[:220]
            if c["term"] in t and not SENSITIVE.search(t) and t not in texts:
                texts.append(t)
            if sum(1 for x in texts if x) >= n + (10 if kind == "blog" else 0):
                break
    c["sources"] = texts[:16]
    return c


NUM = re.compile(r"\d[\d,.]*")


def numbers_ok(text, source):
    """문장 속 숫자가 모두 원문에 있어야 한다(날짜·가격·수량 지어내기 차단)."""
    src = source.replace(",", "")
    return all(n.replace(",", "").rstrip(".") in src for n in NUM.findall(text))


def write(picks, hot=()):
    body = "\n\n".join(f"[{c['term']}] (수집 분야: {c['cat']})\n" + "\n".join(c["sources"]) for c in picks)
    if hot:
        body += "\n\n" + "\n\n".join(f"(여전히 화제) [{c['term']}] (수집 분야: {c['cat']})\n" + "\n".join(c["sources"])
                                     for c in hot)
    items = parse_json(_llm_claude.call(body, system=WRITE_SYSTEM, model=MODEL, timeout=300))["items"]
    by_term = {i.get("term"): i for i in items}
    hot_out = []
    for h in hot:
        it = by_term.get(h["term"], {})
        cat, head, why = it.get("category", "").strip(), it.get("headline", "").strip(), it.get("why", "").strip()
        src = " ".join(h["sources"])
        if cat in CATEGORIES and head and why and len(head) <= 40 and len(why) <= 50 and not any(
                BANNED.search(x) or SENSITIVE.search(x) or not numbers_ok(x, src) for x in (head, why)):
            hot_out.append({**h, "cat": cat, "emoji": (it.get("emoji") or "🔥").strip()[:2], "headline": head, "why": why})
        else:
            log(f"여전히 화제 작성 탈락: {h['term']}")
    out = []
    for p in picks:
        it = by_term.get(p["term"], {})
        cat, head, why = it.get("category", "").strip(), it.get("headline", "").strip(), it.get("why", "").strip()
        src = " ".join(p["sources"])
        if cat not in CATEGORIES:
            log(f"분야 재판정 제외: {p['term']} ({cat!r})")
            continue
        facts = []
        for f in it.get("facts", [])[:4]:
            label, text = f.get("label", "").strip(), f.get("text", "").strip()
            if label and text and len(text) <= 50 and numbers_ok(text, src) \
                    and not BANNED.search(text) and not SENSITIVE.search(text):
                facts.append({"label": label, "text": text})
            else:
                log(f"사실 항목 제외: {p['term']} / {label}: {text[:30]!r}")
        checks = [head, why]
        if not head or not why or len(head) > 40 or len(why) > 50 or not facts \
                or any(BANNED.search(x) or SENSITIVE.search(x) or not numbers_ok(x, src) for x in checks):
            log(f"작성 검사 탈락: {p['term']} ({head[:20]!r} / {why[:20]!r} / facts {len(facts)})")
            continue
        scene = (it.get("scene") or "").strip()
        if len(scene) > 80 or BANNED.search(scene) or SENSITIVE.search(scene) or p["term"] in scene:
            scene = ""                                   # 키워드(고유명사)가 들어간 장면은 쓰지 않는다
        out.append({**p, "cat": cat, "emoji": (it.get("emoji") or "✨").strip()[:2],
                    "headline": head, "facts": facts, "why": why, "scene": scene})
    return out, hot_out


CODEX = str(Path.home() / ".local" / "bin" / "codex")
# 금지 조건은 LLM 이 쓴 장면 설명에 맡기지 않고 코드가 항상 붙인다(공개 글 — 초상권·상표·오해 방지).
IMAGE_RULES = ("사진 같은 스타일, 가로 3:2, 따뜻하고 깔끔한 분위기. 사람 얼굴, 실존 인물, 글자, 간판 문구, "
               "브랜드 로고, 특정 회사 제품 디자인은 절대 넣지 마.")


def generate_photo(scene, out_jpg):
    """Codex(ChatGPT 로그인) 이미지 생성 → 1080px JPEG. 실패하면 False(카드는 사진 없이 만든다)."""
    if not scene:
        return False
    with tempfile.TemporaryDirectory(prefix="trend-img-") as d:
        prompt = (f"이미지 생성 도구로 이미지를 정확히 1장 만들어 현재 폴더에 out.png 로 저장해. 다른 작업은 하지 마. "
                  f"장면: {scene}. {IMAGE_RULES} 저장 후 파일 경로만 출력해.")
        try:
            subprocess.run([CODEX, "exec", "--skip-git-repo-check", "--ephemeral", "-C", d,
                            "--sandbox", "workspace-write", prompt],
                           capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            log(f"이미지 생성 시간 초과: {scene[:30]}")
            return False
        src = Path(d) / "out.png"
        if not src.is_file():
            log(f"이미지 생성 실패: {scene[:30]}")
            return False
        im = Image.open(src).convert("RGB")
        im = im.resize((1080, round(im.height * 1080 / im.width)))
        im.save(out_jpg, "JPEG", quality=85)
    return True


def surge_label(s):
    return "새로 등장" if s >= 99 else f"검색 ×{s:.1f}"   # 99 = 이전 2주 검색 0


BASE_CSS = """
body{margin:0;width:1080px;background:#f6f4ef;font-family:"Apple SD Gothic Neo",sans-serif;color:#1f1d1a}
.wrap{padding:56px 56px 44px}
.top{display:flex;justify-content:space-between;align-items:baseline;font-weight:800;font-size:26px}
.top span{color:#8c8578;font-weight:600;font-size:22px}
.chip{display:inline-block;color:#fff;font-size:22px;font-weight:800;border-radius:999px;padding:5px 16px}
.surge{font-size:22px;font-weight:800;color:#d1495b}
.foot{font-size:19px;color:#a39c8f;margin-top:22px;line-height:1.5}
"""

COVER_CSS = BASE_CSS + """
h1{font-size:60px;line-height:1.15;margin:22px 0 8px;font-weight:900}
h1 em{font-style:normal;color:#d1495b}
.sub{font-size:24px;color:#8c8578;margin-bottom:30px}
.row{display:flex;gap:22px;align-items:center;background:#fff;border-radius:24px;padding:24px 30px;margin-bottom:16px}
.rank{font-size:48px;font-weight:900;color:#d1495b;min-width:40px}
.emo{font-size:56px}
.body{flex:1}
.meta{display:flex;gap:12px;align-items:center;margin-bottom:6px}
.meta .surge{margin-left:auto}
.term{font-size:38px;font-weight:900}
.head{font-size:25px;color:#4a453e;margin-top:4px}
.hot{background:#fff;border:2px dashed #e8c9a8;border-radius:24px;padding:20px 30px;margin-top:8px}
.hot h3{margin:0 0 10px;font-size:26px;color:#b4541a}
.hot .h{display:flex;gap:14px;align-items:baseline;font-size:24px;padding:6px 0;color:#4a453e}
.hot .h b{color:#1f1d1a;font-size:26px;white-space:nowrap}
"""

DETAIL_CSS = BASE_CSS + """
.photo{position:relative;margin:26px 0 0;border-radius:32px;overflow:hidden}
.photo img{display:block;width:100%}
.photo span{position:absolute;right:16px;bottom:14px;background:rgba(0,0,0,.45);color:#fff;font-size:18px;
  border-radius:999px;padding:4px 12px}
.hero{border-radius:32px;padding:44px 48px;color:#fff;margin:26px 0 26px;position:relative;overflow:hidden}
.hero .emo{position:absolute;right:40px;top:30px;font-size:150px;opacity:.95}
.hero .rank{font-size:30px;font-weight:900;opacity:.85}
.hero .term{font-size:72px;font-weight:900;line-height:1.1;margin:10px 0 14px;max-width:760px}
.hero .chip{background:rgba(255,255,255,.25)}
.hero .surge{color:#fff;margin-left:12px}
.def{font-size:36px;font-weight:800;line-height:1.35;margin:0 0 26px}
.facts{background:#fff;border-radius:26px;padding:12px 34px}
.fact{display:flex;gap:22px;padding:20px 0;border-bottom:1px solid #eee;font-size:29px;line-height:1.4}
.fact:last-child{border-bottom:0}
.fact b{min-width:76px;color:#8c8578;font-weight:800}
.why{margin-top:22px;background:#fff3d6;border-radius:22px;padding:24px 30px;font-size:28px;line-height:1.4}
.why b{color:#b4541a}
"""


def shoot(doc, png_path):
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
    im.crop((0, 0, w, min(h, y + 44))).save(png_path)
    raw.unlink(missing_ok=True)
    src.unlink(missing_ok=True)


def page(css, inner):
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>{css}</style></head>'
            f'<body><div class="wrap">{inner}</div></body></html>')


def render_cover(items, today, png_path, hot=()):
    esc = html.escape
    hot_html = ("" if not hot else '<div class="hot"><h3>🔥 여전히 화제</h3>' + "".join(
        f'<div class="h"><b>{esc(h["emoji"])} {esc(h["term"])}</b><span>{esc(h["why"])}</span></div>' for h in hot) + "</div>")
    rows = "".join(
        f'<div class="row"><div class="rank">{i}</div><div class="emo">{esc(c["emoji"])}</div><div class="body">'
        f'<div class="meta"><span class="chip" style="background:{CATEGORIES[c["cat"]][0]}">{esc(c["cat"])}</span>'
        f'<span class="surge">{surge_label(c["surge"])}</span></div>'
        f'<div class="term">{esc(c["term"])}</div><div class="head">{esc(c["headline"])}</div></div></div>'
        for i, c in enumerate(items, 1))
    basis = today - timedelta(days=1)
    shoot(page(COVER_CSS,
               f'<div class="top">Dbro / 오늘의 트렌드<span>{today:%Y-%m-%d}</span></div>'
               f'<h1>지금 <em>뜨는 것</em> TOP {len(items)}</h1>'
               f'<div class="sub">새로 뜨는 것만 · 네이버 검색이 가장 크게 늘어난 순 · {basis:%m/%d} 검색 기준</div>{rows}{hot_html}'
               f'<div class="foot">검색 ×N = 최근 3일 네이버 검색량 ÷ 직전 2주 평균 (네이버 검색어 트렌드 상대값)<br>'
               f'항목별 자세한 설명은 다음 카드에 있습니다.</div>'), png_path)


def render_detail(c, rank, today, png_path, photo=None):
    esc = html.escape
    color = CATEGORIES[c["cat"]][0]
    photo_html = (f'<div class="photo"><img src="{photo.as_uri()}"><span>AI 생성 이미지</span></div>'
                  if photo and photo.is_file() else "")
    facts = "".join(f'<div class="fact"><b>{esc(f["label"])}</b><span>{esc(f["text"])}</span></div>' for f in c["facts"])
    shoot(page(DETAIL_CSS,
               f'<div class="top">Dbro / 오늘의 트렌드<span>{today:%Y-%m-%d}</span></div>{photo_html}'
               f'<div class="hero" style="background:{color}"><div class="emo">{esc(c["emoji"])}</div>'
               f'<div class="rank">TOP {rank}</div><div class="term">{esc(c["term"])}</div>'
               f'<span class="chip">{esc(c["cat"])}</span><span class="surge">{surge_label(c["surge"])}</span></div>'
               f'<div class="def">{esc(c["headline"])}</div><div class="facts">{facts}</div>'
               f'<div class="why"><b>왜 뜨나</b> · {esc(c["why"])}</div>'
               f'<div class="foot">최근 네이버 뉴스·블로그 글을 바탕으로 정리 · 검색 ×N = 최근 3일 ÷ 직전 2주 평균</div>'), png_path)


def existing_picks(post):
    """--rewrite: 이미 발행된 글의 선정 키워드·분야·급증비를 그대로 다시 쓴다(선정 결과가 바뀌지 않게)."""
    text = post.read_text()
    scores = json.loads(re.search(r"^scores: (\{.*\})$", text, re.M).group(1))
    m = re.search(r"^picks: (\[.*\])$", text, re.M)
    cats = dict(json.loads(m.group(1))) if m else dict(
        (t, c) for t, c in re.findall(r"^\d\. \*\*(.+?)\*\* \((.+?),", text, re.M))   # 구 형식 본문
    return [{"term": t, "cat": cats.get(t, "소비·생활"), "surge": s, "blog14": 0} for t, s in scores.items()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--kind", default="trend", choices=["trend"])   # blog_approval 호환
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rewrite", action="store_true", help="이미 있는 글을 같은 키워드로 다시 작성(형식 개편용)")
    ap.add_argument("--replace", action="store_true", help="이미 있는 오늘 글을 키워드부터 새로 뽑아 교체")
    a = ap.parse_args()
    today = date.fromisoformat(a.date)

    post = BLOG / "_posts" / f"{a.date}-trend.md"
    if post.exists() and not (a.rewrite or a.replace):
        log(f"이미 있음: {post.name}")
        return 3
    try:
        hot = []
        if a.rewrite:
            picks = existing_picks(post)
        else:
            docs = collect(today)
            log(f"수집 {len(docs)}건 ({', '.join(f'{k} {sum(d["cat"] == k for d in docs)}' for k in CATEGORIES)})")
            cands = verify(extract(docs), today)
            picks, hot = select(cands, recent_terms(today, skip_today=a.replace))
            log(f"여전히 화제: {', '.join(h['term'] for h in hot) or '없음'}")
            for c in sorted(cands, key=lambda c: -c["surge"]):
                log(f"  {'선정' if c in picks else '    '} [{c['cat']}] {c['term']}: ×{c['surge']} · 블로그14일 {c['blog14']}")
        if a.dry_run:
            return 0
        if len(picks) < MIN_N:
            log(f"선정 {len(picks)}개 < {MIN_N} — 발행 생략")
            return 2
        picks = [gather_sources(c) for c in picks[:TOP_N + 2]]
        hot = [gather_sources(c) for c in hot]
    except Exception as exc:
        log(f"수집·검증 실패: {type(exc).__name__}: {exc}")
        return 1
    try:
        items, hot = write(picks, hot)
        items = items[:TOP_N]
    except Exception as exc:
        log(f"작성 실패: {type(exc).__name__}: {exc}")
        return 1
    if len(items) < MIN_N:
        log(f"작성 통과 {len(items)}개 < {MIN_N} — 발행 생략")
        return 2

    cards = BLOG / "assets" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    cover = cards / f"{a.date}_trend.png"            # 텔레그램 알림·글 첫 이미지
    render_cover(items, today, cover, hot)
    for old in cards.glob(f"{a.date}_trend_*.png"):   # --rewrite 때 이전 상세 카드 정리
        old.unlink()
    url = lambda name: f"{{{{ '/assets/cards/{name}' | relative_url }}}}"
    sections = []
    for i, c in enumerate(items, 1):
        img = cards / f"{a.date}_trend_{i}.png"
        with tempfile.TemporaryDirectory() as d:
            photo = Path(d) / "photo.jpg"
            has_photo = generate_photo(c["scene"], photo) or generate_photo(c["scene"], photo)   # 실패 시 1회 재시도
            log(f"  사진 {'생성' if has_photo else '없음'}: {c['term']} ({c['scene'][:30]})")
            render_detail(c, i, today, img, photo if has_photo else None)
        facts = "\n".join(f"- **{f['label']}** {f['text']}" for f in c["facts"])
        sections.append(f"## {i}. {c['emoji']} {c['term']}\n\n"
                        f"*{c['cat']} · {surge_label(c['surge'])}*\n\n"
                        f"**{c['headline']}**\n\n{facts}\n\n> 왜 뜨나: {c['why']}\n\n"
                        f"![{c['term']} 설명 카드]({url(img.name)})")
    if hot:
        sections.append("## 🔥 여전히 화제\n\n이미 소개했지만 오늘도 검색이 크게 늘어난 키워드입니다.\n\n" +
                        "\n".join(f"- **{h['emoji']} {h['term']}** ({h['cat']}, {surge_label(h['surge'])}) — "
                                   f"{h['headline']}. {h['why']}" for h in hot))
    names = " · ".join(c["term"] for c in items[:3])
    scores = json.dumps({c["term"]: c["surge"] for c in items}, ensure_ascii=False)
    picks_fm = json.dumps([[c["term"], c["cat"]] for c in items], ensure_ascii=False)
    tags = ["트렌드"] + list(dict.fromkeys([c["cat"] for c in items] + [c["term"] for c in items]))
    desc = " / ".join(f"{c['term']}: {c['headline']}" for c in items)[:150].replace('"', "'")
    post.parent.mkdir(exist_ok=True)
    post.write_text(
        "---\n"
        "layout: post\n"
        f'title: "[트렌드] 오늘 뜨는 것: {names}"\n'
        f"date: {a.date} 12:30:00 +0900\n"
        "categories: [trend]\n"
        f"tags: {json.dumps(tags, ensure_ascii=False)}\n"
        f'description: "{desc}"\n'
        f"scores: {scores}\n"
        f"picks: {picks_fm}\n"
        "---\n"
        f"![{a.date} 오늘의 트렌드 TOP {len(items)}]({url(cover.name)})\n\n"
        + "\n\n".join(sections) +
        "\n\n※ 네이버 검색어 트렌드(상대값, 어제까지)와 최근 뉴스·블로그 글을 바탕으로 정리했습니다.\n"
    )
    print(f"[blog] 생성: {post.relative_to(BLOG)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
