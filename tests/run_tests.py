#!/usr/bin/env python3
"""divvy dispatch.sh 테스트.

실행: python3 tests/run_tests.py  (종료코드 0=통과)

두 부류다.
  (A) 사전 거부·dry-run — 진짜 Codex를 부르지 않는다.
  (B) 실제 실행 경로 — PATH 앞에 **가짜 `codex`**를 세워 rc·stdin·`-o` 생성 여부를 조종한다.
      진짜 Codex는 여기서도 호출되지 않는다(과금·비결정성 없음).

여기서 고정하는 계약:
  - 빈/없는 브리프, 없는 디렉터리, 샌드박스 오타는 Codex 호출 전에 거부한다.
  - 브리프가 출력·사이드카(.log/.err/.forcebak/.lock)와 같은 파일이면 거부한다(입력 파괴 금지).
  - read-only가 아닌 샌드박스의 실제 실행은 DIVVY_WRITE_APPROVED=1 없이는 거부한다.
  - 기존 출력·기존 로그는 FORCE 없이 덮지 않는다. FORCE여도 성공 전에는 이전 회수분을 지우지 않는다.
  - 실행 실패 시 이번 실행이 만든 불완전 산출물을 남기지 않는다(로그는 남긴다).
  - `-o` 지원 탐지는 help 출력이 아무리 길어도 거짓 음성이 되지 않는다.
  - 동일 출력 경로 동시 실행은 잠금으로 거부하고, 정상 종료 시 잠금을 해제한다.
  - 도움말에 셸 코드가 새지 않는다.
  - (r2) 관리 경로 심링크·관리 경로 상호 하드링크·사이드카 접미사 출력 이름은 거부한다.
  - (r2) 고립된 `.forcebak`(출력 없이 백업만 남음)은 조용히 진행하지 않는다.
  - (r2) dry-run은 잠금·백업을 만들지 않는다(검사만 한다).
  - (r2) TERM/INT는 Codex child에 전달되고, 중단 시에도 상태가 되돌아간다.
  - (r2) 정리(백업 삭제·잠금 해제) 실패는 성공(rc 0)으로 반올림되지 않는다(rc 8).
"""
import json, os, signal, subprocess, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISPATCH = os.path.join(ROOT, "scripts", "dispatch.sh")
OMX_HOTFIX = os.path.join(ROOT, "scripts", "omx_stop_hotfix.py")
INIT_STATE = os.path.join(ROOT, "scripts", "init_state.py")
ROSTER_PROBE_TESTS = os.path.join(ROOT, "tests", "test_roster_probe.py")
STATE_PERMISSION_TESTS = os.path.join(ROOT, "tests", "test_state_permissions.py")
RELEASE_TESTS = os.path.join(ROOT, "tests", "test_release.py")
README = os.path.join(ROOT, "README.md")
SKILL = os.path.join(ROOT, "SKILL.md")
PASS, FAIL = [], []

FAKE_CODEX = r"""#!/usr/bin/env bash
# divvy 테스트용 가짜 codex — 진짜를 부르지 않고 실행 경로를 조종한다.
if [ "${1:-}" = "exec" ] && [ "${2:-}" = "--help" ]; then
  if [ "${FAKE_HUGE_HELP:-0}" = "1" ]; then
    i=0; while [ $i -lt 4000 ]; do echo "filler help line $i"; i=$((i+1)); done
  fi
  [ "${FAKE_NO_O:-0}" = "1" ] || echo "  -o, --output-last-message <FILE>"
  echo "usage: codex exec [OPTIONS] [PROMPT]"
  exit 0
fi
if [ -n "${FAKE_STDIN_CAPTURE:-}" ]; then cat >"$FAKE_STDIN_CAPTURE"; else cat >/dev/null; fi
if [ -n "${FAKE_ARGS_CAPTURE:-}" ]; then printf '%s\n' "$@" >"$FAKE_ARGS_CAPTURE"; fi
OUTF=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && OUTF="$a"; prev="$a"; done
case "${FAKE_MODE:-ok}" in
  ok)     if [ -n "$OUTF" ]; then printf '%s\n' "${FAKE_OUTPUT:-FAKE OK}" >"$OUTF";
          else printf '%s\n' "${FAKE_OUTPUT:-FAKE OK}"; fi ;;
  empty)  if [ -n "$OUTF" ]; then : >"$OUTF"; else printf '   \n'; fi ;;
  nofile) : ;;
  fail)   echo "fake boom" >&2; exit 9 ;;
  sleep)  # 신호 전달 테스트용 — 시작을 알리고 오래 잔다. TERM이 전달되면 .done 은 안 생긴다.
          touch "$FAKE_MARKER"
          sleep 20
          touch "$FAKE_MARKER.done"
          if [ -n "$OUTF" ]; then printf 'LATE\n' >"$OUTF"; else printf 'LATE\n'; fi ;;
  lockdir) # 정리 실패 주입 — 산출물을 쓴 뒤 부모 디렉터리를 읽기전용으로 만든다.
          # ★ OUTF 가 비면 아무것도 chmod 하지 않는다. 예전엔 dirname "" = "." 로 테스트
          #   임시 루트를 읽기전용으로 만들어 뒤 테스트들이 연쇄로 깨졌다.
          if [ -n "$OUTF" ]; then printf 'FAKE OK\n' >"$OUTF"; chmod a-w "$(dirname "$OUTF")";
          else printf 'FAKE OK\n'; echo "lockdir 모드는 -o 경로 전용" >&2; fi ;;
  faillock) # 실행 실패 + 정리 실패가 겹치는 경우 (r3-03)
          [ -n "$OUTF" ] && chmod a-w "$(dirname "$OUTF")"
          echo "fake boom" >&2; exit 9 ;;
  outdir) # 복구 자리에 디렉터리가 생긴 경우 (r3-04)
          if [ -n "$OUTF" ]; then rm -f "$OUTF"; mkdir -p "$OUTF"; fi
          echo "fake boom" >&2; exit 9 ;;
  grandchild) # 자손이 신호를 넘기고 살아남는지 (r3-01)
          ( sleep 6; touch "$FAKE_MARKER.grandchild" ) &
          touch "$FAKE_MARKER"
          sleep 20 ;;
  deaf)   # 직속 child 가 TERM 을 무시하는 경우 (r3-01)
          trap '' TERM
          touch "$FAKE_MARKER"
          sleep 20 ;;
  deafgc) # 신호를 무시하는 **자손** + 먼저 죽는 직속 child (r4-01)
          ( trap '' TERM; sleep 8; touch "$FAKE_MARKER.gc2" ) &
          touch "$FAKE_MARKER"
          sleep 20 ;;
  partialfail) # 부분 산출물을 쓴 뒤 실패 (r4-02)
          if [ -n "$OUTF" ]; then printf 'PARTIAL' >"$OUTF"; else printf 'PARTIAL'; fi
          echo "fake boom" >&2; exit 9 ;;
  stealock) # 위임 작업이 잠금을 지우고 다른 실행이 새로 예약한 상황 (r5-01)
          if [ -n "$OUTF" ]; then
            rm -rf "$OUTF.lock"
            mkdir -p "$OUTF.lock"
            printf 'someone-else\n' >"$OUTF.lock/owner"
            printf 'FAKE OK\n' >"$OUTF"
          fi ;;
  okgc)   # 성공(rc0) 인데 같은 그룹에 자손이 남아 뒤늦게 workspace 를 고친다 (r6-02)
          ( sleep 6; touch "$FAKE_MARKER.late" ) &
          if [ -n "$OUTF" ]; then printf 'FAKE OK\n' >"$OUTF"; else printf 'FAKE OK\n'; fi ;;
  slowclean) # 리더는 즉시 죽고 자손이 TERM 후 3초간 정리 (r5-03)
          ( trap 'sleep 3; touch "$FAKE_MARKER.clean"; exit 0' TERM; sleep 20 ) &
          touch "$FAKE_MARKER"
          sleep 20 ;;
esac
exit 0
"""

