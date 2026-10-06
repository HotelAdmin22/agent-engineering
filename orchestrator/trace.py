"""A console view of what the agents are doing, while they do it.

Transcripts on disk answer "what happened". They do not answer "what is it
doing right now", and a fifteen-turn loop that prints nothing for two minutes
is indistinguishable from one that has hung.

The tracer is a plain callable passed into the loops. Passing nothing keeps
them silent, so this file changes no behaviour -- it only makes it visible.
"""

import json
import sys

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
BLUE, YELLOW, GREEN, RED = "\033[34m", "\033[33m", "\033[32m", "\033[31m"

STATUS_COLOR = {
    "landed": GREEN,
    "not_landed": RED,
    "regressed": RED,
    "false_positive": YELLOW,
    "unverifiable": DIM,
}

VERDICT_COLOR = {"FAIL": RED, "PASS": GREEN, "NEEDS": YELLOW, "N/A": DIM}


def _clip(text, width=100):
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 3] + "..."


def _paint(code, text, color=True):
    return f"{code}{text}{RESET}" if color else text


class ConsoleTracer:
    """Prints one line per decision. Pass as `trace=` to a loop."""

    def __init__(self, label, stream=sys.stderr, color=True):
        self.label = label
        self.stream = stream
        self.color = color

    def _w(self, line):
        print(line, file=self.stream, flush=True)

    def __call__(self, event):
        kind = event.get("kind")
        turn = event.get("turn")
        head = f"[{self.label} t{turn}]" if turn else f"[{self.label}]"
        tag = _paint(BOLD, head, self.color)

        if kind == "text" and event.get("text"):
            self._w(f"{tag} {_paint(DIM, _clip(event['text']), self.color)}")

        elif kind == "tool_call":
            args = event.get("input")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    pass
            shown = args.get("sql") if isinstance(args, dict) and "sql" in args else args
            self._w(f"{tag} {_paint(BLUE, '-> ' + event['name'], self.color)}  {_clip(shown, 120)}")

        elif kind == "tool_result":
            colour = RED if event.get("is_error") else DIM
            self._w(f"{tag}   {_paint(colour, '<- ' + _clip(event.get('content', ''), 110), self.color)}")

        elif kind == "stop":
            self._w(f"{tag} {_paint(YELLOW, 'stopped: ' + str(event.get('reason')), self.color)}")


def print_findings(findings, stream=sys.stderr, color=True):
    for f in findings:
        mark = VERDICT_COLOR.get(f.verdict, DIM)
        verdict = _paint(mark, f"{f.verdict:<6}", color)
        print(f"  {verdict} {f.check:<9} {_clip(f.recommendation or f.note, 90)}",
              file=stream, flush=True)


def print_landings(landings, stream=sys.stderr, color=True):
    for l in landings:
        status = _paint(STATUS_COLOR.get(l["status"], DIM), f"{l['status']:<15}", color)
        detail = _paint(DIM, _clip(l["detail"], 70), color)
        print(f"  {status} {l['finding_id']}  {detail}", file=stream, flush=True)
