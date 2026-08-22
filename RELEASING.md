# Releasing divvy

이 문서는 divvy의 수동 tag-dispatch 릴리즈 계약이다. PR/label evidence와 batch 계산은 로컬 계획·검증 입력이며
자동으로 공개 객체를 만들지 않는다. `VERSION`이 버전 단일 정본이고,
태그된 metadata snapshot의 같은 `CHANGELOG.md` 절이 GitHub Release 본문의 정본이다.
안정 버전 `MAJOR.MINOR.PATCH`만 발행하며 공개한 태그나 Release를 이동·삭제·덮어쓰지 않는다.

## 기본 정책

- 보호된 `main`에 연결된 PR의 evidence는 로컬 adapter가 후보를 계획하는 데 사용될 수 있다.
- 공개 Release 효과는 `main`에서 수동으로 실행한 정확한 stable tag dispatch에서만 발생한다.
- `release:major`, `release:minor`, `release:patch`는 하나만 쓸 수 있고 semantic floor를 낮출 수 없다.
- `0.x` breaking과 `feat`는 minor, `fix`와 알 수 없는 형식은 patch가 floor다.
- merge/squash commit의 불변 메시지만 분류와 note에 사용한다. 현재 PR body/title은 사용하지 않는다.
- 여러 merge가 worker 실행 전에 쌓이면 순서가 보존된 하나의 truthful batch로 합칠 수 있다.

레이블 이름과 설명은 prompt-pack의 literal contract다. `.github/release_label_actors`에 기록된 정확한
GitHub login만 release label evidence를 만들 수 있다. timeline pagination 누락, merge 이후 label 변경,
직접 push, rebase/모호한 PR 연결은 효과 없이 닫힌다.

## 버전과 snapshot

metadata commit의 첫 parent는 frozen source frontier다. 태그는 그 immutable metadata commit을 가리키고,
그 commit은 dispatch 시점의 보호된 `main` tip과 정확히 일치해야 한다. 이후 main merge가 있어도 태그를 최신 main tip으로
옮기지 않는다. 태그된 snapshot에서 다음 네 값이 정확히 같아야 한다.

1. `VERSION`
2. 최신 dated `CHANGELOG.md` 절
3. trusted signed annotated `v<VERSION>` tag
4. 같은 tag/name/body의 GitHub Release

CHANGELOG의 curated prose는 보존하고 자동 항목은 검증된 `- <immutable title> (#N)` 형식으로 추가한다.

## Trusted tag 생성 절차

1. 변경을 사람 리뷰·CI 후 보호된 `main`에 반영하고, 깨끗한 작업트리에서 `git fetch origin main --tags`를 실행한다.
2. `main`을 fast-forward로 최신화하고 `VERSION`과 dated `CHANGELOG` 절을 검증한다.
3. SSH 서명 identity와 허용 signer 파일을 먼저 설정한다. `git config gpg.format ssh`, `git config user.signingkey <private-key>`, `git config gpg.ssh.allowedSignersFile .github/release_allowed_signers`를 사용하고, 실제 운영 public key만 허용한다. placeholder key는 추가하지 않는다. notes를 만든 뒤 `git tag -s v<VERSION> -F <notes-file> --cleanup=verbatim`으로 signed annotated tag를 만든다.
4. `git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile=.github/release_allowed_signers tag -v v<VERSION>`와 `python3 scripts/release.py verify-tag v<VERSION> --target <main-tip>`으로 서명·대상을 검증한다. 허용 signer와 tag bytes가 검토된 값이 아니면 중단한다.
5. 승인된 계정으로 `git push origin refs/tags/v<VERSION>`만 수행한다. 기존 tag를 force-push하거나 이동하지 않는다.
6. `gh workflow run release.yml --ref main -f tag=v<VERSION>`로 수동 dispatch하고, workflow의 원격 tag/commit/Release readback receipt를 확인한다.

tag가 존재하지만 아직 Release가 없는 복구도 dispatch 시점의 live `main`이 tag commit과 정확히 같을 때만 허용한다. 그렇지 않으면 tag를 재사용하지 않고 새 버전으로 준비한다.

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

