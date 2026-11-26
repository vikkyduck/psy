"""Continue at provider output limits without discarding earlier answer context."""
import hashlib

if __package__:
    from .psy_group_client import ModelIssue, Result, _stage_key, _user
    from .psy_group_prompts import CONTINUE as _CONTINUE
else:
    from psy_group_client import ModelIssue, Result, _stage_key, _user
    from psy_group_prompts import CONTINUE as _CONTINUE


# Retained for compatibility with old fixtures and cache metadata. It no longer
# limits what the model sees. Provider context limits must remain explicit.
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
    """Retain every prior output block in the next request, with no answer-length cap."""
    if previous_chapter is not None:
        # For a supplement the last turn is the missing obligation from editorial review.
        full_chapter = {"role": "assistant", "content": previous_chapter}
        tail = previous_chapter
        first_turns = old_base = [*turns[:-1], full_chapter, turns[-1]]
    else:
        tail = ""
        first_turns = old_base = turns

    # Bind each durable block to the original stage and every preceding output block.
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
    parts = [part]
    seen = {hashlib.sha256(part.text.strip().encode()).digest()}

    def assembled(complete, *, reason=None, stage=None, note=""):
        metadata = {}
        for item in parts:
            metadata.update(getattr(item, "quality_metadata", {}))
        metadata.update(continuation_parts=len(parts), continuation_context="full_output",
                        current_generation_complete=complete)
        return Result("".join(pieces) + note, [image for item in parts for image in item.images],
                      sum(item.searches for item in parts), complete,
                      tuple(dict.fromkeys(url for item in parts for url in item.source_urls)),
                      quality_metadata=metadata,
                      stop_reason=reason or getattr(parts[-1], "stop_reason", "unknown"),
                      stop_stage=stage or getattr(parts[-1], "stop_stage", None),
                      model_actual=getattr(parts[-1], "model_actual", None))

    while not part.complete:
        if getattr(part, "stop_reason", None) == "provider_refusal":
            return assembled(False)
        tail = "".join(pieces)
        continuation = [*old_base, {"role": "assistant", "content": tail}, _user(_CONTINUE)]
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
            if exc.stage == "stream_empty":
                return assembled(False, reason=exc.stop_reason, stage=exc.stage)
            if exc.kind not in ("timeout", "context", "connection", "busy", "quota", "signin", "access_wait"):
                raise
            return assembled(False, reason=exc.stop_reason, stage=exc.stage,
                             note="\n\n[Продолжение не завершено. " + exc.public() + "]")

        parts.append(part)
        text = part.text
        stripped = text.strip()
        signature = hashlib.sha256(stripped.encode()).digest()
        if not stripped or signature in seen or stripped in tail:
            return assembled(False, reason="continuation_no_progress", stage="continuation")
        seen.add(signature)
        overlap = _overlap(tail, text)
        # A provider limit can split a word, number or code token. The model sees
        # the full prefix and must supply the exact continuation, including whitespace.
        # Historical blocks used an inserted newline, so preserve that on replay only.
        separator = "\n" if legacy_hit else ""
        if not legacy_hit and overlap >= 32:
            separator = ""
            text = text[overlap:]
            if not text.strip():
                return assembled(False, reason="continuation_no_progress", stage="continuation")
        pieces.extend((separator, text))
    return assembled(True)
