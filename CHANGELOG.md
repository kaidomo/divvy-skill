# Changelog

이 파일은 divvy의 사용자 가시적 변경을 [Semantic Versioning](https://semver.org/) 기준으로 기록한다.

## [Unreleased]

### Added

- 워크플로 0번 단계: 다라운드·신규결정 작업(설계 결정이 예상되거나 CODEX 왕복이 반복될 것 같은 경우)이면, 1번(작업 단위 쪼개기)보다 먼저 5줄 brief(user_outcome·경계·비책임·stop_condition·예상규모/상한)를 얼리도록 요구. 프로젝트에 자체 계획 계약 문서가 있으면 동등한 다섯 요소를 담고 있는 한 그쪽을 따라도 된다. 검증 체크리스트·연계 섹션에도 반영.

## [0.1.0] - 2026-08-04

### Added

- Claude Code와 Codex CLI 사이의 도구·브리프 비용·적성·오판 비용 기반 배정과 실제 `codex exec` 회수 흐름.
- host-local ROSTER/LEDGER 초기화, read-only drift probe, private state 권한 점검과 명시적 mode migration.
- symlink·hardlink·alias·replacement race를 fail closed하는 descriptor-relative state 처리와 회귀 테스트.
- Codex headless 프로필, 최소 산출 계약, 중단·잠금·백업·복구 안전장치.
- OMX `identity-indeterminate` Stop 반복을 위한 명시적·복구 가능한 로컬 완화 도구.
- VERSION·CHANGELOG 정합 검증, CI, 서명 태그 기반 GitHub Release 자동 발행과 복구 절차.
