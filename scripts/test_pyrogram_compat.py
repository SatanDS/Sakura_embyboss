#!/usr/bin/env python3
import importlib.util
import sys
import unittest
from pathlib import Path

from pyrogram import utils as pyrogram_utils


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bot" / "func_helper" / "pyrogram_compat.py"
SPEC = importlib.util.spec_from_file_location("sakura_pyrogram_compat", MODULE_PATH)
COMPAT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = COMPAT
SPEC.loader.exec_module(COMPAT)


class PyrogramPeerCompatibilityTests(unittest.TestCase):
    def test_large_supergroup_id_is_recognized(self):
        COMPAT.patch_large_channel_ids()

        peer_id = -1003784747037
        self.assertEqual(pyrogram_utils.get_peer_type(peer_id), "channel")
        self.assertEqual(pyrogram_utils.get_channel_id(peer_id), 3784747037)

    def test_legacy_peer_types_still_work(self):
        COMPAT.patch_large_channel_ids()

        self.assertEqual(pyrogram_utils.get_peer_type(-1001234567890), "channel")
        self.assertEqual(pyrogram_utils.get_peer_type(123456), "user")


if __name__ == "__main__":
    unittest.main(verbosity=2)