`find-release`는 다른 서브커맨드와 달리 `--input`이 JSON **객체**가 아니라 `gh api releases` 응답 JSON **배열**(단일 페이지 또는 `--paginate --slurp`의 페이지-배열 둘 다 허용)을 기대한다.

```bash
python3 scripts/release.py find-release --tag v0.1.1 --input release-list.json
```

## 권한과 자동화

`.github/workflows/release.yml`은 `workflow_dispatch(tag)`만 릴리즈 효과 경계로 허용한다. `main`에서만 실행되는
credential-free verify job이 VERSION, dated CHANGELOG, exact current-main target, trusted annotated tag와 signer를 먼저 고정한 뒤에만
publish job이 실행된다. 기본 workflow token(`GITHUB_TOKEN`)은 read-only이고 publish job만 `contents: write`를
가진다(`GH_TOKEN: ${{ github.token }}`). macOS runner는 verify job에만 이 저장소의 검증 의존성 때문에 유지하고,
publish job은 `ubuntu-latest`를 쓴다 — docauth·docloop와 동일한 최소 권한 모델이다(release-auto#14 정책 통일).

활성화 전에는 다음을 별도 승인하고 read back해야 한다: merge/squash-only, main PR/CI protection,
`v*` update/delete 금지, public bot signer, CODEOWNERS human review, Release immutability 지원 여부.
private key/token/passphrase 값은 로그·PR·receipt에 기록하지 않는다.

**이력**: Divvy R1 canary는 한때 전용 단일-repository App의 actor-wide direct bypass 모델을 선택했다 —
당시 workflow가 merge 여러 건을 하나의 batch로 재구성하던 metadata-only path 및 pre-secret
revision/batch 검증을 구현하기 위해서였다(그 App-token workflow는 실제로 설정·활성화된 적이 없다 —
`RELEASE_APP_ID`/`RELEASE_APP_INSTALLATION_ID`/`RELEASE_APP_PRIVATE_KEY` 어느 것도 이 저장소에
등록된 적이 없었다). 이후
workflow가 지금의 단일 exact-tag `workflow_dispatch` 구조로 수렴하면서 그 근거가 사라져,
docauth·docloop와 동일한 `GITHUB_TOKEN` 모델로 되돌렸다.

## 실패와 복구

- metadata 전 실패: tag snapshot과 current main tip을 다시 읽고 한 번만 재계산한다.
- metadata만 존재: 전체 batch identity와 first parent/tree/path를 확인한 뒤 같은 commit에 tag한다.
- 올바른 tag만 존재: dispatch 시점의 live `main`이 tag commit과 정확히 같고 trusted target과 tagged notes가 일치할 때만 누락된 Release를 만든다.
- 정확히 같은 Release: 성공 no-op이며 추가 효과는 0이다.
- 잘못된 공개 tag/Release: 이동·삭제·수정하지 않고 quarantine한다. reviewed 새 버전으로만 교정한다.
- `workflow_dispatch`는 정확히 하나의 안정 tag 입력을 받는다. 기존 v0.1.0 Release의 historical
  `target_commitish`는 GitHub의 표시용 필드로 태그가 이미 존재할 때 태그 객체를 인증하는 근거가 아니다.
  모든 Release는 signed tag object와 tag commit을 원격에서 exact readback한다.

### 잔류 draft Release 복구 절차

release 워크플로 실행 중 오류가 나면 draft Release가 남을 수 있습니다. 이후 재실행은 이 draft를 발견하고 fail-closed 되어 자동으로 이어서 publish하지 않습니다. 복구 절차:

1. 해당 tag의 draft Release 존재 여부를 확인합니다.
2. draft의 tag/name/body가 의도한 값과 일치하는지 확인합니다.
3. 안전하지 않거나 불일치하면 draft를 삭제합니다.
4. 삭제 후 workflow를 재dispatch합니다.

draft를 검증 없이 자동으로 publish하지 않는 것이 이 워크플로의 fail-closed 정책입니다.

공개 객체 생성과 repository 설정은 항상 별도 task-scoped authority 경계다.
