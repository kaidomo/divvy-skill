# Releasing divvy

이 문서는 divvy의 병합 기반 릴리즈 계약이다. `VERSION`이 버전 단일 정본이고,
태그된 metadata snapshot의 같은 `CHANGELOG.md` 절이 GitHub Release 본문의 정본이다.
안정 버전 `MAJOR.MINOR.PATCH`만 발행하며 공개한 태그나 Release를 이동·삭제·덮어쓰지 않는다.

## 기본 정책

- 보호된 `main`에 연결된 PR이 merge되면 기본적으로 릴리즈 대상이다.
- merge 시점에 허용된 actor가 설정한 `release:skip`만 그 merge의 즉시 버전/CHANGELOG 절/태그/Release cut을 생략한다.
- `release:major`, `release:minor`, `release:patch`는 하나만 쓸 수 있고 semantic floor를 낮출 수 없다.
- `0.x` breaking과 `feat`는 minor, `fix`와 알 수 없는 형식은 patch가 floor다.
- merge/squash commit의 불변 메시지만 분류와 note에 사용한다. 현재 PR body/title은 사용하지 않는다.
- 여러 merge가 worker 실행 전에 쌓이면 순서가 보존된 하나의 truthful batch로 합칠 수 있다.

레이블 이름과 설명은 prompt-pack의 literal contract다. `.github/release_label_actors`에 기록된 정확한
GitHub login만 release label evidence를 만들 수 있다. timeline pagination 누락, merge 이후 label 변경,
직접 push, rebase/모호한 PR 연결은 효과 없이 닫힌다.

## 버전과 snapshot

metadata commit의 첫 parent는 frozen source frontier다. 태그는 그 immutable metadata commit을 가리키고,
그 commit은 현재 보호된 `main`에서 도달 가능해야 한다. 이후 main merge가 있어도 태그를 최신 main tip으로
옮기지 않는다. 태그된 snapshot에서 다음 네 값이 정확히 같아야 한다.

1. `VERSION`
2. 최신 dated `CHANGELOG.md` 절
3. trusted signed annotated `v<VERSION>` tag
4. 같은 tag/name/body의 GitHub Release

CHANGELOG의 curated prose는 보존하고 자동 항목은 검증된 `- <immutable title> (#N)` 형식으로 추가한다.

## 검증

```bash
python3 scripts/release.py check
python3 tests/test_release.py
env -u CODEX_SANDBOX python3 tests/run_tests.py
python3 scripts/ledger_distribution.py --check templates/LEDGER.md
git diff --check
```

JSON evidence는 파일이나 stdin으로 전달한다. shell source로 보간하지 않는다.

```bash
python3 scripts/release.py classify --input classification.json
python3 scripts/release.py reconcile --input envelopes.json
python3 scripts/release.py authorize --input authorization.json
python3 scripts/release.py resume --input repair.json
python3 scripts/release.py compare-release --input release-state.json
python3 scripts/release.py verify-tag "v$(python3 scripts/release.py current)" --target <metadata-commit>
```

## 권한과 자동화

`.github/workflows/release.yml`은 `push main`을 직렬화한다. credential-free authorize/test가 repository,
ref, source frontier, workflow revision과 digest를 고정한 뒤에만 `release-automation` environment의 effect job이
App/signing secret 이름을 참조한다. 기본 workflow token은 read-only이고 job별 최소 권한만 올린다.

활성화 전에는 다음을 별도 승인하고 read back해야 한다: merge/squash-only, main PR/CI protection,
`v*` update/delete 금지, actor-wide bypass 위험을 수용한 repo-scoped App 또는 metadata-PR fallback,
environment/variables/secret names, public bot signer, CODEOWNERS human review, Release immutability 지원 여부.
private key/token/passphrase 값은 로그·PR·receipt에 기록하지 않는다.

Divvy R1 canary는 전용 단일-repository App의 actor-wide direct bypass 모델을 선택했다. 이 선택은
workflow의 metadata-only path 및 pre-secret revision/batch 검증을 구현하기 위한 것이며, App 설치나
ruleset·environment·secret·signer 변경 자체의 권한은 아니다. 그 설정은 별도 packet과 readback 없이는 활성화하지 않는다.

## 실패와 복구

- metadata 전 실패: 현재 main/tag/coverage를 다시 읽고 한 번만 재계산한다.
- metadata만 존재: 전체 batch identity와 first parent/tree/path를 확인한 뒤 같은 commit에 tag한다.
- 올바른 tag만 존재: trusted target과 tagged notes를 확인한 뒤 누락된 Release만 만든다.
- 정확히 같은 Release: 성공 no-op이며 추가 효과는 0이다.
- 잘못된 공개 tag/Release: 이동·삭제·수정하지 않고 quarantine한다. reviewed 새 버전으로만 교정한다.
- `workflow_dispatch` repair는 schema, batch, frontier, prior tag, PR/label-note digest, metadata tree,
  candidate, phase, stop owner 전체가 같을 때만 같은 상태 기계를 재개한다.

공개 객체 생성과 repository 설정은 항상 별도 task-scoped authority 경계다.
