#!/usr/bin/env python3
"""블로그 발행 승인 흐름 — 텔레그램 미리보기 → [발행]/[건너뛰기] 버튼.

전용 봇(kdh_gemini_bot, BLOG_BOT_TOKEN)만 쓴다. 이 봇을 수신하는 다른 프로그램은 없으므로
getUpdates offset 을 확정해도 된다(브리핑 봇·대시보드 봇과 분리).

  prepare --kind kr_morning [--date D]   글 생성(커밋 안 함) + 미리보기 발송   (launchd: 브리핑 직후)
  poll                                    버튼 응답 처리 → 발행(푸시) 또는 폐기  (launchd: 60초마다)
  ping                                    봇 연결 테스트 메시지

종료코드: 0 정상 · 1 실패 · 2 아카이브 없음(휴장 등) · 3 이미 처리됨
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

BLOG = Path(__file__).resolve().parent
PENDING = BLOG / ".pending"
STATE = PENDING / "state.json"
SITE = "https://kdh170117.github.io"
KST = timezone(timedelta(hours=9))
EXPIRE_HOURS = 24
# 2026-10-12 도형님: "글 올린 거 보니 자동화해도 무리 없겠다" → 승인 없이 바로 발행하고 알림만 보낸다.
# 알림의 [🗑 내리기] 버튼으로 사후에 내릴 수 있다. False 로 바꾸면 기존 승인 흐름으로 돌아간다.
AUTO_PUBLISH = True
PYTHON = "/opt/homebrew/bin/python3"
MARKET_KINDS = ["kr_morning", "kr_close", "us"]          # publish_market.py (stocks_briefing 카드)
BOARD_KINDS = ["macro_global", "flow_kr"]                # publish_board.py (macro_board 보드)
TREND_KINDS = ["trend"]                                   # publish_trend.py (분야 무관 검색 급증 TOP 5)

load_dotenv(BLOG / ".env")
load_dotenv(Path.home() / "ai" / "agent-pipeline" / ".env")   # TELEGRAM_CHAT_ID·TELEGRAM_PROXY_URL
TOKEN = os.getenv("BLOG_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def log(msg):
    print(f"{datetime.now(KST):%Y-%m-%d %H:%M:%S} {msg}")


def err(msg):
    # requests 예외 문자열에는 요청 URL(=봇 토큰)이 들어간다 — 로그에 남기지 않는다
    msg = str(msg).replace(TOKEN, "***") if TOKEN else str(msg)
    print(f"{datetime.now(KST):%Y-%m-%d %H:%M:%S} {msg}", file=sys.stderr)


def tg(method, data=None, files=None, timeout=30):
    """직통 → 네트워크 오류 시에만 프록시 (INFRA.md 폴백 패턴)."""
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    proxy = os.getenv("TELEGRAM_PROXY_URL")
    routes = [None] + ([proxy] if proxy else [])
    for i, route in enumerate(routes):
        try:
            if files:
                for f in files.values():
                    f.seek(0)
            r = requests.post(url, data=data, files=files, timeout=timeout,
                              proxies={"https": route} if route else None)
            body = r.json()
            if not body.get("ok"):
                raise RuntimeError(f"{method} 거부: {body.get('description')}")
            return body["result"]
        except (requests.ConnectionError, requests.Timeout) as exc:
            if i + 1 < len(routes):
                err(f"[tg] {method} 직통 실패({type(exc).__name__}) → 프록시")
                continue
            raise


def load_state():
    if STATE.is_file():
        return json.loads(STATE.read_text())
    return {"offset": 0, "items": {}}


def save_state(state):
    PENDING.mkdir(exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    tmp.replace(STATE)


def git(*args):
    r = subprocess.run(["git", *args], cwd=BLOG, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} 실패: {r.stderr.strip()}")
    return r.stdout


def post_url(post_name):
    # _posts/2026-10-12-kr-morning.md → /posts/2026/10/12/kr-morning/
    y, m, d, slug = post_name.removesuffix(".md").split("-", 3)
    return f"{SITE}/posts/{y}/{m}/{d}/{slug}/"


def cmd_prepare(date, kind):
    pid = f"{date}_{kind}"
    state = load_state()
    if pid in state["items"]:
        log(f"[prepare] {pid} 이미 처리됨({state['items'][pid]['status']})")
        return 3
    script = ("publish_board.py" if kind in BOARD_KINDS else
              "publish_trend.py" if kind in TREND_KINDS else "publish_market.py")
    r = subprocess.run([PYTHON, str(BLOG / script), "--date", date, "--kind", kind],
                       capture_output=True, text=True, timeout=900)
    if r.returncode == 2:
        log(f"[prepare] {pid} 원본 없음(휴장·새 데이터 없음) — 생략")
        return 2
    if r.returncode == 3:
        log(f"[prepare] {pid} 이미 블로그에 있음 — 생략")
        return 3
    if r.returncode != 0:
        err(f"[prepare] 글 생성 실패 rc={r.returncode}: {r.stderr.strip()[-500:]}")
        return 1
    post = f"_posts/{date}-{kind.replace('_', '-')}.md"
    img = f"assets/cards/{pid}.png"
    title = next(l for l in (BLOG / post).read_text().splitlines() if l.startswith("title:"))
    title = title.split(":", 1)[1].strip().strip('"')
    item = {"status": "pending", "post": post, "img": img, "title": title,
            "message_id": None, "created": time.time()}
    if AUTO_PUBLISH:
        try:
            publish(item)
        except Exception as exc:
            item["status"] = "failed"
            state["items"][pid] = item
            save_state(state)
            err(f"[prepare] {pid} 자동 발행 실패: {exc}")
            tg("sendMessage", data={"chat_id": CHAT_ID, "text": f"⚠️ 블로그 자동 발행 실패 — 로그 확인 필요\n{title}"})
            return 1
        item["status"] = "published"
        state["items"][pid] = item
        save_state(state)   # 발행 결과를 먼저 저장(알림 실패와 무관하게)
        caption = f"✅ 블로그 자동 발행 (반영까지 1~2분)\n{title}\n{post_url(Path(post).name)}"
        keyboard = {"inline_keyboard": [[{"text": "🗑 내리기", "callback_data": f"rm|{pid}"}]]}
    else:
        caption = f"[블로그 발행 대기]\n{title}\n\n{EXPIRE_HOURS}시간 내 미응답 시 자동 폐기"
        keyboard = {"inline_keyboard": [[{"text": "✅ 발행", "callback_data": f"ok|{pid}"},
                                         {"text": "❌ 건너뛰기", "callback_data": f"no|{pid}"}]]}
    with open(BLOG / img, "rb") as f:
        msg = tg("sendPhoto", data={"chat_id": CHAT_ID, "caption": caption,
                                    "reply_markup": json.dumps(keyboard)}, files={"photo": f}, timeout=60)
    item["message_id"] = msg["message_id"]
    state["items"][pid] = item
    save_state(state)
    log(f"[prepare] {pid} {'자동 발행+알림' if AUTO_PUBLISH else '미리보기 발송'} (message_id={msg['message_id']})")
    return 0


def discard(item):
    for rel in (item["post"], item["img"]):
        (BLOG / rel).unlink(missing_ok=True)


def publish(item):
    """멱등: 이미 커밋된 항목을 다시 받아도(재시도) 실패하지 않고 푸시만 다시 한다."""
    git("add", item["post"], item["img"])
    if git("status", "--porcelain", "--", item["post"], item["img"]).strip():
        git("commit", "-qm", f"브리핑 발행: {item['title']}")
    git("push", "-q")


def unpublish(item):
    """발행된 글 내리기(사이트에서 제거). 멱등: 이미 내려간 파일이면 푸시만 다시 한다."""
    tracked = git("ls-files", "--", item["post"], item["img"]).split()
    if tracked:
        git("rm", "-q", "--", *tracked)
        git("commit", "-qm", f"글 내림: {item['title']}")
    git("push", "-q")


def answer(cq, text):
    # 버튼 응답은 수 초 안에 하지 않으면 텔레그램이 거부한다(폴링 주기 60초).
    # 알림 표시용일 뿐이라 실패해도 처리 결과·상태 저장에 영향을 주면 안 된다.
    try:
        tg("answerCallbackQuery", data={"callback_query_id": cq["id"], "text": text})
    except Exception as exc:
        err(f"[poll] 버튼 응답 생략: {exc}")


def finish_message(item, text):
    try:
        tg("editMessageCaption", data={"chat_id": CHAT_ID, "message_id": item["message_id"],
                                       "caption": f"{text}\n{item['title']}"})
    except Exception as exc:   # 캡션 수정 실패는 발행 결과에 영향 없음
        err(f"[poll] 캡션 수정 실패: {exc}")


def cmd_poll():
    state = load_state()
    updates = tg("getUpdates", data={"offset": state["offset"], "timeout": 0,
                                     "allowed_updates": json.dumps(["callback_query"])})
    rc = 0
    for upd in updates:
        state["offset"] = upd["update_id"] + 1
        cq = upd.get("callback_query")
        if not cq:
            continue
        chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if chat != CHAT_ID and str(cq["from"]["id"]) != CHAT_ID:
            err(f"[poll] 허용되지 않은 사용자 응답 무시 (from={cq['from']['id']})")
            continue
        action, _, pid = cq.get("data", "").partition("|")
        item = state["items"].get(pid)
        expected = "published" if action == "rm" else "pending"
        if not item or item["status"] != expected:
            answer(cq, "이미 처리된 항목입니다")
            save_state(state)
            continue
        try:
            if action == "rm":
                unpublish(item)
                item["status"] = "removed"
            elif action == "ok":
                publish(item)
                item["status"] = "published"
            else:
                discard(item)
                item["status"] = "skipped"
        except Exception as exc:
            err(f"[poll] {pid} 처리 실패: {exc}")
            answer(cq, "처리 실패 — 로그 확인")
            finish_message(item, "⚠️ 발행 실패 — 로그 확인 후 다시 시도 필요")
            item["status"] = "failed"
            save_state(state)
            rc = 1
            continue
        # 결과를 먼저 저장한다: 이후 알림이 실패해도 같은 버튼을 두 번 처리하지 않는다
        save_state(state)
        if item["status"] == "removed":
            answer(cq, "내렸습니다")
            finish_message(item, "🗑 내림 (사이트 반영까지 1~2분)")
            log(f"[poll] {pid} 내림")
        elif item["status"] == "published":
            answer(cq, "발행했습니다")
            finish_message(item, f"✅ 발행됨 (반영까지 1~2분)\n{post_url(Path(item['post']).name)}")
            log(f"[poll] {pid} 발행")
        else:
            answer(cq, "건너뛰었습니다")
            finish_message(item, "❌ 건너뜀")
            log(f"[poll] {pid} 건너뜀")
    # 만료 처리
    now = time.time()
    for pid, item in state["items"].items():
        if item["status"] == "pending" and now - item["created"] > EXPIRE_HOURS * 3600:
            discard(item)
            item["status"] = "expired"
            finish_message(item, "⌛ 응답 없어 폐기")
            log(f"[poll] {pid} 만료 폐기")
    save_state(state)
    return rc


def main():
    if not TOKEN or not CHAT_ID:
        err("BLOG_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 누락")
        return 1
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("prepare")
    pp.add_argument("--kind", required=True, choices=MARKET_KINDS + BOARD_KINDS + TREND_KINDS)
    pp.add_argument("--date", default=None, help="기본: 오늘(KST)")
    sub.add_parser("poll")
    sub.add_parser("ping")
    a = p.parse_args()
    try:
        if a.cmd == "prepare":
            return cmd_prepare(a.date or datetime.now(KST).strftime("%Y-%m-%d"), a.kind)
        if a.cmd == "poll":
            return cmd_poll()
        tg("sendMessage", data={"chat_id": CHAT_ID, "text": "블로그 승인 봇 연결 테스트입니다."})
        log("[ping] 발송 완료")
        return 0
    except Exception as exc:
        err(f"[{a.cmd}] 실패: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
