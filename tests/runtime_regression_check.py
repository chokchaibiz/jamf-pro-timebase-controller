#!/usr/bin/env python3
"""Runtime regression checks using mocked Jamf responses; no external API calls."""
from __future__ import annotations

import logging
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harrow_timebase import GroupInfo, TimeBaseController


class FakeResponse:
    def json(self):
        # Reproduce the verified tenant quirk: Email exists but serialNumber is null.
        return {
            "totalCount": 2,
            "results": [
                {
                    "general": {"serialNumber": None},
                    "userAndLocation": {"emailAddress": "harrow02@harrowbangkok.th"},
                },
                {
                    "general": {"serialNumber": None},
                    "userAndLocation": {"emailAddress": "harrow04@harrowbangkok.th"},
                },
            ],
        }


class FakeJamf:
    def request(self, *args, **kwargs):
        return FakeResponse()

    def classic_xml(self, method, path, **kwargs):
        if "DMQWG6ZYJMVT" in path:
            email = "harrow02@harrowbangkok.th"
        elif "XDLQYHQ260" in path:
            email = "harrow04@harrowbangkok.th"
        else:
            email = ""
        return ET.fromstring(
            f"<mobile_device><location><email_address>{email}</email_address>"
            f"<room>100</room></location></mobile_device>"
        )


controller = TimeBaseController.__new__(TimeBaseController)
controller.cfg = {
    "attendance": {
        "inventory_page_size": 100,
        "classic_email_fallback_enabled": True,
        "classic_fallback_concurrency": 2,
    },
    "performance": {"concurrency": 2},
    "safety": {"email_inventory_min_coverage": 0.95},
}
controller.jamf = FakeJamf()
controller.logger = logging.getLogger("runtime-regression")
controller.master_group = lambda: GroupInfo(123, "Harrow-All-iPads", False)

master = {"DMQWG6ZYJMVT", "XDLQYHQ260"}
index = controller._master_inventory_email_index(master)
assert index["harrow02@harrowbangkok.th"] == {"DMQWG6ZYJMVT"}
assert index["harrow04@harrowbangkok.th"] == {"XDLQYHQ260"}

print("Runtime regression: v2 serialNumber=None -> Classic Email fallback: PASS")
