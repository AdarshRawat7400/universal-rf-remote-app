import builtins
import importlib.util
import os
import sys
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
PIO_RX_PATH = os.path.join(ROOT, "app", "ir", "pio_rx.py")


class PioRxConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        previous_const = getattr(builtins, "const", None)
        previous_rp2 = sys.modules.get("rp2")
        builtins.const = lambda value: value

        fake_rp2 = types.ModuleType("rp2")
        fake_rp2.PIO = types.SimpleNamespace(SHIFT_LEFT=0, JOIN_RX=0)
        fake_rp2.asm_pio = lambda **_kwargs: (lambda function: function)
        sys.modules["rp2"] = fake_rp2
        try:
            spec = importlib.util.spec_from_file_location("testable_pio_rx", PIO_RX_PATH)
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)
        finally:
            if previous_const is None:
                del builtins.const
            else:
                builtins.const = previous_const
            if previous_rp2 is None:
                del sys.modules["rp2"]
            else:
                sys.modules["rp2"] = previous_rp2

    def test_underflowed_terminal_idle_becomes_positive_timeout(self):
        self.assertEqual(8191, self.module.count_to_idle_us(0xFFFF))

    def test_normal_short_idle_conversion_is_unchanged(self):
        self.assertEqual(5, self.module.count_to_idle_us(self.module.IDLE_COUNT_TIMEOUT))


if __name__ == "__main__":
    unittest.main()
