"""Keep structured continuations intact without changing upstream Hermes.

Hermes' prose join inserts a newline at non-whitespace continuation seams. Inside
a JSON string that creates invalid JSON. Only reconcile this exact transformation,
against every observed upstream part and a complete JSON object/array.
"""
import json


def response_content(final, parts, streamed=None):
    parts = [part for part in parts if part]
    original = "".join(parts)
    prose = ""
    for part in parts:
        if prose and not prose[-1].isspace() and not part[0].isspace():
            prose += "\n"
        prose += part
    try:
        structured = isinstance(json.loads(original), (dict, list))
    except (ValueError, TypeError):
        structured = False
    exact_join = bool(parts) and structured and final == prose.strip()
    if streamed is None:
        # Some backends restart a complete JSON answer after an incomplete
        # attempt. Hermes' prose concatenation then prefixes it with truncated
        # JSON. Select only the actual complete last upstream reply, and only
        # when every preceding part is incomplete and the concatenation is
        # exactly accounted for. No JSON or source text is repaired/invented.
        if len(parts)>1 and not structured and final==prose.strip():
            complete=[]
            for part in parts:
                try:complete.append(isinstance(json.loads(part),(dict,list)))
                except (ValueError,TypeError):complete.append(False)
            if complete[-1] and not any(complete[:-1]):
                return parts[-1]
        return original if exact_join else final
    visible = "".join(streamed)
    if visible == final:
        return visible
    if visible != original or not (final == visible.strip() or exact_join):
        raise ValueError("hermes_stream_not_contiguous")
    return visible