# help 탐지가 멈추는 가짜 codex (r3-02) — exec --help 에서만 오래 잔다.
FAKE_CODEX_SLOW_HELP = r"""#!/usr/bin/env bash
if [ "${1:-}" = "exec" ] && [ "${2:-}" = "--help" ]; then
  touch "${FAKE_MARKER:-/dev/null}.probe"
  sleep 30
  exit 0
fi
if [ -n "${FAKE_STDIN_CAPTURE:-}" ]; then cat >"$FAKE_STDIN_CAPTURE"; else cat >/dev/null; fi
printf 'FAKE OK\n'
exit 0
"""


def check(n, c):
    (PASS if c else FAIL).append(n)
    print(("ok   " if c else "FAIL ") + n)


def selector_is(path, expected):
    args = read(path).splitlines() if os.path.exists(path) else []
    profile_count = sum(
        1 for i, arg in enumerate(args[:-1])
        if arg == "--profile" and args[i + 1] == "headless"
    )
    isolate_count = args.count("--ignore-user-config")
    if expected == "profile":
        return profile_count == 1 and isolate_count == 0
    return profile_count == 0 and isolate_count == 1


def test_env():
    e = dict(os.environ)
    e.pop("CODEX_MODEL", None)
    e.pop("CODEX_SANDBOX", None)
    return e


def run(args, env=None, dry=False, fake=None):
    e = test_env()
    if dry:
        e["DRY_RUN"] = "1"
    if fake is not None:
        e["PATH"] = fake + os.pathsep + e.get("PATH", "")
    e.update(env or {})
    return subprocess.run(["bash", DISPATCH, *args], capture_output=True, text=True, env=e)


def write(p, s):
    with open(p, "w") as f:
        f.write(s)


def write_bytes(p, data):
    with open(p, "wb") as f:
        f.write(data)


def read(p):
    with open(p) as f:
        return f.read()


# ---------------------------------------------------------------- (A) 사전 거부
check("dispatch.sh 실행권한", os.access(DISPATCH, os.X_OK))

r = subprocess.run(["bash", DISPATCH, "-h"], capture_output=True, text=True)
check("-h 사용법 exit0", r.returncode == 0 and "브리프파일" in r.stdout)
check("-h 에 셸 코드 없음(r1-16)",
      "set -uo pipefail" not in r.stdout and "usage()" not in r.stdout and 'BRIEF="${1' not in r.stdout)
check("-h 에 경로 계약 명시(r1-10)", "작업디렉터리> 기준" in r.stdout or "작업디렉터리 기준" in r.stdout)

