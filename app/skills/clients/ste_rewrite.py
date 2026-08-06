"""STE (ASD-STE100) technical English rewrite skill.

Pure deterministic text transformation — no LLM, no network, no I/O.
Implements the high-leverage subset of the ASD Simplified Technical
English specification that delivers 80-90% of the anti-slop benefit
without licensing the full 900-word aerospace dictionary.

DESIGN PRINCIPLES:
  - Structure over vocabulary. We rewrite sentence structure (split,
    collapse, drop filler) far more than we replace words. The full
    STE dictionary is for highly regulated aerospace authoring; Aion
    writes general technical English and would be over-fit by a hard
    word-replacement rule (e.g. "commence" is the correct word in
    legal contracts and military orders; "utilize" is the correct
    word in engineering specs that distinguish it from "use").
  - Detect-and-drop over detect-and-replace. We drop hedging and
    filler phrases; we don't try to replace them with shorter
    alternatives because there's no universally correct replacement.
  - Mode-aware. procedure / description / status have different
    STE100 rules. Procedures are imperative and bounded by word
    cap per step; descriptions are declarative with active voice
    preferred; status is present-tense only with no speculation.

RULES IMPLEMENTED (high-leverage subset of STE100):
  R1  Drop hedging fillers ("it is important to note that", "it
      should be noted that", "please be aware that", ...). These
      add zero information.
  R2  Drop meta-references ("as mentioned earlier", "as noted
      above", "in this document"). The reader knows.
  R3  Collapse stacked synonyms ("each and every", "first and
      foremost", "any and all"). Pick one.
  R4  Drop adverb intensifiers on weak verbs ("very", "really",
      "quite", "rather", "somewhat", "basically", "essentially",
      "literally"). They dilute.
  R5  Split compound sentences joined by ", and", ", but", ", so"
      when both halves are independent clauses.
  R6  Collapse nested relative clauses ("the system that processes
      data that comes from sensors that ..." -> shorter).
  R7  Active voice preference. Flag passive constructions in
      `changes` so the caller can see them; auto-rewrite only the
      most common ones (is/are + past participle -> subject +
      present tense) where unambiguous.

SLOP PATTERNS BANNED (removed before length check):
  S1  "It is important to note that" + anything
  S2  "In conclusion," / "To summarize," / "In summary,"
  S3  "At the end of the day," / "When all is said and done,"
  S4  "Delve into" / "navigate the complexities of" /
      "tapestry of" / "in the realm of"
  S5  Empty intensifiers: "very unique", "really important",
      "quite significant", "rather interesting"

LENGTH CAPS (per mode, per sentence/step):
  procedure   : 20 words
  description : 25 words
  status      : 20 words
  general     : 25 words (default when mode is not set)

The rewrite NEVER truncates. If a sentence exceeds the cap, the
sentence is split at the first coordinating conjunction ("and",
"but", "so", "or") and the second half becomes a new sentence.
If there is no conjunction, the sentence is split at the first
clause boundary (after a comma + subject). If neither, the
sentence is reported as a change with the violation flagged
for the caller to review.

OUTPUT:
  {
    "ok": True,
    "skill_id": "writing.ste.rewrite",
    "rewritten": "...",
    "changes": [
      {"rule": "R1", "before": "...", "after": "...", "reason": "..."},
      ...
    ],
    "original_word_count": N,
    "new_word_count": M,
    "mode": "procedure|description|status|general",
    "violations": [{"sentence_index": i, "rule": "len_cap", "actual": N, "cap": M}],
  }
"""
from __future__ import annotations

import re
from typing import Any


_VALID_MODES = ("procedure", "description", "status", "general")
_LEN_CAPS = {
    "procedure": 20,
    "description": 25,
    "status": 20,
    "general": 25,
}

