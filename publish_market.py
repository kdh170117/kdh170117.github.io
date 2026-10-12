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
        if t["text"].isupper() or t["size"] != 30:   # MARKET SNAPSHOT 등 다음 섹션에서 멈춤
            break
        lede.append(t["text"])
    return headline, " ".join(lede).strip()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True)
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--push", action="store_true", help="커밋+푸시(공개 발행)")
    a = p.parse_args()

    if not (SB / "logs" / f"{a.date}_{a.kind}.md").is_file():
        print(f"[blog] 아카이브 없음: {a.date}_{a.kind} (휴장일 등) — 발행 생략", file=sys.stderr)
        return 2
    category, prefix, hhmm = KINDS[a.kind]
    post = BLOG / "_posts" / f"{a.date}-{a.kind.replace('_', '-')}.md"
    if post.exists():
        print(f"[blog] 이미 있음: {post.name}", file=sys.stderr)
        return 3

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        png, texts = render(a.date, a.kind, Path(tmp))
        if not png:
            return 1
        headline, lede = headline_and_lede(texts)
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
        "---\n"
        f"{lede}\n\n"
        f"![{a.date} {prefix} 브리핑 카드]({{{{ '/assets/cards/{img.name}' | relative_url }}}})\n\n"
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
