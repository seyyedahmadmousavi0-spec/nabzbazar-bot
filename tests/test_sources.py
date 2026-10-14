import json
import unittest
from pathlib import Path

from marketintel.sources import load_sources_file, set_enabled, upsert_source

from .helpers import make_db


class SourceRegistry(unittest.TestCase):
    def test_load_from_json_without_code_changes_and_upsert_is_idempotent(self):
        c = make_db()
        p = Path(__file__).parent.parent / "config" / "sources.example.json"
        self.assertEqual(load_sources_file(c, p), ["example_fx_channel"])
        self.assertEqual(load_sources_file(c, p), ["example_fx_channel"])
        row = c.execute("SELECT * FROM sources").fetchone()
        self.assertEqual((row["enabled"], row["chat_ref"], json.loads(row["categories"])),
                         (0, "@example_channel", ["currency", "gold", "coin"]))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)

    def test_enable_disable_is_audited(self):
        c = make_db()
        sid = upsert_source(c, "x", name="X")
        set_enabled(c, sid, False, "admin")
        self.assertEqual(c.execute("SELECT enabled FROM sources").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT action FROM audit_log").fetchone()[0], "source_disable")


if __name__ == "__main__":
    unittest.main()
