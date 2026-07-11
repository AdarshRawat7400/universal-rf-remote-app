"""Small built-in IR profiles for immediate first-run use."""

import config


# Common Samsung television mapping, cross-checked against multiple remotes in
# the CC0-licensed Flipper-IRDB Samsung collection:
# https://github.com/Lucaslhm/Flipper-IRDB/tree/main/TVs/Samsung
SAMSUNG_TV_ADDRESS = 0x07
SAMSUNG_TV_COMMANDS = (
    ("Power", 0x02),
    ("Input", 0x01),
    ("Volume +", 0x07),
    ("Volume -", 0x0B),
    ("Mute", 0x0F),
    ("Channel +", 0x12),
    ("Channel -", 0x10),
    ("Home", 0x79),
    ("Menu", 0x1A),
    ("Info", 0x1F),
    ("Tools", 0x4B),
    ("Up", 0x60),
    ("Down", 0x61),
    ("Left", 0x65),
    ("Right", 0x62),
    ("OK", 0x68),
    ("Back", 0x2D),
    ("Play", 0x47),
    ("Pause", 0x4A),
    ("Stop", 0x46),
    ("Rewind", 0x45),
    ("Fast Forward", 0x48),
    ("Previous Channel", 0x13),
    ("Channel List", 0x6B),
    ("Sleep", 0x03),
    ("0", 0x11),
    ("1", 0x04),
    ("2", 0x05),
    ("3", 0x06),
    ("4", 0x08),
    ("5", 0x09),
    ("6", 0x0A),
    ("7", 0x0C),
    ("8", 0x0D),
    ("9", 0x0E),
)

# A blank learned remote starts with the useful superset below. Commands are
# not persisted until the user successfully learns them, so an empty template
# costs no storage and never pretends that a code is available.
STANDARD_BUTTON_LABELS = tuple(name for name, _command in SAMSUNG_TV_COMMANDS)


def samsung_tv_profile(button_names=None):
    """Return storage-ready commands for the common Samsung32 TV mapping.

    ``button_names`` can request a subset during an in-place profile upgrade,
    avoiding a second full pulse database in RAM on the constrained badge.
    """

    commands = {}
    for name, command in SAMSUNG_TV_COMMANDS:
        if button_names is not None and name not in button_names:
            continue
        commands[name] = {
            "format": "samsung32",
            "carrier_hz": config.CARRIER_HZ,
            "repeat_count": 1,
            "repeat_gap_us": 40_000,
            "description": "SAMSUNG32 A:%02X C:%02X"
            % (SAMSUNG_TV_ADDRESS, command),
            "address": SAMSUNG_TV_ADDRESS,
            "command": command,
        }
    return commands
