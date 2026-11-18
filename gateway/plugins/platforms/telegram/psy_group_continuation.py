"""Continuation stitching: keep a long text locally and resend only its tail when the model stops at
its output ceiling, joining the parts without repeats."""
import hashlib

if __package__:
    from .psy_group_client import ModelIssue, Result, _stage_key, _user
    from .psy_group_prompts import CONTINUE as _CONTINUE
else:
    from psy_group_client import ModelIssue, Result, _stage_key, _user
    from psy_group_prompts import CONTINUE as _CONTINUE


CONTINUATION_TAIL_CHARS = 2048


def _overlap(tail, text):
    """Longest suffix/prefix shared at the join, in linear time within the context window."""
    prefix = text[:len(tail)]
    if not prefix:
        return 0
    # None separates the two strings even if either contains arbitrary Unicode.
    sequence = [*prefix, None, *tail]
    lengths = [0] * len(sequence)
    for i in range(1, len(sequence)):
        matched = lengths[i - 1]
        while matched and sequence[i] != sequence[matched]:
            matched = lengths[matched - 1]
        lengths[i] = matched + (sequence[i] == sequence[matched])
    return lengths[-1]


def _complete(call, turns, instructions, *, initial=None, continuation_call=None,
              model=None, previous_chapter=None):
    """Keep the full text locally. Each continuation sees only the current prose tail."""
    if previous_chapter is not None:
        # For a supplement the last turn is the missing obligation from editorial review.
        full_chapter = {"role": "assistant", "content": previous_chapter}
        tail = previous_chapter[-CONTINUATION_TAIL_CHARS:]
        first_turns = [*turns[:-1], {"role": "assistant", "content": tail}, turns[-1]]
        old_base = [*turns[:-1], full_chapter, turns[-1]]
    else:
        tail = ""
        first_turns = old_base = turns

    # The compact prompt can recur at different positions or after different chapters.
    # Bind each durable block to the original stage and to all preceding local output.
    if continuation_call is not None:
        chain = hashlib.sha256(_stage_key(model, old_base, instructions).encode())
    look_up_legacy = True

    if initial is not None:
        part = initial
    elif previous_chapter is not None and continuation_call is not None:
        part, look_up_legacy = continuation_call(first_turns, instructions, chain.hexdigest(),
                                                 lambda: old_base)
    else:
        part = call(first_turns, instructions)

    pieces = [part.text]
    separator = "\n" if tail else ""
    seen = {hashlib.sha256(part.text.strip().encode()).digest()}
    while not part.complete:
        # Limit each operand before concatenating. Only local assembly uses full pieces.
        tail = (tail + separator + pieces[-1][-CONTINUATION_TAIL_CHARS:])[-CONTINUATION_TAIL_CHARS:]
        continuation = [*turns, {"role": "assistant", "content": tail}, _user(_CONTINUE)]
        legacy_hit = False
        try:
            if continuation_call is None:
                part = call(continuation, instructions)
            else:
                data = part.text.encode()
                chain.update(len(data).to_bytes(8, "big"))
                chain.update(data)

                def legacy_prompt():
                    return [*old_base, {"role": "assistant", "content": "".join(pieces)}, _user(_CONTINUE)]

                part, legacy_hit = continuation_call(continuation, instructions, chain.hexdigest(),
                                                     legacy_prompt if look_up_legacy else None)
                look_up_legacy = legacy_hit
        except ModelIssue as exc:
            prior = "".join(pieces)
            if exc.stage == "stream_empty":
                return Result(prior, [], 0, False)
            if exc.kind not in ("timeout", "context", "connection", "busy", "quota", "signin", "access_wait"):
                raise
            return Result(prior + "\n\n[Продолжение не завершено. " + exc.public() + "]", [], 0, False)

        text = part.text
        stripped = text.strip()
        signature = hashlib.sha256(stripped.encode()).digest()
        if not stripped or signature in seen or stripped in tail:
            return Result("".join(pieces), [], 0, False)
        seen.add(signature)
        overlap = _overlap(tail, text)
        separator = "\n"
        if not legacy_hit and overlap >= 32:
            separator = ""
            text = text[overlap:]
            if not text.strip():
                return Result("".join(pieces), [], 0, False)
        pieces.extend((separator, text))
    return Result("".join(pieces), [], 0, True)
