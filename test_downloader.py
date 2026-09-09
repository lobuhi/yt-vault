import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import downloader


class DownloaderQueueTests(unittest.TestCase):
    def test_pending_queue_only_contains_explicitly_queued_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'queue.sqlite3'
            with closing(sqlite3.connect(db)) as c:
                with c:
                    c.execute('''CREATE TABLE videos(
                        video_id TEXT PRIMARY KEY, status TEXT, priority INTEGER,
                        category TEXT, attempts INTEGER, title TEXT
                    )''')
                    c.executemany(
                        'INSERT INTO videos VALUES(?,?,?,?,?,?)',
                        [
                            ('queued', 'pending', 0, 'Curso', 0, 'En cola'),
                            ('failed', 'error', 0, 'Curso', 1, 'Fallido'),
                            ('remote', 'remote', 0, 'Curso', 0, 'Remoto'),
                        ],
                    )
            with patch.object(downloader, 'DB', db):
                result = downloader.pending()
            self.assertEqual([row['video_id'] for row in result], ['queued'])


if __name__ == '__main__':
    unittest.main()
