"""Word error rate helper for the speech tests (docs/testing/20 T-P2-04-006: normalise_text, then count errors).

    normalise_text("Hello, Syd! It's 23.")   -> "hello syd it's twenty three"
    wer("turn left", "turn lift")             -> 0.5   (substitutions + deletions + insertions over reference words)
"""

from __future__ import annotations

import re

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
         "seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _number_words(digits: str) -> str:
    """1 or 2 digits as a number ('23' -> 'twenty three'); a longer run digit by digit ('1234' -> 'one two three four')."""
    if len(digits) <= 2 and (len(digits) == 1 or digits[0] != "0"):
        n = int(digits)
        if n < 20:
            return _ONES[n]
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    return " ".join(_ONES[int(d)] for d in digits)


def normalise_text(text: str) -> str:
    t = text.lower().replace("-", " ")
    t = re.sub(r"\d+", lambda m: " " + _number_words(m.group(0)) + " ", t)
    t = re.sub(r"[^a-z' ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def wer(reference: str, hypothesis: str) -> float:
    ref, hyp = normalise_text(reference).split(), normalise_text(hypothesis).split()
    if not ref:
        return 0.0 if not hyp else float(len(hyp))
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1] / len(ref)
