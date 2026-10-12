"""_llm_claude.py — Claude CLI 헤드리스 호출 공통 헬퍼 (로컬 LLM 대체).

로컬 Ollama(Gemma) 자리를 대신한다. **OAuth 전용**(ANTHROPIC_API_KEY 미사용 — 헌법 준수).
launchd 에서 CLI 가 keychain 을 직접 읽으면 900s hang 하므로(실측) 토큰을 미리 주입한다.
계정1 한도/인증 실패 시 계정2(~/.claude-account2)로 폴백 — **단, 계정2가 실제로 쓸 수 있을 때만.**

호출부 계약
-----------
- 성공: 텍스트 반환(공백 제거, 항상 비어있지 않음).
- 실패: 아래 예외를 올린다. 모두 RuntimeError 하위이므로 기존 `except RuntimeError`
  호출부와 호환된다. 호출부는 타입으로 원인을 구분할 수 있다.
    * LLMAuthError     — 로그인/토큰 문제 (재시도해도 소용없음. 사람이 /login 해야 함)
    * LLMLimitError    — 사용량 한도 (시간이 지나면 풀림)
    * LLMResponseError — 응답이 비었거나 형식이 이상함 (LLM 은 살아있음)
    * LLMError         — 그 외 실행 실패 (위 셋의 공통 부모이기도 함)

2026-09-13 수정 배경 (실측):
  계정2(~/.claude-account2)에 자격증명이 없어 CLI 가 rc=1 과 함께 stdout 으로
  "Not logged in · Please run /login" 을 뱉는데, 구버전 _run 은 `rc!=0 and not out`
  조건 탓에 **그 오류 문구를 정상 LLM 응답으로 반환**했다. 그 결과 인증 실패가
  상위에서 "번역 응답 개수 불일치: 0/15" 라는 엉뚱한 파싱 오류로 둔갑했다.

2026-09-13 2차 수정 (검수 지적 반영):
  1차 수정이 넣은 '짧은 출력(<80자) + 약한 키워드' 휴리스틱은 반대 방향으로 틀렸다.
  rc=0 이고 80자 미만인 **정상 응답**에 'oauth'·'401'·'authentication' 등이 들어 있으면
  LLMAuthError 로 둔갑한다(예: "Fed's 401(k) rule" 번역, 'authentication' 포함 짧은 요약).
  실측상 CLI 인증 실패는 **항상 rc != 0** 이므로,
    * rc != 0 → 부분 문자열 매칭(오탐해도 실패는 실패)
    * rc == 0 → **응답 전체가 알려진 오류 배너인지**(접두/완전 일치 + 줄 수 제한)
  로 판정을 좁혔다. 길이 게이트는 제거했다.

2026-09-13 3차 수정 (격리):
  `--system-prompt` 가 CLAUDE.md·auto-memory 로딩을 끈다는 **구 주석은 거짓이었다.**
  실측상 전역 개인 문서와 cwd 의 프로젝트 CLAUDE.md 가 모든 호출 프롬프트로 유입됐다
  (입력 13,510토큰 → 격리 후 861토큰). `--safe-mode` + 중립 임시 작업디렉터리로 차단한다.
  상세 근거와 '남는 것'은 `_run` 안의 주석 참조.

2026-10-12 장기 토큰 (오후 무인 작업 인증 실패):
  keychain accessToken 은 수 시간짜리이고 이 모듈은 갱신하지 못한다(갱신은 대화형 CLI 만).
  아침에 로그인해도 오후 잡이 줄줄이 LLMAuthError 로 죽었다. `claude setup-token` 장기 토큰을
  `CLAUDE_HEADLESS_OAUTH_TOKEN`(환경변수 또는 ~/ai/agent-pipeline/.env)에 두면 그걸 먼저 쓰고,
  없거나 인증이 거부되면 keychain 으로 내려간다(OAuth 그대로 — 헌법 준수).
"""
from __future__ import annotations

import json as _json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CLAUDE_BIN = str(Path.home() / ".local/bin/claude")
# 값싼 대량 처리 기본값. 품질이 필요한 호출부는 model="claude-sonnet-5" 로 지정.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

