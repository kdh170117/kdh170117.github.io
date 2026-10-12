#!/usr/bin/env python3
"""macro_board 산출물(글로벌 매크로 / 한국 수급 보드) → 블로그 글 생성.

macro_board 가 이미 만든 out/{global,kr}_YYYY-MM-DD.png 와 data/*_analysis.json 헤드라인을
읽기만 한다(재분석·재렌더·발송 없음). 보드가 새 데이터가 없어 생략한 날은 파일이 없어 rc 2.

사용:
  python3 publish_board.py --date 2026-10-12 --kind macro_global   # 글 파일만 생성(커밋 안 함)
  python3 publish_board.py --date 2026-10-12 --kind flow_kr --push

종료코드: 0 생성/발행 완료 · 1 git 실패 · 2 보드 산출물 없음 · 3 이미 있음
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

BLOG = Path(__file__).resolve().parent
MB = Path.home() / "ai" / "projects" / "macro_board"

KINDS = {
    # kind: (macro_board 이름, 카테고리, 제목 접두, 발행 시각, 요약 문구)
    "macro_global": ("global", "market", "글로벌 매크로", "07:20",
                     "금리·변동성·주식·유럽·일본·통화·원자재 9개 영역을 한 장으로 정리한 보드입니다."),
    "flow_kr": ("kr", "market", "국내 수급", "08:25",
                "코스피·코스닥 투자자별 수급, 프로그램, 파생, ETF, 업종 강약을 한 장으로 정리한 보드입니다."),
}

DISCLAIMER = ("※ 시장 전반 정보 정리이며 투자 권유가 아닙니다. 수치는 수집 데이터와 자동 대조했고, "
              "'자비스 해석'은 데이터 기반 의견입니다. 출처는 보드 하단에 표기했습니다.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True)
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--push", action="store_true", help="커밋+푸시(공개 발행)")
    a = p.parse_args()

    name, category, prefix, hhmm, about = KINDS[a.kind]
    src_png = MB / "out" / f"{name}_{a.date}.png"
    src_json = MB / "data" / f"{a.date}_{name}_analysis.json"
    if not src_png.is_file() or not src_json.is_file():
        print(f"[blog] 보드 산출물 없음: {name} {a.date} (휴장·새 데이터 없음 등) — 발행 생략", file=sys.stderr)
        return 2
    post = BLOG / "_posts" / f"{a.date}-{a.kind.replace('_', '-')}.md"
    if post.exists():
        print(f"[blog] 이미 있음: {post.name}", file=sys.stderr)
        return 3

    headline = json.loads(src_json.read_text()).get("headline", "").strip()
    if not headline:
        print(f"[blog] 헤드라인 없음: {src_json.name}", file=sys.stderr)
        return 2
    img = BLOG / "assets" / "cards" / f"{a.date}_{a.kind}.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_png, img)

    title = f"[{prefix}] {headline}".replace('"', "'")
    post.parent.mkdir(exist_ok=True)
    post.write_text(
        "---\n"
        "layout: post\n"
        f'title: "{title}"\n'
        f"date: {a.date} {hhmm}:00 +0900\n"
        f"categories: [{category}]\n"
        "---\n"
        f"{about}\n\n"
        f"[![{a.date} {prefix} 보드]({{{{ '/assets/cards/{img.name}' | relative_url }}}})]"
        f"({{{{ '/assets/cards/{img.name}' | relative_url }}}})\n\n"
        "이미지를 누르면 원본 크기로 볼 수 있습니다.\n\n"
        f"{DISCLAIMER}\n"
    )
    print(f"[blog] 생성: {post.relative_to(BLOG)}")

    if a.push:
        for cmd in (["git", "add", str(post), str(img)],
                    ["git", "commit", "-qm", f"{a.date} {prefix} 보드"],
                    ["git", "push", "-q"]):
            r = subprocess.run(cmd, cwd=BLOG, capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[blog] {' '.join(cmd[:2])} 실패: {r.stderr.strip()}", file=sys.stderr)
                return 1
        print("[blog] 발행(푸시) 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
