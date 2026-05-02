"""Headless intent-parser benchmark suite. Pure Python — no Qt, no GUI —
so it can be invoked from a script for branch-to-branch comparison.

Usage as a library:
    from korder.intent_bench import CASES, run_suite
    results = run_suite(parser)

Usage as a CLI:
    uv run python -m korder.intent_bench --model gemma4:e4b
    uv run python -m korder.intent_bench --thinking --json > main.json
"""
from __future__ import annotations
import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from typing import Optional

from korder.actions.base import all_actions
from korder.intent import IntentParser, segment_input_by_actions


@dataclass(frozen=True)
class BenchCase:
    utterance: str
    expected_action: Optional[str]  # action name, or None for "no action"
    note: str = ""


@dataclass
class BenchResult:
    utterance: str
    expected_action: Optional[str]
    got_action: Optional[str]
    pipeline_ok: bool
    ok: bool
    latency_ms: float
    thinking: str = ""
    error: Optional[str] = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


CASES: tuple[BenchCase, ...] = (
    # --- Basic correctness ---
    BenchCase("Naciśnij Enter", "press_enter", "Polish, basic key"),
    BenchCase("press enter", "press_enter", "English baseline"),
    BenchCase("Wznów", "play_pause", "Polish synonym, no explicit trigger"),
    BenchCase("play music", "play_pause", "English baseline"),
    BenchCase("Zwiększ głośność", "volume_up", "Polish volume up"),
    BenchCase("ciszej", "volume_down", "Polish single-word, volume down"),
    BenchCase("louder", "volume_up", "English single-word"),
    BenchCase("Pisz", "enter_write_mode", "Polish mode toggle on"),
    BenchCase("Przestań", "exit_write_mode", "Polish mode toggle off"),
    # --- Whisper corruption tolerance ---
    BenchCase("Znów odtwarzanie", "play_pause", "Whisper dropped W"),
    BenchCase("Znów odstwarzanie", "play_pause", "Whisper W + extra s"),
    # --- Action distinction ---
    BenchCase(
        "Zatrzymaj odtwarzanie",
        "stop_playback",
        "Stop — distinct from play_pause",
    ),
    BenchCase("Usuń słowo", "delete_word", "Word-level shortcut"),
    # --- Parameterized ---
    BenchCase(
        "Spotify zagraj Linkin Park",
        "spotify_play",
        "Parameterized, default kind",
    ),
    BenchCase(
        "Spotify zagraj album Pink Floyd",
        "spotify_play",
        "Parameterized, explicit kind=album",
    ),
    BenchCase(
        "Spotify zagraj utwór Numb",
        "spotify_play",
        "Parameterized, explicit kind=track",
    ),
    # --- False positives ---
    BenchCase(
        "she pressed enter on the keyboard",
        None,
        "Descriptive prose",
    ),
    BenchCase(
        "hello world this is just dictation",
        None,
        "Pure dictation",
    ),
    BenchCase(
        "the new line of code is broken",
        None,
        "'new line' as noun",
    ),
    BenchCase(
        "Wczoraj stało się coś niezwykłego",
        None,
        "Polish dictation, diacritics",
    ),
    # --- Multi-action ---
    BenchCase(
        "press enter and run it",
        "press_enter",
        "Action with trailing text",
    ),
)


def classify(parser: IntentParser, transcript: str) -> tuple[Optional[str], bool, str]:
    """Run the LLM step and return (action_name, pipeline_ok, thinking).

    Mirrors the benchmark UI's classifier — returns the *first* action
    name from the LLM's structured response (None if the model classified
    as plain dictation), whether segmentation accepts the response, and
    Gemma's thinking trace if thinking_mode was on.
    """
    actions = parser._call_ollama(transcript)  # noqa: SLF001
    name = None
    if isinstance(actions, list) and actions:
        first = actions[0]
        if isinstance(first, dict):
            name = first.get("name")
    pipeline_ok = segment_input_by_actions(transcript, actions) is not None
    return name, pipeline_ok, parser.last_thinking