ACCOUNT2_DIR = Path.home() / ".claude-account2"

# 계정1 장기 토큰(`claude setup-token` 발급, 약 1년) — 2026-10-12 도입.
# keychain 의 accessToken 은 수 시간짜리이고 이 모듈은 갱신(refresh)을 못 한다(갱신은 대화형 CLI 만).
# 그래서 대화형 세션을 몇 시간 안 열면 오후 무인 작업이 줄줄이 인증 실패했다("아침에 로그인했는데 또?").
# 장기 토큰이 있으면 그걸 먼저 쓰고, 없거나 거부되면 기존 keychain 경로로 내려간다.
# 변수명을 CLAUDE_CODE_OAUTH_TOKEN 과 일부러 다르게 둔다 — .env 를 로드하는 다른 도구가 자동으로 집어 쓰지 않게.
LONG_TOKEN_VAR = "CLAUDE_HEADLESS_OAUTH_TOKEN"
LONG_TOKEN_ENV_FILE = Path.home() / "ai" / "agent-pipeline" / ".env"

# 토큰 만료가 이 시간 안으로 다가오면 stderr 에 경고한다(값이 아니라 만료 '시각'만).
TOKEN_WARN_SECONDS = 6 * 3600

# rc==0 출력이 '오류 배너 그 자체'인지 볼 때 허용하는 최대 줄 수.
# CLI 오류 배너는 한 줄(실측 2건 모두 1줄)이다. 여러 줄짜리 결과물은 배너가 아니다.
_ERROR_BANNER_MAX_LINES = 3


class LLMError(RuntimeError):
    """Claude CLI 호출 실패 공통 예외."""


class LLMAuthError(LLMError):
    """로그인/토큰 문제. 재시도 무의미 — 사람이 개입해야 한다."""


class LLMLimitError(LLMError):
    """사용량 한도 초과."""


class LLMResponseError(LLMError):
    """CLI 는 정상 종료했으나 응답이 비었거나 형식이 이상하다."""


# --- CLI 가 실제로 뱉는 인증 실패 문구 (실측 2026-09-13, 소문자 비교) ---------
# 실측 1: 계정2(자격증명 없음)  → rc=1, stdout "Not logged in · Please run /login"
# 실측 2: 잘못된 OAuth 토큰      → rc=1, stdout "Failed to authenticate. API Error: 401 OAuth access token is invalid."
_AUTH_MARKERS = (
    "not logged in",
    "please run /login",
    "please run `/login`",
    "run /login",
    "failed to authenticate",
    "authentication_error",
    "invalid authentication credentials",
    "oauth access token is invalid",
    "oauth token has expired",
    "api error: 401",
    "error: 401",
    "invalid api key",
    "invalid bearer token",
    "does not have access to claude",
    "please log in",
)

# 단독으로는 정상 응답에도 나올 수 있는 약한 단서.
# **rc != 0 인 출력에서만** 쓴다. rc==0 응답에 이 단어들을 적용하면
# "Fed's 401(k) rule" 번역 같은 정상 응답을 인증 실패로 둔갑시킨다(2026-09-13 검수 지적).
_AUTH_HINTS = (
    "oauth",
    "unauthorized",
    "authenticate",
    "authentication",
    "credential",
    "token expired",
    "expired token",
    "401",
    "/login",
)

_LIMIT_MARKERS = (
    "you've hit your limit",
    "you have hit your limit",
    "usage limit",
    "rate limit",
    "too many requests",
    "api error: 429",
    "error: 429",
    "quota exceeded",
)

# --- rc == 0 전용: '응답 전체가 오류 배너인가' 판정용 문구 ----------------------
# 부분 문자열 매칭을 쓰지 않는다. 정상 요약·번역 안에 'oauth'·'401'·'authentication'
# 같은 단어가 얼마든지 들어가기 때문이다. 길이 게이트(구 _SHORT_OUTPUT_LIMIT)로는
# 막을 수 없다 — 짧은 정상 응답('2' 같은 것)이 흔하기 때문.
# 실측(2026-09-13) 인증 실패는 둘 다 rc=1 이었으므로 이 경로는 2중 방어다.
_AUTH_BANNER_PREFIXES = (
    "not logged in",
    "please run /login",
    "please run `/login`",
    "please log in",
    "failed to authenticate",
    "authentication failed",
    "invalid api key",
    "invalid bearer token",
    "invalid authentication credentials",
    "oauth access token is invalid",
    "oauth token has expired",
    "api error: 401",
)