with tempfile.TemporaryDirectory() as tmp:
    fakebin = os.path.join(tmp, "bin")
    os.makedirs(fakebin)
    fc = os.path.join(fakebin, "codex")
    write(fc, FAKE_CODEX)
    os.chmod(fc, 0o755)

    brief = os.path.join(tmp, "brief.md")
    empty = os.path.join(tmp, "empty.md")
    write(brief, "작업: 테스트용 브리프 본문 GUARD_MARKER\n")
    write(empty, "   \n\n")

    def out_path(name):
        d = os.path.join(tmp, name)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "out.md")

    check("브리프 없음 → rc1", run([os.path.join(tmp, "nope.md"), out_path("a")], dry=True).returncode == 1)
    check("빈 브리프 → rc1", run([empty, out_path("b")], dry=True).returncode == 1)
    check("작업디렉터리 없음 → rc1",
          run([brief, out_path("c"), os.path.join(tmp, "nodir")], dry=True).returncode == 1)
    check("출력 디렉터리 없음 → rc1",
          run([brief, os.path.join(tmp, "nodir", "o.md")], dry=True).returncode == 1)
    check("샌드박스 오타 → rc2", run([brief, out_path("d")], {"CODEX_SANDBOX": "bogus"}, dry=True).returncode == 2)

    r = run([brief, out_path("e"), tmp], dry=True)
    check("dry-run rc0 + codex 미호출", r.returncode == 0 and "codex 미호출" in r.stdout)
    check("dry-run 계획에 read-only·medium",
          "--sandbox read-only" in r.stdout and "effort=medium" in r.stdout)
    check("dry-run 에 작업루트 기준 표시(r1-10)", "브리프 내 상대경로 기준" in r.stdout)
    check("dry-run 은 출력파일을 만들지 않음", not os.path.exists(out_path("e")))

    # 경로 충돌 (r1-01·r1-02)
    check("브리프 == 출력 → rc7", run([brief, brief, tmp], dry=True).returncode == 7)
    # 사이드카 충돌: 출력이 X면 X.log 도 쓰인다 — 브리프가 X.log 이면 입력이 파괴된다.
    brief_log = os.path.join(tmp, "collide.log")
    write(brief_log, "브리프인데 하필 .log 이름\n")
    check("브리프 == 출력.log → rc7",
          run([brief_log, os.path.join(tmp, "collide"), tmp], dry=True).returncode == 7)
    link = os.path.join(tmp, "brief_link.md")
    os.symlink(brief, link)
    check("심링크로 같은 실체 → rc7", run([brief, link, tmp], dry=True).returncode == 7)

    # write 샌드박스 승인 게이트 (r1-06)
    cap = os.path.join(tmp, "stdin_cap.txt")
    o = out_path("w1")
    r = run([brief, o, tmp], {"CODEX_SANDBOX": "workspace-write", "FAKE_STDIN_CAPTURE": cap}, fake=fakebin)
    check("write 샌드박스 + 승인 없음 → rc5", r.returncode == 5)
    check("  거부는 codex 호출 전(stdin 캡처 없음)", not os.path.exists(cap))
    o = out_path("w2")
    wsroot = os.path.join(tmp, "w2root")   # 출력은 작업루트 밖에 둔다(r5-01)
    os.makedirs(wsroot, exist_ok=True)
    r = run([brief, o, wsroot],
            {"CODEX_SANDBOX": "workspace-write", "DIVVY_WRITE_APPROVED": "1"}, fake=fakebin)
    check("write 샌드박스 + 승인 → rc0", r.returncode == 0 and os.path.exists(o))

    # ------------------------------------------------------- (B) 실제 실행 경로
    o = out_path("r1")
    cap = os.path.join(tmp, "cap1.txt")
    args_cap = os.path.join(tmp, "args1.txt")
    r = run([brief, o, tmp], {"FAKE_STDIN_CAPTURE": cap, "FAKE_ARGS_CAPTURE": args_cap}, fake=fakebin)
    check("정상 실행 → rc0", r.returncode == 0)
    check("  출력 회수됨", os.path.exists(o) and "FAKE OK" in read(o))
    check("  -o 경로 사용(.log 생성)", os.path.exists(o + ".log"))
    check("  stdin = 브리프 + 회수규약", os.path.exists(cap)
          and "GUARD_MARKER" in read(cap) and "divvy 회수 규약" in read(cap))
    check("  기본 -o 실행은 headless 프로필만 사용", selector_is(args_cap, "profile"))
    check("  성공 후 잠금 해제", not os.path.exists(o + ".lock"))
    check("  성공 후 백업 잔여물 없음", not os.path.exists(o + ".forcebak"))
    # 성공 3조건이 "요청에 답했는가"를 보증하지 못한다는 경고가 회수 메시지에 있어야 한다.
    # (실측: Codex가 '작업 불가' 한 줄을 rc 0으로 회수한 사례)
    check("  내용 확인 경고 노출", "읽어야 안다" in (r.stdout + r.stderr))

    # P-C20: 선언된 최소 산출 계약은 모두 fixed-string으로 충족해야 한다.
    expect_one = os.path.join(tmp, "expect_one.md")
    write(expect_one, "작업: 단일 마커\n<!-- DIVVY_EXPECT:   FAKE OK   -->\n")
    o = out_path("expect_ok")
    r = run([expect_one, o, tmp], fake=fakebin)
    check("DIVVY_EXPECT 단일 마커 충족 → rc0", r.returncode == 0)

    # 거짓 거부 회귀(2026-08-01): 비ASCII 1KB 초과 브리프가 게이트를 통과해야 한다.
    expect_non_ascii = os.path.join(tmp, "expect_non_ascii.md")
    write(expect_non_ascii, "\ud55c" * 350 + "\n<!-- DIVVY_EXPECT: FAKE OK -->\n")
    o = out_path("expect_non_ascii")
    r = run([expect_non_ascii, o, tmp], fake=fakebin)
    check("1KB 초과 비ASCII DIVVY_EXPECT 브리프 → rc0",
          os.path.getsize(expect_non_ascii) > 1024 and r.returncode == 0)

    # 위 완화가 게이트를 무력화하지 않았는지 — 진짜 무효 UTF-8은 여전히 rc9로 fail-closed.
    expect_invalid_utf8 = os.path.join(tmp, "expect_invalid_utf8.md")
    write_bytes(expect_invalid_utf8,
                b"\xff\xfe\n<!-- DIVVY_EXPECT: FAKE OK -->\n")
    cap = os.path.join(tmp, "expect_invalid_utf8.cap")
    r = run([expect_invalid_utf8, out_path("expect_invalid_utf8"), tmp],
            {"FAKE_STDIN_CAPTURE": cap}, fake=fakebin)
    check("유효하지 않은 UTF-8 DIVVY_EXPECT 브리프 → rc9",
          r.returncode == 9
          and "DIVVY_EXPECT 마커가 있는 브리프는 UTF-8이어야 한다." in r.stderr
          and not os.path.exists(cap)
          and not os.path.exists(out_path("expect_invalid_utf8")))

    expect_missing = os.path.join(tmp, "expect_missing.md")
    write(expect_missing, "작업: 단일 마커 미충족\n<!-- DIVVY_EXPECT: NEEDLE -->\n")
    o = out_path("expect_missing")
    r = run([expect_missing, o, tmp], fake=fakebin)
    check("DIVVY_EXPECT 단일 마커 미충족 → rc9 + 산출물 정리",
          r.returncode == 9
          and not os.path.exists(o)
          and not os.path.exists(o + ".lock")
          and not os.path.exists(o + ".forcebak"))

    o = out_path("expect_force_missing")
    write(o, "이전 회수분\n")
    r = run([expect_missing, o, tmp], {"FORCE": "1"}, fake=fakebin)
    check("DIVVY_EXPECT 미충족 FORCE → 이전 산출물 복구",
          r.returncode == 9
          and read(o) == "이전 회수분\n"
          and not os.path.exists(o + ".lock")
          and not os.path.exists(o + ".forcebak"))

    o = out_path("expect_none")
    r = run([brief, o, tmp], fake=fakebin)
    check("마커 없음 → 기존 3조건만으로 rc0", r.returncode == 0)

    expect_many = os.path.join(tmp, "expect_many.md")
    write(expect_many, "작업: 복수 마커\n<!-- DIVVY_EXPECT: FIRST -->\n<!-- DIVVY_EXPECT: SECOND -->\n")
    o = out_path("expect_partial")
    r = run([expect_many, o, tmp], {"FAKE_OUTPUT": "FIRST"}, fake=fakebin)
    check("복수 DIVVY_EXPECT 부분 충족 → rc9 + 산출물 정리",
          r.returncode == 9
          and not os.path.exists(o)
          and not os.path.exists(o + ".lock")
          and not os.path.exists(o + ".forcebak"))

    bad_expectations = (
        ("empty", "작업: 빈 마커\n<!-- DIVVY_EXPECT:   -->\n"),
        ("malformed", "작업: 닫힘 누락\n<!-- DIVVY_EXPECT: NEVER\n"),
    )
    for name, contents in bad_expectations:
        bad_brief = os.path.join(tmp, "expect_" + name + ".md")
        write(bad_brief, contents)
        cap = os.path.join(tmp, "expect_" + name + ".cap")
        r = run([bad_brief, out_path("expect_" + name), tmp],
                {"FAKE_STDIN_CAPTURE": cap}, fake=fakebin)
        check(f"{name} DIVVY_EXPECT → 계약 오류 rc9",
              r.returncode == 9
              and not os.path.exists(cap)
              and not os.path.exists(out_path("expect_" + name)))

    o = out_path("r2")
    args_cap = os.path.join(tmp, "args2.txt")
    r = run([brief, o, tmp], {"FAKE_NO_O": "1", "FAKE_ARGS_CAPTURE": args_cap}, fake=fakebin)
    check("-o 미지원 → stdout fallback rc0", r.returncode == 0 and "FAKE OK" in read(o))
    check("  fallback 은 .err 사이드카", os.path.exists(o + ".err"))
    check("  기본 fallback은 headless 프로필만 사용", selector_is(args_cap, "profile"))

    o = out_path("iso_real_o")
    args_cap = os.path.join(tmp, "args_iso_o.txt")
    r = run([brief, o, tmp],
            {"DIVVY_ISOLATE_CONFIG": "1", "FAKE_ARGS_CAPTURE": args_cap}, fake=fakebin)
    check("격리 -o 실제 실행 → rc0", r.returncode == 0 and "FAKE OK" in read(o))
    check("  격리 -o 실행은 ignore-user-config만 사용", selector_is(args_cap, "isolate"))

    o = out_path("iso_real_fallback")
    args_cap = os.path.join(tmp, "args_iso_fallback.txt")
    r = run([brief, o, tmp],
            {"DIVVY_ISOLATE_CONFIG": "1", "FAKE_NO_O": "1", "FAKE_ARGS_CAPTURE": args_cap},
            fake=fakebin)
    check("격리 fallback 실제 실행 → rc0", r.returncode == 0 and "FAKE OK" in read(o))
    check("  격리 fallback은 ignore-user-config만 사용", selector_is(args_cap, "isolate"))

    o = out_path("r3")
    r = run([brief, o, tmp], {"FAKE_HUGE_HELP": "1"}, fake=fakebin)
    check("긴 help 에도 -o 탐지 유지(r1-13)", r.returncode == 0 and os.path.exists(o + ".log"))

    for mode, label in (("fail", "종료코드 실패"), ("empty", "산출물 빔"), ("nofile", "산출물 없음")):
        o = out_path("f_" + mode)
        r = run([brief, o, tmp], {"FAKE_MODE": mode}, fake=fakebin)
        check(f"{label} → rc4", r.returncode == 4)
        check(f"  {label}: 불완전 산출물 안 남김(r1-05)", not os.path.exists(o))
        check(f"  {label}: 잠금 해제", not os.path.exists(o + ".lock"))

    # clobber + FORCE 의미론
    o = out_path("c1")
    write(o, "이전 회수분\n")
    check("기존 출력 + FORCE 없음 → rc3", run([brief, o, tmp], fake=fakebin).returncode == 3)
    check("  거부 후 이전 내용 보존", read(o).strip() == "이전 회수분")

    r = run([brief, o, tmp], {"FORCE": "1", "FAKE_MODE": "fail"}, fake=fakebin)
    check("FORCE + 실행 실패 → rc4", r.returncode == 4)
    check("  이전 회수분 복구됨(r1-04)", read(o).strip() == "이전 회수분")
    check("  .forcebak 잔여물 없음", not os.path.exists(o + ".forcebak"))

    r = run([brief, o, tmp], {"FORCE": "1"}, fake=fakebin)
    check("FORCE + 성공 → rc0", r.returncode == 0)
    check("  새 내용으로 교체", "FAKE OK" in read(o))
    check("  백업 삭제됨", not os.path.exists(o + ".forcebak"))

    o = out_path("c2")
    write(o + ".log", "이전 로그\n")
    check("기존 로그 + FORCE 없음 → rc3", run([brief, o, tmp], fake=fakebin).returncode == 3)
    check("  이전 로그 보존", read(o + ".log").strip() == "이전 로그")

    # 동시 실행 잠금 (r1-12)
    o = out_path("l1")
    os.makedirs(o + ".lock")
    r = run([brief, o, tmp], fake=fakebin)
    check("잠금 점유 중 → rc6", r.returncode == 6)
    check("  남의 잠금을 지우지 않음", os.path.isdir(o + ".lock"))

    # ---------------------------------------------------- (C) r2 회귀
    # r2-01: 관리 경로 심링크 — 마지막 성분은 정규화로 접히지 않는다
    o = out_path("s1")
    os.symlink(os.path.join(tmp, "elsewhere.md"), o)          # dangling symlink
    check("출력이 dangling 심링크 → rc7", run([brief, o, tmp], dry=True).returncode == 7)
    o = out_path("s2")
    real = os.path.join(tmp, "real_target.md")
    write(real, "남의 파일\n")
    os.symlink(real, o + ".log")                               # 사이드카가 심링크
    check("사이드카가 심링크 → rc7", run([brief, o, tmp], dry=True).returncode == 7)
    check("  남의 파일 보존", read(real).strip() == "남의 파일")

    # r2-01: 관리 경로끼리 하드링크(출력 ↔ 로그가 같은 실체)
    o = out_path("h1")
    write(o, "이전 회수분\n")
    os.link(o, o + ".log")
    r = run([brief, o, tmp], {"FORCE": "1"}, fake=fakebin)
    check("출력↔로그 하드링크 → rc7", r.returncode == 7)
    check("  이전 회수분 보존", read(o).strip() == "이전 회수분")

    # r2-05: 사이드카 접미사를 출력 이름으로 쓰면 다른 실행과 파일이 겹친다
    sfxdir = os.path.join(tmp, "sfx")
    os.makedirs(sfxdir, exist_ok=True)
    for suf in (".log", ".err", ".forcebak", ".lock"):
        rc = run([brief, os.path.join(sfxdir, "out" + suf), tmp], dry=True).returncode
        check("출력 이름이 " + suf + " → rc7", rc == 7)

    # r2-03: 고립된 백업(출력 없이 .forcebak만) — 조용히 진행하면 증거가 사라진다
    o = out_path("orph")
    write(o + ".forcebak", "이전 실행의 회수분\n")
    r = run([brief, o, tmp], fake=fakebin)
    check("고립된 .forcebak → rc3", r.returncode == 3)
    check("  고립 백업 보존", read(o + ".forcebak").strip() == "이전 실행의 회수분")

    # r2-04: FORCE dry-run은 백업·잠금을 만들지 않는다(검사만)
    o = out_path("d1")
    write(o, "이전 회수분\n")
    r = run([brief, o, tmp], {"FORCE": "1"}, dry=True)
    check("FORCE dry-run rc0", r.returncode == 0)
    check("  백업 안 만듦", not os.path.exists(o + ".forcebak"))
    check("  잠금 안 만듦", not os.path.exists(o + ".lock"))
    check("  이전 내용 그대로", read(o).strip() == "이전 회수분")

    # r2-02: TERM 이 Codex child 에 전달된다 — 래퍼만 죽고 child가 계속 돌면 안 된다.
    # 전달되지 않으면 bash 는 child(20초)를 기다린 뒤에야 trap 을 돌리므로 timeout 으로 잡힌다.
    o = out_path("sig")
    marker = os.path.join(tmp, "sig_marker")
    env = test_env()
    env["PATH"] = fakebin + os.pathsep + env.get("PATH", "")
    env["FAKE_MODE"] = "sleep"
    env["FAKE_MARKER"] = marker
    p = subprocess.Popen(["bash", DISPATCH, brief, o, tmp],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    started = False
    for _ in range(100):
        if os.path.exists(marker):
            started = True
            break
        time.sleep(0.1)
    check("가짜 codex 실행 시작 확인", started)
    p.send_signal(signal.SIGTERM)
    timed_out = False
    try:
        rc_sig = p.wait(timeout=6)
    except subprocess.TimeoutExpired:
        p.kill(); p.wait(); rc_sig = None; timed_out = True
    check("TERM → child 에 전달(6초 내 종료)", not timed_out)
    check("  종료코드 143", rc_sig == 143)
    check("  child 가 계속 돌지 않음(.done 없음)", not os.path.exists(marker + ".done"))
    check("  중단 시 불완전 산출물 안 남김", not os.path.exists(o))
    check("  중단 시 잠금 해제", not os.path.exists(o + ".lock"))

    # r2-04: 정리 실패는 성공으로 반올림되지 않는다 — 산출물은 유효하지만 rc 8
    cdir = os.path.join(tmp, "cleanfail")
    os.makedirs(cdir)
    o = os.path.join(cdir, "out.md")
    write(o, "이전 회수분\n")
    r = run([brief, o, tmp], {"FORCE": "1", "FAKE_MODE": "lockdir"}, fake=fakebin)
    os.chmod(cdir, 0o755)   # 테스트 자신의 정리를 위해 되돌린다
    check("정리 실패 → rc8", r.returncode == 8)
    check("  산출물 자체는 유효", "FAKE OK" in read(o))
    check("  남은 상태를 사람에게 알림", "정리 실패" in (r.stdout + r.stderr))

    # ---------------------------------------------------- (D) r3 회귀
    # r3-05: 대소문자 비구분 파일시스템에서 .LOG 는 .log 와 같은 파일이다
    for suf in (".LOG", ".Err", ".FORCEBAK", ".Lock"):
        rc = run([brief, os.path.join(sfxdir, "up" + suf), tmp], dry=True).returncode
        check("출력 이름이 " + suf + "(대문자) → rc7", rc == 7)

    # r3-06: dry-run 은 임시파일도 만들지 않는다 → 쓰기 불가 TMPDIR 에서도 계획을 낸다
    o = out_path("tmpd")
    r = run([brief, o, tmp], {"TMPDIR": os.path.join(tmp, "no_such_tmpdir")}, dry=True)
    check("쓰기 불가 TMPDIR + dry-run → rc0", r.returncode == 0 and "dry-run 종료" in r.stdout)

    # r3-03: 실행 실패와 정리 실패가 겹치면 rc8 이 이긴다(rc4 에 가려지면 남은 상태를 못 본다)
    cdir2 = os.path.join(tmp, "faillock")
    os.makedirs(cdir2)
    o = os.path.join(cdir2, "out.md")
    write(o, "이전 회수분\n")
    r = run([brief, o, tmp], {"FORCE": "1", "FAKE_MODE": "faillock"}, fake=fakebin)
    os.chmod(cdir2, 0o755)
    check("실행 실패 + 정리 실패 → rc8(rc4 아님)", r.returncode == 8)
    check("  원래 rc 를 메시지에 보존", "원래 종료코드 4" in (r.stdout + r.stderr))

    # r3-04: 복구 자리에 디렉터리가 생기면 mv 가 그 안으로 들어가 '복구 성공'을 오보한다
    o = out_path("odir")
    write(o, "지켜야 할 이전 회수분\n")
    r = run([brief, o, tmp], {"FORCE": "1", "FAKE_MODE": "outdir"}, fake=fakebin)
    check("복구 자리에 디렉터리 → rc8", r.returncode == 8)
    check("  백업 보존(유실 없음)", os.path.isfile(o + ".forcebak")
          and "지켜야 할" in read(o + ".forcebak"))
    check("  복구 성공을 오보하지 않음", "복구 불가" in (r.stdout + r.stderr))

    # r3-01: 자손 프로세스가 신호를 넘기고 살아남아 지연 쓰기를 하지 못한다
    o = out_path("gc")
    marker = os.path.join(tmp, "gc_marker")
    env = test_env()
    env["PATH"] = fakebin + os.pathsep + env.get("PATH", "")
    env.update({"FAKE_MODE": "grandchild", "FAKE_MARKER": marker, "DIVVY_SIG_GRACE": "2"})
    p = subprocess.Popen(["bash", DISPATCH, brief, o, tmp],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    for _ in range(100):
        if os.path.exists(marker):
            break
        time.sleep(0.1)
    p.send_signal(signal.SIGTERM)
    try:
        rc_gc = p.wait(timeout=12)
    except subprocess.TimeoutExpired:
        p.kill(); p.wait(); rc_gc = None
    check("자손 있는 실행도 TERM 으로 종료", rc_gc == 143)
    time.sleep(7)   # 자손이 살아 있었다면 이 사이에 지연 쓰기를 한다
    check("  자손이 살아남아 지연 쓰기 안 함", not os.path.exists(marker + ".grandchild"))

    # r3-01: 직속 child 가 TERM 을 무시해도 무기한 대기하지 않는다(유예 후 KILL)
    o = out_path("deaf")
    marker = os.path.join(tmp, "deaf_marker")
    env = test_env()
    env["PATH"] = fakebin + os.pathsep + env.get("PATH", "")
    env.update({"FAKE_MODE": "deaf", "FAKE_MARKER": marker, "DIVVY_SIG_GRACE": "2"})
    p = subprocess.Popen(["bash", DISPATCH, brief, o, tmp],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    for _ in range(100):
        if os.path.exists(marker):
            break
        time.sleep(0.1)
    p.send_signal(signal.SIGTERM)
    deaf_timeout = False
    try:
        rc_deaf = p.wait(timeout=12)
    except subprocess.TimeoutExpired:
        p.kill(); p.wait(); rc_deaf = None; deaf_timeout = True
    check("TERM 무시하는 child 도 유예 후 종료", not deaf_timeout and rc_deaf == 143)

    # r3-02: help 탐지가 멈춰도 시간 제한 후 fallback 으로 진행한다
    slowbin = os.path.join(tmp, "slowbin")
    os.makedirs(slowbin)
    sc = os.path.join(slowbin, "codex")
    write(sc, FAKE_CODEX_SLOW_HELP)
    os.chmod(sc, 0o755)
    o = out_path("probe")
    r = run([brief, o, tmp],
            {"DIVVY_PROBE_TIMEOUT": "2", "FAKE_MARKER": os.path.join(tmp, "probe_marker")},
            fake=slowbin)
    check("help 탐지 멈춤 → 시간 제한 후 fallback rc0", r.returncode == 0)
    check("  탐지 포기를 알림", "탐지가" in (r.stdout + r.stderr))
    check("  fallback 으로 산출물 회수", "FAKE OK" in read(o))

    # ---------------------------------------------------- (E) r4 회귀
    # r4-01: 직속 child 가 먼저 죽고 자손이 신호를 무시하면, 직속 PID 기준 판단으로는
    #        KILL 승격이 일어나지 않아 자손이 살아남는다. 그룹 잔존 여부로 판단해야 한다.
    o = out_path("deafgc")
    marker = os.path.join(tmp, "deafgc_marker")
    env = test_env()
    env["PATH"] = fakebin + os.pathsep + env.get("PATH", "")
    env.update({"FAKE_MODE": "deafgc", "FAKE_MARKER": marker, "DIVVY_SIG_GRACE": "2"})
    p = subprocess.Popen(["bash", DISPATCH, brief, o, tmp],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    for _ in range(100):
        if os.path.exists(marker):
            break
        time.sleep(0.1)
    p.send_signal(signal.SIGTERM)
    try:
        rc_gc2 = p.wait(timeout=12)
    except subprocess.TimeoutExpired:
        p.kill(); p.wait(); rc_gc2 = None
    check("신호 무시 자손 + 먼저 죽는 child → 종료", rc_gc2 == 143)
    time.sleep(9)   # 자손이 살아 있었다면 이 사이에 지연 쓰기를 한다
    check("  신호 무시 자손도 KILL 로 정리됨(r4-01)", not os.path.exists(marker + ".gc2"))

    # r4-02: FORCE 실행이 부분 산출물을 쓴 뒤 실패하면, 그 파일은 이번 실행 것이므로 지우고
    #        백업을 복구해야 한다. r3-04 가드가 이 경로를 "예상 밖 항목"으로 막으면 안 된다.
    o = out_path("pf")
    write(o, "지켜야 할 이전 회수분\n")
    r = run([brief, o, tmp], {"FORCE": "1", "FAKE_MODE": "partialfail"}, fake=fakebin)
    check("FORCE + 부분 산출물 후 실패 → rc4(rc8 아님)", r.returncode == 4)
    check("  이전 회수분 복구됨", read(o).strip() == "지켜야 할 이전 회수분")
    check("  백업 잔여물 없음", not os.path.exists(o + ".forcebak"))

    # ---------------------------------------------------- (F) r5 회귀
    # r5-01: 쓰기 샌드박스에서 출력이 작업루트 안에 있으면 위임 작업이 잠금·백업을 지울 수 있다
    inwork = os.path.join(tmp, "ws")
    os.makedirs(inwork, exist_ok=True)
    o = os.path.join(inwork, "out.md")
    r = run([brief, o, inwork],
            {"CODEX_SANDBOX": "workspace-write", "DIVVY_WRITE_APPROVED": "1"}, fake=fakebin)
    check("쓰기 샌드박스 + 출력이 작업루트 안 → rc7", r.returncode == 7)
    r = run([brief, o, inwork], fake=fakebin)   # read-only 면 허용돼야 한다(과잉 제한 방지)
    check("  read-only 면 작업루트 안도 허용", r.returncode == 0 and "FAKE OK" in read(o))

    # r5-01: 잠금이 남의 것으로 바뀌면 해제하지 않는다(둘이 같은 출력에 쓰는 것을 막는다)
    o = out_path("steal")
    r = run([brief, o, tmp], {"FAKE_MODE": "stealock"}, fake=fakebin)
    check("잠금이 남의 것으로 바뀜 → rc8", r.returncode == 8)
    check("  남의 잠금을 해제하지 않음", os.path.isdir(o + ".lock")
          and "someone-else" in read(os.path.join(o + ".lock", "owner")))
    check("  소유자 불일치를 알림", "잠금이 우리 것이 아니다" in (r.stdout + r.stderr))
    check("  남의 산출물을 건드리지 않음(r6-01)", "건드리지 않는다" in (r.stdout + r.stderr))
    import shutil as _sh
    _sh.rmtree(o + ".lock", ignore_errors=True)

    # r5-03: 유예는 그룹 기준이어야 한다 — 리더가 먼저 죽어도 자손의 정상 정리를 끊지 않는다
    o = out_path("slowclean")
    marker = os.path.join(tmp, "slowclean_marker")
    env = test_env()
    env["PATH"] = fakebin + os.pathsep + env.get("PATH", "")
    env.update({"FAKE_MODE": "slowclean", "FAKE_MARKER": marker, "DIVVY_SIG_GRACE": "6"})
    p = subprocess.Popen(["bash", DISPATCH, brief, o, tmp],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    for _ in range(100):
        if os.path.exists(marker):
            break
        time.sleep(0.1)
    p.send_signal(signal.SIGTERM)
    try:
        rc_sc = p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill(); p.wait(); rc_sc = None
    check("자손 정리 중이면 유예를 지킴 → 종료", rc_sc == 143)
    check("  자손이 정리를 마쳤다(KILL 조기 승격 없음)", os.path.exists(marker + ".clean"))

    # r5-04: 기존 출력이 없는 실행이 OUT 자리에 디렉터리를 남기면 rc8로 알린다(rc4로 묻지 않는다)
    o = out_path("odir2")
    r = run([brief, o, tmp], {"FAKE_MODE": "outdir"}, fake=fakebin)
    check("기존 출력 없이 디렉터리 잔존 → rc8", r.returncode == 8)
    check("  디렉터리를 자동 제거하지 않음", os.path.isdir(o))
    check("  사람에게 알림", "일반 파일이 아닌 항목을 남겼다" in (r.stdout + r.stderr))
    _sh.rmtree(o, ignore_errors=True)

    # r5-05: FORCE 실행은 이번 모드가 쓰지 않는 사이드카까지 치운다(낡은 진단 오인 방지)
    o = out_path("stale")
    write(o, "이전 회수분\n")
    write(o + ".err", "아주 오래된 에러\n")
    r = run([brief, o, tmp], {"FORCE": "1"}, fake=fakebin)
    check("FORCE 성공 → rc0", r.returncode == 0)
    check("  낡은 .err 제거됨(r5-05)", not os.path.exists(o + ".err"))
    check("  이번 실행 .log 존재", os.path.exists(o + ".log"))

    # ---------------------------------------------------- (G) r6 회귀
    # r6-01: danger-full-access 는 지원하지 않는다(자기 장부를 지킬 수단이 없다)
    r = run([brief, out_path("dfa")],
            {"CODEX_SANDBOX": "danger-full-access", "DIVVY_WRITE_APPROVED": "1"}, dry=True)
    check("danger-full-access → rc2 거부", r.returncode == 2)
    check("  이유를 설명함", "지원하지 않는다" in (r.stdout + r.stderr))

    # r6-02: 성공(rc0)이어도 같은 그룹의 자손이 남으면 정리하고 나서 성공을 선언한다
    o = out_path("okgc")
    marker = os.path.join(tmp, "okgc_marker")
    r = run([brief, o, tmp],
            {"FAKE_MODE": "okgc", "FAKE_MARKER": marker, "DIVVY_SIG_GRACE": "1"}, fake=fakebin)
    check("성공인데 자손 잔존 → 정리 후 rc0", r.returncode == 0 and "FAKE OK" in read(o))
    check("  잔존을 알림", "남은 프로세스" in (r.stdout + r.stderr))
    time.sleep(7)
    check("  자손의 지연 쓰기 차단(r6-02)", not os.path.exists(marker + ".late"))

    # 실사용 발견: 기본 headless 프로필과 사용자 config 격리 폴백이 올바르게 갈리는가
    r = run([brief, out_path("iso"), tmp], {"DIVVY_ISOLATE_CONFIG": "1"}, dry=True)
    check("DIVVY_ISOLATE_CONFIG=1 → --ignore-user-config", "--ignore-user-config" in r.stdout)
    check("  격리 폴백은 headless 프로필을 함께 쓰지 않음", "--profile headless" not in r.stdout)
    r = run([brief, out_path("iso2"), tmp], dry=True)
    check("  기본값은 --profile headless", "--profile headless" in r.stdout
          and "--ignore-user-config" not in r.stdout)

    # r6-03: 토큰 기록이 실패해도 잔여 잠금을 남기지 않는다 → 이후 실행이 rc6 에 영구 막히지 않는다
    o = out_path("tokfail")
    os.makedirs(o + ".lock", exist_ok=True)
    os.chmod(o + ".lock", 0o555)          # owner 파일을 쓸 수 없게
    # 잠금이 이미 있으니 rc6 이 정상. 잔여물 검사는 아래 정상 경로로 확인한다.
    r = run([brief, o, tmp], fake=fakebin)
    check("잠금 선점 상태 → rc6", r.returncode == 6)
    os.chmod(o + ".lock", 0o755)
    os.rmdir(o + ".lock")
    r = run([brief, o, tmp], fake=fakebin)
    check("  잠금 치운 뒤 정상 실행 rc0", r.returncode == 0)
    check("  잠금 잔여물 없음", not os.path.exists(o + ".lock"))

# ------------------------------------------------------ 로컬 상태 + LEDGER 분포 회귀
LEDGER = os.path.join(ROOT, "templates", "LEDGER.md")
ROSTER_TEMPLATE = os.path.join(ROOT, "templates", "ROSTER.md")
LEDGER_DISTRIBUTION = os.path.join(ROOT, "scripts", "ledger_distribution.py")

with tempfile.TemporaryDirectory() as tmp:
    env = test_env()
    state_base = os.path.join(tmp, "state")
    config_base = os.path.join(tmp, "config")
    os.makedirs(state_base, mode=0o700)
    os.makedirs(config_base, mode=0o700)
    env["XDG_STATE_HOME"] = state_base
    env["XDG_CONFIG_HOME"] = config_base
    for name in ("DIVVY_STATE_DIR", "DIVVY_CONFIG_DIR", "DIVVY_LEDGER", "DIVVY_ROSTER"):
        env.pop(name, None)
    r = subprocess.run([sys.executable, INIT_STATE, "init"], capture_output=True, text=True, env=env)
    local_ledger = os.path.join(tmp, "state", "divvy", "LEDGER.md")
    local_roster = os.path.join(tmp, "config", "divvy", "ROSTER.md")
    check("로컬 상태 init이 repo 밖에 ledger/roster 생성",
          r.returncode == 0 and read(local_ledger) == read(LEDGER) and read(local_roster) == read(ROSTER_TEMPLATE))
    r = subprocess.run([sys.executable, LEDGER_DISTRIBUTION, "--check"], capture_output=True, text=True, env=env)
    check("  ledger_distribution 기본값은 로컬 state를 사용",
          r.returncode == 0 and "CLAUDE primary: 0건" in r.stdout)
    write(local_ledger, "PERSONAL LEDGER\n")
    r = subprocess.run([sys.executable, INIT_STATE, "init"], capture_output=True, text=True, env=env)
    check("  기존 개인 ledger/roster는 덮어쓰지 않음",
          r.returncode == 0 and "ledger_status=preserved" in r.stdout and read(local_ledger) == "PERSONAL LEDGER\n")
    r = subprocess.run([sys.executable, INIT_STATE, "paths"], capture_output=True, text=True, env=env)
    check("  paths는 XDG 경로를 출력하고 파일을 수정하지 않음",
          r.returncode == 0 and f"ledger={os.path.realpath(local_ledger)}" in r.stdout
          and read(local_ledger) == "PERSONAL LEDGER\n")

with tempfile.TemporaryDirectory() as tmp:
    ledger_link = os.path.join(tmp, "ledger-link")
    roster_path = os.path.join(tmp, "roster")
    os.symlink(LEDGER, ledger_link)
    r = subprocess.run(
        [sys.executable, INIT_STATE, "init"], capture_output=True, text=True,
        env={**os.environ, "DIVVY_LEDGER": ledger_link, "DIVVY_ROSTER": roster_path},
    )
check(
    "로컬 상태 init은 심링크 대상을 거부",
    r.returncode == 2 and "reason_code=symlink_refused" in (r.stdout + r.stderr),
)

r = subprocess.run(
    [sys.executable, ROSTER_PROBE_TESTS],
    capture_output=True,
    text=True,
)
check("host-local ROSTER read-only probe 회귀", r.returncode == 0 and "OK" in r.stderr)

r = subprocess.run(
    [sys.executable, STATE_PERMISSION_TESTS],
    capture_output=True,
    text=True,
)
check("host-local state permission/security contract", r.returncode == 0)

r = subprocess.run(
    [sys.executable, RELEASE_TESTS],
    capture_output=True,
    text=True,
)
check("release metadata and workflow contract", r.returncode == 0 and "OK" in r.stderr)

# r1-03: SKILL은 host capability를 고정하거나 README/probe schema를 복제하지 않는다.
skill_text = read(SKILL)
readme_text = read(README)

for document_name, document_text in (("README", readme_text), ("SKILL", skill_text)):
    check(
        f"{document_name}는 빈 public template과 비공개 live history를 구분",
        "host-local" in document_text
        and "의도적으로 공개하지 않" in document_text
        and "사람이 채점한 결과 3건 이상" in document_text,
    )
    check(
        f"{document_name}는 global-zero 표현을 재도입하지 않음",
        not any(legacy in document_text for legacy in ("LEDGER 0건", "실사용 이력 0건", "전체 실사용 0건")),
    )


def section_between(text, start, end):
    if start not in text:
        return None
    section, separator, _ = text.split(start, 1)[1].partition(end)
    return section if separator else None


runner_boundary = section_between(skill_text, "## 러너 2개", "## 판정")
g1_boundary = section_between(skill_text, "**G1 도구 게이트 (hard).**", "**G2 브리프 비용")
g4_boundary = section_between(skill_text, "### G4 보증 위상", "## 워크플로")
check(
    "SKILL capability 경계 누락은 예외 없이 실패로 판정(r1-03)",
    section_between("start only", "start", "end") is None,
)
runner_boundary = runner_boundary or ""
g1_boundary = g1_boundary or ""
g4_boundary = g4_boundary or ""
normalized_runner_boundary = " ".join(runner_boundary.split())
check(
    "SKILL capability는 host-local·단계별 관측이고 native child/tmux를 분리(r1-03)",
    all(term in runner_boundary for term in ("host-local ROSTER", "configured", "callable", "usable", "다른 호스트"))
    and "configured만으로 callable이나 usable을 추론하지 않는다" in normalized_runner_boundary
    and all(term in runner_boundary for term in ("App native child", "tmux Workflow", "별도 capability"))
    and "schema를 복제하지 않는다" in runner_boundary
    and "현재 host-local ROSTER" in g1_boundary
    and "다른 호스트" in g1_boundary
    and "CLAUDE로 갈리는 경우" not in g1_boundary
    and "CODEX로 갈리는 경우" not in g1_boundary
    and all(term in g4_boundary for term in ("callable·usable", "App native child", "tmux Workflow", "별도로 확인"))
    and "Codex는 T-2상" not in g4_boundary
    and "접근 못 하는 것" not in runner_boundary,
)

r = subprocess.run(
    [sys.executable, LEDGER_DISTRIBUTION, "--check", LEDGER],
    capture_output=True, text=True,
)
check("LEDGER 표와 분포 집계 일치", r.returncode == 0)
check("  빈 공개 장부는 모든 집계가 0",
      "CLAUDE primary: 0건" in r.stdout
      and "CODEX primary: 0건" in r.stdout
      and "CLAUDE+CODEX 동시 primary: 0건" in r.stdout)

with tempfile.TemporaryDirectory() as tmp:
    stale_ledger = os.path.join(tmp, "LEDGER.md")
    original = read(LEDGER)
    separator = "|------|------|------|---------|----------|---------------------------|--------|----|--------|------------|----------|---------------|------------|------------|"
    completed = "| L-01 | 2026-01-01 | 공개 fixture | CODEX | tie | 1줄 / 1패스 | — | 低 | 없음 | — | 해당없음 | **완료** — fixture | — | |"
    nonterminal = completed.replace("**완료** — fixture", "**미착수** — fixture")
    unknown_status = completed.replace("**완료** — fixture", "**보류** — fixture")

    write(stale_ledger, original.replace(separator, separator + "\n" + completed, 1))
    r = subprocess.run(
        [sys.executable, LEDGER_DISTRIBUTION, "--check", stale_ledger],
        capture_output=True, text=True,
    )
    check("LEDGER 행만 바뀌면 낡은 분포를 실패로 탐지",
          r.returncode == 1 and "분포 불일치" in r.stderr)

    placeholder_ledger = os.path.join(tmp, "LEDGER_placeholder.md")
    placeholder = original.replace(separator, separator + "\n" + nonterminal, 1)
    write(placeholder_ledger, placeholder)
    r = subprocess.run(
        [sys.executable, LEDGER_DISTRIBUTION, "--check", placeholder_ledger],
        capture_output=True, text=True,
    )
    check("LEDGER 빈 결과 표기는 완료로 세지 않음", r.returncode == 0)

    unknown_ledger = os.path.join(tmp, "LEDGER_unknown.md")
    unknown = original.replace(separator, separator + "\n" + unknown_status, 1)
    write(unknown_ledger, unknown)
    r = subprocess.run(
        [sys.executable, LEDGER_DISTRIBUTION, "--check", unknown_ledger],
        capture_output=True, text=True,
    )
    check("LEDGER 알 수 없는 결과 상태는 오류", r.returncode == 2 and "알 수 없는" in r.stderr)

    duplicate_ledger = os.path.join(tmp, "LEDGER_duplicate.md")
    duplicate = original.replace(
        "- CLAUDE primary: 0건", "- CLAUDE primary: 0건\n- CLAUDE primary: 999건", 1
    )
    write(duplicate_ledger, duplicate)
    r = subprocess.run(
        [sys.executable, LEDGER_DISTRIBUTION, "--check", duplicate_ledger],
        capture_output=True, text=True,
    )
    check("LEDGER 모순된 중복 분포 항목은 실패",
          r.returncode == 1 and "정확히 1개 필요" in r.stderr)

    extra_metric_ledger = os.path.join(tmp, "LEDGER_extra_metric.md")
    extra_metric = original.replace(
        "- user: 0건", "- user: 0건\n- 폐기된 지표: 7건", 1
    )
    write(extra_metric_ledger, extra_metric)
    r = subprocess.run(
        [sys.executable, LEDGER_DISTRIBUTION, "--check", extra_metric_ledger],
        capture_output=True, text=True,
    )
    check("LEDGER 생성 집계에 없는 유령 항목은 실패",
          r.returncode == 1 and "생성 집계에 없는" in r.stderr)

# ------------------------------------------------------ OMX #3420 로컬 핫픽스
SOURCE_VULNERABLE = '''\
      const unmatchedStopSession = failure.stopReason === "session_scope_unmatched";
      // keep generic bounded behavior
      if (pointerCannotAuthorizeThisCwd || unmatchedStopSession || stopHookActive) {
        outputJson = null;
      }
'''
DIST_VULNERABLE = '''\
            const unmatchedStopSession = failure.stopReason === "session_scope_unmatched";
            // keep generic bounded behavior
            if (pointerCannotAuthorizeThisCwd || unmatchedStopSession || stopHookActive) {
                outputJson = null;
            }
'''

def make_fake_omx(root, version="0.20.4", source=SOURCE_VULNERABLE, dist=DIST_VULNERABLE):
    os.makedirs(os.path.join(root, "src", "scripts"), exist_ok=True)
    os.makedirs(os.path.join(root, "dist", "scripts"), exist_ok=True)
    write(os.path.join(root, "package.json"), json.dumps({"version": version}))
    write(os.path.join(root, "src", "scripts", "codex-native-hook.ts"), source)
    write(os.path.join(root, "dist", "scripts", "codex-native-hook.js"), dist)

def run_hotfix(root, command):
    return subprocess.run(
        [sys.executable, OMX_HOTFIX, command, "--omx-root", root],
        capture_output=True, text=True,
    )

with tempfile.TemporaryDirectory() as tmp:
    make_fake_omx(tmp)
    r = run_hotfix(tmp, "status")
    check("OMX 핫픽스 status가 취약 source/dist 탐지", r.returncode == 0 and r.stdout.count("vulnerable") == 2)

    r = run_hotfix(tmp, "apply")
    source_path = os.path.join(tmp, "src", "scripts", "codex-native-hook.ts")
    dist_path = os.path.join(tmp, "dist", "scripts", "codex-native-hook.js")
    source_patched = read(source_path)
    dist_patched = read(dist_path)
    check("OMX 핫픽스가 source/dist 양쪽에만 적용됨",
          r.returncode == 0
          and 'pointer.status === "identity-indeterminate"' in source_patched
          and 'pointer.status === "identity-indeterminate"' in dist_patched
          and "keep generic bounded behavior" in source_patched)
    check("  원본 백업 생성",
          os.path.exists(source_path + ".divvy-omx-3420.bak")
          and os.path.exists(dist_path + ".divvy-omx-3420.bak"))

    before = (source_patched, dist_patched)
    r = run_hotfix(tmp, "apply")
    check("  재적용은 멱등 no-op",
          r.returncode == 0 and "변경 없음" in r.stdout and before == (read(source_path), read(dist_path)))

    r = run_hotfix(tmp, "restore")
    check("  백업에서 원본 복원",
          r.returncode == 0 and read(source_path) == SOURCE_VULNERABLE and read(dist_path) == DIST_VULNERABLE)
    check("  복원 후 백업 제거",
          not os.path.exists(source_path + ".divvy-omx-3420.bak")
          and not os.path.exists(dist_path + ".divvy-omx-3420.bak"))

with tempfile.TemporaryDirectory() as tmp:
    make_fake_omx(tmp, version="0.20.5")
    r = run_hotfix(tmp, "apply")
    check("알 수 없는 OMX 버전은 수정 거부",
          r.returncode == 2 and "지원하지 않는 OMX 버전" in r.stderr
          and read(os.path.join(tmp, "src", "scripts", "codex-native-hook.ts")) == SOURCE_VULNERABLE)

with tempfile.TemporaryDirectory() as tmp:
    make_fake_omx(tmp, source=SOURCE_VULNERABLE.replace("unmatchedStopSession", "changedUpstream", 1))
    r = run_hotfix(tmp, "apply")
    check("source/dist 코드 형태가 다르면 수정 거부",
          r.returncode == 2 and "예상과 다름" in r.stderr
          and not any(name.endswith(".divvy-omx-3420.bak") for _base, _dirs, files in os.walk(tmp) for name in files))

with tempfile.TemporaryDirectory() as tmp:
    make_fake_omx(tmp)
    dist_backup = os.path.join(tmp, "dist", "scripts", "codex-native-hook.js.divvy-omx-3420.bak")
    write(dist_backup, "existing backup")
    r = run_hotfix(tmp, "apply")
    source_backup = os.path.join(tmp, "src", "scripts", "codex-native-hook.ts.divvy-omx-3420.bak")
    check("기존 백업이 하나라도 있으면 새 백업 전에 거부",
          r.returncode == 2 and "기존 백업" in r.stderr and not os.path.exists(source_backup))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
