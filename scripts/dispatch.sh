#!/usr/bin/env bash
# divvy 위임 실행 — 배정된 한 작업을 Codex(헤드리스)에 던지고 산출물을 회수한다.
# 과금은 양쪽 20x 정액이라 비용이 아니라 적성·독립성·유휴가 기준이다(ROSTER T-10).
#
# 사용: dispatch.sh <브리프파일> <출력파일> [작업디렉터리]
#   <브리프파일>  Codex에게 줄 프롬프트 전문(비어 있으면 거부 — 빈 입력 금지).
#   <출력파일>    Codex의 최종 메시지를 받을 파일. 기존 파일이 있으면 거부(FORCE=1로 우회).
#                 이름이 .log/.err/.forcebak/.lock 으로 끝나면 거부한다(대소문자 무관) — 그건
#                 사이드카 이름이고, 다른 실행의 사이드카와 같은 파일이 되어 서로를 덮을 수 있다.
#   [작업디렉터리] Codex의 작업 루트(기본: 현재 디렉터리).
#
# ★ 경로 계약: <브리프파일>·<출력파일>은 **이 스크립트를 호출한 PWD** 기준으로 절대화된다.
#   그러나 **브리프 본문에 적는 상대경로는 <작업디렉터리> 기준**으로 Codex가 해석한다(--cd).
#   둘이 다르면 브리프의 상대경로가 다른 파일을 가리킨다. 브리프에는 작업루트 기준 상대경로만 쓰라.
#
# 환경변수:
#   CODEX_EFFORT   추론 effort (기본 medium — 레포 실측상 소·중형은 medium=high Δ=0.
#                  대형·복잡 건만 high. 올려서 capability를 사지는 못한다)
#   CODEX_MODEL    모델 override (기본: codex config의 model)
#   CODEX_SANDBOX  read-only(기본) | workspace-write
#                  `danger-full-access`는 **지원하지 않는다**(rc 2) — 샌드박스가 없으면 위임 작업이
#                  잠금·백업을 어디서든 지울 수 있어 회수 무결성을 보장할 수 없다.
#   DIVVY_WRITE_APPROVED=1
#                  read-only가 아닌 샌드박스로 **실제 실행**할 때 필수. 없으면 거부한다(rc 5).
#                  기본 경로는 "Codex가 diff를 제안 → 사람이 검토 → 적용"이다.
#   DIVVY_SIG_GRACE  중단 시 Codex 프로세스 그룹이 스스로 끝나기를 기다리는 초(기본 5).
#                  넘기면 그룹에 KILL을 보낸다(자손이 승인된 쓰기를 계속하지 못하게).
#                  ⚠️ **경계**: 신호는 Codex의 **프로세스 그룹까지만** 닿는다. 자손이 `setsid`로
#                  새 세션·그룹을 만들어 이탈하면 이 스크립트로는 종료시킬 수 없다(bash·macOS에
#                  cgroup 류의 작업 containment가 없다). 그런 프로세스가 남을 수 있는 작업은
#                  read-only로 돌리거나, 위임 브리프에서 detached 백그라운드 실행을 금지하라.
#   DIVVY_ISOLATE_CONFIG=1
#                  `--ignore-user-config`를 붙여 Codex의 `~/.codex/config.toml`(훅·플러그인·프로필)을
#                  **로드하지 않고** 돌린다. 인증은 그대로 쓴다.
#                  기본은 `--profile headless`이며, 이 값은 프로필 자체가 실패할 때만 쓰는 비상 폴백이다.
#   DIVVY_PROBE_TIMEOUT  `codex exec --help` 탐지에 허용할 초(기본 15). 넘기면 탐지를 포기하고
#                  stdout fallback으로 내려간다(경고 출력).
#   FORCE=1        기존 출력파일·로그 덮어쓰기 허용(출력은 먼저 치워두고, 성공 시에만 삭제)
#   DRY_RUN=1      codex를 호출하지 않고 실행 계획만 출력. 잠금·백업·임시파일 같은 변경을
#                  하기 **전에** 끝난다(검사는 하되 아무것도 바꾸지 않는다).
#
# ⚠️ rc 0 = "돌았고 비어 있지 않으며 선언된 최소 산출 계약을 충족했다"까지다.
#   산출물이 요청에 **답했는지**는 기계가 완전히 판정할 수 없다 —
#   회수한 쪽이 읽어야 한다(실측: Codex가 자기 환경 훅 때문에 '작업 불가' 한 줄을 rc 0으로 회수).
#
# 종료코드: 0 성공 · 1 입력/경로 없음·빈 브리프 · 2 환경변수 값 오류 · 3 기존 산출물(clobber)
#           4 실행 실패(회수 실패) · 5 write 샌드박스 승인 없음 · 6 동일 출력 동시 실행
#           7 경로 충돌·관리 이름 오용 · 8 정리(백업 복구·삭제·잠금 해제) 실패 —
#             **다른 실패보다 우선해 보고한다**(원래 rc는 메시지에 남긴다). 남은 상태를 사람이 봐야 한다.
#           9 브리프 산출 계약 오류·산출 계약 미충족
set -uo pipefail