# 한 단어짜리는 접두 매칭조차 위험하므로 **완전 일치**만 인정한다.
_AUTH_BANNER_EXACT = ("unauthorized", "authentication_error")

_LIMIT_BANNER_PREFIXES = (
    "you've hit your limit",
    "you have hit your limit",
    "claude usage limit reached",
    "usage limit reached",
    "rate limit exceeded",
    "too many requests",
    "api error: 429",
    "quota exceeded",
)


def _contains(haystack_lower: str, needles) -> str:
    """일치한 마커를 반환(없으면 빈 문자열)."""
    for needle in needles:
        if needle in haystack_lower:
            return needle
    return ""


def _normalize_banner(text: str) -> str:
    """오류 배너 비교용 정규화 — 소문자 + 공백 압축 + 양끝 장식문자 제거."""
    lowered = " ".join(text.lower().split())
    return lowered.strip(" \t.!?·-–—:;*\"'`")


def _classify_failure_text(text: str) -> tuple[str, str]:
    """**rc != 0** 인 출력의 실패 원인을 판정한다. 반환: (kind, 일치마커).

    이미 실패가 확정된 호출이므로 부분 문자열 매칭을 써도 정상 응답을 망칠 수 없다.
    여기서의 오판은 예외 '종류'만 바꿀 뿐 성공/실패 판정을 뒤집지 않는다.
    """
    lowered = text.lower()
    marker = _contains(lowered, _AUTH_MARKERS)
    if marker:
        return "auth", marker
    marker = _contains(lowered, _LIMIT_MARKERS)
    if marker:
        return "limit", marker
    marker = _contains(lowered, _AUTH_HINTS)
    if marker:
        return "auth", marker
    return "", ""


def _classify_success_text(out: str) -> tuple[str, str]:
    """**rc == 0** 인 출력이 응답이 아니라 CLI 오류 배너 자체인지 판정한다.

    판정 기준은 '포함'이 아니라 '출력 전체가 그 문구인가'다:
      1) 의미 있는 줄이 _ERROR_BANNER_MAX_LINES 를 넘으면 배너가 아니다.
      2) 정규화한 출력이 알려진 배너 문구로 **시작**하거나(다중 단어)
         정확히 **일치**할 때만(단일 단어) 실패로 본다.
    """
    lines = [line for line in out.splitlines() if line.strip()]
    if len(lines) > _ERROR_BANNER_MAX_LINES:
        return "", ""
    normalized = _normalize_banner(out)
    if not normalized:
        return "", ""
    for prefix in _AUTH_BANNER_PREFIXES:
        if normalized.startswith(prefix):
            return "auth", prefix
    if normalized in _AUTH_BANNER_EXACT:
        return "auth", normalized
    for prefix in _LIMIT_BANNER_PREFIXES:
        if normalized.startswith(prefix):
            return "limit", prefix
    return "", ""


# --- 계정1: keychain OAuth -----------------------------------------------------