def run_suite(
    parser: IntentParser,
    cases: tuple[BenchCase, ...] = CASES,
    *,
    warmup: bool = True,
    progress: Optional[callable] = None,
) -> list[BenchResult]:
    """Run all cases through the parser and return per-case results.

    `progress` is an optional callback invoked as progress(idx, total)
    so callers (CLI / GUI) can report.
    """
    if warmup:
        try:
            parser._call_ollama("warmup")  # noqa: SLF001
        except Exception:
            # Surface any warmup error as a per-case error in the first result
            # rather than crashing the whole run.
            pass

    results: list[BenchResult] = []
    total = len(cases)
    for idx, case in enumerate(cases, start=1):
        if progress is not None:
            progress(idx, total)
        t0 = time.perf_counter()
        try:
            got, pipeline_ok, thinking = classify(parser, case.utterance)
            latency_ms = (time.perf_counter() - t0) * 1000.0
            ok = (got == case.expected_action) and pipeline_ok
            results.append(
                BenchResult(
                    utterance=case.utterance,
                    expected_action=case.expected_action,
                    got_action=got,
                    pipeline_ok=pipeline_ok,
                    ok=ok,
                    latency_ms=latency_ms,
                    thinking=thinking,
                    note=case.note,
                )
            )
        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            results.append(
                BenchResult(
                    utterance=case.utterance,
                    expected_action=case.expected_action,
                    got_action=None,
                    pipeline_ok=False,
                    ok=False,
                    latency_ms=latency_ms,
                    error=str(e),
                    note=case.note,
                )
            )
    return results


def summarize(results: list[BenchResult]) -> dict:
    latencies = [r.latency_ms for r in results if r.error is None]
    passes = sum(1 for r in results if r.ok)
    return {
        "total": len(results),
        "passes": passes,
        "fails": len(results) - passes,
        "correctness_pct": 100.0 * passes / len(results) if results else 0.0,
        "latency_avg_ms": statistics.fmean(latencies) if latencies else 0.0,
        "latency_median_ms": statistics.median(latencies) if latencies else 0.0,
        "latency_min_ms": min(latencies) if latencies else 0.0,
        "latency_max_ms": max(latencies) if latencies else 0.0,
        "thinking_trace_count": sum(1 for r in results if r.thinking),
    }


def _print_table(results: list[BenchResult]) -> None:
    print(
        f"{'utterance':40s} {'expected':18s} {'got':18s} {'ms':>6s}  ok  note"
    )
    print("-" * 120)
    for r in results:
        ok_mark = "PASS" if r.ok else "FAIL"
        utter = r.utterance if len(r.utterance) <= 38 else r.utterance[:37] + "…"
        print(
            f"{utter:40s} {str(r.expected_action):18s} {str(r.got_action):18s} "
            f"{r.latency_ms:>6.0f}  {ok_mark}  {r.note}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Korder intent benchmark")
    parser.add_argument("--model", default="gemma4:e4b", help="ollama model tag")
    parser.add_argument(
        "--thinking", action="store_true", help="enable Gemma's thinking step"
    )
    parser.add_argument(
        "--show-triggers",
        action="store_true",
        help="include trigger lists in the prompt (legacy mode)",
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0, help="per-call timeout in seconds"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit per-case results + summary as JSON (for diffing across branches)",
    )
    args = parser.parse_args(argv)

    # Trigger default action registrations.
    import korder.actions  # noqa: F401

    intent_parser = IntentParser(
        model=args.model,
        timeout_s=args.timeout,
        thinking_mode=args.thinking,
        show_triggers_in_prompt=args.show_triggers,
    )

    print(
        f"Running {len(CASES)} cases against {args.model} "
        f"(thinking={'on' if args.thinking else 'off'}, "
        f"triggers={'on' if args.show_triggers else 'off'})…",
        file=sys.stderr,
    )

    def _on_progress(idx: int, total: int) -> None:
        print(f"  [{idx:>2}/{total}] running…", file=sys.stderr)

    results = run_suite(intent_parser, progress=_on_progress)
    summary = summarize(results)

    if args.json:
        print(
            json.dumps(
                {
                    "config": {
                        "model": args.model,
                        "thinking": args.thinking,
                        "show_triggers": args.show_triggers,
                    },
                    "summary": summary,
                    "results": [r.to_dict() for r in results],
                    "registered_actions": [a.name for a in all_actions()],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        _print_table(results)
        print()
        print(
            f"correctness: {summary['passes']}/{summary['total']} "
            f"({summary['correctness_pct']:.0f}%)  "
            f"latency: avg {summary['latency_avg_ms']:.0f}ms · "
            f"median {summary['latency_median_ms']:.0f}ms · "
            f"min {summary['latency_min_ms']:.0f} · "
            f"max {summary['latency_max_ms']:.0f}"
        )

    return 0 if summary["fails"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