# 도움말은 위 주석 블록만 출력한다(셸 코드가 도움말에 새지 않게).
usage() { awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; exit "${1:-0}"; }
{ [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; } && usage 0

BRIEF="${1:?브리프파일 경로 필요 (-h로 도움말)}"
OUT="${2:?출력파일 경로 필요 (-h로 도움말)}"
WORKDIR="${3:-$PWD}"
EFFORT="${CODEX_EFFORT:-medium}"
SANDBOX="${CODEX_SANDBOX:-read-only}"
DRY_RUN="${DRY_RUN:-0}"
FORCE="${FORCE:-0}"
WRITE_OK="${DIVVY_WRITE_APPROVED:-0}"
SIG_GRACE="${DIVVY_SIG_GRACE:-5}"
PROBE_TIMEOUT="${DIVVY_PROBE_TIMEOUT:-15}"
ISOLATE="${DIVVY_ISOLATE_CONFIG:-0}"

case "$SANDBOX" in
  read-only|workspace-write) ;;
  danger-full-access)
    # divvy는 이 모드를 지원하지 않는다. 샌드박스가 없으면 위임된 작업이 잠금·백업을
    # (작업루트 밖이라도) 지울 수 있어, 이 스크립트가 자기 장부를 지킬 수단이 없다.
    echo "CODEX_SANDBOX=danger-full-access 는 divvy가 지원하지 않는다." >&2
    echo "  이 모드에서는 위임 작업이 잠금·백업을 어디서든 지울 수 있어 회수 무결성을 보장할 수 없다." >&2
    echo "  read-only(기본) 또는 workspace-write 를 쓰라. 정말 필요하면 codex를 직접 호출하라." >&2
    exit 2;;
  *) echo "CODEX_SANDBOX 값 오류: '$SANDBOX' (read-only|workspace-write)" >&2; exit 2;;
esac
case "$EFFORT" in
  ''|*[!a-z]*) echo "CODEX_EFFORT 값 오류: '$EFFORT' (소문자 영문만)" >&2; exit 2;;
esac
for v in SIG_GRACE PROBE_TIMEOUT; do
  eval "val=\$$v"
  case "$val" in
    ''|*[!0-9]*) echo "$v 값 오류: '$val' (0 이상 정수)" >&2; exit 2;;
  esac
done

[ -f "$BRIEF" ] || { echo "브리프파일 없음: $BRIEF" >&2; exit 1; }
grep -q '[^[:space:]]' "$BRIEF" 2>/dev/null || { echo "브리프가 빔: $BRIEF (빈 입력 금지)" >&2; exit 1; }
[ -d "$WORKDIR" ] || { echo "작업디렉터리 없음: $WORKDIR" >&2; exit 1; }

# 경로 정규화 — 부모 디렉터리를 실체 경로로 접는다(심링크·`..`·중복 슬래시).
# 마지막 성분은 여기서 풀지 않고, 아래에서 "관리 경로는 심링크 금지"로 처리한다.
norm_dir() { (cd "$1" 2>/dev/null && pwd -P) || return 1; }
norm_file() {
  local d b
  d="$(dirname -- "$1")"; b="$(basename -- "$1")"
  d="$(norm_dir "$d")" || return 1
  printf '%s/%s' "$d" "$b"
}

# 브리프의 최소 산출 계약을 한 줄씩 출력한다. 마커 후보(`<!-- DIVVY_EXPECT`)는
# 정확한 콜론과 같은 행의 닫힘을 갖춰야 하며, 하나의 행에 복수 마커도 허용한다.
parse_expectations() {
  awk '
  {
    line = $0
    while ((start = index(line, "<!-- DIVVY_EXPECT")) != 0) {
      marker = substr(line, start)
      prefix = "<!-- DIVVY_EXPECT:"
      if (substr(marker, 1, length(prefix)) != prefix) exit 1
      marker = substr(marker, length(prefix) + 1)
      endpos = index(marker, "-->")
      if (endpos == 0) exit 1
      value = substr(marker, 1, endpos - 1)
      sub(/^[[:space:]]+/, "", value)
      sub(/[[:space:]]+$/, "", value)
      if (value == "") exit 1
      print value
      line = substr(marker, endpos + 3)
    }
  }' "$1"
}

BRIEF_IN="$BRIEF"; OUT_IN="$OUT"
BRIEF="$(norm_file "$BRIEF")" || { echo "브리프 경로 해석 실패: $BRIEF_IN" >&2; exit 1; }
OUT="$(norm_file "$OUT")" || { echo "출력 디렉터리 없음: $(dirname -- "$OUT_IN")" >&2; exit 1; }
WORKDIR="$(norm_dir "$WORKDIR")" || { echo "작업디렉터리 해석 실패" >&2; exit 1; }

