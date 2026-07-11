"""Small, MicroPython-safe navigation model for the badge UI.

Keeping route, cursor and multi-select behavior independent of ``badgeware``
lets it be exercised on a desktop before copying the app to the badge.
"""


class AppModel:
    HOME = "home"
    DEVICES = "devices"
    REMOTE = "remote"
    DEVICE_DETAIL = "device_detail"
    DEVICE_ACTIONS = "device_actions"
    DELETE_CONFIRM = "delete_confirm"
    POWER_REPAIR_CONFIRM = "power_repair_confirm"
    NAME_EDITOR = "name_editor"
    ADD_DEVICE = "add_device"
    NEARBY_OPTIONS = "nearby_options"
    NEARBY_RESULTS = "nearby_results"
    NEARBY_ACTIONS = "nearby_actions"
    DIAGNOSTICS = "diagnostics"
    LISTEN_TEST = "listen_test"
    SYNC = "sync"
    RESTORE_CONFIRM = "restore_confirm"
    ABOUT = "about"

    def __init__(self, visible_rows=5):
        visible_rows = int(visible_rows)
        if visible_rows < 1:
            raise ValueError("visible_rows must be positive")
        self.visible_rows = visible_rows
        self.route = self.HOME
        self.history = []
        self.cursors = {}
        self.offsets = {}
        self.selected_results = set()

    @property
    def cursor(self):
        return self.cursors.get(self.route, 0)

    @property
    def offset(self):
        return self.offsets.get(self.route, 0)

    def open(self, route, reset=True):
        if route != self.route:
            self.history.append(self.route)
        self.route = route
        if reset:
            self.cursors[route] = 0
            self.offsets[route] = 0
        return route

    def replace(self, route, reset=True):
        self.route = route
        if reset:
            self.cursors[route] = 0
            self.offsets[route] = 0
        return route

    def back(self):
        if self.history:
            self.route = self.history.pop()
        else:
            self.route = self.HOME
        return self.route

    def go_home(self):
        self.history = []
        self.route = self.HOME
        return self.route

    def clamp(self, count):
        """Clamp the active route's cursor and scrolling to ``count`` rows."""

        count = max(0, int(count))
        if count == 0:
            index = 0
            offset = 0
        else:
            index = min(max(0, self.cursor), count - 1)
            max_offset = max(0, count - self.visible_rows)
            offset = min(max(0, self.offset), max_offset)
            if index < offset:
                offset = index
            elif index >= offset + self.visible_rows:
                offset = index - self.visible_rows + 1
        self.cursors[self.route] = index
        self.offsets[self.route] = offset
        return index

    def move(self, count, delta):
        """Move without wrapping so list ends remain predictable."""

        count = max(0, int(count))
        delta = int(delta)
        self.clamp(count)
        if count:
            self.cursors[self.route] = min(
                count - 1, max(0, self.cursor + delta)
            )
        return self.clamp(count)

    def visible_range(self, count):
        self.clamp(count)
        return self.offset, min(int(count), self.offset + self.visible_rows)

    @staticmethod
    def result_key(result):
        if not isinstance(result, dict):
            raise ValueError("discovery result must be an object")
        transport = result.get("transport")
        address = result.get("address")
        if not transport or not address:
            raise ValueError("discovery result needs transport and address")
        return str(transport) + ":" + str(address)

    def is_result_selected(self, result):
        return self.result_key(result) in self.selected_results

    def toggle_result(self, result):
        key = self.result_key(result)
        if key in self.selected_results:
            self.selected_results.remove(key)
            return False
        self.selected_results.add(key)
        return True

    def select_all_results(self, results):
        self.selected_results = set(self.result_key(item) for item in results)
        return len(self.selected_results)

    def clear_result_selection(self):
        self.selected_results = set()

    def selected_from(self, results):
        return [item for item in results if self.is_result_selected(item)]
