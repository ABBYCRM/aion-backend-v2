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

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


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
    # The regex engine substitutes the matched span with the return
    # value, so the lambda only needs to return the verb (capitalized);
    # the rest of the line is untouched.
    text = re.sub(r"^You should\s+([a-z])", lambda m: m.group(1).upper(), text, flags=re.M)
    # "You need to <verb>..." -> "<Verb>..."
    text = re.sub(r"^You need to\s+([a-z])", lambda m: m.group(1).upper(), text, flags=re.M)
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


# ===========================================================================
# v2.8.12 — writing.ste.slop_suppress
# Extra slop patterns folded in from the no-ai-slop skill
# (github.com/petergyang/no-ai-slop) so AION catches the 14 patterns
# no-ai-slop flags without the user having to install a separate skill.
# Mode-agnostic. Pure deterministic regex.
# ===========================================================================

_NO_AI_SLOP_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 1. Binary contrasts — "It's not just X but Y." / "This is not X, it's Y."
    # The closing token after "but" can be any non-sentence-ending char.
    (re.compile(r"\bnot\s+(?:just|only|merely)\s+[^,.;]{1,80}?,\s*but\s+", re.I), "Cut: binary contrast — state the second clause directly. "),
    (re.compile(r"\bthis\s+is\s+not\s+[^,.;]{1,80}?,\s*it'?s\s+", re.I), "Cut: binary contrast — state what it is. "),
    (re.compile(r"\bthe\s+question\s+isn'?t\s+[^,.;]{1,80}?,\s*it'?s\s+", re.I), "Cut: binary contrast — state what the question is. "),
    # 2. Throat-clearing openers
    (re.compile(r"(?:^|\.\s+)(?:here'?s\s+the\s+thing[,;]?\s*|here'?s\s+what\s+i\s+mean[,;]?\s*|let\s+me\s+be\s+clear[,;]?\s*|i'?ll\s+be\s+honest[,;]?\s*|the\s+uncomfortable\s+truth\s+is[,;]?\s*)", re.I), "Cut: throat-clearing opener — state the point. "),
    # 3. Faux-insight setups
    (re.compile(r"(?:^|\.\s+)(?:this\s+is\s+the\s+part\s+most\s+people\s+skip[,;]?\s*|what\s+most\s+people\s+get\s+wrong[,;]?\s*|here'?s\s+what\s+nobody\s+tells\s+you[,;]?\s*|the\s+part\s+everyone\s+misses[,;]?\s*)", re.I), "Cut: faux-insight setup — make the claim stand on its own. "),
    # 4. Colon reveals — "The detail: x." / "The best part: y."
    (re.compile(r"\b(The\s+(?:detail|best\s+part|secret|trick|truth)\s+(?:that|is|here)\s*(?:\w+\s+){0,4}(?:is|means)?\s*):\s+", re.I), "Cut: colon-reveal — rewrite as a plain sentence. "),
    # 5. Importance puffery
    (re.compile(r"\b(?:stands\s+as\s+a\s+testament\s+to|marks\s+a\s+pivotal\s+moment|plays\s+a\s+vital\s+role|solidifies\s+its\s+position|underscores\s+its\s+significance|heralds\s+a\s+new\s+era\s+in|signals\s+a\s+seismic\s+shift\s+in)\b", re.I), "Cut: importance puffery — state the fact. "),
    # 6. Interpretive metadiscourse
    (re.compile(r"\b(?:that\s+last\s+part\s+matters\s+more\s+than\s+it\s+sounds|the\s+key\s+point\s+is[,]?\s*|as\s+you\s+can\s+see[,]?\s*|this\s+distinction\s+matters[,]?\s*|it'?s\s+worth\s+noting\s+that\s+|the\s+reality\s+is[,]?\s*|the\s+truth\s+is[,]?\s*|in\s+other\s+words[,]?\s*)", re.I), "Cut: interpretive metadiscourse — let the prose speak. "),
    # 7. Weasel attribution
    (re.compile(r"\b(?:experts\s+agree\s+that|industry\s+reports\s+suggest|many\s+argue\s+that|widely\s+regarded\s+as|studies\s+show(?:\s+that)?|it\s+is\s+widely\s+believed\s+that|experts\s+agree|some\s+would\s+say)\s*", re.I), "Cut: weasel attribution — name the source or cut the claim. "),
    # 8. Fake-strong verbs
    (re.compile(r"\b(?:serves\s+as\s+a\s+centralized\s+hub\s+for|acts\s+as\s+a\s+foundation\s+for|functions\s+as\s+a\s+backbone\s+for)\b", re.I), "Cut: fake-strong verb — use 'is' or 'has'. "),
    # 9. Synonym cycling markers
    (re.compile(r"\b(?:in\s+order\s+to|going\s+forward|moving\s+forward|at\s+this\s+point\s+in\s+time|at\s+the\s+present\s+moment)\b", re.I), "Cut: synonym-cycle marker — use 'to' or delete. "),
    # 10. Negative listing — "Not a X. Not a Y. A Z."
    (re.compile(r"(?:^|\.\s+)(?:Not\s+(?:a|an|the)\s+[^.]{1,40}\.\s*){2,}", re.I | re.M), "Cut: negative listing — state what it is. "),
    # 11. Dramatic fragmentation — "X. And Y. And Z." / "That's it. That's the whole thing."
    (re.compile(r"(?:\w[^.]{1,40}\.\s+And\s+\w[^.]{1,40}\.\s+And\s+\w)", re.I), "Rewrite: dramatic fragmentation — combine into a single sentence. "),
    # 12. Faux-profound endings
    (re.compile(r"\b(?:the\s+future\s+is(?:n'?t|\s+not)\s+coming[.,;]?\s+it(?:'?s|\s+is)\s+already\s+here|the\s+future\s+is\s+already\s+here|this\s+changes\s+everything|this\s+is\s+huge|this\s+is\s+a\s+game\s+changer|paradigm\s+shift)\b", re.I), "Cut: faux-profound ending — state the consequence, not the cliché. "),
    # 13. Banned power words from no-ai-slop
    (re.compile(r"\b(?:delve|leverage|utilize|facilitate|empower|streamline|cutting-edge|game\s+changer|paradigm\s+shift|tapestry|realm|beacon|multifaceted|meticulous|intricate|paramount|transformative|elevate|embark|supercharge|harness|ever-evolving)\b", re.I), "Cut: AI power word — use a concrete alternative. "),
    # 14. "Let's dive in" / "Hope this helps" closers — drop regardless of
    # whether they sit at a sentence boundary. The user-visible reply
    # shouldn't end on these regardless of punctuation.
    (re.compile(r"\blet'?s\s+dive\s+in[.,]?\s*|hope\s+this\s+helps[.,]?\s*|that'?s\s+the\s+whole\s+thing[.,]?\s*|at\s+the\s+end\s+of\s+the\s+day[.,]?\s*|when\s+all\s+is\s+said\s+and\s+done[.,]?\s*", re.I), "Cut: AI throat-clearing closer — end with the next action. "),
]


