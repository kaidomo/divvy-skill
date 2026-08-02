# divvy

**두 러너(Claude Code · Codex CLI)를 동시에 물고 있을 때, 일마다 누가 할지 정하고 그 배정대로 실제로 돌리는 분배기.**

Claude Code 스킬 1개. 판정만 하는 조언 도구가 아니라, Codex 몫은 `codex exec`로 직접 던져 산출물을 회수한다.

## 왜

두 도구를 다 구독해도 한쪽이 유휴가 된다. **[가설]** 그 원인은 실력 차이가 아니라 **맥락 이전 비용**이다 —
"설명하느니 내가 하지"가 매번 이긴다. divvy는 그 비용을 판정에 명시적으로 넣어(G2) 그 편향을 깬다.

**아직 실측이 아니다**(LEDGER 0건). 경쟁 원인이 최소 셋 있다: 도구 편중(G1에서 갈리는 일이 원래 많다),
작업 구성 자체, 노출 승인 보류. 그래서 LEDGER는 러너 총계 말고 **결정출처별·미위임사유별**로 따로 센다 —
원인은 그 집계가 쌓인 뒤에 말한다. 어느 러너가 잘했는지도 사람이 기록한다(자기채점 금지).

과금은 판정 기준이 아니다. 양쪽 다 정액이면 "싼 쪽으로 밀기"가 성립하지 않는다.
남는 기준은 **도구 가용성 · 브리프 비용 · 적성 · 오판 비용**뿐이다.

## 판정 구조

배정은 두 질문이다: 누가 하나(primary), 한 명으로 충분한가(보증).

```
primary 선택
  사용자가 지정했으면  그 지정이 primary · 결정출처=user · 게이트 적용 안 함(사후 정당화 금지)
  아니면 — 처음 갈린 게이트에서 멈춘다
    G1 도구 게이트   한쪽이 **아예 못 하는** 도구가 필요한가   → 근거 T-n
    G2 브리프 비용   브리프 크기 × 실행 패스 수 2축            → 측정값 기록
    G3 적성          ROSTER 행 매칭, 상위 계층만 비교          → 근거 C-n / K-n
    타이브레이커     안 갈리면 CODEX (유휴 해소)

보증 위상 — primary와 별개로 항상 평가
  G4 오판 비용     高 → 독립 검토 + 사람 게이트 / 中 → 검토 1회 / 低 → 단독
                   ★ 검토자는 항상 primary가 아닌 러너 (자기검토는 보증이 아니다)
                   ★ 선행조건(검토 가능한 근거 · 노출 승인) 불충족이면 `G4 blocked`
                     — 검토 완료로 적지 않고 대체 경로를 기록한다
```

## 파일

