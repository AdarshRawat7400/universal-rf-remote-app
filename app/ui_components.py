"""Shared 160x120 drawing helpers for Universal IR."""

from badgeware import brushes, io, screen, shapes

from marquee import marquee_window


BACKGROUND = brushes.color(10, 14, 22)
PANEL = brushes.color(28, 36, 49)
PANEL_SELECTED = brushes.color(52, 75, 104)
HEADER = brushes.color(211, 250, 55)
HEADER_TEXT = brushes.color(12, 18, 25)
TEXT = brushes.color(240, 245, 252)
MUTED = brushes.color(139, 151, 168)
SUCCESS = brushes.color(91, 218, 130)
WARNING = brushes.color(255, 191, 82)
DANGER = brushes.color(255, 100, 112)
ACCENT = brushes.color(88, 166, 255)


def fit_text(value, maximum_width):
    value = str(value)
    width, _height = screen.measure_text(value)
    if width <= maximum_width:
        return value
    suffix = "..."
    while value:
        value = value[:-1]
        width, _height = screen.measure_text(value + suffix)
        if width <= maximum_width:
            return value + suffix
    return suffix


def marquee_text(value, maximum_width, now_ms=None):
    def measure(value):
        width, _height = screen.measure_text(value)
        return width

    if now_ms is None:
        now_ms = io.ticks
    return marquee_window(value, maximum_width, measure, now_ms)


def centered_text(value, y):
    value = str(value)
    width, _height = screen.measure_text(value)
    screen.text(value, 80 - width / 2, y)


def clear():
    screen.brush = BACKGROUND
    screen.clear()


def header(title, badge=None):
    screen.brush = HEADER
    screen.draw(shapes.rectangle(0, 0, 160, 16))
    screen.brush = HEADER_TEXT
    screen.text(fit_text(title, 116 if badge else 150), 5, 2)
    if badge:
        badge = fit_text(badge, 37)
        width, _height = screen.measure_text(badge)
        screen.text(badge, 155 - width, 2)


def footer(
    left="",
    center="",
    right="",
    message=None,
    danger=False,
    message_elapsed_ms=None,
):
    screen.brush = DANGER if danger else PANEL
    screen.draw(shapes.rectangle(0, 101, 160, 19))
    if message:
        screen.brush = TEXT
        screen.text(marquee_text(message, 152, message_elapsed_ms), 4, 105)
        return
    screen.brush = MUTED
    if left:
        screen.text(fit_text(left, 51), 4, 105)
    if center:
        center = fit_text(center, 55)
        width, _height = screen.measure_text(center)
        screen.text(center, 80 - width / 2, 105)
    if right:
        right = fit_text(right, 51)
        width, _height = screen.measure_text(right)
        screen.text(right, 156 - width, 105)


def menu_rows(
    rows,
    cursor,
    offset,
    visible_rows=5,
    has_above=None,
    has_below=None,
):
    """Draw compact rows.

    Each row is a dict with ``label`` and optional ``detail``, ``checked``,
    ``disabled`` or ``danger`` keys.
    """

    y = 19
    end = min(len(rows), offset + visible_rows)
    for index in range(offset, end):
        row = rows[index]
        selected = index == cursor
        if selected:
            screen.brush = PANEL_SELECTED
            screen.draw(shapes.rounded_rectangle(3, y - 2, 154, 15, 3))

        checked = row.get("checked")
        if checked is True:
            prefix = "[x] "
        elif checked is False:
            prefix = "[ ] "
        elif row.get("active"):
            prefix = "* "
        else:
            prefix = ""

        if row.get("disabled"):
            label_brush = MUTED
        elif row.get("danger"):
            label_brush = DANGER
        else:
            label_brush = TEXT

        detail = str(row.get("detail", ""))
        detail_width = 0
        if detail:
            detail = marquee_text(detail, 43) if selected else fit_text(detail, 43)
            detail_width, _height = screen.measure_text(detail)
            screen.brush = ACCENT if selected else MUTED
            screen.text(detail, 154 - detail_width, y)

        screen.brush = label_brush
        prefix_width = 0
        if prefix:
            prefix_width, _height = screen.measure_text(prefix)
            screen.text(prefix, 7, y)
        label_width = max(4, 144 - detail_width - prefix_width)
        label = str(row.get("label", ""))
        label = marquee_text(label, label_width) if selected else fit_text(label, label_width)
        screen.text(label, 7 + prefix_width, y)
        y += 16

    if has_above is None:
        has_above = offset > 0
    if has_below is None:
        has_below = end < len(rows)
    if has_above:
        screen.brush = HEADER
        screen.text("^", 151, 17)
    if has_below:
        screen.brush = HEADER
        screen.text("v", 151, 89)


def empty_state(title, detail=None):
    screen.brush = MUTED
    centered_text(title, 43)
    if detail:
        screen.brush = TEXT
        centered_text(fit_text(detail, 148), 59)


def status_pill(value, y, color=None):
    value = fit_text(value, 142)
    width, _height = screen.measure_text(value)
    screen.brush = color or PANEL_SELECTED
    screen.draw(shapes.rounded_rectangle(80 - width / 2 - 5, y - 2, width + 10, 15, 4))
    screen.brush = TEXT
    screen.text(value, 80 - width / 2, y)