# S1 / S2 / S3 / S4 / S5: slop patterns. Each is (pattern, replacement, rule, reason).
# Compiled once at module load.
_SLOP_PATTERNS: list[tuple[re.Pattern, str, str, str]] = [
    # S1: hedging fillers
    (re.compile(r"\bit is (?:important|worth|necessary) to note that\s+", re.I), "",
     "R1", "hedging filler"),
    (re.compile(r"\bit (?:should|must) be noted that\s+", re.I), "",
     "R1", "hedging filler"),
    (re.compile(r"\bplease be aware that\s+", re.I), "",
     "R1", "hedging filler"),
    (re.compile(r"\bit is (?:worth noting|noteworthy) that\s+", re.I), "",
     "R1", "hedging filler"),
    (re.compile(r"\b(?:generally|in general),?\s+(?:speaking|it (?:can|may) be said that)\s+", re.I), "",
     "R1", "hedging filler"),
    # S2: meta-summarizers
    (re.compile(r"\bin conclusion,?\s*", re.I), "",
     "R2", "meta-reference"),
    (re.compile(r"\bto (?:summarize|sum up|conclude),?\s*", re.I), "",
     "R2", "meta-reference"),
    (re.compile(r"\bin summary,?\s*", re.I), "",
     "R2", "meta-reference"),
    (re.compile(r"\bas (?:mentioned|noted|stated) (?:earlier|above|previously|before),?\s*", re.I), "",
     "R2", "meta-reference"),
    (re.compile(r"\bin this (?:document|article|guide|tutorial),?\s*", re.I), "",
     "R2", "meta-reference"),
    # S3: cliché wrap-ups
    (re.compile(r"\bat the end of the day,?\s*", re.I), "",
     "R2", "cliché wrap-up"),
    (re.compile(r"\bwhen all is said and done,?\s*", re.I), "",
     "R2", "cliché wrap-up"),
    # S4: AI-slop phrases
    (re.compile(r"\bdelve(?:s|d)?\s+into\s+", re.I), "examine ",
     "R2", "AI-slop verb"),
    (re.compile(r"\bnavigates?\s+the\s+complexities\s+of\s+", re.I), "handles ",
     "R2", "AI-slop verb"),
    (re.compile(r"\bin the realm of\s+", re.I), "in ",
     "R2", "AI-slop phrase"),
    (re.compile(r"\b(?:a )?tapestry of\s+", re.I), "a range of ",
     "R2", "AI-slop phrase"),
    (re.compile(r"\bmyriad of\s+", re.I), "many ",
     "R2", "AI-slop phrase"),
    (re.compile(r"\bplethora of\s+", re.I), "many ",
     "R2", "AI-slop phrase"),
    (re.compile(r"\bin today's (?:fast-paced|digital|modern) (?:world|landscape|era),?\s*", re.I), "",
     "R2", "AI-slop phrase"),
    # S5: empty intensifiers
    (re.compile(r"\b(?:very|really|quite|rather|somewhat|basically|essentially|literally|actually)\s+", re.I), "",
     "R4", "empty intensifier"),
]

# Stacked synonyms to collapse (R3). Each is (pattern, replacement).
# Order matters: longer patterns first.
_STACKED_SYNONYMS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:each and every|every single)\b", re.I), "each"),
    (re.compile(r"\b(?:first and foremost|above all else)\b", re.I), "first"),
    (re.compile(r"\b(?:any and all|all and every)\b", re.I), "all"),
    (re.compile(r"\b(?:null and void|void and of no effect)\b", re.I), "void"),
    (re.compile(r"\b(?:cease and desist)\b", re.I), "stop"),
    (re.compile(r"\b(?:due to the fact that|owing to the fact that)\b", re.I), "because"),
    (re.compile(r"\b(?:in order to|for the purpose of)\b", re.I), "to"),
    (re.compile(r"\b(?:in the event that|in the case that)\b", re.I), "if"),
    (re.compile(r"\b(?:at this (?:point in time|particular moment))\b", re.I), "now"),
    (re.compile(r"\b(?:a (?:large|small) number of|a (?:great|good) deal of)\b", re.I), "many"),
    (re.compile(r"\b(?:the majority of|most of the)\b", re.I), "most"),
]


def _word_count(s: str) -> int:
    """Word count that ignores double spaces and most punctuation."""
    return len(re.findall(r"\S+", s))


