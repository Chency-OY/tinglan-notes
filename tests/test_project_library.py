import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from interpreter import HistoryStore


class LibraryTests(unittest.TestCase):
    def test_upgrade_and_export_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'history.sqlite3'
            db = sqlite3.connect(path)
            db.executescript("CREATE TABLE sessions(id INTEGER PRIMARY KEY, started REAL NOT NULL, ended REAL, provider TEXT NOT NULL); INSERT INTO sessions VALUES(12,100,105,'test');")
            db.close()
            store = HistoryStore(path)
            a = store.create_project('大学英语')
            b = store.create_project('其他项目')
            with self.assertRaises(ValueError):
                store.create_project(' 大学英语 ')
            self.assertEqual(store.session_previews(), [(12,100,None)])
            store.rename(12, '../演讲:讨论')
            store.move_session(12,a)
            store.append(12,100,102,'原句 A','译句 B')
            store.append(12,102,105,'原句 C','译句 D')
            store.archive_session(12)
            sid = store.begin('test','../演讲:讨论',a)
            outsider = store.begin('test',project_id=b)
            store.append(outsider,1,2,'其他','PRIVATE')
            store = HistoryStore(path)
            self.assertEqual([r[0] for r in store.session_previews(a)], [sid])
            self.assertEqual([r[0] for r in store.session_previews(None,True)], [12])
            out = Path(tmp)/'导出.zip'
            self.assertEqual(store.project_zip(a,out), 2)
            with zipfile.ZipFile(out) as z:
                self.assertIsNone(z.testzip()); self.assertEqual(len(z.namelist()),4)
                texts = {n:z.read(n).decode('utf-8-sig') for n in z.namelist()}
                self.assertTrue(all(n.count('/') == 1 and ':' not in n for n in texts))
                self.assertTrue(all('PRIVATE' not in text for text in texts.values()))
                orig = next(t for n,t in texts.items() if '_12_' in n and n.endswith('原文.txt'))
                trans = next(t for n,t in texts.items() if '_12_' in n and n.endswith('译文.txt'))
                self.assertLess(orig.index('原句 A'),orig.index('原句 C')); self.assertNotIn('译句',orig)
                self.assertIn('译句 D',trans); self.assertNotIn('原句',trans); self.assertIn('结束：',trans)
            old = out.read_bytes()
            with patch('interpreter.zipfile.ZipFile.writestr',side_effect=OSError), self.assertRaises(OSError):
                store.project_zip(a,out)
            self.assertEqual(old,out.read_bytes()); self.assertFalse(list(Path(tmp).glob('*.tmp')))
            store.archive_session(12,False); self.assertIn(12,[r[0] for r in store.session_previews(a)])
            store.move_session(12,None); self.assertNotIn(12,[r[0] for r in store.session_previews(a)])
