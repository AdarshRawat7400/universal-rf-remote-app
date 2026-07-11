# SPDX-License-Identifier: MIT
# Derived from badger/home badge/apps/menu/__init__.py at commit
# 4a3bf0395f79ae386a8d952f7da54281a2f00299. Modified for reliable
# navigation across every discovered app page. See THIRD_PARTY_NOTICES.md.

import sys
import os

sys.path.insert(0, "/system/apps/menu")
os.chdir("/system/apps/menu")

import math
from badgeware import screen, PixelFont, Image, SpriteSheet, is_dir, file_exists, shapes, brushes, io, run
from icon import Icon
import ui

mona = SpriteSheet("/system/assets/mona-sprites/mona-default.png", 11, 1)
screen.font = PixelFont.load("/system/assets/fonts/ark.ppf")
# screen.antialias = Image.X2

# Auto-discover apps with __init__.py
apps = []
APP_DISPLAY_NAMES = {
    "universal_ir": "Universal IR",
    "monapet": "MonaPet",
}
try:
    app_entries = os.listdir("/system/apps")
except Exception as e:
    print(f"Error listing apps: {e}")
    app_entries = []

for entry in app_entries:
    try:
        app_path = f"/system/apps/{entry}"
        if is_dir(app_path):
            has_init = file_exists(f"{app_path}/__init__.py")
            if has_init:
                # Skip menu and startup apps
                if entry not in ("menu", "startup"):
                    # Turn app folder names into readable labels while keeping
                    # the original path for launching.
                    # This MicroPython build does not implement title-casing.
                    # Keep unknown app names untouched and only prettify known
                    # folders through a small, portable lookup table.
                    display_name = APP_DISPLAY_NAMES.get(entry, entry)
                    apps.append((display_name, entry))
    except Exception as e:
        # One malformed third-party app must not blank the entire launcher.
        print(f"Error discovering app {entry}: {e}")

# Pagination constants
APPS_PER_PAGE = 6
current_page = 0
total_pages = max(1, math.ceil(len(apps) / APPS_PER_PAGE))

# find installed apps and create icons for current page
def load_page_icons(page):
    icons = []
    start_idx = page * APPS_PER_PAGE
    end_idx = min(start_idx + APPS_PER_PAGE, len(apps))

    for i in range(start_idx, end_idx):
        app = apps[i]
        name, path = app[0], app[1]

        if is_dir(f"/system/apps/{path}"):
            icon_idx = i - start_idx
            x = icon_idx % 3
            y = math.floor(icon_idx / 3)
            pos = (x * 48 + 33, y * 48 + 42)
            try:
                # Try to load app-specific icon, fall back to default
                icon_path = f"/system/apps/{path}/icon.png"
                if not file_exists(icon_path):
                    icon_path = "/system/apps/menu/default_icon.png"
                sprite = Image.load(icon_path)
                icons.append(Icon(pos, name, icon_idx % APPS_PER_PAGE, sprite))
            except Exception as e:
                print(f"Error loading icon for {path}: {e}")
    return icons

icons = load_page_icons(current_page)

active = 0

# Navigation operates on one global app index rather than wrapping inside a
# six-icon page. Moving beyond the visible grid automatically scrolls to the
# next/previous page, including partially filled final pages.
NAV_REPEAT_MS = 180
last_navigation_at = -NAV_REPEAT_MS


def move_selection(delta):
    global active, current_page, icons

    if not apps:
        return
    global_index = (current_page * APPS_PER_PAGE + active + delta) % len(apps)
    next_page = global_index // APPS_PER_PAGE
    next_active = global_index % APPS_PER_PAGE
    if next_page != current_page:
        current_page = next_page
        icons = load_page_icons(current_page)
    active = min(next_active, max(0, len(icons) - 1))


def navigation_delta():
    """Return one grid movement, with hold-to-scroll rate limiting."""

    global last_navigation_at
    bindings = (
        (io.BUTTON_A, -1),
        (io.BUTTON_C, 1),
        (io.BUTTON_UP, -3),
        (io.BUTTON_DOWN, 3),
    )
    for button, delta in bindings:
        if button in io.pressed:
            last_navigation_at = io.ticks
            return delta
    if io.ticks - last_navigation_at >= NAV_REPEAT_MS:
        for button, delta in bindings:
            if button in io.held:
                last_navigation_at = io.ticks
                return delta
    return 0

MAX_ALPHA = 255
alpha = 30


def update():
    global active, icons, alpha, current_page, total_pages

    delta = navigation_delta()
    if delta:
        move_selection(delta)

    # Launch app with error handling
    if io.BUTTON_B in io.pressed:
        app_idx = current_page * APPS_PER_PAGE + active
        if app_idx < len(apps):
            app_path = f"/system/apps/{apps[app_idx][1]}"
            try:
                # Verify the app still exists before launching
                if is_dir(app_path) and file_exists(f"{app_path}/__init__.py"):
                    return app_path
                else:
                    print(f"Error: App {apps[app_idx][1]} not found or missing __init__.py")
            except Exception as e:
                print(f"Error launching app {apps[app_idx][1]}: {e}")

    ui.draw_background()
    ui.draw_header()

    # draw menu icons
    for i in range(len(icons)):
        icons[i].activate(active == i)
        icons[i].draw()

    # draw label for active menu icon
    if Icon.active_icon:
        label = f"{Icon.active_icon.name}"
        w, _ = screen.measure_text(label)
        screen.brush = brushes.color(211, 250, 55)
        screen.draw(shapes.rounded_rectangle(80 - (w / 2) - 4, 100, w + 8, 15, 4))
        screen.brush = brushes.color(0, 0, 0, 150)
        screen.text(label, 80 - (w / 2), 101)

    # draw page indicator if multiple pages
    if total_pages > 1:
        page_label = f"< {current_page + 1}/{total_pages} >"
        w, _ = screen.measure_text(page_label)
        screen.brush = brushes.color(211, 250, 55, 150)
        screen.text(page_label, 160 - w - 5, 108)

    if alpha <= MAX_ALPHA:
        screen.brush = brushes.color(0, 0, 0, 255 - alpha)
        screen.clear()
        alpha += 30

    return None

if __name__ == "__main__":
    run(update)