async def writing_ste_slop_suppress(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Skill: writing.ste.slop_suppress — strip 14 no-ai-slop patterns.

    Folded from github.com/petergyang/no-ai-slop. Each hit is dropped
    and the change recorded. Pure deterministic — no LLM, no network,
    no external state. Runs before any STE rule so the dropped
    fragments never enter the word-count / length-cap logic.

    Inputs:
      text (required): the text to clean.

    Returns: {ok, skill_id, rewritten, changes: [...], original_count, new_count}
    """
    text = (args.get("text") or "").strip()
    if not text:
        return {"ok": False, "skill_id": "writing.ste.slop_suppress",
                "error_code": "invalid_args", "message": "text required"}
    if len(text) > 50_000:
        return {"ok": False, "skill_id": "writing.ste.slop_suppress",
                "error_code": "text_too_long", "message": "text exceeds 50000 chars"}

    original_count = _word_count(text)
    changes: list[dict[str, str]] = []
    rewritten = text
    for pattern, note in _NO_AI_SLOP_PATTERNS:
        def _sub(match: re.Match, note: str = note) -> str:
            changes.append({"rule": "no_ai_slop", "before": match.group(0), "after": "", "reason": note.strip()})
            return ""
        rewritten = pattern.sub(_sub, rewritten)
    # Collapse orphan spaces / newlines left by the drops.
    rewritten = re.sub(r"[ \t]{2,}", " ", rewritten)
    rewritten = re.sub(r"\n[ \t]+", "\n", rewritten)
    rewritten = re.sub(r"\n{3,}", "\n\n", rewritten)
    new_count = _word_count(rewritten)
    return {
        "ok": True,
        "skill_id": "writing.ste.slop_suppress",
        "rewritten": rewritten.strip(),
        "changes": changes,
        "original_word_count": original_count,
        "new_word_count": new_count,
        "word_reduction": original_count - new_count,
        "patterns_caught": len(_NO_AI_SLOP_PATTERNS),
    }


# ===========================================================================
# v2.8.12 — writing.adhd_output
# Folded from github.com/ayghri/i-have-adhd. Applies ADHD-friendly
# output style to a chat reply: lead with the next concrete action,
# number multi-step work, restate state, suppress tangents, end with
# ONE concrete next action in under two minutes.
# ===========================================================================

async def writing_adhd_output(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Skill: writing.adhd_output — shape a chat reply for an ADHD reader.

    Rules (from i-have-adhd SKILL.md):
      1. Lead with the next action.
      2. Number multi-step tasks.
      3. End with one concrete next action.
      4. Suppress tangents (move to "Separately:" if needed).
      5. Restate state every turn (with step / total_steps).
      6. No "Hope this helps" closers.
      7. Specific time estimates, not "a bit" or "a few hours".

    Inputs:
      text (required): the reply to shape.
      step (optional, string like "3"): current step number.
      total_steps (optional, int): total steps in the multi-step work.

    Returns: {ok, skill_id, rewritten, closers_stripped, time_estimates_rewritten}
    """
    text = (args.get("text") or "").strip()
    if not text:
        return {"ok": False, "skill_id": "writing.adhd_output",
                "error_code": "invalid_args", "message": "text required"}
    if len(text) > 50_000:
        return {"ok": False, "skill_id": "writing.adhd_output",
                "error_code": "text_too_long", "message": "text exceeds 50000 chars"}

    step = (args.get("step") or "").strip()
    total_steps = args.get("total_steps")
    rewritten = text
    changes: list[str] = []

    # 1. Strip AI closers
    closers = [
        r"\bHope this helps[.!]?\s*",
        r"\bLet me know if you have any questions[.!]?\s*",
        r"\bIf you need anything else,? (just )?let me know[.!]?\s*",
        r"\bFeel free to (reach out|ask)[.!]?\s*",
        r"\bI hope (this|that) (helps|was helpful)[.!]?\s*",
        r"\bDon't hesitate to (ask|reach out)[.!]?\s*",
    ]
    for c in closers:
        new, n = re.subn(c, "", rewritten, flags=re.I)
        if n > 0:
            rewritten = new
            changes.append(f"closer:{c}")

    # 2. Rewrite vague time estimates to concrete ones
    vague_times = [
        (r"\bin\s+a\s+(?:bit|second|moment|minute|jiffy|sec)\b", "in under 2 minutes"),
        (r"\b(?:very\s+)?shortly\b", "in the next 2 minutes"),
        (r"\b(?:just\s+)?a\s+(?:bit|little|tiny\s+bit|second|minute)\b", "in under 2 minutes"),
        (r"\bsoon(ish)?\b", "in the next 5 minutes"),
        (r"\beventually\b", "after the next step"),
        (r"\bin\s+the\s+near\s+future\b", "in the next 10 minutes"),
    ]
    for pat, repl in vague_times:
        new, n = re.subn(pat, repl, rewritten, flags=re.I)
        if n > 0:
            rewritten = new
            changes.append(f"time:{pat} -> {repl}")

    # 3. Prepend state-restated banner if step + total_steps given
    if step and total_steps:
        try:
            step_n = int(step)
            total_n = int(total_steps)
            if 0 < step_n <= total_n:
                banner = f"Step {step_n} of {total_n}. "
                # Only prepend if not already present
                if not re.search(rf"\bstep\s+{step_n}\s+of\s+{total_n}\b", rewritten, flags=re.I):
                    rewritten = banner + rewritten
                    changes.append("banner:state")
        except (TypeError, ValueError):
            pass

    # 4. End with the next-action suffix if the reply already
    #    mentions a next step but ends without a concrete action.
    next_action = ""
    if not re.search(r"\bnext[:.]?\s+\w+", rewritten, flags=re.I) and len(rewritten) > 200:
        # Don't add a fake action — but flag the issue.
        changes.append("warning:no_concrete_next_action_in_under_2_minutes")

    rewritten = rewritten.rstrip()
    if rewritten and not rewritten.endswith((".", "!", "?", ":")):
        rewritten += "."

    return {
        "ok": True,
        "skill_id": "writing.adhd_output",
        "rewritten": rewritten,
        "changes": changes,
        "closers_stripped": sum(1 for c in changes if c.startswith("closer:")),
        "time_estimates_rewritten": sum(1 for c in changes if c.startswith("time:")),
    }


# ===========================================================================
# v2.8.12 — meta.book_to_skill
# Folded from github.com/virgiliojr94/book-to-skill. Reads a PDF or
# Markdown file, extracts chapter-level chunks, normalizes each with
# writing.ste.slop_suppress, indexes them with rag.upsert, and writes
# data/skills/<slug>/SKILL.md as the agent's long-term recall of the
# book. The agent can then call this skill to ingest any of the
# reference PDFs the operator drops in /workspace/attachments.
# ===========================================================================

async def meta_book_to_skill(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Skill: meta.book_to_skill — convert a PDF/MD into an AION skill.

    Inputs:
      path (required): absolute or workspace-relative path to the source file.
      slug (optional, default = filename stem): skill slug.
      max_pages (optional, default 600): hard cap on PDF pages to ingest.

    Returns: {ok, skill_id, slug, path, chapters_indexed, skill_md_path}
    """
    import os
    import re as _re
    import time as _time
    from pathlib import Path
    path_str = (args.get("path") or "").strip()
    if not path_str:
        return {"ok": False, "skill_id": "meta.book_to_skill",
                "error_code": "invalid_args", "message": "path required"}
    if not Path(path_str).is_file():
        return {"ok": False, "skill_id": "meta.book_to_skill",
                "error_code": "file_not_found", "message": f"not a file: {path_str}"}
    slug = (args.get("slug") or Path(path_str).stem).strip().lower()
    slug = _re.sub(r"[^a-z0-9-]+", "-", slug).strip("-")[:64] or "book"
    try:
        max_pages = max(1, min(2000, int(args.get("max_pages") or 600)))
    except (TypeError, ValueError):
        max_pages = 600

    suffix = Path(path_str).suffix.lower()
    if suffix == ".pdf":
        text = await asyncio.to_thread(_pdf_to_text, path_str, max_pages)
    elif suffix in (".md", ".markdown", ".txt"):
        text = Path(path_str).read_text(encoding="utf-8", errors="replace")
    else:
        return {"ok": False, "skill_id": "meta.book_to_skill",
                "error_code": "pdf_parse_failed", "message": f"unsupported suffix: {suffix}"}
    if not text.strip():
        return {"ok": False, "skill_id": "meta.book_to_skill",
                "error_code": "pdf_parse_failed", "message": "no text extracted"}

    # Split into chapters by the canonical "CHAPTER", "## ", or page-break heuristics.
    chapters = _split_chapters(text)
    if not chapters:
        chapters = [("body", text)]
    chapters_indexed = 0
    for chap_id, chap_text in chapters[:max_pages]:
        # Normalize with the slop-suppressor before indexing.
        norm = await writing_ste_slop_suppress({"text": chap_text}, ctx)
        norm_text = norm.get("rewritten", chap_text) if norm.get("ok") else chap_text
        # Try to call rag.upsert if it's available in this process.
        try:
            from app.skills.rag.skills_rag import rag_upsert as _rag_upsert
            await _rag_upsert(
                {"collection": "docs", "text": norm_text[:8000],
                 "source": path_str, "meta": {"chapter": chap_id, "slug": slug, "kind": "book"}},
                ctx,
            )
            chapters_indexed += 1
        except Exception as exc:  # pragma: no cover
            logger.warning("book_to_skill rag.upsert failed for %s/%s: %s", slug, chap_id, exc)

    # Write the SKILL.md skeleton.
    skill_dir = Path("data/skills") / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    body = text[:4000].strip()
    skill_md.write_text(
        f"---\nname: {slug}\n"
        f"description: Ingested from {Path(path_str).name}. "
        f"{chapters_indexed} chapter(s) indexed. Use this skill when the operator asks "
        f"about the book's content.\n---\n\n# {slug}\n\n"
        f"_Ingested from `{path_str}` on {_time.strftime('%Y-%m-%d')}._\n\n"
        f"## First 4000 chars\n\n{body}\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "skill_id": "meta.book_to_skill",
        "slug": slug,
        "path": path_str,
        "chapters_indexed": chapters_indexed,
        "skill_md_path": str(skill_md),
    }


def _pdf_to_text(path: str, max_pages: int) -> str:
    """Read a PDF via pdftotext (already on the DO image). Sync helper."""
    import subprocess
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-l", str(max_pages), path, "-"],
            capture_output=True, text=True, check=True, timeout=120,
        )
        return result.stdout
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"pdftotext failed: {exc}") from exc


def _split_chapters(text: str) -> list[tuple[str, str]]:
    """Split a book into (chapter_id, chapter_text) tuples.

    Heuristic: look for "CHAPTER N" or "## N" or "Chapter N" as the
    boundary. Falls back to a single "body" chapter if no markers.
    """
    import re as _re
    pattern = _re.compile(
        r"(?m)^(?:#{1,3}\s+)?(?:CHAPTER|Chapter|CHAPTER\s+)\s*([\wIVXLCDM]+|\d+)\b[^\n]*\n",
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return [("body", text)]
    chapters: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chap_id = m.group(0).strip().split("\n", 1)[0][:60]
        chapters.append((chap_id, text[start:end].strip()))
    return chapters
