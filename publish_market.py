#!/usr/bin/env python3
"""stocks_briefing 아카이브 → 블로그 글(카드 PNG + 요약) 발행.

카드는 stocks_briefing/tools/replay_card.py 로 오프라인 재생한다(텔레그램·네트워크 차단).
stocks_briefing 코드는 읽기만 하고 고치지 않는다.

사용:
  python3 publish_market.py --date 2026-10-12 --kind kr_morning          # 글 파일만 생성(커밋 안 함)
  python3 publish_market.py --date 2026-10-12 --kind kr_morning --push   # 커밋 + 푸시 = 공개 발행

종료코드: 0 생성/발행 완료 · 1 렌더·git 실패 · 2 아카이브 없음(휴장 등) · 3 이미 발행됨
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BLOG = Path(__file__).resolve().parent
SB = Path.home() / "ai" / "projects" / "stocks_briefing"
PYTHON = "/opt/homebrew/bin/python3"

KINDS = {
    # kind: (카테고리, 제목 접두, 발행 시각)
    "kr_morning": ("market", "장시작", "09:20"),
    "kr_close": ("market", "장마감", "16:00"),
    "us": ("market", "미국장", "07:00"),
}

DISCLAIMER = "※ 시장 전반 정보 정리이며 투자 권유가 아닙니다. 데이터 출처는 카드 하단에 표기했습니다."

# 개인 보유 종목 정보 — 블로그(공개)에는 절대 싣지 않는다.
# 장마감 브리핑 상단의 '조기경보' 블록은 개인 보유 종목 모니터라 아카이브 사본에서 제거한 뒤 렌더한다.
PERSONAL_LINE = re.compile(r"출구 전략|위험 자산\(")
PERSONAL_BLOCK_START = re.compile(r"조기경보 시스템")
# 렌더 결과 검사: 카드에 남으면 발행 중단 (제거 로직이 깨져도 새지 않게 하는 마지막 관문)
PERSONAL_GUARD = re.compile(r"조기경보|출구 전략|위험 자산|내 종목|보유|포트폴리오|주주 종목")


def sanitize(src, dst):
    """아카이브 사본에서 개인 보유 종목 줄·블록을 지운다(원본은 읽기만)."""
    out, skipping = [], False
    for line in src.read_text().splitlines():
        if skipping:
            if not line.strip():       # 블록은 빈 줄로 끝난다
                skipping = False
            continue
        if PERSONAL_BLOCK_START.search(line):
            skipping = True
            continue
        if PERSONAL_LINE.search(line):
            continue
        out.append(line)
    dst.write_text("\n".join(out) + "\n")


KIND_TAGS = {"kr_morning": ["국내증시", "장시작"], "kr_close": ["국내증시", "장마감"], "us": ["미국증시"]}
TAG_NORMALIZE = {"S&P 500": "S&P500", "USD/KRW": "환율", "미국 10Y": "미국 10년물"}
DIGEST_SKIP = ("상세 차트", "내 종목")   # 대시보드 내부 링크 섹션 — 공개 금지


def render(date, kind, out):
    logs = out / "logs"
    logs.mkdir()
    sanitize(SB / "logs" / f"{date}_{kind}.md", logs / f"{date}_{kind}.md")
    cmd = [PYTHON, str(SB / "tools" / "replay_card.py"), "--date", date, "--kind", kind,
           "--logs-dir", str(logs), "--out", str(out / "png")]
    r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "TMPDIR": "/tmp"})
    png = out / "png" / f"{date}_{kind}.png"
    if r.returncode != 0 or not png.is_file():
        print(f"[blog] 카드 렌더 실패 rc={r.returncode}\n{r.stdout[-800:]}{r.stderr[-800:]}", file=sys.stderr)
        return None, None
    texts = json.loads((out / "png" / f"{date}_{kind}.texts.json").read_text())
    leaked = [t["text"] for t in texts if PERSONAL_GUARD.search(t["text"])]
    if leaked:
        print(f"[blog] 개인 보유 정보가 카드에 남음 — 발행 중단: {leaked[:3]}", file=sys.stderr)
        return None, None
    return png, texts


def headline_and_lede(texts):
    """카드 문자열에서 헤드라인(가장 큰 글꼴)과 바로 아래 요약 문단을 뽑는다."""
    big = max(t["size"] for t in texts)
    head_idx = [i for i, t in enumerate(texts) if t["size"] == big]
    headline = " ".join(texts[i]["text"] for i in head_idx).strip()
    lede = []
    for t in texts[head_idx[-1] + 1:]:
        # 요약 문단(30pt)이 끝나면 멈춘다. isupper() 는 쓰지 않는다 — 한글은 대소문자가 없어
        # "…AI 수요…" 같은 줄이 대문자 제목으로 오인됐다(2026-10-12 장마감 요약 누락).
        if t["size"] != 30 or t["text"] == "MARKET SNAPSHOT":
            break
        lede.append(t["text"])
    return headline, " ".join(lede).strip()


def snapshot(texts):
    """카드 MARKET SNAPSHOT 표 → [(이름, 값, 등락)]. 큰 행(지수·종목) + 작은 행(환율·금리·VIX)."""
    rows, i = [], next((k for k, t in enumerate(texts) if t["text"] == "MARKET SNAPSHOT"), None)
    if i is None:
        return rows
    i += 1
    while i + 2 < len(texts) and texts[i]["size"] == 30 and texts[i + 1]["size"] == 40:
        rows.append((texts[i]["text"], texts[i + 1]["text"], texts[i + 2]["text"]))
        i += 3
    while i + 2 < len(texts) and texts[i]["size"] == 22:
        label = texts[i]["text"]
        j = i + 1
        while j < len(texts) and texts[j]["size"] == 22 and texts[j]["text"] == "잠정":
            j += 1
        if j + 1 < len(texts) and texts[j]["size"] == 32 and texts[j + 1]["size"] == 26:
            rows.append((label, texts[j]["text"], texts[j + 1]["text"]))
            i = j + 2
        else:
            break
    return rows


def card_line_after(texts, label):
    """'자비스 한줄' 처럼 라벨 바로 다음 문자열."""
    for k, t in enumerate(texts[:-1]):
        if t["text"] == label:
            return texts[k + 1]["text"]
    return ""


def digest_sections(md_text):
    """아카이브의 '실제 텔레그램 발송분' 블록 → [(섹션명, [줄])]. 내부 링크 섹션·URL 은 버린다."""
    m = re.search(r"## 실제 텔레그램 발송분.*?```\n(.*?)```", md_text, re.S)
    if not m:
        return []
    sections = []
    for block in re.split(r"\n\s*\n", m.group(1).strip()):
        lines = block.strip().splitlines()
        head = re.match(r"^\S+\s+\*(.+?)\*\s*$", lines[0]) if lines else None
        if not head or any(k in head.group(1) for k in DIGEST_SKIP):
            continue
        body = [l.strip().lstrip("•").strip() for l in lines[1:]
                if l.strip() and "http" not in l and "해석 금지" not in l]   # 내부 판단용 문구는 독자용이 아니다
        if body:
            sections.append((head.group(1), body))
    return sections


def build_body(lede, texts, md_text):
    parts = [lede]
    rows = snapshot(texts)
    if rows:
        parts.append("## 주요 지표\n\n| 항목 | 값 | 변동 |\n|---|---|---|\n" +
                     "\n".join(f"| {n} | {v} | {c} |" for n, v, c in rows))
    for name, lines in digest_sections(md_text):
        if len(lines) == 1 and name in ("체제", "바뀐 것"):
            parts.append(f"**{name}** — {lines[0]}")
        else:
            parts.append(f"## {name}\n\n" + "\n".join(f"- {l}" for l in lines))
    one = card_line_after(texts, "자비스 한줄")
    if one:
        parts.append(f"> 한 줄 요약: {one}")
    return "\n\n".join(parts)


def make_tags(kind, texts):
    tags = list(KIND_TAGS[kind])
    for n, _, _ in snapshot(texts):
        t = TAG_NORMALIZE.get(n, n)
        if t not in tags:
            tags.append(t)
    return tags


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True)
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--push", action="store_true", help="커밋+푸시(공개 발행)")
    p.add_argument("--rewrite", action="store_true", help="이미 있는 글을 같은 원본으로 다시 생성(본문 개편용)")
    a = p.parse_args()

    if not (SB / "logs" / f"{a.date}_{a.kind}.md").is_file():
        print(f"[blog] 아카이브 없음: {a.date}_{a.kind} (휴장일 등) — 발행 생략", file=sys.stderr)
        return 2
    category, prefix, hhmm = KINDS[a.kind]
    post = BLOG / "_posts" / f"{a.date}-{a.kind.replace('_', '-')}.md"
    if post.exists() and not a.rewrite:
        print(f"[blog] 이미 있음: {post.name}", file=sys.stderr)
        return 3

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        png, texts = render(a.date, a.kind, Path(tmp))
        if not png:
            return 1
        headline, lede = headline_and_lede(texts)
        body = build_body(lede, texts, (Path(tmp) / "logs" / f"{a.date}_{a.kind}.md").read_text())
        tags = make_tags(a.kind, texts)
        leaked = PERSONAL_GUARD.findall(body + " ".join(tags))
        if leaked:   # 본문도 카드와 같은 관문을 통과해야 한다
            print(f"[blog] 개인 보유 정보가 본문에 남음 — 발행 중단: {leaked[:3]}", file=sys.stderr)
            return 1
        img = BLOG / "assets" / "cards" / f"{a.date}_{a.kind}.png"
        shutil.copyfile(png, img)

    title = f"[{prefix}] {headline}".replace('"', "'")
    post.parent.mkdir(exist_ok=True)
    post.write_text(
        "---\n"
        "layout: post\n"
        f'title: "{title}"\n'
        f"date: {a.date} {hhmm}:00 +0900\n"
        f"categories: [{category}]\n"
        f"tags: {json.dumps(tags, ensure_ascii=False)}\n"
        f'description: "{lede[:150].replace(chr(34), chr(39))}"\n'
        "---\n"
        f"![{a.date} {prefix} 브리핑 카드]({{{{ '/assets/cards/{img.name}' | relative_url }}}})\n\n"
        f"{body}\n\n"
        f"{DISCLAIMER}\n"
    )
    print(f"[blog] 생성: {post.relative_to(BLOG)}")

    if a.push:
        for cmd in (["git", "add", str(post), str(img)],
                    ["git", "commit", "-qm", f"{a.date} {prefix} 브리핑"],
                    ["git", "push", "-q"]):
            r = subprocess.run(cmd, cwd=BLOG, capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[blog] {' '.join(cmd[:2])} 실패: {r.stderr.strip()}", file=sys.stderr)
                return 1
        print("[blog] 발행(푸시) 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
