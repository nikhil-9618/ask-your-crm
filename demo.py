"""Ask Your CRM — demonstration / test-checklist runner.

Runs real questions through the full agent pipeline and prints the answer,
the tool used and the generated SQL.

    python demo.py               # read-only questions + greeting + general chat
    python demo.py --actions     # the two write actions (mutates the demo DB)
    python demo.py --all         # everything
"""

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from agent import FIXED_GREETING, ask_crm  # noqa: E402

READ_QUERIES = [
    "Which leads haven't been contacted in the last 7 days?",
    "Show me the opportunities currently stuck in the Proposal stage.",
    "Which salesperson has the highest conversion rate this month?",
    "What are my top 5 opportunities by value?",
    "Which customers have pending follow-ups?",
    "How many leads do I have?",
    "What's my average deal size?",
    "Total pipeline value in Negotiation stage.",
    "Who owns the Nova Retail account?",
    # greeting must be deterministic + one non-CRM question (ChatGPT-style)
    "hello",
    "What is the capital of France?",
]

ACTION_QUERIES = [
    "Create follow-up tasks for all leads not contacted in 7 days.",
    "Mark followup 3 as Done.",
]


def run(queries):
    history = []
    for q in queries:
        print("\n" + "=" * 78)
        print(f"Q: {q}")
        print("-" * 78)
        result = ask_crm(q, history)
        print(f"tool={result['tool']}  intent={result['intent']}")
        print()
        print(result["answer"])
        data = result.get("data") or {}
        if data.get("sql"):
            print("\nSQL:", data["sql"])
        if data.get("rows") is not None:
            print("rows:", data.get("row_count"))
        history += [
            {"role": "user", "content": q},
            {"role": "assistant", "content": result["answer"]},
        ]


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--all" in args:
        run(READ_QUERIES + ACTION_QUERIES)
    elif "--actions" in args:
        run(ACTION_QUERIES)
    else:
        run(READ_QUERIES)