| 파일 | 역할 |
|---|---|
| `SKILL.md` | 판정 게이트·워크플로·규칙·검증(스킬 본문) |
| `ROSTER.md` | 러너 명부. `[사실]`(관찰된 도구) / `[도출]`(도구 사실의 필연적 귀결 = 한쪽이 못 한다) / `[실측]`(LEDGER 3건 이상으로 확인된 우위) / `[가설]`(측정 없음) 4등급. G3 우선순위는 `P1 = 사실·도출` > `P2 = 실측` > `P3 = 가설` |
| `LEDGER.md` | 배정 기록. `L-<nn>` 불변 ID. **우열 칸은 사람만 채운다** |
| `scripts/dispatch.sh` | 위임 실행기(`codex exec` 래퍼). `headless` 프로필·read-only 기본, 성공 판정 3조건 |
| `scripts/omx_stop_hotfix.py` | OMX 0.20.4의 `identity-indeterminate` Stop 반복을 검사·완화·복원하는 명시적 로컬 도구 ([upstream #3420](https://github.com/Yeachan-Heo/oh-my-codex/issues/3420)) |
| `scripts/ledger_distribution.py` | LEDGER 표에서 분포 집계를 생성하고 문서의 수치가 맞는지 검사 |
| `config/headless.config.toml` | Codex headless 프로필 설치 템플릿. 기존 사용자 파일은 덮어쓰지 않는다. |
| `tests/run_tests.py` | 151건. 가짜 `codex`와 임시 OMX fixture로 실제 실행 경로·신호·정리 실패·핫픽스 안전장치를 검증(진짜 Codex 미호출, 실제 OMX 미수정) |

## 빠른 시작

전제: [Claude Code](https://claude.com/claude-code)와 [Codex CLI](https://developers.openai.com/codex/cli)가
설치돼 있고 **둘 다 로그인**돼 있어야 한다. divvy는 두 러너를 부르는 얇은 층이지 둘을 설치해주지 않는다.

```bash
# 0. 전제 확인 — 둘 다 응답해야 한다
claude --version
codex --version          # 개발 기준: codex-cli 0.145.0

# 1. 설치
git clone https://github.com/kaidomo/divvy-skill.git ~/divvy
mkdir -p ~/.codex
if [ ! -e ~/.codex/headless.config.toml ]; then
  cp ~/divvy/config/headless.config.toml ~/.codex/headless.config.toml
else
  echo "기존 ~/.codex/headless.config.toml 보존 — 내용을 직접 확인하라"
fi
mkdir -p ~/.claude/skills                              # 없을 수 있다
ln -s ~/divvy ~/.claude/skills/divvy                   # 심링크 이름이 스킬 이름이 된다

# 2. Codex 프로필·인증 확인(미로그인이면 `codex login`)
codex exec --profile headless --skip-git-repo-check --sandbox read-only "hi"

# 선택: 부모 Codex App에서 OMX identity-indeterminate Stop 반복이 재현될 때만
python3 ~/divvy/scripts/omx_stop_hotfix.py status
python3 ~/divvy/scripts/omx_stop_hotfix.py apply

# 3. 설치 확인 — Claude Code를 켜고
#    "이 작업들 분배해줘" 처럼 물으면 divvy 가 뜬다. 안 뜨면 세션을 새로 시작한다.

# 4. 첫 위임 — 브리프를 파일로 쓰고 던진다
mkdir -p ~/divvy-runs && cd ~/divvy-runs
cat > brief.md <<'EOF'
작업: <작업루트>의 <대상>을 읽고 <무엇>을 찾아라.
산출: <형식>. 주장마다 파일·라인 근거를 붙여라.
이미 내린 결정(재논의 금지):
- <이건 이미 정해졌다>
EOF
~/divvy/scripts/dispatch.sh brief.md out.md ~/작업할레포     # 스크립트는 절대경로로 부르는 게 안전하다

# 5. 회수 확인 — 3조건은 스크립트가 찍는다. 네 번째는 사람 몫이다
cat out.md                # 요청에 답했는지 읽는다(rc 0이어도 "작업 불가"일 수 있다)
```

**브리프에 뭘 적나**: 작업 · 산출 형식 · **이미 내린 결정**(재논의 금지 목록). 세 번째가 핵심이다 —
빠뜨리면 Codex가 결정된 것을 다시 논의하고, 그 회수 비용이 위임 이득을 먹는다.

**첫 `L-<nn>`은 `L-01`이다**(2자리, 1부터, 재사용·재번호 금지 — `LEDGER.md` 기록 규칙).

**설치 위치 주의**: `~/.claude/skills/`가 다른 git 레포라면 심링크가 `?? divvy`로 뜬다.
그 레포에서 `git add -A` 하면 남의 레포에 커밋되니 `.gitignore`에 넣거나 개별 add 하라.

## 위임 실행

```bash
scripts/dispatch.sh <브리프파일> <출력파일> [작업디렉터리]
```

- 기본 `--profile headless` + `--sandbox read-only` + effort `medium`. `headless` 프로필은
  `~/.codex/headless.config.toml`에서 `features.hooks=false`로 두며, 사용자 설정·플러그인은 유지한다.
  수정이 필요하면 Codex가 diff를 제안하고 **사람이 적용**한다.
- `danger-full-access`는 **지원하지 않는다**(rc 2). 샌드박스가 없으면 위임 작업이 잠금·백업을 어디서든
  지울 수 있어 회수 무결성을 보장할 수 없다 — 정말 필요하면 `codex`를 직접 부르는 쪽이 정직하다.
- read-only가 아닌 샌드박스로 실제 실행하려면 `DIVVY_WRITE_APPROVED=1`이 필요하다(없으면 rc 5로 거부).
- `DRY_RUN=1`은 Codex를 호출하지 않고 계획만 출력한다. **검사는 하되 잠금·백업을 만들지 않는다** —
  스모크 테스트가 상태를 남기면 안 된다.
- **성공 = 종료코드 0 그리고 출력 파일 존재 그리고 공백 아님.** 종료코드만으로 성공을 찍지 않는다 —
  "못 찾았다"와 "돌지 않았다"가 구분되지 않으면 회수한 사람이 오판한다.
- **단 그 셋은 "돌았다"까지만 보증한다.** 산출물이 요청에 답했는지는 기계가 판정할 수 없다 —
  회수한 쪽이 읽어야 한다. 실측(2026-07-30): Codex의 훅·플러그인 preflight가 런을 중단시키고
  "리뷰 불가" 한 줄을 남겼는데 rc 0 · 파일 존재 · 공백 아님을 모두 통과했다. 이런 회수는 성공이
  아니라 재실행이나 CLAUDE 되돌리기 대상이다. 기본 `headless` 프로필 자체가 실패할 때만
  **`DIVVY_ISOLATE_CONFIG=1`**로 완전 격리 재실행한다
  (`--ignore-user-config`를 붙여 훅·플러그인·프로필을 로드하지 않는다. 인증은 그대로).
  2026-07-30 하루에 2회 겪었다: ① 리뷰 라운드가 "리뷰 불가" 한 줄로 rc 0 회수 ② Stop 훅이 종료를
  계속 거부해 판정을 마치고도 내보내지 못하고 루프.
- **경로 계약**: 인자 경로는 호출한 PWD 기준, **브리프 본문의 상대경로는 작업디렉터리 기준**(`--cd`).
- **최소 산출 계약(`DIVVY_EXPECT`)**: 산출물에 반드시 들어가야 하는 고정 문자열이 있으면 브리프에
  `<!-- DIVVY_EXPECT: 문자열 -->` 마커를 넣는다. 마커마다 여는 표기·콜론·비어 있지 않은 문자열·닫는
  `-->`가 **같은 행**에 있어야 하며, 마커가 하나라도 있는 브리프는 UTF-8이어야 한다. 마커가 여러 개면
  산출물이 **전부** 포함해야 한다. 문법 오류·UTF-8 오류·하나라도 미충족이면 rc 9로 실패하고 이번 실행의
  산출물(캡처 파일)을 남기지 않는다. 마커가 없으면 기존 성공 3조건만 적용한다.

  ```markdown
  작업: 변경점을 검토하고 판정을 보고한다.
  <!-- DIVVY_EXPECT: 최종 판정: -->
  <!-- DIVVY_EXPECT: 검증 결과: -->
  ```
- 출력 이름은 `.log`/`.err`/`.forcebak`/`.lock`으로 끝날 수 없다(**대소문자 무관** — macOS 기본 FS는
  대소문자를 구분하지 않아 `x.LOG`가 `x.log`와 같은 파일이 된다). 관리 경로가 심링크거나 서로 같은
  실체(하드링크)면 거부한다.
- 중단(INT·TERM)은 Codex **프로세스 그룹**에 전달된다(자손까지). 유예 동안 **그룹이** 비기를 기다리고
  (자손의 정상 정리를 끊지 않는다), 안 비면 그룹에 KILL — `DIVVY_SIG_GRACE`(기본 5초).
  그 경로도 실패 경로와 같은 정리를 지난다.
  - **경계**: 신호는 프로세스 그룹까지만 닿는다. 자손이 `setsid`로 새 세션을 만들어 이탈하면
    종료시킬 수 없다(bash·macOS에 cgroup 류 containment가 없다). 그런 프로세스가 남을 수 있는
    작업은 read-only로 돌리거나, 브리프에서 detached 백그라운드 실행을 금지하라.
- **쓰기 샌드박스에서는 출력 경로를 작업루트 안에 둘 수 없다**(rc 7). 위임된 작업이 잠금·백업을
  지울 수 있어서다. 잠금에는 소유 토큰이 있고, 남의 것으로 바뀌면 해제하지 않는다(rc 8).
- `FORCE` 실행은 `.log`·`.err`를 **둘 다** 치운다 — 이번 모드가 쓰지 않는 쪽에 이전 실행 내용이
  남으면 낡은 오류를 이번 진단으로 오인한다.

종료코드: `0` 성공 · `1` 입력/경로 · `2` 환경변수 값 · `3` 기존 산출물·고립 백업 · `4` 실행 실패 ·
`5` 쓰기 승인 없음 · `6` 동일 출력 동시 실행 · `7` 경로 충돌·관리 이름 오용 ·
`8` 정리 실패 — **다른 실패보다 우선해 보고한다**(원래 rc는 메시지에 남는다) ·
`9` `DIVVY_EXPECT` 산출 계약 오류·미충족. rc 8에서는 산출물이 유효할 수 있으나 남은 상태를 봐야 한다.

## OMX Stop 반복 로컬 완화

Codex App의 부모 대화에서 divvy가 `codex exec` 결과를 회수한 뒤 OMX Stop hook이
`identity-indeterminate`를 반복 주입하는 현상은 upstream
[#3420](https://github.com/Yeachan-Heo/oh-my-codex/issues/3420)에서 추적한다. divvy 자체 원인으로
단정하지 않으며, `headless` 프로필은 자식 runner의 hook만 끄므로 부모 App의 Stop hook에는 닿지 않는다.

개인 컴퓨터에서 같은 현상이 재현될 때만 아래 도구를 명시적으로 실행한다. `dispatch.sh`는 이 도구를
자동 호출하거나 전역 OMX 설치본을 자동 수정하지 않는다.

```bash
python3 scripts/omx_stop_hotfix.py status   # 설치본과 source/dist 상태 확인
python3 scripts/omx_stop_hotfix.py apply    # OMX 0.20.4의 정확한 취약 형태일 때만 적용
python3 scripts/omx_stop_hotfix.py restore  # 생성된 백업에서 원복
```

도구는 source와 실제 hook이 실행하는 dist를 함께 검사하고, `identity-indeterminate` Stop에만 neutral
output 조건을 추가한다. 다른 포인터 실패의 기존 bounded/fail-closed 분기는 바꾸지 않는다. 이미 같은
수정이 있으면 no-op이며, 버전이나 코드 형태가 예상과 다르면 파일을 건드리지 않고 거부한다. 적용 시
각 파일 옆에 `.divvy-omx-3420.bak` 백업을 만든다. OMX 업데이트·재설치는 패치를 덮어쓸 수 있으므로
그 뒤에는 `status`를 다시 실행한다. upstream 공식 수정이 배포되면 로컬 핫픽스 대신 그 버전을 사용한다.

## 테스트

```bash
python3 tests/run_tests.py                              # 151 passed
python3 scripts/ledger_distribution.py --check LEDGER.md  # 표↔분포 일치 검사
```

**위 "위임 실행" 절의 약속들(read-only 기본·기존 출력 보호·잠금·신호 전달·자손 정리·백업 복구·
성공 3조건·종료코드)은 각각 대응하는 테스트가 고정한다.** 문서를 읽는 것만으로는 확인할 수 없으니,
의심되면 이 스위트를 돌려 실제 동작을 보라.

진짜 Codex를 부르지 않는다. 사전 거부·dry-run 경로와, PATH 앞에 세운 가짜 `codex`로 실제 실행 경로
(rc·stdin 내용·`-o` 캡처·stdout fallback·실패 정리·백업 복구·원자 잠금·**신호 전달**·**정리 실패 주입**)를
함께 고정한다.

## 상태

- **실사용 이력 0건.** ROSTER의 적성 행 중 `[가설]`은 아직 실측되지 않았다. 우열 주장은 하지 않는다.
- 가드·경로·실패·중단·정리·빈 LEDGER 집계·OMX 핫픽스 안전장치 **151건 통과**.
- 공개본은 개인 작업 이력과 호스트 환경 보고서를 포함하지 않는 새 이력으로 시작한다.

## 경계

- **`modelchk`이 아니다** — 티어·추론량 판정(모델 중립)은 그쪽 몫. divvy는 그 결과를 구체 러너에 묶는다.
- **`peer-review`가 아니다** — 교차리뷰 라운드·triage 계약은 그쪽 소유. divvy는 검토자만 지정해 넘긴다.
- 러너 3개 이상, 라우팅 자동학습, 비용 최적화는 비목표다.