# 계약 문법 오류는 Codex를 부르기 전에 fail-closed한다. 마커가 없는 브리프는 기존 동작 그대로다.
if grep -Fq '<!-- DIVVY_EXPECT' "$BRIEF" 2>/dev/null; then
  # ★ iconv 출력을 /dev/null로 **직접** 보내지 않는다 — macOS iconv는 비ASCII 내용을 문자 장치로
  #   변환할 때 `Inappropriate ioctl for device`로 rc 1을 내서 정상 UTF-8 브리프를 거짓 거부한다
  #   (2026-08-01 실측, 결정론적 5/5). 파이프로 받으면 rc 0이고, 무효 UTF-8은 그대로 rc 1이다.
  #   `set -o pipefail`이 있어야 iconv의 rc가 파이프라인 rc로 살아난다(위 `set -uo pipefail`).
  if ! command -v iconv >/dev/null 2>&1 || ! iconv -f UTF-8 -t UTF-8 "$BRIEF" 2>/dev/null | cat >/dev/null; then
    echo "브리프 산출 계약 오류: DIVVY_EXPECT 마커가 있는 브리프는 UTF-8이어야 한다." >&2
    exit 9
  fi
  if ! parse_expectations "$BRIEF" >/dev/null; then
    echo "브리프 산출 계약 오류: DIVVY_EXPECT는 '<!-- DIVVY_EXPECT: 문자열 -->' 단일 행이어야 한다." >&2
    exit 9
  fi
fi
EXPECTATIONS=()
while IFS= read -r expected; do EXPECTATIONS+=("$expected"); done < <(parse_expectations "$BRIEF")

# 출력 이름이 사이드카 접미사로 끝나면 다른 실행의 사이드카와 같은 파일이 될 수 있다.
# (실행 A가 OUT=x면 x.log를 쓴다. 실행 B가 OUT=x.log면 둘이 같은 파일을 서로의 산출물로 본다.)
# ★ 대소문자 무관으로 본다 — macOS 기본 파일시스템은 대소문자를 구분하지 않으므로 `x.LOG`는
#   `x.log`와 같은 파일이지만 잠금 이름은 달라져 경쟁을 못 막는다.
OUT_LOWER="$(printf '%s' "$OUT" | tr '[:upper:]' '[:lower:]')"
case "$OUT_LOWER" in
  *.log|*.err|*.forcebak|*.lock)
    echo "출력 이름 오용: $OUT" >&2
    echo "  .log/.err/.forcebak/.lock(대소문자 무관)로 끝나는 이름은 사이드카 전용이다 — 다른 이름을 쓰라." >&2
    exit 7;;
esac

LOG="$OUT.log"; ERR="$OUT.err"; BAKPATH="$OUT.forcebak"; LOCK="$OUT.lock"
MANAGED=("$OUT" "$LOG" "$ERR" "$BAKPATH" "$LOCK")

# 관리 경로가 심링크면 거부한다. 마지막 성분 심링크는 정규화로 접히지 않으므로 alias 검사를
# 통과한 뒤 리다이렉션이 엉뚱한 파일(또는 dangling 대상)을 만들거나 truncate할 수 있다.
for p in "${MANAGED[@]}"; do
  if [ -L "$p" ]; then
    echo "관리 경로가 심링크: $p — 실체 경로를 직접 지정하라(입력 파괴 방지)." >&2
    exit 7
  fi
done

# 브리프가 관리 경로와 같은 파일이면 입력이 파괴된다(문자열 동일 + 하드링크/실체 동일 모두).
for p in "${MANAGED[@]}"; do
  if [ "$p" = "$BRIEF" ] || { [ -e "$p" ] && [ -e "$BRIEF" ] && [ "$p" -ef "$BRIEF" ]; }; then
    echo "경로 충돌: 브리프와 산출물(또는 사이드카)이 같은 파일 — $BRIEF" >&2
    echo "  브리프는 출력 경로와 겹치지 않는 곳에 두라(출력 $OUT, 사이드카 .log/.err/.forcebak/.lock)." >&2
    exit 7
  fi
done