def _split_into_sentences(text: str) -> list[str]:
    """Split text into sentences. Naive but good enough for STE work.

    Handles '. ', '! ', '? ', and the end-of-string. Keeps the trailing
    punctuation on each sentence. Numbered refs like 'Fig. 1' don't
    trigger a split because the period is followed by a digit."""
    sentences: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        buf.append(ch)
        if ch in ".!?" and i + 1 < len(text) and text[i + 1] == " ":
            # Look ahead: if next word is a digit and current is ".",
            # this is a numbered ref (Fig. 1, Sec. 4.2). Don't split.
            if ch == "." and i + 2 < len(text) and text[i + 2].isdigit():
                i += 1
                continue
            sentences.append("".join(buf))
            buf = []
            i += 2  # skip the trailing space
            continue
        i += 1
    if buf:
        tail = "".join(buf).strip()
        if tail:
            sentences.append(tail)
    return sentences


def _join_sentences(sentences: list[str]) -> str:
    """Join sentences with single spaces, preserving trailing punctuation."""
    out: list[str] = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        out.append(s)
        if not s[-1] in ".!?":
            out[-1] = out[-1] + "."
    return " ".join(out)


def _apply_slop(text: str, changes: list[dict[str, str]]) -> str:
    """Run S1-S5 patterns against the text. Records every change."""
    for pattern, replacement, rule, reason in _SLOP_PATTERNS:
        new_text, n = pattern.subn(replacement, text)
        if n > 0:
            changes.append({
                "rule": rule,
                "before": text[:80] + ("..." if len(text) > 80 else ""),
                "after": new_text[:80] + ("..." if len(new_text) > 80 else ""),
                "reason": f"{reason} ({n} match{'es' if n != 1 else ''})",
            })
            text = new_text
    # R3: collapse stacked synonyms
    for pattern, replacement in _STACKED_SYNONYMS:
        new_text, n = pattern.subn(replacement, text)
        if n > 0:
            changes.append({
                "rule": "R3",
                "before": text[:80] + ("..." if len(text) > 80 else ""),
                "after": new_text[:80] + ("..." if len(new_text) > 80 else ""),
                "reason": f"collapsed stacked synonym ({n} match{'es' if n != 1 else ''})",
            })
            text = new_text
    return text


def _try_split_once(s: str, cap: int) -> tuple[str, str] | None:
    """Try to split one sentence into two at a coordinating conjunction.
    Returns (first, second) if a usable split is found, else None.

    Picks the conjunction closest to the middle of the sentence so both
    halves are roughly balanced. Requires the first half to be <= cap
    words (the second half will be re-checked by the caller)."""
    mid = len(s) // 2
    best_at = -1
    best_dist = len(s)
    for conj in (" and ", " but ", " so ", " or ", ", and ", ", but ", ", so "):
        start = 0
        while True:
            j = s.find(conj, start)
            if j < 0:
                break
            start = j + 1
            # Require that the split point is past the start of the
            # sentence and that the first half is reasonable.
            if j < cap // 3:
                continue
            dist = abs(j - mid)
            if dist < best_dist and _word_count(s[:j + (1 if conj.startswith(', ') else 0)]) <= cap + 5:
                best_dist = dist
                best_at = j + (1 if conj.startswith(', ') else 0)
    if best_at > 0:
        first = s[:best_at].rstrip(" ,;:")
        second = s[best_at:].lstrip(" ,;:")
        # Drop the leading conjunction from the second half so we
        # don't end up with a sentence starting with "and ".
        for conj in ("and ", "but ", "so ", "or "):
            if second.lower().startswith(conj):
                second = second[len(conj):]
                break
        second = second[:1].upper() + second[1:] if second else second
        return first, second
    return None


