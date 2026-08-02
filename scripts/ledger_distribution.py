#!/usr/bin/env python3
"""Generate or verify LEDGER.md distribution counts from its assignment table."""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

from init_state import resolve_paths


PRIMARY_VALUES = ("CLAUDE", "CODEX", "CLAUDE+CODEX")
SOURCE_VALUES = ("G1", "G2", "G3", "tie", "user")
REASON_VALUES = ("도구부재", "브리프비용", "적성(G3)", "노출보류", "G4blocked", "user")
TERMINAL_STATUSES = ("완료", "부분", "부분 완료", "막힘", "실패", "종료", "취소")
NONTERMINAL_STATUSES = ("미착수", "진행 중")


def parse_rows(text):
    rows = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not re.match(r"^\| L-\d+ \|", line):
            continue
        protected = line.replace(r"\|", "\0")
        cells = [cell.replace("\0", r"\|").strip()
                 for cell in protected.strip().strip("|").split("|")]
        if len(cells) != 14:
            raise ValueError(f"{line_number}행: LEDGER 열이 14개가 아님 ({len(cells)}개)")
        row = {
            "id": cells[0],
            "primary": cells[3],
            "source": cells[4],
            "g4": cells[7],
            "reviewer": cells[8],
            "reason": cells[9],
            "primary_result": cells[11],
        }
        if row["primary"] not in PRIMARY_VALUES:
            raise ValueError(f"{line_number}행 {row['id']}: 알 수 없는 primary {row['primary']!r}")
        if row["source"] not in SOURCE_VALUES:
            raise ValueError(f"{line_number}행 {row['id']}: 알 수 없는 결정출처 {row['source']!r}")
        if row["reason"] not in (*REASON_VALUES, "—", ""):
            raise ValueError(f"{line_number}행 {row['id']}: 알 수 없는 미위임사유 {row['reason']!r}")
        rows.append(row)
    return rows


def is_finished(row):
    result = row["primary_result"]
    if not result or result == "—":
        return False
    match = re.match(r"^\*\*([^*]+)\*\*", result)
    if not match:
        raise ValueError(f"{row['id']}: 결과(primary)에 선두 상태 표기가 없음: {result!r}")
    status = match.group(1)
    if status in TERMINAL_STATUSES:
        return True
    if status in NONTERMINAL_STATUSES:
        return False
    raise ValueError(f"{row['id']}: 알 수 없는 결과(primary) 상태 {status!r}")


def summarize(rows):
    finished = [row for row in rows if is_finished(row)]
    primary = Counter(row["primary"] for row in finished)
    source = Counter(row["source"] for row in finished)
    reason = Counter(row["reason"] for row in finished if row["reason"] in REASON_VALUES)
    simultaneous_ids = [row["id"] for row in finished if row["primary"] == "CLAUDE+CODEX"]
    review_ids = [
        row["id"] for row in finished
        if row["primary"] in ("CLAUDE", "CODEX") and "**실행됨**" in row["reviewer"]
    ]
    blocked_ids = [
        row["id"] for row in finished
        if "g4 blocked" in row["reviewer"].lower() or row["reason"] == "G4blocked"
    ]
    return {
        "finished": len(finished),
        "primary": primary,
        "source": source,
        "reason": reason,
        "simultaneous_ids": simultaneous_ids,
        "review_ids": review_ids,
        "blocked_ids": blocked_ids,
    }


def id_suffix(ids):
    return f" ({', '.join(ids)})" if ids else ""


def render(summary):
    primary = summary["primary"]
    source = summary["source"]
    reason = summary["reason"]
    return "\n".join((
        "러너별:",
        "",
        f"- CLAUDE primary: {primary['CLAUDE']}건",
        f"- CODEX primary: {primary['CODEX']}건",
        f"- CLAUDE+CODEX 동시 primary: {primary['CLAUDE+CODEX']}건"
        f"{id_suffix(summary['simultaneous_ids'])}",
        f"- 반대 러너 검토가 **실행된** 건: {len(summary['review_ids'])}건"
        f"{id_suffix(summary['review_ids'])}",
        f"- G4 blocked(中·高인데 검토를 못 돌린 건): {len(summary['blocked_ids'])}건",
        "",
        "결정출처별 — 왜 그렇게 갈렸는지는 여기서만 보인다:",
        "",
        f"- G1(도구): {source['G1']}건",
        f"- G2(브리프 비용): {source['G2']}건",
        f"- G3(적성): {source['G3']}건",
        f"- tie(타이브레이커): {source['tie']}건",
        f"- user(사용자 지정): {source['user']}건",
        "",
        "미위임 사유별(CODEX로 가지 않은 이유):",
        "",
        f"- 도구부재(T-1~T-5): {reason['도구부재']}건",
        f"- 브리프비용 큼: {reason['브리프비용']}건",
        f"- 적성(G3): {reason['적성(G3)']}건",
        f"- 노출보류: {reason['노출보류']}건",
        f"- G4blocked: {reason['G4blocked']}건",
        f"- user: {reason['user']}건",
    ))


def distribution_section(text):
    start = text.find("## 분포")
    if start == -1:
        raise ValueError("'## 분포' 절을 찾지 못함")
    end = text.find("\n### ", start)
    return text[start:] if end == -1 else text[start:end]


def check_distribution(text, generated):
    section_lines = distribution_section(text).splitlines()
    generated_lines = [line for line in generated.splitlines() if line.startswith("- ")]
    generated_labels = {line.rsplit(": ", 1)[0] for line in generated_lines}
    errors = []
    for line in generated_lines:
        label = line.rsplit(": ", 1)[0]
        matches = [candidate for candidate in section_lines
                   if candidate.startswith(label + ": ")]
        if len(matches) != 1:
            errors.append(f"{label}: 분포 절 항목이 {len(matches)}개임(정확히 1개 필요)")
        elif matches[0] != line:
            errors.append(f"{label}: 문서값 {matches[0]!r}, 생성값 {line!r}")
    for line in section_lines:
        if not line.startswith("- "):
            continue
        label = line.rsplit(": ", 1)[0]
        if label not in generated_labels:
            errors.append(f"{label}: 생성 집계에 없는 항목이 문서에 남아 있음")
    return errors


def main():
    parser = argparse.ArgumentParser(
        description="LEDGER 표에서 분포 Markdown을 생성하고, 선택적으로 문서의 집계와 대조한다."
    )
    parser.add_argument("--check", action="store_true", help="문서의 분포 절과 불일치하면 종료코드 1")
    default_ledger, _default_roster = resolve_paths()
    parser.add_argument(
        "ledger",
        nargs="?",
        default=str(default_ledger),
        help="검사할 LEDGER.md 경로(기본: DIVVY_LEDGER 또는 사용자 state 경로)",
    )
    args = parser.parse_args()

    try:
        text = Path(args.ledger).read_text(encoding="utf-8")
        summary = summarize(parse_rows(text))
        generated = render(summary)
        errors = check_distribution(text, generated) if args.check else []
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"LEDGER 처리 오류: {exc}", file=sys.stderr)
        return 2

    print(generated)
    if errors:
        print("\nLEDGER 분포 불일치:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    if args.check:
        print(f"\nLEDGER 분포 일치: 완료·종료 {summary['finished']}건", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
