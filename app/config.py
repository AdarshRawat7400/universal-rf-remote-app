"""Hardware and UI configuration for the GitHub Universe 2025 badge."""

APP_NAME = "Universal IR"

try:
    import board as _board
except ImportError:
    _board = None


def _board_pin(name, fallback):
    """Prefer the badge firmware's aliases without breaking desktop tools."""
    if _board is None:
        return fallback
    pin = getattr(_board, name, None)
    return fallback if pin is None else pin


# MonaOS exposes these aliases on the Universe 2025 badge.  The numeric values
# keep the app usable with older badge firmware and desktop/simulator imports.
RX_PIN = _board_pin("IR_RX", 21)
RX_PIO = 0
RX_STATE_MACHINE = 0

TX_PIN = _board_pin("IR_TX", 20)
TX_PIO = 0
TX_STATE_MACHINE = 1

CARRIER_HZ = 38_000
TX_REPETITIONS = 1
TX_INTER_FRAME_GAP_MS = 40
TX_MAX_BURST_FRAMES = 3
TX_BURST_PRESETS = {
    "single": 1,
    "reliable": 2,
    "strong": 3,
}
TX_PROTOCOL_REPEAT_PERIOD_US = {
    "NEC": 110_000,
    "NEC2": 110_000,
    "SAMSUNG": 110_000,
    "SAMSUNG32": 110_000,
    "SAMSUNGLG": 110_000,
}
RX_RECOVERY_MS = 20
RX_LISTEN_TIMEOUT_MS = 8_000
RX_CAPTURE_STALL_MS = 750
MAX_CAPTURE_PAIRS = 512
GLITCH_FILTER_US = 200
LEARNING_RELEASE_GAP_MS = 220
HOLD_REPEAT_MS = 140

STORAGE_DIR = "/storage/universal_ir"
PROFILE_PATH = STORAGE_DIR + "/profiles.json"

BUTTON_LABELS = (
    "Power",
    "Volume +",
    "Volume -",
    "Mute",
    "Channel +",
    "Channel -",
)

# Re-sending a complete Power or Mute frame while a key is held can toggle a
# device twice. Continuous sending is therefore limited to naturally
# repeatable controls until protocol-specific repeat frames are added.
REPEATABLE_BUTTONS = (
    "Volume +",
    "Volume -",
    "Channel +",
    "Channel -",
)
