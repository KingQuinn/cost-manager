"""Golden set for the intent router — run against the real Gemini model.

Usage:
    python -u validate_intents.py              # print pass/fail per case
    python -u validate_intents.py --langfuse   # also record it as a Langfuse experiment

The cases live in evals/intent_router_cases.json (the first ones are the
three silent-data-change bugs from architecture.md changelog v1.5). Each case
builds a small in-memory household, sends the message through the real
router, and checks the decision. Nothing is written to Supabase and no
WhatsApp message is sent. Re-run it whenever the router prompt or
GEMINI_CHAT_MODEL changes, and note the pass rate in architecture.md's
changelog.

With --langfuse (needs LANGFUSE_* keys): the cases are upserted into the
Langfuse dataset `intent-router-golden` and this run is recorded against it,
with an `intent_correct` and an `all_fields_correct` score per case, so runs
can be compared side by side in Langfuse.
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()

DATASET = "intent-router-golden"


def route(case: dict) -> dict:
    from app.services.agent import ConversationAgent
    from evals.intent_cases import build_household, decision_summary

    store, member = build_household(case)
    agent = ConversationAgent(store, whatsapp=None)
    ctx = agent._build_context(member)
    d = agent._route(member, case["message"], ctx)
    out = decision_summary(d)
    out["reasoning"] = d.reasoning
    return out


def run_local(cases: list[dict]) -> int:
    from evals.intent_cases import check

    passed = 0
    for case in cases:
        got = route(case)
        problems = check(case["expect"], got)
        if got["source"] != "llm":
            problems.append("router call failed — this used the offline fallback parser")
        passed += not problems
        print(f"[{'PASS' if not problems else 'FAIL'}] {case['id']}: {case['message']!r}")
        print(f"       -> {got['intent']} conf={got['confidence']:.2f} item={got['item_number']} "
              f"verdict={got['verdict']} price={got['price']} category={got['category']} "
              f"scope={got['scope']} period={got['period']} purchase_total={got['purchase_total']}")
        for p in problems:
            print(f"       ✗ {p}")
        if got.get("reasoning"):
            print(f"       why: {got['reasoning']}")
    print(f"\n{passed}/{len(cases)} passed")
    return 0 if passed == len(cases) else 1


def run_langfuse(cases: list[dict]) -> int:
    from langfuse.experiment import Evaluation

    from app import observability
    from app.config import get_settings
    from evals.intent_cases import check

    lf = observability.get_langfuse()
    if lf is None:
        print("[FAIL] --langfuse needs LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY", file=sys.stderr)
        return 3
    try:
        lf.get_dataset(DATASET)
    except Exception:
        lf.create_dataset(name=DATASET, description="Intent router golden set (evals/intent_router_cases.json)")
    for case in cases:  # same id -> upsert, so re-running never duplicates items
        lf.create_dataset_item(
            dataset_name=DATASET, id=f"{DATASET}-{case['id']}",
            input={k: case[k] for k in ("message", "household") if k in case}
            | ({"awaiting_description": True} if case.get("awaiting_description") else {}),
            expected_output=case["expect"],
            metadata={"case_id": case["id"], "tags": case.get("tags", [])},
        )

    def task(*, item, **_):
        return route(dict(item.input))

    def intent_correct(*, output, expected_output, **_):
        if "intent" not in expected_output:
            return Evaluation(name="intent_correct", value=1.0, comment="no intent expected")
        return Evaluation(name="intent_correct", value=float(output["intent"] == expected_output["intent"]))

    def all_fields_correct(*, output, expected_output, **_):
        problems = check(expected_output, output)
        return Evaluation(name="all_fields_correct", value=float(not problems), comment="; ".join(problems) or None)

    result = lf.get_dataset(DATASET).run_experiment(
        name="intent-router", description=f"model={get_settings().gemini_chat_model}",
        task=task, evaluators=[intent_correct, all_fields_correct], max_concurrency=4,
        metadata={"model": get_settings().gemini_chat_model},
    )
    lf.flush()
    print(result.format())
    scores = [e.value for r in result.item_results for e in r.evaluations if e.name == "all_fields_correct"]
    return 0 if scores and all(v == 1.0 for v in scores) else 1


def main() -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("[FAIL] GEMINI_API_KEY not set. Add it to .env or `export GEMINI_API_KEY=...`", file=sys.stderr)
        return 3
    from evals.intent_cases import load_cases

    cases = load_cases()
    if "--langfuse" in sys.argv[1:]:
        return run_langfuse(cases)
    return run_local(cases)


if __name__ == "__main__":
    raise SystemExit(main())
