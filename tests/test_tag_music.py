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
        pf.assert_called_once_with(Path(d), fix_art=True, organize=True, delete=None, lyrics=True, fill_only=None, reprocess=False)

    def test_flags(self):
        d = tempfile.mkdtemp()
        rc, pf = self.run_cli([d, "--no-fix-art", "--no-organize", "--no-delete", "--no-lyrics", "--fill-only"])
        pf.assert_called_once_with(Path(d), fix_art=False, organize=False, delete=False, lyrics=False, fill_only=True, reprocess=False)

    def test_default_folder_is_music_dir(self):
        d = tempfile.mkdtemp()
        with mock.patch.object(pipeline, "DEFAULT_FOLDER", Path(d)):
            rc, pf = self.run_cli([])
        self.assertEqual(pf.call_args.args[0], Path(d))

    def test_missing_folder(self):
        rc, pf = self.run_cli([str(Path(tempfile.mkdtemp()) / "nope")])
        self.assertEqual(rc, 2); pf.assert_not_called()

    def test_help(self):
        with self.assertRaises(SystemExit) as cm, mock.patch("sys.stdout"):
            tag_music.build_parser().parse_args(["--help"])
        self.assertEqual(cm.exception.code, 0)


class ReprocessAndDefault(unittest.TestCase):
    def test_reprocess_flag(self):
        d = tempfile.mkdtemp()
        with mock.patch.object(pipeline, "process_folder") as pf, mock.patch.object(pipeline, "init_db"):
            tag_music.main([d, "--reprocess"])
        self.assertTrue(pf.call_args.kwargs["reprocess"])

    def test_default_is_home_music(self):
        self.assertEqual(pipeline.DEFAULT_FOLDER, Path.home() / "Music")

    def test_main_py_cli(self):
        d = tempfile.mkdtemp()
        with mock.patch.object(pipeline, "process_folder") as pf, mock.patch.object(pipeline, "init_db"), \
             mock.patch("sys.argv", ["main.py", d, "--reprocess", "--no-lyrics"]):
            pipeline.main()
        pf.assert_called_once()
        self.assertEqual(pf.call_args.args[0], Path(d))
        self.assertTrue(pf.call_args.kwargs["reprocess"]); self.assertFalse(pf.call_args.kwargs["lyrics"])
        with mock.patch.object(pipeline, "process_folder") as pf, mock.patch.object(pipeline, "init_db"), \
             mock.patch("sys.argv", ["main.py"]):
            pipeline.main()
        self.assertEqual(pf.call_args.args[0], Path.home() / "Music")
        self.assertFalse(pf.call_args.kwargs["reprocess"])
