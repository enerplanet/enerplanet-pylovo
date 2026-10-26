"""Tests for reading an export_pylovo.py package (runme/import/load_region.py). No database needed.

    python -m unittest tests.test_load_region -v
"""
import gzip
import importlib
import tempfile
import unittest
from pathlib import Path

load_region = importlib.import_module("runme.import.load_region")

PACKAGE = """-- pylovo fixture: DE 94469, version_id 1.
CREATE TABLE IF NOT EXISTS grid_result (grid_result_id integer);

-- version: 1 rows
INSERT INTO version (version_id) VALUES ('1');

-- grid_result: 2 rows
INSERT INTO grid_result (grid_result_id,grid) VALUES ('7','{"a":
 "b"}');
INSERT INTO grid_result (grid_result_id,grid) VALUES ('8',NULL);

-- lines_result: 0 rows

-- CREATE MATERIALIZED VIEW populates on creation
REFRESH MATERIALIZED VIEW lines_result_with_grid;
"""


class ReadSectionsTest(unittest.TestCase):
    """read_sections splits a package into staging-qualified INSERTs per table."""

    def _write(self, text: str, gz: bool = False) -> Path:
        directory = Path(tempfile.mkdtemp())
        path = directory / ("p.sql.gz" if gz else "p.sql")
        if gz:
            with gzip.open(path, "wt", encoding="utf-8") as f:
                f.write(text)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def test_sections_and_counts(self):
        sections = load_region.read_sections(self._write(PACKAGE))
        self.assertEqual(set(sections), {"version", "grid_result", "lines_result"})
        self.assertEqual(sections["grid_result"][0], 2)
        self.assertEqual(sections["lines_result"], (0, ""))

    def test_inserts_target_staging_and_keep_multiline_values(self):
        statements = load_region.read_sections(self._write(PACKAGE, gz=True))["grid_result"][1]
        self.assertEqual(statements.count("INSERT INTO pg_temp.grid_result ("), 2)
        self.assertNotIn("INSERT INTO grid_result", statements)
        self.assertIn('{"a":\n "b"}', statements)

    def test_schema_and_trailer_are_not_rows(self):
        statements = "".join(s for _, s in load_region.read_sections(self._write(PACKAGE)).values())
        self.assertNotIn("CREATE TABLE", statements)
        self.assertNotIn("REFRESH", statements)

    def test_insert_into_another_table_is_rejected(self):
        bad = PACKAGE.replace("INSERT INTO grid_result (grid_result_id,grid) VALUES ('8'",
                              "INSERT INTO lines_result (grid_result_id,grid) VALUES ('8'")
        with self.assertRaises(ValueError):
            load_region.read_sections(self._write(bad))

    def test_file_without_sections_is_rejected(self):
        with self.assertRaises(ValueError):
            load_region.read_sections(self._write("SELECT 1;\n"))


if __name__ == "__main__":
    unittest.main()
