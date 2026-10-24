"""Relocate path references in an extracted handover package; never touch hardware."""
import argparse
import json
from pathlib import Path


def prepare(root):
    root = Path(root).resolve()
    manifest = root/'package_info.json'
    info = json.loads(manifest.read_text(encoding='utf-8'))
    old_root = str(info['original_project_root']).replace('\\', '/')
    if str(root).replace('\\', '/').casefold() == old_root.casefold():
        raise RuntimeError('Refusing to relocate the original development workspace.')
    prior = info.get('prepared_root')
    if prior and str(root).casefold() == str(prior).casefold():
        return {'changed_files': 0, 'already_prepared': True}
    origins = [old_root]
    if prior:
        origins.append(str(prior).replace('\\', '/'))

    def transform(value):
        if isinstance(value, str):
            normalized = value.replace('\\', '/')
            for origin in origins:
                if normalized.casefold() == origin.casefold():
                    return str(root)
                if normalized.casefold().startswith(origin.casefold()+'/'):
                    return str(root/normalized[len(origin)+1:])
            return value
        if isinstance(value, list):
            return [transform(item) for item in value]
        if isinstance(value, dict):
            return {key: transform(item) for key,item in value.items()}
        return value

    changed = 0
    # Indexes/configuration only. Raw telemetry, CSV/JSONL, images and sources
    # stay byte-for-byte as delivered; migration does not alter measurements.
    paths = [root/'cv_mvp/instrument_config.json']
    paths += list((root/'bench_app/runs').glob('*/session.json'))
    paths += list((root/'bench_app/runs').glob('*/camera_config.json'))
    for path in paths:
        if not path.is_file():
            continue
        value = json.loads(path.read_text(encoding='utf-8'))
        result = transform(value)
        if result != value:
            temp = path.with_suffix('.relocate.tmp')
            temp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            temp.replace(path)
            changed += 1
    info['prepared_root'] = str(root)
    info['last_path_migration'] = {'changed_files':changed}
    temp = manifest.with_suffix('.relocate.tmp')
    temp.write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(manifest)
    return {'changed_files':changed,'already_prepared':False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    print(json.dumps(prepare(args.root),ensure_ascii=False))
