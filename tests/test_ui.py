"""Real Tk widget tests. Run on a desktop-capable host."""
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from textlab.app import ObservedText, TextLab
from textlab.core import full_report


class TextProxyTest(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.changes = []
        self.text = ObservedText(self.root, lambda *args: self.changes.append(args), undo=True)

    def tearDown(self):
        self.root.update_idletasks()
        self.root.destroy()

    def test_typing_selection_delete_and_undo(self):
        self.text.insert("1.0", "Привет мир")
        self.text.edit_separator()
        self.text.tag_add("sel", "1.7", "1.10")
        self.text.delete("sel.first", "sel.last")
        self.assertEqual(self.changes[-1][0], "Привет ")
        self.assertEqual(self.changes[-1][2], [7, 10])
        before_undo = len(self.changes)
        self.text.edit_undo()
        self.assertEqual(self.changes[-1][0], "Привет мир")
        self.assertEqual(len(self.changes), before_undo + 1)

    def test_programmatic_topic_load_is_not_an_edit(self):
        self.text.replace_all("Тема два 😊")
        self.assertEqual(self.text.get("1.0", "end-1c"), "Тема два 😊")
        self.assertEqual(self.changes, [])

    def test_readonly_widget_produces_no_edits(self):
        self.text.configure(state="disabled")
        self.text.insert("end", "invalid")
        self.assertEqual(self.changes, [])

    def test_tcl_bindings_can_catch_missing_selection(self):
        result = self.root.tk.eval(f'catch {{{self.text._w} index sel.first}}')
        self.assertEqual(result, "1")
        self.text.event_generate("<Button-1>", x=20, y=20)
        self.root.update()
        self.assertEqual(self.changes, [])

    def test_undo_without_history_is_a_normal_tcl_error(self):
        with self.assertRaises(tk.TclError):
            self.text.edit_undo()
        self.root.update()


class ApplicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = TextLab(Path(self.temp.name))
        self.app.withdraw()
        self.app.update_idletasks()

    def tearDown(self):
        self.app.store.close()
        self.app.destroy()
        self.temp.cleanup()

    def start(self, topics=False):
        self.app.pvars["age"].set("29")
        if topics:
            self.app.cvars["mode"].set("topics")
            self.app.update_topic_fields()
            self.app.topic_list.selection_set(0, 1)
        self.app.start_session()

    def test_full_thematic_workflow_report_archive(self):
        self.start(topics=True)
        self.app.editor.insert("end", "Первый текст.")
        self.app.switch_topic(1)
        self.app.editor.insert("end", "Второй текст.")
        self.app.operation("paste")
        self.app.editor.insert("end", " Вставка.")
        self.app.rec.finish()
        self.app.finish_report()
        self.app.update_idletasks()
        self.assertEqual(self.app.view, "report")
        self.assertEqual(len(self.app.report["topics"]), 2)
        self.assertEqual(self.app.report["overall"]["pasted_characters"], 9)
        self.app.show_archive()
        self.assertEqual(self.app.view, "archive")
        saved = self.app.store.load(self.app.rec.session_id)
        self.assertEqual(saved["texts"], ["Первый текст.", "Второй текст. Вставка."])

    def test_character_limit_finishes_without_extra_input(self):
        self.app.cvars["target_characters"].set("3")
        self.start()
        self.app.editor.insert("end", "abc")
        self.assertEqual(self.app.rec.status, "completed")
        self.app.editor.insert("end", "d")
        self.app.update_idletasks()
        self.assertEqual(self.app.view, "report")
        self.assertEqual(self.app.snapshot["texts"], ["abc"])

    def test_focus_loss_stops_capture_until_resume(self):
        self.start()
        self.app.editor.insert("end", "a")
        self.app.on_focus_out(None)
        self.app.editor.insert("end", "b")
        self.assertEqual(self.app.rec.texts, ["a"])
        self.app.toggle_pause()
        self.app.editor.insert("end", "c")
        self.assertEqual(self.app.rec.texts, ["ac"])
        self.app.rec.finish()

    def test_pause_button_focus_change_cannot_resume_recording(self):
        self.start()
        self.app.capture_pause_intent()
        self.app.on_focus_out(None)
        self.app.toggle_pause()
        self.assertEqual(self.app.rec.status, "paused")
        self.app.capture_pause_intent()
        self.app.toggle_pause()
        self.assertEqual(self.app.rec.status, "running")
        self.app.rec.finish()

    def test_invalid_age_does_not_start(self):
        with patch("textlab.app.messagebox.showerror") as error:
            self.app.start_session()
            error.assert_called_once()
        self.assertIsNone(self.app.rec)

    def test_failed_close_preserves_report_for_manual_export(self):
        self.start()
        self.app.editor.insert("end", "Сохранить вручную")
        with patch("textlab.app.messagebox.askyesno", return_value=True), patch("textlab.app.messagebox.showerror"), patch.object(self.app.store, "flush", side_effect=OSError("disk full")):
            self.app.close()
        self.assertEqual(self.app.view, "report")
        self.assertEqual(self.app.snapshot["texts"], ["Сохранить вручную"])
        self.assertTrue(self.app.winfo_exists())


if __name__ == "__main__":
    unittest.main()
