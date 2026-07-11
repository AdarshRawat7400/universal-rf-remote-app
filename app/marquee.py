"""Character-window marquee logic independent of the badge renderer."""


def marquee_window(
    value,
    maximum_width,
    measure_width,
    now_ms,
    step_ms=180,
    pause_steps=5,
    spacer="   ",
):
    """Return a wrapping text window that fits ``maximum_width`` pixels.

    The beginning pauses briefly before moving one character per step. Using
    measured glyph width instead of a fixed character count supports the
    variable-width fonts shipped with MonaOS.
    """

    value = str(value)
    maximum_width = int(maximum_width)
    if maximum_width < 1:
        return ""
    if measure_width(value) <= maximum_width:
        return value

    step_ms = max(40, int(step_ms))
    pause_steps = max(0, int(pause_steps))
    cycle = value + spacer
    phase = (int(now_ms) // step_ms) % (len(cycle) + pause_steps)
    start = 0 if phase < pause_steps else phase - pause_steps

    result = ""
    # One extra full cycle is enough even for a very narrow glyph set.
    for offset in range(len(cycle) * 2):
        character = cycle[(start + offset) % len(cycle)]
        candidate = result + character
        if measure_width(candidate) > maximum_width:
            break
        result = candidate
    return result