# 관리 경로끼리도 서로 같은 실체이면(하드링크·대소문자 비구분 FS 등) 백업·로그가 서로를 truncate한다.
n=${#MANAGED[@]}
i=0
while [ "$i" -lt "$n" ]; do
  j=$((i + 1))
  while [ "$j" -lt "$n" ]; do
    a="${MANAGED[$i]}"; b="${MANAGED[$j]}"
    if [ -e "$a" ] && [ -e "$b" ] && [ "$a" -ef "$b" ]; then
      echo "경로 충돌: 관리 경로 둘이 같은 실체 — $a ↔ $b" >&2
      exit 7
    fi
    j=$((j + 1))
  done
  i=$((i + 1))
done

# read-only가 아닌 샌드박스로 **실제** 돌리려면 명시 승인이 필요하다.
# prose 규칙만으로는 상속된 환경변수 하나로 쓰기 권한이 켜지는 것을 막을 수 없다.
if [ "$SANDBOX" != "read-only" ]; then
  echo "⚠️ 샌드박스가 read-only가 아니다: $SANDBOX — Codex가 파일을 쓸 수 있다." >&2
  if [ "$DRY_RUN" != "1" ] && [ "$WRITE_OK" != "1" ]; then
    echo "거부: 실제 실행에는 DIVVY_WRITE_APPROVED=1 이 필요하다(사람의 명시 승인)." >&2
    echo "  기본 경로는 read-only + diff 제안 → 사람이 적용이다." >&2
    exit 5
  fi
  # ★ 승인이 있어도, 출력·사이드카가 작업루트 안에 있으면 위임된 작업이 잠금·백업을 지울 수 있다
  #   (`git clean -fdx` 한 번으로). 그러면 다른 실행이 같은 출력을 예약하고, 우리 정리가 남의
  #   잠금을 지우며, FORCE 백업이 사라진 뒤 실패하면 이전 회수분도 복구되지 않는다.
  case "$OUT/" in
    "$WORKDIR"/*)
      echo "거부: 쓰기 샌드박스($SANDBOX)에서는 출력 경로를 작업루트 안에 둘 수 없다." >&2
      echo "  출력: $OUT" >&2
      echo "  작업루트: $WORKDIR" >&2
      echo "  위임된 작업이 잠금($LOCK)·백업($BAKPATH)을 지울 수 있다 — 작업루트 밖으로 옮겨라." >&2
      exit 7;;
  esac
fi

COMMITTED=0; FINALIZED=0; BAK=""; BAK_MOVED=0; PROMPT_TMP=""
LOCK_HELD=0; LOCK_TOKEN=""; OUT_PREEXISTED=0; RUN_STARTED=0; CODEX_PID=""; CLEAN_FAIL=0

cleanup() {
  local rc=$?
  # ★ OUT을 만지기 **전에** 잠금 소유권을 판정한다. 잠금이 남의 것으로 바뀌었다면 다른 실행이
  #   같은 OUT에 쓰고 있을 수 있고, 우리 "정리"가 그쪽 산출물을 훼손한다.
  local own="none"
  if [ "$LOCK_HELD" = "1" ]; then
    if [ ! -d "$LOCK" ]; then own="gone"
    elif [ "$(cat "$LOCK/owner" 2>/dev/null)" != "$LOCK_TOKEN" ]; then own="other"
    else own="mine"; fi
  fi
  if [ "$own" = "gone" ] || [ "$own" = "other" ]; then
    CLEAN_FAIL=1
    echo "  ✗ 잠금이 우리 것이 아니다($own) — $OUT 을 건드리지 않는다(다른 실행이 쓰고 있을 수 있다)." >&2
    [ -n "$BAK" ] && [ -e "$BAK" ] && echo "  ! 백업이 남아 있다(사람이 판단): $BAK" >&2
    [ -n "$PROMPT_TMP" ] && rm -f "$PROMPT_TMP"
    echo "정리 실패(rc 8) — 원래 종료코드 $rc. 남은 상태를 확인하라." >&2
    exit 8
  fi
  # 커밋 전에 죽으면(신호·오류 포함) 이번 실행이 만든 것을 치우고 이전 회수분을 되돌린다.
  if [ "$COMMITTED" != "1" ]; then
    # OUT 자리의 **일반 파일**은 이번 실행이 만든 것이다: 백업을 떠냈으면(BAK_MOVED) 원본은
    # 이미 옆으로 옮겨졌고, 애초에 없었으면(OUT_PREEXISTED != 1) 원본이 없었다. 둘 중 하나면
    # 지워야 복구가 진행된다 — 안 지우면 아래 "예상 밖 항목" 가지에 걸려 백업 복구가 막힌다.
    if [ "$RUN_STARTED" = "1" ] && [ -e "$OUT" ] \
       && { [ "$OUT_PREEXISTED" != "1" ] || [ "$BAK_MOVED" = "1" ]; }; then
      if [ -f "$OUT" ]; then
        rm -f "$OUT" && echo "  (이번 실행이 만든 불완전 산출물 제거: $OUT)" >&2
        # 삭제 실패도 조용히 넘기지 않는다 — 다음 실행이 이 잔여물에 막힌다.
        [ -e "$OUT" ] && { CLEAN_FAIL=1; echo "  ✗ 불완전 산출물 제거 실패: $OUT" >&2; }
      else
        # 일반 파일이 아니다(디렉터리 등). 자동으로 지우지 않는다 — 안에 무엇이 있는지 모른다.
        # 그냥 rc4로 끝내면 이후 실행이 FORCE를 줘도 rc3에 막히므로 사람에게 알린다.
        CLEAN_FAIL=1
        echo "  ✗ 실행이 $OUT 자리에 일반 파일이 아닌 항목을 남겼다 — 자동 제거하지 않는다." >&2
      fi
    fi
    if [ -n "$BAK" ] && [ -e "$BAK" ]; then
      # ★ 복구 전에 OUT 자리가 비어 있어야 한다. OUT이 디렉터리(또는 일반 파일이 아닌 것)로
      #   남아 있으면 `mv`는 백업을 그 안으로 넣고 성공을 반환해, 복구했다고 보고하면서
      #   실제로는 복구하지 않는다.
      if [ -e "$OUT" ]; then
        CLEAN_FAIL=1
        echo "  ✗ 복구 불가 — $OUT 자리에 일반 파일이 아닌 항목이 있다. 백업은 보존: $BAK" >&2
      elif mv -f "$BAK" "$OUT" 2>/dev/null && [ -f "$OUT" ]; then
        echo "  (이전 회수분 복구: $OUT)" >&2
      else
        CLEAN_FAIL=1
        echo "  ✗ 이전 회수분 복구 실패 — 백업 또는 잔여물을 확인하라: $BAK / $OUT" >&2
      fi
    fi
  fi
  [ -n "$PROMPT_TMP" ] && rm -f "$PROMPT_TMP"
  if [ "$own" = "mine" ]; then
    rm -f "$LOCK/owner"
    rmdir "$LOCK" 2>/dev/null || { CLEAN_FAIL=1; echo "  ✗ 잠금 해제 실패: $LOCK" >&2; }
  fi
  # 성공 경로에서 백업 삭제까지 끝나지 않았으면 성공으로 반올림하지 않는다.
  if [ "$COMMITTED" = "1" ] && [ "$FINALIZED" != "1" ]; then CLEAN_FAIL=1; fi
  # ★ 정리 실패는 원래 rc와 무관하게 rc 8로 보고한다. 실행 실패(4)에 가려지면 남은 상태를
  #   아무도 보지 않는다 — 원래 rc는 메시지에 남긴다.
  if [ "$CLEAN_FAIL" = "1" ]; then
    echo "정리 실패(rc 8) — 원래 종료코드 $rc. 산출물은 유효할 수 있으나 남은 상태를 확인하라." >&2
    exit 8
  fi
  return 0
}
trap cleanup EXIT

# 프로세스 그룹이 비워질 때까지 최대 $2초 기다린다(0=비었음, 1=아직 남아 있음).
# ★ 직속 PID로 기다리면 안 된다: 리더가 먼저 죽고 자손이 정리 중일 때 즉시 반환해 유예를
#   지키지 않고 KILL을 보내게 되고, 자손의 정상 정리가 끊겨 부분 상태가 남는다.
wait_group_gone() {
  local i=0
  while kill -0 -"$1" 2>/dev/null; do
    [ "$i" -ge "$2" ] && return 1
    sleep 1
    i=$((i + 1))
  done
  return 0
}

# 신호는 foreground child에 자동 전달되지 않는다. 래퍼만 죽이면 Codex(그리고 승인된 쓰기)가
# 계속 돌 수 있으므로 **프로세스 그룹 전체**에 전달한다. Codex가 만든 자손까지 닿게 하고,
# 응답하지 않으면 유예 후 KILL로 올린다(자손의 지연 쓰기 차단).
forward_signal() {
  local sig="$1" i=0
  [ -z "$CODEX_PID" ] && return 0
  kill -"$sig" -"$CODEX_PID" 2>/dev/null || kill -"$sig" "$CODEX_PID" 2>/dev/null
  # ★ 유예도 승격도 **그룹 기준**이다. 그룹이 유예 안에 비면 자손의 정상 정리를 끊지 않는다.
  #   비지 않으면 KILL로 올린다 — 회수(`wait`)보다 **앞에** 둬야 신호를 무시하는 직속 child에서
  #   무기한 멈추지 않는다.
  if ! wait_group_gone "$CODEX_PID" "$SIG_GRACE"; then
    echo "  ! 프로세스 그룹이 ${SIG_GRACE}초 안에 끝나지 않음($sig 무시) — 그룹에 KILL" >&2
    kill -KILL -"$CODEX_PID" 2>/dev/null
    while kill -0 -"$CODEX_PID" 2>/dev/null; do
      if [ "$i" -ge 3 ]; then
        echo "  ✗ 프로세스 그룹 종료 확인 실패(PGID $CODEX_PID)" >&2
        break
      fi
      sleep 1
      i=$((i + 1))
    done
  fi
  wait "$CODEX_PID" 2>/dev/null
  return 0
}
trap 'echo "중단됨(INT) — Codex 프로세스 그룹에 전달" >&2; forward_signal INT; exit 130' INT
trap 'echo "중단됨(TERM) — Codex 프로세스 그룹에 전달" >&2; forward_signal TERM; exit 143' TERM

# 기존 산출물 검사 — 여기까지는 **읽기만** 한다(dry-run도 같은 판정을 받는다).
if [ -e "$OUT" ]; then
  OUT_PREEXISTED=1
  if [ "$FORCE" != "1" ]; then echo "이미 존재: $OUT (FORCE=1 또는 다른 경로)" >&2; exit 3; fi
  [ -f "$OUT" ] || { echo "기존 출력이 일반 파일이 아님: $OUT — 자동 제거하지 않고 중단." >&2; exit 3; }
elif [ -e "$BAKPATH" ]; then
  # 출력은 없는데 백업만 남았다 = 이전 실행이 복구를 못 마쳤다. 조용히 진행하면 그 증거가 지워진다.
  echo "고립된 백업 발견: $BAKPATH (출력 $OUT 은 없음)" >&2
  echo "  이전 실행의 복구가 끝나지 않았다. 사람이 복구하거나 지운 뒤 다시 실행하라." >&2
  exit 3
fi
for p in "$LOG" "$ERR"; do
  if [ -e "$p" ] && [ "$FORCE" != "1" ]; then
    echo "이미 존재: $p (FORCE=1 또는 다른 출력 경로)" >&2; exit 3
  fi
done

MODEL_ARG=(); MODEL_DESC=""
if [ -n "${CODEX_MODEL:-}" ]; then MODEL_ARG=(-m "$CODEX_MODEL"); MODEL_DESC=" -m $CODEX_MODEL"; fi
CONFIG_ARG=(--profile headless); CONFIG_DESC=" --profile headless"
if [ "$ISOLATE" = "1" ]; then
  CONFIG_ARG=(--ignore-user-config); CONFIG_DESC=" --ignore-user-config"
fi

# ── dry-run은 여기서 끝난다: 잠금·백업·임시파일 등 어떤 상태 변경도 하기 **전** ──────────
if [ "$DRY_RUN" = "1" ]; then
  echo "[dry] codex exec --skip-git-repo-check${CONFIG_DESC} --cd '$WORKDIR' --sandbox $SANDBOX -c model_reasoning_effort=$EFFORT${MODEL_DESC}"
  echo "[dry] 브리프: $BRIEF ($(wc -c <"$BRIEF" | tr -d ' ')바이트) + 회수 규약 → stdin"
  echo "[dry] 출력: $OUT"
  echo "[dry] 브리프 내 상대경로 기준(작업루트): $WORKDIR"
  [ "$SANDBOX" != "read-only" ] && echo "[dry] ⚠️ 샌드박스가 read-only가 아니다 — 실제 실행에는 DIVVY_WRITE_APPROVED=1 필요"
  echo "dry-run 종료(codex 미호출, 잠금·백업·임시파일 없음)."
  COMMITTED=1; FINALIZED=1
  exit 0
fi

# -o/--output-last-message 지원 여부 — help를 끝까지 받아서 검사한다.
# `codex exec --help | grep -q`는 pipefail에서 SIGPIPE로 거짓 음성이 날 수 있고, 명령치환으로
# 부르면 멈췄을 때 추적 대상이 아니라 신호도 전달되지 않는다. 그래서 배경 실행 + 시간 제한으로
# 돌리고 CODEX_PID로 감독한다(잠금·백업 **전**이라 여기서 죽어도 남는 상태가 없다).
OUT_FLAG=""
PROBE_TMP="$(mktemp -t divvy_probe)" || { echo "임시파일 생성 실패(TMPDIR 확인)" >&2; exit 1; }
set -m
( codex exec --help >"$PROBE_TMP" 2>/dev/null ) &
CODEX_PID=$!
set +m
if wait_group_gone "$CODEX_PID" "$PROBE_TIMEOUT"; then
  wait "$CODEX_PID" 2>/dev/null
  case "$(cat "$PROBE_TMP" 2>/dev/null)" in *--output-last-message*) OUT_FLAG=1;; esac
else
  echo "  ! codex --help 탐지가 ${PROBE_TIMEOUT}초를 넘겨 포기 — stdout fallback으로 진행" >&2
  kill -KILL -"$CODEX_PID" 2>/dev/null || kill -KILL "$CODEX_PID" 2>/dev/null
  wait "$CODEX_PID" 2>/dev/null
fi
CODEX_PID=""
rm -f "$PROBE_TMP"

# 같은 출력 경로로 두 실행이 동시에 들어오면 서로의 산출물을 자기 결과로 오인할 수 있다.
# mkdir은 원자적이라 예약에 쓴다. (사이드카 이름 금지 규칙이 실행 간 경로 겹침을 막는다.)
if mkdir "$LOCK" 2>/dev/null; then
  LOCK_HELD=1
  # 소유 토큰 — 위임된 작업이 잠금을 지우고 다른 실행이 새로 만들면, 우리 정리가 **남의 잠금**을
  # 해제해 버린다. 해제 전에 토큰이 우리 것인지 확인한다.
  LOCK_TOKEN="$$:$(date +%s):$RANDOM"
  if ! printf '%s\n' "$LOCK_TOKEN" >"$LOCK/owner" 2>/dev/null; then
    echo "잠금 소유 토큰 기록 실패: $LOCK/owner" >&2
    # 부분 파일이 남으면 rmdir이 실패해 잠금이 영구히 남고 이후 실행이 계속 rc6에 막힌다.
    rm -f "$LOCK/owner" 2>/dev/null
    if rmdir "$LOCK" 2>/dev/null; then
      LOCK_HELD=0
      echo "  (잠금 정리됨 — 남은 것 없음)" >&2
    else
      LOCK_HELD=0
      CLEAN_FAIL=1
      echo "  ✗ 잠금 잔여물 제거 실패 — 사람이 지워야 한다: rmdir '$LOCK'" >&2
    fi
    exit 6
  fi
else
  echo "동일 출력 경로가 이미 실행 중이거나 잠금이 남아 있다: $LOCK" >&2
  echo "  끝난 실행의 잔여 잠금이면 지워라: rmdir '$LOCK'" >&2
  exit 6
fi

# 회수 규약 — 브리프 본문(사람이 쓴 결정)은 손대지 않고 뒤에만 덧붙인다.
if [ "$SANDBOX" = "read-only" ]; then
  TRAILER='

---
[divvy 회수 규약]
- 위 브리프에서 이미 결정된 것은 재논의하지 말고, 요청된 산출만 내라.
- 파일을 수정·생성·삭제하지 말고 최종 메시지로만 답하라(수정이 필요하면 적용할 diff를 텍스트로 제시하라 — 적용은 사람이 한다).
- 브리프의 상대경로는 이 작업 루트 기준이다.
- 주장마다 근거 위치(파일·섹션·라인)를 붙여라. 확인하지 못한 것은 확인하지 못했다고 적어라.'
else
  TRAILER='

---
[divvy 회수 규약]
- 위 브리프에서 이미 결정된 것은 재논의하지 말고, 요청된 산출만 내라.
- 브리프에 적힌 대상 밖의 파일은 건드리지 마라. 무엇을 바꿨는지 최종 메시지에 파일별로 요약하라.
- 브리프의 상대경로는 이 작업 루트 기준이다.
- 주장마다 근거 위치(파일·섹션·라인)를 붙여라. 확인하지 못한 것은 확인하지 못했다고 적어라.'
fi

# 프롬프트를 먼저 파일로 조립한다 — 파이프로 만들면 `cat` 실패가 뒤 `printf` 성공에 가려져
# 회수 규약만 담긴 프롬프트가 Codex로 넘어간다.
PROMPT_TMP="$(mktemp -t divvy_prompt)" || { echo "임시파일 생성 실패(TMPDIR 확인)" >&2; exit 1; }
if ! cat -- "$BRIEF" >"$PROMPT_TMP"; then echo "브리프 읽기 실패: $BRIEF" >&2; exit 1; fi
if ! printf '%s' "$TRAILER" >>"$PROMPT_TMP"; then echo "회수 규약 덧붙이기 실패" >&2; exit 1; fi
grep -q '[^[:space:]]' "$PROMPT_TMP" || { echo "조립된 프롬프트가 빔 — 중단(빈 입력 금지)" >&2; exit 1; }

# clobber 보호 — 이전 회수분을 이번 실행 결과로 오인하지 않게 한다.
# ★ FORCE=1이어도 곧바로 지우지 않는다: `-o` 경로는 Codex가 아무것도 쓰지 않으면 파일을
#   건드리지 않으므로, 낡은 내용이 남아 아래 "공백 아님" 검사를 통과해 거짓 성공이 된다.
#   그래서 먼저 .forcebak으로 치우고, 성공했을 때만 실제로 지운다(실패·중단 시 trap이 복구).
# ★ 복구 의도를 mv **전에** arm한다. mv 직후 신호가 처리되면 백업은 생겼는데 복구 대상이
#   비어 있어 이전 회수분이 고립된다.
if [ "$OUT_PREEXISTED" = "1" ]; then
  BAK="$BAKPATH"
  if [ -e "$BAKPATH" ] || ! mv -f "$OUT" "$BAKPATH" 2>/dev/null; then
    BAK=""
    echo "기존 출력 치움 실패: $OUT — 낡은 내용이 이번 결과로 오인될 수 있어 중단." >&2
    exit 3
  fi
  BAK_MOVED=1
fi

# FORCE 실행은 **두 사이드카를 모두** 치운다. 이번 모드가 쓰지 않는 쪽(예: `-o` 경로에서는 `.err`)에
# 이전 실행의 내용이 남으면, 사람이 낡은 오류를 이번 실행의 진단으로 오인한다.
if [ "$FORCE" = "1" ]; then
  for p in "$LOG" "$ERR"; do
    if [ -e "$p" ] && ! rm -f "$p"; then
      echo "이전 사이드카 제거 실패: $p — 낡은 진단이 이번 실행으로 오인될 수 있어 중단." >&2
      exit 3
    fi
  done
fi

echo "Codex 위임 (effort=$EFFORT, sandbox=$SANDBOX${MODEL_DESC}${CONFIG_DESC})"
echo "  브리프: $BRIEF"
echo "  작업루트: $WORKDIR  ← 브리프 내 상대경로는 이 기준"

RUN_STARTED=1
rc=0
# `set -m`으로 자체 프로세스 그룹을 만든다 — 중단 시 Codex가 만든 자손까지 신호가 닿는다.
set -m
if [ -n "$OUT_FLAG" ]; then
  codex exec --skip-git-repo-check ${CONFIG_ARG[@]+"${CONFIG_ARG[@]}"} --cd "$WORKDIR" --sandbox "$SANDBOX" \
      -c model_reasoning_effort="$EFFORT" ${MODEL_ARG[@]+"${MODEL_ARG[@]}"} \
      -o "$OUT" - <"$PROMPT_TMP" >"$LOG" 2>&1 &
else
  codex exec --skip-git-repo-check ${CONFIG_ARG[@]+"${CONFIG_ARG[@]}"} --cd "$WORKDIR" --sandbox "$SANDBOX" \
      -c model_reasoning_effort="$EFFORT" ${MODEL_ARG[@]+"${MODEL_ARG[@]}"} \
      - <"$PROMPT_TMP" >"$OUT" 2>"$ERR" &
fi
CODEX_PID=$!
set +m
PGID="$CODEX_PID"
wait "$CODEX_PID" || rc=$?

# ★ 직속 child가 끝나도 같은 그룹의 자손이 남아 workspace를 계속 고칠 수 있다(`setsid` 이탈이
#   아니어도). 성공을 선언하고 잠금을 풀면 그 변경이 다음 실행과 겹친다. 그래서 성공 판정 전에
#   그룹이 비기를 기다리고, 안 비면 TERM → KILL로 정리한다.
if kill -0 -"$PGID" 2>/dev/null; then
  echo "  ! 실행 종료 후에도 프로세스 그룹에 남은 프로세스가 있다 — ${SIG_GRACE}초 기다린다" >&2
  if ! wait_group_gone "$PGID" "$SIG_GRACE"; then
    echo "  ! 남은 프로세스 정리: TERM → KILL" >&2
    kill -TERM -"$PGID" 2>/dev/null
    if ! wait_group_gone "$PGID" 2; then
      kill -KILL -"$PGID" 2>/dev/null
      wait_group_gone "$PGID" 2 || {
        CLEAN_FAIL=1
        echo "  ✗ 남은 프로세스를 정리하지 못했다(PGID $PGID) — workspace가 계속 바뀔 수 있다." >&2
      }
    fi
  fi
fi
CODEX_PID=""

# 성공 판정 4조건 — 종료코드만으로 ✓를 찍지 않는다.
# "못 찾았다"와 "돌지 않았다"를 구분해야 회수한 사람이 오판하지 않는다.
why=""; fail_rc=4
if [ "$rc" != "0" ]; then why="종료코드 실패(rc=$rc)"
elif [ ! -f "$OUT" ]; then why="산출물 없음"
elif ! grep -q '[^[:space:]]' "$OUT" 2>/dev/null; then why="산출물 빔"
fi

if [ -z "$why" ]; then
  for expected in ${EXPECTATIONS[@]+"${EXPECTATIONS[@]}"}; do
    if ! grep -Fq -- "$expected" "$OUT" 2>/dev/null; then
      why="산출 계약 미충족: '$expected' 없음"
      fail_rc=9
      break
    fi
  done
fi

if [ -z "$why" ]; then
  COMMITTED=1
  if [ "$BAK_MOVED" = "1" ] && ! rm -f "$BAK"; then
    echo "  ! 이전 백업 삭제 실패(남아 있음): $BAK" >&2
  else
    BAK=""
    FINALIZED=1
  fi
  echo "  ✓ 회수: $OUT"
  echo "완료. 이 결과는 Codex 단독 산출이다 — 채택 전 사람 판단, LEDGER에 기록."
  echo "⚠️ 성공 기계 검사는 실행·회수·선언된 최소 문자열만 보증한다. **산출물이 요청에 답했는지는 읽어야 안다** —"
  echo "   Codex가 자기 환경 문제로 '작업 불가' 한 줄만 rc 0으로 돌려주는 사례가 실측됐다"
  echo "   (2026-07-30: 훅/플러그인 preflight가 런을 중단시키고 '리뷰 불가' 한 줄을 남겼다)."
  exit 0
fi

# 실패 정리는 cleanup(EXIT trap)이 한다 — 신호로 죽는 경로와 같은 상태기를 지나게 한다.
echo "  ✗ 실패 — $why (로그: $LOG / $ERR)" >&2
echo "실패는 '못 찾았다'가 아니다. 재실행하거나 CLAUDE로 되돌려라." >&2
exit "$fail_rc"
