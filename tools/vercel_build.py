"""Publish the existing static directory at its current /static URL paths."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]

if __name__ == '__main__':
    target = ROOT / 'public' / 'static'
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / 'static', target, dirs_exist_ok=True)
    print('Prepared static assets for Vercel.')
