"""Crash-visible bootstrap for the Universal IR application.

MonaOS otherwise returns directly to the launcher when an app import fails.
Keeping this shim tiny means a MicroPython-only error is both shown on-screen
and written beside the app for USB diagnosis.
"""

import os
import sys


APP_DIR = "/system/apps/universal_ir"
ERROR_PATH = APP_DIR + "/boot_error.txt"
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

_boot_error = None
_main = None

try:
    import main_app as _main
except BaseException as error:
    _boot_error = error
    try:
        with open(ERROR_PATH, "w") as handle:
            handle.write("Universal IR startup failed\n")
            handle.write(repr(error) + "\n")
            printer = getattr(sys, "print_exception", None)
            if printer is not None:
                printer(error, handle)
            try:
                import gc

                memory_free = getattr(gc, "mem_free", None)
                if memory_free is not None:
                    handle.write("heap_free=" + str(memory_free()) + "\n")
            except Exception:
                pass
    except Exception:
        pass
else:
    try:
        os.remove(ERROR_PATH)
    except OSError:
        pass


if _main is not None:
    app = _main.app
    update = _main.update
    on_exit = _main.on_exit
else:
    from badgeware import PixelFont, brushes, screen, shapes

    screen.font = PixelFont.load("/system/assets/fonts/ark.ppf")
    _error_text = repr(_boot_error)

    def update():
        screen.brush = brushes.color(13, 17, 23)
        screen.clear()
        screen.brush = brushes.color(255, 100, 112)
        screen.draw(shapes.rectangle(0, 0, 160, 17))
        screen.brush = brushes.color(15, 20, 28)
        screen.text("Universal IR failed", 5, 3)
        screen.brush = brushes.color(245, 247, 250)
        screen.text(_error_text[:25], 5, 28)
        screen.text(_error_text[25:50], 5, 43)
        screen.text(_error_text[50:75], 5, 58)
        screen.brush = brushes.color(255, 191, 82)
        screen.text("See boot_error.txt", 5, 82)
        screen.brush = brushes.color(45, 54, 66)
        screen.draw(shapes.rectangle(0, 101, 160, 19))
        screen.brush = brushes.color(200, 210, 222)
        screen.text("HOME returns to launcher", 5, 105)

    def on_exit():
        return None


if __name__ == "__main__":
    from badgeware import run

    run(update)