def _keychain_oauth() -> dict:
    """macOS keychain 의 Claude OAuth 레코드를 파싱해 반환(값은 호출부에서만 사용, 로깅 금지)."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 — 원인을 반드시 남긴다(조용한 실패 금지)
        print(f"[_llm_claude] keychain 조회 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return {}
    if r.returncode != 0:
        print(f"[_llm_claude] keychain 조회 rc={r.returncode}: "
              f"{(r.stderr or '').strip()[:150]}", file=sys.stderr)
        return {}
    raw = (r.stdout or "").strip()
    if not raw:
        print("[_llm_claude] keychain 레코드가 비어 있음", file=sys.stderr)
        return {}
    try:
        oauth = _json.loads(raw).get("claudeAiOauth", {})
    except Exception as exc:  # noqa: BLE001
        print(f"[_llm_claude] keychain 레코드 JSON 파싱 실패: {type(exc).__name__}", file=sys.stderr)
        return {}
    return oauth if isinstance(oauth, dict) else {}


def _expiry_epoch(oauth: dict) -> float | None:
    """expiresAt(ms) → epoch 초. 없으면 None. **토큰 값은 절대 다루지 않는다.**"""
    value = oauth.get("expiresAt")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) / 1000.0


def _fmt_kst(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S KST", time.localtime(epoch))


def token_expiry_epoch() -> float | None:
    """계정1 OAuth 액세스 토큰 만료 시각(epoch 초). 읽을 수 없으면 None.

    토큰 값 자체는 반환하지도 로깅하지도 않는다.
    """
    return _expiry_epoch(_keychain_oauth())


def _warn_if_expiring(oauth: dict) -> None:
    """만료가 임박/경과했으면 stderr 경고. 만료 '시각'만 출력한다(토큰 값 금지)."""
    expiry = _expiry_epoch(oauth)
    if expiry is None:
        return
    remain = expiry - time.time()
    if remain <= 0:
        print(f"[_llm_claude] 경고: 계정1 OAuth 토큰이 이미 만료됨 "
              f"(만료 {_fmt_kst(expiry)}). 대화형 세션으로 갱신 필요.", file=sys.stderr)
    elif remain <= TOKEN_WARN_SECONDS:
        print(f"[_llm_claude] 경고: 계정1 OAuth 토큰 만료 임박 "
              f"({int(remain // 60)}분 남음, 만료 {_fmt_kst(expiry)}).", file=sys.stderr)


def _keychain_token() -> str:
    """keychain 에서 accessToken 만 꺼내 반환(값 노출 없음). 실패 시 빈 문자열."""
    return _keychain_oauth().get("accessToken", "") or ""


def _long_lived_token() -> str:
    """장기 토큰을 환경변수 → `LONG_TOKEN_ENV_FILE` 순으로 찾는다. 없으면 빈 문자열(값 로깅 금지).

    python-dotenv 에 의존하지 않고 `KEY=value` 한 줄만 직접 읽는다(다른 키는 건드리지 않는다).
    """
    value = os.environ.get(LONG_TOKEN_VAR, "").strip()
    if value:
        return value
    try:
        lines = LONG_TOKEN_ENV_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    except Exception as exc:  # noqa: BLE001 — 원인은 남기되 keychain 경로로 계속 간다
        print(f"[_llm_claude] 장기 토큰 파일 읽기 실패: {type(exc).__name__}", file=sys.stderr)
        return ""
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        if sep and key.strip() == LONG_TOKEN_VAR:
            return val.strip().strip('"').strip("'")
    return ""


def _uses_long_token() -> bool:
    """이번 계정1 호출이 장기 토큰을 쓰는가(호출부가 토큰을 직접 넣었으면 그게 우선)."""
    return not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and bool(_long_lived_token())


# --- 계정2: 실제 사용 가능 여부 판정 -------------------------------------------

def account2_status() -> tuple[bool, str]:
    """계정2를 실제로 쓸 수 있는지 판정한다. 반환: (사용가능, 사유).

    '디렉토리 존재'만으로 폴백하면 로그의 "계정2 폴백"이 거짓말이 된다(09-11·09-12 실측).
    자격증명 파일과 accessToken, 만료 시각까지 확인한다.
    """
    if not ACCOUNT2_DIR.exists():
        return False, f"설정 디렉토리 없음({ACCOUNT2_DIR})"
    cred = ACCOUNT2_DIR / ".credentials.json"
    if not cred.is_file():
        return False, f"자격증명 파일 없음({cred.name}) — 계정2는 로그인되어 있지 않다"
    try:
        data = _json.loads(cred.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"자격증명 파일 파싱 실패({type(exc).__name__})"
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return False, "자격증명에 accessToken 없음"
    expiry = _expiry_epoch(oauth)
    if expiry is not None and expiry <= time.time():
        return False, f"자격증명 토큰 만료됨(만료 {_fmt_kst(expiry)})"
    return True, ""


def _run(prompt: str, system: str, model: str, timeout: int, account2: bool,
         use_long: bool = True) -> str:
    """CLI 를 한 번 호출한다. 실패는 예외로만 알린다(오류 문구를 응답으로 반환하지 않는다).

    계정1 토큰 우선순위: 호출부가 넣은 CLAUDE_CODE_OAUTH_TOKEN → 장기 토큰(use_long) → keychain.
    """
    account_label = "계정2" if account2 else "계정1"
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)  # OAuth 인증 방해 방지
    env.pop(LONG_TOKEN_VAR, None)       # 원본 변수는 자식에게 넘기지 않는다(필요한 값만 아래에서 주입)
    if account2:
        usable, reason = account2_status()
        if not usable:
            raise LLMAuthError(f"계정2 사용 불가: {reason}")
        env["CLAUDE_CONFIG_DIR"] = str(ACCOUNT2_DIR)
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)  # 계정2는 자체 자격증명 사용
    else:
        token = env.get("CLAUDE_CODE_OAUTH_TOKEN")
        if not token and use_long:
            token = _long_lived_token()
            if token:
                account_label = "계정1(장기 토큰)"
        if not token:
            oauth = _keychain_oauth()
            _warn_if_expiring(oauth)
            token = oauth.get("accessToken", "") or ""
            account_label = "계정1(keychain)"
        if not token:
            raise LLMAuthError("Claude OAuth 토큰을 keychain 에서 읽지 못함(잠김/미로그인?) — "
                               f"장기 토큰({LONG_TOKEN_VAR})도 없음")
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    base_sys = ("너는 정확하고 조용한 어시스턴트다. 사용자가 요구한 결과물만 출력하고, "
                "서두·인사·해설·메타 발언('~하겠습니다', '~해드리겠습니다' 등)은 절대 쓰지 않는다.")
    if system.strip():
        base_sys += "\n\n" + system.strip()
    # ── 격리: 개인 메모리·프로젝트 문서가 프롬프트로 유입되는 것을 막는다 ─────────
    # 구 주석은 "--system-prompt 가 CLAUDE.md 자동탐색·auto-memory 로딩을 끈다"고 단언했으나
    # **거짓이었다**(2026-09-13 실측, CLI 2.1.269). 같은 프로브 질의로 인자셋만 바꿔 대조:
    #   (a) 구 인자셋 + 프로젝트 cwd → 입력 13,510토큰, 전역 개인문서 2종 + cwd 의 프로젝트
    #       CLAUDE.md 까지 유입(프로브 YES)
    #   (b) 구 인자셋 + 중립 cwd     → 3,017토큰, 전역 개인문서는 **여전히** 유입
    #   (c) --safe-mode + 중립 cwd   →   861토큰, 프로브 전부 NO
    # --safe-mode 가 CLAUDE.md·스킬·훅·플러그인·커스텀 설정을 전부 끈다(인증·모델 선택은 정상).
    # --bare 는 ANTHROPIC_API_KEY 를 강제하므로 헌법 위반 → 사용 금지.
    # safe-mode 만으로는 **작업디렉터리 경로**가 환경정보로 남는다(실측: 프로젝트 cwd 에서
    # 프로젝트명 프로브가 YES). 그래서 중립 임시 디렉터리에서 실행한다.
    # 도구를 전부 닫았으므로(--tools "") cwd 가 어디든 응답 품질에는 영향이 없다.
    # MCP 는 safe-mode 와 별개로 --strict-mcp-config + 빈 설정으로 2중 차단한다.
    #
    # 제거한 인자(둘 다 격리처럼 보였지만 실제 효과가 없었다):
    #   --exclude-dynamic-system-prompt-sections → --system-prompt 를 주면 **무시된다**(--help 명시)
    #   --dangerously-skip-permissions           → --tools "" 로 도구가 없어 승인받을 동작 자체가 없다
    #
    # **격리되지 않고 남는 것**(다음 사람이 또 속지 않도록 명시):
    #   - 관리자(policy) 설정은 safe-mode 로도 꺼지지 않는다(--help 명시).
    #     이 기기엔 managed-settings.json 이 없어 현재 유입원이 아니다(2026-09-13 확인).
    #   - 환경변수는 그대로 자식에게 전달된다(OAuth 토큰 주입에 필요). ANTHROPIC_API_KEY 만 제거한다.
    #   - 호출부가 prompt/system 으로 직접 넘기는 내용은 당연히 그대로 전달된다.
    cmd = [
        CLAUDE_BIN, "--print", "--output-format", "text",
        "--safe-mode",
        "--system-prompt", base_sys,
        "--tools", "", "--no-session-persistence",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--model", model,
    ]
    # 중립 작업디렉터리(프로젝트 CLAUDE.md 도, 사용자 경로 정보도 없는 곳)에서 실행한다.
    with tempfile.TemporaryDirectory(prefix="llm-claude-") as workdir:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=timeout, env=env, cwd=workdir)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()

    # 1) rc != 0 이면 stdout 에 무엇이 있든 절대 성공이 아니다.
    #    (구버전은 stdout 이 비어있을 때만 예외를 올려 "Not logged in..." 을 응답으로 반환했다.)
    if r.returncode != 0:
        combined = f"{out}\n{err}".strip()
        kind, marker = _classify_failure_text(combined)
        detail = (combined or "(출력 없음)")[:300]
        if kind == "auth":
            raise LLMAuthError(f"{account_label} 인증 실패(rc={r.returncode}, 마커='{marker}'): {detail}")
        if kind == "limit":
            raise LLMLimitError(f"{account_label} 사용량 한도(rc={r.returncode}, 마커='{marker}'): {detail}")
        raise LLMError(f"{account_label} claude 비정상 종료(rc={r.returncode}): {detail}")

    # 2) rc == 0 인데 빈 응답도 성공이 아니다.
    if not out:
        raise LLMResponseError(
            f"{account_label} 빈 응답(rc=0){': ' + err[:200] if err else ''}")

    # 3) rc == 0 이어도 본문이 인증/한도 오류 문구면 실패다.
    kind, marker = _classify_success_text(out)
    if kind == "auth":
        raise LLMAuthError(f"{account_label} 인증 실패(rc=0, 마커='{marker}'): {out[:200]}")
    if kind == "limit":
        raise LLMLimitError(f"{account_label} 사용량 한도(rc=0, 마커='{marker}'): {out[:200]}")

    return out


def call(prompt: str, system: str = "", model: str | None = None, timeout: int = 120) -> str:
    """Claude 에 프롬프트를 보내 텍스트 응답을 받는다. 계정1 실패 시 계정2 폴백.

    계정2가 실제로 로그인돼 있지 않으면 폴백하지 않고, 계정1 원인 + 폴백 불가 사유를
    함께 담은 예외를 올린다("폴백했다"는 거짓 로그를 남기지 않는다).
    """
    model = model or DEFAULT_MODEL
    try:
        try:
            return _run(prompt, system, model, timeout, account2=False)
        except LLMAuthError as exc_long:
            # 장기 토큰이 폐기·만료됐어도 keychain 이 살아 있으면 작업은 계속돼야 한다.
            if not _uses_long_token():
                raise
            print(f"[_llm_claude] 장기 토큰 인증 실패 → keychain 토큰으로 재시도: {exc_long}",
                  file=sys.stderr)
            try:
                return _run(prompt, system, model, timeout, account2=False, use_long=False)
            except LLMError as exc_kc:
                raise type(exc_kc)(f"장기 토큰: {exc_long} / keychain: {exc_kc}") from exc_kc
    except LLMError as exc:
        usable, reason = account2_status()
        if not usable:
            print(f"[_llm_claude] 계정1 실패, 계정2 폴백 불가({reason})", file=sys.stderr)
            raise type(exc)(f"{exc} / 계정2 폴백 불가: {reason}") from exc
        print(f"[_llm_claude] 계정1 실패 → 계정2 폴백 시도: {exc}", file=sys.stderr)
        try:
            return _run(prompt, system, model, timeout, account2=True)
        except LLMError as exc2:
            raise type(exc2)(f"계정1: {exc} / 계정2: {exc2}") from exc2


if __name__ == "__main__":  # 스모크 테스트
    try:
        print(call(sys.argv[1] if len(sys.argv) > 1 else "한국어로 'hello' 를 번역. 단어만."))
    except LLMError as exc:
        print(f"[_llm_claude] 호출 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
