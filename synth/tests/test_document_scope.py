"""A moved meeting does not show the previous folder's untouched pending items."""
import unittest
from unittest.mock import MagicMock

from synth.server.document_scope import for_readers

CONTENT = {"tldr": "Resumen.", "topics": [], "decisions": [], "tasks": [], "open_questions": [],
           "continuity": [{"text": "Revisar contrato", "status": "hecho", "citations": ["t3"]},
                          {"text": "Presupuesto confidencial", "status": "sin mencionar", "citations": []}]}


def database(written, present, receipt=True):
    db = MagicMock()
    def execute(sql, params=None):
        result = MagicMock()
        result.fetchone.return_value = ({"written": written, "present": present} if "FROM meetings" in sql
                                        else ({"exists": 1} if receipt else None))
        return result
    db.execute.side_effect = execute
    return db


class DocumentScopeTests(unittest.TestCase):
    def row(self):
        return {"version": 3, "content": CONTENT, "markdown": "# Acta de reunión\n\nPresupuesto confidencial\n"}

    def test_the_folder_the_acta_was_written_for_reads_it_whole(self):
        for folder in ("folder-a", None):
            with self.subTest(folder=folder):
                self.assertEqual(for_readers(database(folder, folder), "meeting", self.row()), self.row())

    def test_after_a_move_only_what_the_meeting_discussed_remains(self):
        for db in (database("folder-a", "folder-b"), database("folder-a", None), database(None, "folder-b"),
                   database(None, None, receipt=False)):
            visible = for_readers(db, "meeting", self.row())
            self.assertEqual([item["text"] for item in visible["content"]["continuity"]], ["Revisar contrato"])
            self.assertNotIn("Presupuesto confidencial", visible["markdown"])
            self.assertIn("Revisar contrato", visible["markdown"])
        # The stored row is never altered.
        self.assertEqual(len(CONTENT["continuity"]), 2)

    def test_an_acta_without_untouched_items_needs_no_lookup(self):
        db = MagicMock()
        row = {"version": 1, "content": {**CONTENT, "continuity": CONTENT["continuity"][:1]}}
        self.assertIs(for_readers(db, "meeting", row), row)
        db.execute.assert_not_called()
