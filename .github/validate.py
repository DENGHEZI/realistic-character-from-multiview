"""Static checks that need no Blender: skill metadata, asset links, repo hygiene."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
problems = []


def read(path):
    return path.read_text(encoding='utf-8')


def check_skill():
    path = ROOT / 'SKILL.md'
    if not path.exists():
        problems.append('SKILL.md missing')
        return
    text = read(path)
    match = re.match(r'^---\n(.*?)\n---\n', text, re.S)
    if not match:
        problems.append('SKILL.md has no YAML frontmatter')
        return
    fields = {}
    for line in match.group(1).splitlines():
        if ':' in line:
            key, value = line.split(':', 1)
            fields[key.strip()] = value.strip()
    for key in ('name', 'description'):
        if not fields.get(key):
            problems.append(f'SKILL.md frontmatter missing {key}')
    if fields.get('name') != ROOT.name:
        problems.append(f"skill name {fields.get('name')!r} does not match folder {ROOT.name!r}")
    if fields.get('agent_created') != 'true':
        problems.append('SKILL.md frontmatter must set agent_created: true')
    for folder in ('references', 'scripts'):
        if not (ROOT / folder).is_dir():
            problems.append(f'missing folder {folder}/')


def check_assets():
    readme = ROOT / 'README.md'
    if not readme.exists():
        problems.append('README.md missing')
        return
    text = read(readme)
    linked = set(re.findall(r'assets/[\w.\-]+\.(?:png|jpg|jpeg|svg|gif)', text))
    if not linked:
        problems.append('README.md references no images')
    for link in sorted(linked):
        if not (ROOT / link).exists():
            problems.append(f'README image not found: {link}')


def check_sizes():
    for path in ROOT.rglob('*'):
        if '.git' in path.parts or not path.is_file():
            continue
        if path.suffix.lower() == '.png' and path.stat().st_size > 600 * 1024:
            problems.append(f'{path.relative_to(ROOT)} is larger than 600 KiB')


def main():
    check_skill()
    check_assets()
    check_sizes()
    if problems:
        print('FAIL')
        for item in problems:
            print(' -', item)
        sys.exit(1)
    print('PASS: skill metadata, asset links and repository hygiene')


if __name__ == '__main__':
    main()
