from pathlib import Path
import subprocess
import tempfile
import yaml


def quality_scripts():
    data = yaml.safe_load((Path(__file__).parents[1] / '.cnb.yml').read_text(encoding='utf-8'))
    for jobs in data['main'].values():
        if isinstance(jobs, list):
            for job in jobs:
                if isinstance(job, dict):
                    for stage in job.get('stages', []):
                        if stage.get('name') == '商品质量门禁':
                            yield stage['script']


def test_quality_rejection_survives_tee():
    scripts = list(quality_scripts())
    assert len(scripts) == 2
    for script in scripts:
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'state').mkdir()
            result = subprocess.run(['sh', '-c', 'python() { echo rejected; return 7; };\n' + script],
                                    cwd=temp, capture_output=True, text=True)
            assert result.returncode == 7, result.stderr


def test_quality_acceptance_survives_tee():
    for script in quality_scripts():
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'state').mkdir()
            result = subprocess.run(['sh', '-c', 'python() { echo accepted; return 0; };\n' + script],
                                    cwd=temp, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr


def test_quality_precedes_every_publication_leg():
    data = yaml.safe_load((Path(__file__).parents[1] / '.cnb.yml').read_text(encoding='utf-8'))
    checked = 0
    for jobs in data['main'].values():
        if not isinstance(jobs, list):
            continue
        for job in jobs:
            names = [s.get('name') for s in job.get('stages', [])]
            if '商品质量门禁' not in names:
                continue
            for publish in ['推送数据到GitHub Release','发布到Cloudflare Pages','发布到Codeberg Pages']:
                assert names.index('商品质量门禁') < names.index(publish)
            checked += 1
    assert checked == 2