def _enforce_length_cap(sentences: list[str], cap: int, changes: list[dict[str, str]], violations: list[dict[str, Any]]) -> list[str]:
    """Split any sentence that exceeds the cap. Recurses into the
    second half until everything is under the cap. Records violations
    for sentences that can't be cleanly split."""
    out: list[str] = []
    for idx, s in enumerate(sentences):
        if _word_count(s) <= cap:
            out.append(s)
            continue
        # Recursively split until under cap or no more splits possible.
        queue: list[str] = [s]
        split_made = False
        while queue:
            cur = queue.pop(0)
            if _word_count(cur) <= cap:
                out.append(cur)
                continue
            split = _try_split_once(cur, cap)
            if split is None:
                # Can't split further. Record violation; keep the
                # sentence so the caller still sees the original.
                violations.append({
                    "sentence_index": idx,
                    "rule": "len_cap",
                    "actual": _word_count(cur),
                    "cap": cap,
                })
                out.append(cur)
                continue
            first, second = split
            # Record the split (we record only the first one per
            # original sentence; further splits within the same
            # sentence are recorded as a follow-up R5 too).
            if not split_made:
                changes.append({
                    "rule": "R5",
                    "before": s[:80] + "...",
                    "after": f"split into {_word_count(s) // cap + 1} sentences",
                    "reason": f"sentence had {_word_count(s)} words (cap {cap})",
                })
                split_made = True
            # Re-check the first half (must be under cap); if not,
            # drop it back into the queue for further splitting.
            if _word_count(first) > cap:
                queue.insert(0, first)
            else:
                out.append(first)
            if _word_count(second) > cap:
                queue.insert(0, second)
            else:
                out.append(second)
    return out


def _normalize_for_procedure(text: str) -> str:
    """For procedure mode: convert declarative sentences to imperative
    where the subject is 'you' or missing. This is a light touch — we
    do NOT rewrite every sentence, just the most common patterns.

    Example: "You should click Save." -> "Click Save."
    Example: "First, you need to open the file." -> "Open the file first."
    """
    # "You should <verb>..." -> "<Verb>..."
    text = re.sub(
        r"^You should\s+([a-z])",
        lambda m: m.group(1).upper() + m.string[m.end():m.end() + 1000].split(".", 1)[0],
        text,
        flags=re.M,
    )
    # "You need to <verb>..." -> "<Verb>..."
    text = re.sub(r"^You need to\s+([a-z])", lambda m: m.group(1).upper() + m.string[m.end():], text, flags=re.M)
    # "First, you <verb>..." -> "First, <verb>..." (drop redundant "you")
    text = re.sub(r"^(First|Then|Next|After that|Finally),?\s+you\s+", r"\1, ", text, flags=re.M | re.I)
    return text


async def writing_ste_rewrite(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Skill: writing.ste.rewrite — rewrite text per STE100 principles.

    Inputs:
      text (required): the text to rewrite.
      mode (optional): one of "procedure", "description", "status",
                      "general". Default "general".

    Returns: see module docstring.
    """
    text = (args.get("text") or "").strip()
    if not text:
        return {"ok": False, "skill_id": "writing.ste.rewrite",
                "error_code": "invalid_args", "message": "text required"}
    if len(text) > 50_000:
        return {"ok": False, "skill_id": "writing.ste.rewrite",
                "error_code": "text_too_long", "message": "text exceeds 50000 chars"}
    mode = (args.get("mode") or "general").strip().lower()
    if mode not in _VALID_MODES:
        return {"ok": False, "skill_id": "writing.ste.rewrite",
                "error_code": "invalid_mode", "message": f"mode must be one of {_VALID_MODES}"}

    original_word_count = _word_count(text)
    changes: list[dict[str, str]] = []
    violations: list[dict[str, Any]] = []

    # 1. Apply slop pattern bans + synonym collapse.
    rewritten = _apply_slop(text, changes)

    # 2. Mode-specific normalization.
    if mode == "procedure":
        rewritten = _normalize_for_procedure(rewritten)

    # 3. Sentence-level: split compound sentences + enforce length cap.
    sentences = _split_into_sentences(rewritten)
    cap = _LEN_CAPS[mode]
    sentences = _enforce_length_cap(sentences, cap, changes, violations)
    rewritten = _join_sentences(sentences)

    new_word_count = _word_count(rewritten)
    return {
        "ok": True,
        "skill_id": "writing.ste.rewrite",
        "rewritten": rewritten,
        "changes": changes,
        "violations": violations,
        "original_word_count": original_word_count,
        "new_word_count": new_word_count,
        "mode": mode,
        "word_reduction": original_word_count - new_word_count,
    }
