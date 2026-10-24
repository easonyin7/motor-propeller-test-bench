"""Offline deployment regressions; do not access any bench hardware."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from distribution.prepare_package import prepare
from px4_mvp.runtime_paths import project_python


class DistributionTests(unittest.TestCase):
    def test_local_runtime_takes_priority_and_venv_remains_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            venv=root/'cv_mvp/.venv/Scripts/python.exe'
            venv.parent.mkdir(parents=True);venv.touch()
            self.assertEqual(project_python(root),str(venv))
            local=root/'runtime/python.exe';local.parent.mkdir();local.touch()
            self.assertEqual(project_python(root),str(local))

    def test_relocation_updates_indexes_and_leaves_telemetry_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);old='C:/Original/Bench'
            (root/'package_info.json').write_text(json.dumps({'original_project_root':old}))
            session=root/'bench_app/runs/one/session.json';session.parent.mkdir(parents=True)
            session.write_text(json.dumps({'run_dirs':[old+'/bench_app/runs/one/stage'],
                                           'specimen':'2006', 'mean':12.34}))
            raw=session.with_name('observed_cv.jsonl');raw.write_bytes(b'original raw evidence')
            self.assertEqual(prepare(root)['changed_files'],1)
            data=json.loads(session.read_text(encoding='utf-8'))
            self.assertEqual(data['run_dirs'],[str(root/'bench_app/runs/one/stage')])
            self.assertEqual(data['mean'],12.34)
            self.assertEqual(raw.read_bytes(),b'original raw evidence')
            self.assertTrue(prepare(root)['already_prepared'])

    def test_relocation_handles_another_move_and_external_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'package_info.json').write_text(json.dumps(dict(
                original_project_root='C:/Original',prepared_root='D:/First copy')))
            config=root/'cv_mvp/instrument_config.json';config.parent.mkdir()
            config.write_text(json.dumps(dict(reference_image='D:/First copy/image.png',
                                              external='E:/unrelated/file.png')))
            prepare(root)
            value=json.loads(config.read_text(encoding='utf-8'))
            self.assertEqual(value['reference_image'],str(root/'image.png'))
            self.assertEqual(value['external'],'E:/unrelated/file.png')

    def test_refuses_original_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            (root/'package_info.json').write_text(json.dumps({'original_project_root':str(root)}))
            with self.assertRaisesRegex(RuntimeError,'original'):
                prepare(root)


if __name__=='__main__':unittest.main()
