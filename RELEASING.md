# Releasing divvy

이 문서는 divvy의 반복 가능한 릴리즈 계약이다. 현재 버전의 단일 정본은 `VERSION`이며,
`CHANGELOG.md`의 같은 버전 절이 GitHub Release notes의 정본이다. 이 저장소는 안정 버전
`MAJOR.MINOR.PATCH`만 발행하며 prerelease와 build metadata는 사용하지 않는다.

## 버전 규칙

- 수정: 기존 계약을 지키는 버그 수정은 `patch`를 올린다.
- 기능: 하위 호환되는 사용자 가시적 capability는 `minor`를 올린다.
- 비호환: `1.0.0` 전에는 `minor`, `1.0.0`부터는 `major`를 올린다.
- 한 번 공개한 버전과 태그는 재사용하거나 다른 커밋으로 옮기지 않는다.
- 릴리즈 PR에서 `VERSION`과 `CHANGELOG.md`를 함께 갱신한다.
- CHANGELOG 제목은 정확히 `## [MAJOR.MINOR.PATCH] - YYYY-MM-DD`이고 같은 버전은 한 번만 쓴다.
- 변경 항목에는 필요에 따라 `Added`, `Changed`, `Fixed`, `Removed`, `Security` 제목을 사용한다.

다음 버전은 스크립트로 계산한다.

```bash
python3 scripts/release.py current
python3 scripts/release.py next patch
python3 scripts/release.py next minor
python3 scripts/release.py next major
```

## 1. 릴리즈 PR 검증

릴리즈 PR을 만들기 전에 아래를 모두 통과시킨다.

```bash
python3 scripts/release.py check
python3 tests/test_release.py
env -u CODEX_SANDBOX python3 tests/run_tests.py
python3 scripts/ledger_distribution.py --check templates/LEDGER.md
git diff --check
```

PR을 병합한 뒤에만 태그 단계로 이동한다. 태그 생성과 push는 코드 변경 PR과 분리된 공개 릴리즈
승인 경계다.

## 2. 최신 main에 서명 태그 생성

로컬 변경이 없는 저장소에서 최신 `main`과 모든 태그를 받은 뒤 실행한다.

```bash
git fetch origin main --tags
git switch main
git pull --ff-only origin main
test -z "$(git status --porcelain)"

VERSION_VALUE="$(python3 scripts/release.py current)"
python3 scripts/release.py check --tag "v$VERSION_VALUE" --history

mkdir -p .re0/release
python3 scripts/release.py notes --output .re0/release/RELEASE_NOTES.local.md
git tag -s "v$VERSION_VALUE" -F .re0/release/RELEASE_NOTES.local.md --cleanup=verbatim
git tag -v "v$VERSION_VALUE"
git push origin "v$VERSION_VALUE"
```

`git tag -s`는 로컬 Git에 설정된 GPG 또는 SSH 서명 구성을 사용한다. 서명이나 `git tag -v` 검증이
실패하면 push하지 않는다. `release.py notes --output`은 기존 파일을 덮어쓰지 않으므로, 이전 scratch
파일을 검토해 보관했거나 새 경로를 준비한 뒤 다시 실행한다. `.re0/`는 Git에서 제외된다.

## 3. 자동 발행 확인

태그 push는 `.github/workflows/release.yml`을 실행한다. 워크플로는 다음 조건을 모두 다시 확인한 뒤
GitHub Release를 만든다.

- 태그 이름이 정확히 `v<VERSION>`이다.
- 현재 버전이 기존 안정 릴리즈보다 크다.
- annotated tag가 실행 시점의 정확한 `origin/main` head를 가리킨다.
- 릴리즈 메타데이터, 전체 테스트, LEDGER 검사가 통과한다.

워크플로는 annotated tag 여부를 검사하지만 서명의 신뢰성까지 검증하지는 않는다. 서명 신뢰 검증은
push 전 `git tag -v`가 담당한다.

```bash
gh run list --workflow release.yml --limit 5
gh release view "v$VERSION_VALUE"
```

저장소 Actions에는 기본 `GITHUB_TOKEN`의 `contents: write` 권한으로 Release를 만들 수 있어야 한다.
별도 개인 액세스 토큰은 사용하지 않는다.

## 실패와 복구

- 태그 push 전 검증 실패: 메타데이터나 코드를 PR로 수정하고 다시 검증한다. 공개 상태는 바뀌지 않았다.
- 태그는 push됐지만 Release 생성 실패: Actions 로그를 확인하고 수정 PR에서 새 버전을 준비한다.
  공개 태그를 삭제하거나 옮기는 것을 정상 복구 절차로 사용하지 않는다.
- Release notes만 잘못된 경우: GitHub Release 본문을 고친다. 태그와 버전은 유지한다.
- 공개된 코드나 버전이 잘못된 경우: 수정 내용을 다음 patch 릴리즈로 발행한다.

자동 발행은 릴리즈를 만드는 역할만 맡는다. PR 병합, 서명 태그 생성, 태그 push는 자동으로 수행하지 않는다.
