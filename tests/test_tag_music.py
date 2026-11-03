import sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tag_music
import main as pipeline


class TagMusicCLI(unittest.TestCase):
    def run_cli(self, argv):
        with mock.patch.object(pipeline, "process_folder") as pf, mock.patch.object(pipeline, "init_db"):
            rc = tag_music.main(argv)
        return rc, pf

    def test_defaults(self):
        d = tempfile.mkdtemp()
        rc, pf = self.run_cli([d])
        self.assertEqual(rc, 0)
        pf.assert_called_once_with(Path(d), fix_art=True, organize=True, delete=None)

    def test_flags(self):
        d = tempfile.mkdtemp()
        rc, pf = self.run_cli([d, "--no-fix-art", "--no-organize", "--no-delete"])
        pf.assert_called_once_with(Path(d), fix_art=False, organize=False, delete=False)

    def test_default_folder_is_music_dir(self):
        d = tempfile.mkdtemp()
        with mock.patch.object(pipeline, "MUSIC_DIR", Path(d)):
            rc, pf = self.run_cli([])
        self.assertEqual(pf.call_args.args[0], Path(d))

    def test_missing_folder(self):
        rc, pf = self.run_cli([str(Path(tempfile.mkdtemp()) / "nope")])
        self.assertEqual(rc, 2); pf.assert_not_called()

    def test_help(self):
        with self.assertRaises(SystemExit) as cm, mock.patch("sys.stdout"):
            tag_music.build_parser().parse_args(["--help"])
        self.assertEqual(cm.exception.code, 0)
