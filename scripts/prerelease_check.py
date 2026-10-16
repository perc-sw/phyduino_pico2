#!/usr/bin/env python3
"""Pre-release checks for the Phyduino Pico 2 repository.

Checks that the documentation and generated outputs match the design:

  links      every local href/src in the README and the docs site points to a file
  stale      generated outputs were committed no earlier than the design files they
             come from (needs full git history)
  parts      part numbers named in the README, the datasheet table and the
             datasheet file names are on the board's BOM
  pinout     the GPIO numbers and functions in docs/images/phyduino_pico2_pinout.svg
             match the netlist and the RP2350 function-select table, and the
             J2/J3 power-header labels match their nets
  erc        schematic ERC, error-level violations only
  drc        PCB DRC, error-level violations only
  external   every external http(s) link answers (off by default: some sites
             block scripted requests, so failures are reported as warnings)

Usage:
  python scripts/prerelease_check.py                  # all checks except external
  python scripts/prerelease_check.py --external       # also check external links
  python scripts/prerelease_check.py --skip drc,erc   # skip some checks
  python scripts/prerelease_check.py --regenerate     # rebuild outputs, then check

--regenerate rebuilds the schematic and PCB PDFs, STEP and GLB models, Gerber
and drill files, board renders and iBOMs. Run it locally only: the renders and
iBOMs need the maintainer's KiCad configuration and InteractiveHtmlBom plugin.

kicad-cli is found from $KICAD_CLI, then PATH, then the default Windows install.
Exit code: 0 if no errors, 1 if any check reported an error.
"""
import argparse
import csv
import glob
import html.parser
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCH = 'phyduino_pico2.kicad_sch'
PCB = 'phyduino_pico2.kicad_pcb'
PINOUT = 'docs/images/phyduino_pico2_pinout.svg'
DOC_PAGES = ['README.md', 'docs/index.html'] + sorted(
    os.path.relpath(p, ROOT).replace('\\', '/')
    for p in glob.glob(os.path.join(ROOT, 'docs', 'planning', '*.html')))

# Parts the README names that are deliberately not on the BOM.
PART_ALLOWLIST = {
    'ESP32-C61-MINI-1',   # pin-compatible alternative to the ESP32-C6 module
}

# Generated output -> design files it is built from.
SCH_FILES = sorted(os.path.basename(p) for p in glob.glob(os.path.join(ROOT, '*.kicad_sch')))
OUTPUTS = {
    'docs/pdf/board/phyduino_pico2-schematic.pdf': SCH_FILES,
    'docs/pdf/board/phyduino_pico2_pcb-top.pdf': [PCB],
    'docs/pdf/board/phyduino_pico2_pcb-inner1.pdf': [PCB],
    'docs/pdf/board/phyduino_pico2_pcb-inner2.pdf': [PCB],
    'docs/pdf/board/phyduino_pico2_pcb-bottom.pdf': [PCB],
    'docs/models/phyduino_pico2.step': [PCB],
    'docs/models/phyduino_pico2.glb': [PCB],
    'docs/images/phyduino_pico2_top.png': [PCB],
    'docs/images/phyduino_pico2_render.png': [PCB],
    'docs/images/phyduino_pico2_pinout.svg': [PCB],   # embedded board picture
    'docs/bom/ibom.html': [PCB],
    'docs/bom/ibom_all.html': [PCB],
    'docs/bom/ibom_student.html': [PCB],
    'fabrication/phyduino_pico2-job.gbrjob': [PCB],
}

IN_CI = os.environ.get('GITHUB_ACTIONS') == 'true'


class Report:
    def __init__(self):
        self.errors = 0
        self.warnings = 0

    def section(self, name):
        print(f'\n== {name}')

    def ok(self, msg):
        print(f'  ok    {msg}')

    def warn(self, msg, file=None):
        self.warnings += 1
        print(f'  WARN  {msg}')
        if IN_CI:
            print(f'::warning{" file=" + file if file else ""}::{msg}')

    def error(self, msg, file=None):
        self.errors += 1
        print(f'  ERROR {msg}')
        if IN_CI:
            print(f'::error{" file=" + file if file else ""}::{msg}')


# ---------------------------------------------------------------- helpers

def kicad_cli():
    exe = os.environ.get('KICAD_CLI') or shutil.which('kicad-cli')
    if not exe:
        guess = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'KiCad',
                             '10.0', 'bin', 'kicad-cli.exe')
        exe = guess if os.path.isfile(guess) else None
    return exe


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', **kw)


def git(*args):
    r = run(['git', *args])
    return r.stdout.strip() if r.returncode == 0 else None


def norm(s):
    return re.sub(r'[^A-Z0-9]', '', s.upper())


class LinkParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'link' and dict(attrs).get('rel') in ('preconnect', 'dns-prefetch'):
            return                  # hostnames for the browser, not pages
        for k, v in attrs:
            if k in ('href', 'src') and v is not None:
                self.links.append(v)


def page_links(path):
    text = open(os.path.join(ROOT, path), encoding='utf-8').read()
    p = LinkParser()
    p.feed(text)
    links = list(p.links)
    if path.endswith('.md'):
        links += re.findall(r'\]\(([^)\s]+)\)', text)
    return links


def bom_rows(cli, tmp):
    out = os.path.join(tmp, 'bom.csv')
    r = run([cli, 'sch', 'export', 'bom', '--fields', 'Reference,Value,PN,Variant',
             '--group-by', 'Value,PN', '-o', out, SCH])
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout)
    with open(out, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def netlist(cli, tmp):
    """Return {net name: [(ref, pin, pinfunction)]}."""
    out = os.path.join(tmp, 'net.net')
    r = run([cli, 'sch', 'export', 'netlist', '--format', 'kicadsexpr', '-o', out, SCH])
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout)
    text = open(out, encoding='utf-8').read()
    nets = {}
    for blk in re.split(r'\n\s*\(net\s*\n', text)[1:]:
        name = re.search(r'\(name "([^"]*)"\)', blk).group(1)
        nodes = []
        for nb in blk.split('(node')[1:]:
            ref = re.search(r'\(ref "([^"]+)"\)', nb).group(1)
            pin = re.search(r'\(pin "([^"]+)"\)', nb).group(1)
            fn = re.search(r'\(pinfunction "([^"]*)"\)', nb)
            nodes.append((ref, pin, fn.group(1) if fn else ''))
        nets[name] = nodes
    return nets


def rp2350_functions(n):
    """UART (F2), PWM (F4), SPI (F1) and I2C (F3) for GPIO n, from RP2350
    datasheet Table 3 (General GPIO function select)."""
    if n < 32:
        pwm = (n // 2) % 8
    else:
        pwm = 8 + ((n - 32) // 2) % 4
    return {
        'MICRO': f'GPIO{n}',
        'UART': ['TX', 'RX', 'CTS', 'RTS'][n % 4] + str([0, 1, 1, 0][(n // 4) % 4]),
        'PWM': f'PWM{pwm}' + 'AB'[n % 2],
        'SPI': ['RX', 'CSn', 'SCK', 'TX'][n % 4] + str((n // 8) % 2),
        'I2C': ['SDA', 'SCL'][n % 2] + str((n // 2) % 2),
    }


# ---------------------------------------------------------------- checks

def check_links(rep):
    rep.section('Local links')
    bad = 0
    for page in DOC_PAGES:
        base = os.path.dirname(os.path.join(ROOT, page))
        for link in page_links(page):
            target = link.split('#')[0].split('?')[0]
            if not target or re.match(r'^[a-z]+:', target):
                if link == '' or link == '#':
                    rep.error(f'{page}: empty link', page)
                    bad += 1
                continue
            path = os.path.normpath(os.path.join(base, urllib.parse.unquote(target)))
            if not os.path.exists(path):
                rep.error(f'{page}: missing file {link}', page)
                bad += 1
    if not bad:
        rep.ok(f'all local links in {len(DOC_PAGES)} pages resolve')


def check_stale(rep):
    rep.section('Generated outputs vs design files')
    if git('rev-parse', '--is-shallow-repository') == 'true':
        rep.warn('shallow clone: fetch full history (fetch-depth: 0) to check outputs')
        return
    dirty = git('status', '--porcelain', '--', *SCH_FILES, PCB) or ''
    if dirty:
        rep.warn('design files have uncommitted changes; outputs are checked '
                 'against the last commit only')
    when = {}

    def last(path):
        if path not in when:
            t = git('log', '-1', '--format=%ct', '--', path)
            when[path] = int(t) if t else None
        return when[path]

    stale = 0
    for out, sources in OUTPUTS.items():
        t_out = last(out)
        if t_out is None:
            rep.error(f'{out} is not committed', out)
            stale += 1
            continue
        newest = max((last(s) or 0, s) for s in sources)
        if newest[0] > t_out:
            days = (newest[0] - t_out) / 86400
            if git('status', '--porcelain', '--', out):
                rep.error(f'{out} has been regenerated but not committed', out)
            else:
                rep.error(f'{out} is older than {newest[1]} ({days:.1f} days); '
                          'regenerate it', out)
            stale += 1
    if not stale:
        rep.ok(f'{len(OUTPUTS)} outputs are up to date')


def check_parts(rep, rows):
    rep.section('Part numbers in the documentation')
    bom = [norm(r.get('PN', '') + ' ' + r.get('Value', '')) for r in rows]

    def on_bom(name):
        n = norm(name)
        return any(n in b for b in bom)

    names = {}
    # README Features section: part-number-like tokens (letters then a digit, at
    # least 6 characters). Other sections name parts of related boards.
    readme = open(os.path.join(ROOT, 'README.md'), encoding='utf-8').read()
    feat = re.search(r'^## Features\s*$(.*?)(?=^## )', readme, re.M | re.S)
    if not feat:
        rep.warn('README.md has no "## Features" section to check')
    readme = feat.group(1) if feat else ''
    for tok in re.findall(r'\b[A-Z]{2,}[0-9][A-Z0-9]*(?:-[A-Z0-9]+)*\b', readme):
        if len(tok) >= 6:
            names.setdefault(tok, 'README.md')
    # Datasheet table in index.html: first cell of each row.
    index = open(os.path.join(ROOT, 'docs/index.html'), encoding='utf-8').read()
    table = re.search(r'<table class="ds-table">(.*?)</table>', index, re.S)
    for row in re.findall(r'<tr>\s*<td>([^<]+)</td>', table.group(1) if table else ''):
        names.setdefault(re.sub(r'\s+TRM$', '', row.strip()), 'docs/index.html')
    # Datasheet files: "<category> - <part>[ - <title>].pdf"; the category may
    # itself contain a hyphen ("IO-Expander"), so split at a dash followed by a space.
    ds_dir = os.path.join(ROOT, 'docs', 'pdf', 'datasheets')
    linked = set(urllib.parse.unquote(l) for l in page_links('docs/index.html'))
    for f in sorted(os.listdir(ds_dir)):
        m = re.match(r'^(.+?)\s*-\s+(.+?)(?:\s+-\s+.*)?\.pdf$', f)
        if m:
            names.setdefault(m.group(2), 'docs/pdf/datasheets/' + f)
        if './pdf/datasheets/' + f not in linked:
            rep.warn(f'datasheet {f} is not linked from docs/index.html')
    missing = 0
    for name, where in sorted(names.items()):
        if name in PART_ALLOWLIST:
            continue
        if not on_bom(name):
            rep.error(f'{where}: "{name}" is not on the BOM', where)
            missing += 1
    if not missing:
        rep.ok(f'{len(names)} part names match the BOM')


def check_pinout(rep, nets):
    rep.section('Pinout diagram vs netlist')
    SVG = '{http://www.w3.org/2000/svg}'
    LABEL = '{http://www.inkscape.org/namespaces/inkscape}label'
    root = ET.parse(os.path.join(ROOT, PINOUT)).getroot()

    # Header pin -> net, and digital net -> GPIO.
    pin_net, d_gpio = {}, {}
    for name, nodes in nets.items():
        gpio = [int(m.group(1)) for r, _, f in nodes if r == 'U1'
                for m in [re.match(r'GPIO(\d+)', f)] if m]
        for ref, pin, _ in nodes:
            if ref in ('J2', 'J3', 'J4', 'J5'):
                pin_net[(ref, int(pin))] = name
        m = re.search(r'/D(\d+)$', name)
        if m and gpio:
            d_gpio[int(m.group(1))] = gpio[0]

    bad = 0
    layer = next(g for g in root.iter(SVG + 'g') if g.get(LABEL) == 'PINOUT_DETAILS')
    seen = set()
    for g in layer.iter(SVG + 'g'):
        lab = g.get(LABEL, '')
        if not re.fullmatch(r'D\d+', lab):
            continue
        alt = next((c for c in g if c.get(LABEL) == 'ALT FEATURES'), None)
        if alt is None:
            continue
        d = int(lab[1:])
        seen.add(d)
        if d not in d_gpio:
            rep.error(f'pinout shows {lab}, but no D{d} net reaches the RP2350', PINOUT)
            bad += 1
            continue
        want = rp2350_functions(d_gpio[d])
        for sub in alt:
            key = sub.get(LABEL)
            if key in want:
                got = ''.join(sub.itertext()).strip()
                if got != want[key]:
                    rep.error(f'pinout {lab} {key}: shows {got}, should be {want[key]}', PINOUT)
                    bad += 1
    for d in sorted(set(d_gpio) - seen):
        rep.error(f'D{d} (GPIO{d_gpio[d]}) is missing from the pinout', PINOUT)
        bad += 1

    # Power header: Arduino Uno R3 roles for J2, and AREF on J3 pin 8.
    roles = {('J2', 1): ('NC', lambda n: n.startswith('unconnected')),
             ('J2', 2): ('IOREF', lambda n: n == '+3.3V'),
             ('J2', 3): ('RESET', lambda n: 'RESET' in n.upper()),
             ('J2', 4): ('+3.3V', lambda n: n == '+3.3V'),
             ('J2', 5): ('+5.0V', lambda n: n == '+5.0V'),
             ('J2', 6): ('GND', lambda n: n == 'GND'),
             ('J2', 7): ('GND', lambda n: n == 'GND'),
             ('J2', 8): ('+VIN', lambda n: n not in ('GND', '+3.3V', '+5.0V')
                         and not n.startswith('unconnected')),
             ('J3', 7): ('GND', lambda n: n == 'GND'),
             ('J3', 8): ('AREF', lambda n: n.upper().endswith('AREF'))}
    labels = {re.sub(r'-\d+$', '', g.get(LABEL, '')) for g in layer.iter(SVG + 'g')}
    for (ref, pin), (role, test) in roles.items():
        net = pin_net.get((ref, pin), '(none)')
        if not test(net):
            rep.error(f'{ref} pin {pin} should be {role}, but its net is {net}', SCH)
            bad += 1
        if role not in labels:
            rep.error(f'pinout has no label for {role} ({ref} pin {pin})', PINOUT)
            bad += 1
    if not bad:
        rep.ok(f'{len(seen)} digital pins and the power-header labels match the netlist')


def check_kicad(rep, cli, tmp, kind):
    rep.section('ERC (errors only)' if kind == 'erc' else 'DRC (errors only)')
    out = os.path.join(tmp, f'{kind}.txt')
    if kind == 'erc':
        cmd = [cli, 'sch', 'erc', '--severity-error', '--exit-code-violations',
               '--format', 'report', '-o', out, SCH]
    else:
        cmd = [cli, 'pcb', 'drc', '--severity-error', '--exit-code-violations',
               '--format', 'report', '-o', out, PCB]
    r = run(cmd)
    report = open(out, encoding='utf-8', errors='replace').read() if os.path.exists(out) else ''
    if r.returncode == 0:
        rep.ok('no error-level violations')
    elif r.returncode == 5:
        kinds = re.findall(r'^\[([a-z_]+)\]', report, re.M)
        summary = ', '.join(f'{k} ×{kinds.count(k)}' for k in sorted(set(kinds)))
        rep.error(f'{kind.upper()} found {len(kinds)} error-level violations: {summary}',
                  SCH if kind == 'erc' else PCB)
        dest = os.path.join(ROOT, f'{kind}-report.txt') if IN_CI else None
        if dest:
            shutil.copy(out, dest)
    else:
        rep.error(f'kicad-cli failed to run {kind.upper()} (exit {r.returncode}): '
                  f'{(r.stderr or r.stdout).strip()[:200]}')


def check_external(rep):
    rep.section('External links (warnings only)')
    urls = set()
    for page in DOC_PAGES:
        for link in page_links(page):
            if link.startswith(('http://', 'https://')):
                urls.add(link)
    bad = 0
    for url in sorted(urls):
        req = urllib.request.Request(url, method='GET', headers={
            'User-Agent': 'Mozilla/5.0 (phyduino_pico2 link check)'})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:      # DNS, timeout, TLS
            code = type(e).__name__
        if code != 200:
            rep.warn(f'{url} -> {code}')
            bad += 1
    if not bad:
        rep.ok(f'{len(urls)} external links answer')


# ---------------------------------------------------------------- regenerate

def kicad_config_dir():
    if sys.platform == 'win32':
        return os.path.join(os.environ['APPDATA'], 'kicad', '10.0')
    return os.path.expanduser('~/.config/kicad/10.0')


def ibom_plugin():
    if os.environ.get('IBOM_PLUGIN'):
        return os.environ['IBOM_PLUGIN']
    home = os.path.expanduser('~')
    rel = os.path.join('KiCad', '10.0', '3rdparty', 'plugins',
                       'org_openscopeproject_InteractiveHtmlBom', 'generate_interactive_bom.py')
    for docs in ('Documents', os.path.join('OneDrive', 'Documents')):
        p = os.path.join(home, docs, rel)
        if os.path.isfile(p):
            return p
    return None


def regenerate(cli, tmp):
    print('\n== Regenerating outputs from the files saved on disk')
    if glob.glob(os.path.join(ROOT, '~*.lck')):
        print('  note  KiCad has the project open: unsaved changes are not included')

    def step(desc, cmd, env=None):
        r = run(cmd, env=env)
        status = 'ok   ' if r.returncode == 0 else 'FAIL '
        print(f'  {status} {desc}')
        if r.returncode != 0:
            print('        ' + (r.stderr or r.stdout).strip().replace('\n', '\n        ')[:800])
        return r.returncode == 0

    good = True
    good &= step('schematic PDF', [cli, 'sch', 'export', 'pdf', '-o',
                                   'docs/pdf/board/phyduino_pico2-schematic.pdf', SCH])
    # The bottom layer is mirrored so it shows the board as seen from below.
    for name, layers, extra in (('top', 'F.Cu,F.Silkscreen', []), ('inner1', 'In1.Cu', []),
                                ('inner2', 'In2.Cu', []),
                                ('bottom', 'B.Cu,B.Silkscreen', ['--mirror'])):
        good &= step(f'PCB PDF ({name})', [
            cli, 'pcb', 'export', 'pdf', '--mode-single', '--include-border-title',
            '--bg-color', '#001023', *extra, '--layers', layers + ',Edge.Cuts,User.Drawings',
            '-o', f'docs/pdf/board/phyduino_pico2_pcb-{name}.pdf', PCB])
    good &= step('STEP model', [cli, 'pcb', 'export', 'step', '--subst-models', '-f',
                                '-o', 'docs/models/phyduino_pico2.step', PCB])
    good &= step('GLB model', [cli, 'pcb', 'export', 'glb', '--subst-models', '-f',
                               '--include-pads', '--include-silkscreen', '--include-soldermask',
                               '-o', 'docs/models/phyduino_pico2.glb', PCB])
    good &= step('Gerber files', [cli, 'pcb', 'export', 'gerbers', '--board-plot-params',
                                  '-o', 'fabrication/', PCB])
    good &= step('drill files', [cli, 'pcb', 'export', 'drill', '--format', 'excellon',
                                 '--drill-origin', 'absolute', '--excellon-units', 'mm',
                                 '--excellon-zeros-format', 'decimal',
                                 '--excellon-oval-format', 'route', '--excellon-separate-th',
                                 '-o', 'fabrication/', PCB])

    # Renders: use a copy of the KiCad config with through-hole models shown.
    cfg = os.path.join(tmp, 'kicad-config')
    src = kicad_config_dir()
    os.makedirs(os.path.join(cfg, '10.0'))
    for f in glob.glob(os.path.join(src, '*.json')) + glob.glob(os.path.join(src, '*lib-table')):
        shutil.copy(f, os.path.join(cfg, '10.0'))
    viewer = os.path.join(cfg, '10.0', '3d_viewer.json')
    if os.path.exists(viewer):
        text = open(viewer, encoding='utf-8').read()
        text = re.sub(r'"show_footprints_normal":\s*false', '"show_footprints_normal": true', text)
        open(viewer, 'w', encoding='utf-8').write(text)
    env = dict(os.environ, KICAD_CONFIG_HOME=cfg)
    good &= step('top render', [cli, 'pcb', 'render', '--side', 'top', '--quality', 'high',
                                '-w', '1592', '-h', '1256', '--zoom', '0.96',
                                '-o', 'docs/images/phyduino_pico2_top.png', PCB], env)
    good &= step('angled render', [cli, 'pcb', 'render', '--side', 'top', '--quality', 'high',
                                   '--perspective', '-w', '1300', '-h', '936',
                                   '--rotate', '-42,0,18', '--zoom', '1.048',
                                   '--pan', '-0.59,0.99,0',
                                   '-o', 'docs/images/phyduino_pico2_render.png', PCB], env)

    # iBOMs, with KiCad's own Python (it provides the pcbnew module).
    plugin = ibom_plugin()
    py = os.path.join(os.path.dirname(cli), 'python.exe' if sys.platform == 'win32' else 'python3')
    if not plugin or not os.path.isfile(py):
        print('  SKIP  iBOMs: InteractiveHtmlBom plugin or KiCad Python not found '
              '(set IBOM_PLUGIN)')
        good = False
    else:
        common = [py, plugin, '--no-browser', '--no-dark-mode', '--show-pads',
                  '--hide-fabrication', '--show-silkscreen', '--redraw-on-drag',
                  '--highlight-pin1', 'none', '--board-rotation', '0',
                  '--no-offset-back-rotation', '--checkboxes', 'Sourced,Placed',
                  '--bom-view', 'left-right', '--layer-view', 'FB', '--compression',
                  '--dest-dir', os.path.join(ROOT, 'docs', 'bom'),
                  '--sort-order', 'C,R,L,D,U,Y,X,F,SW,A,~,HS,CNN,J,P,NT',
                  '--blacklist', 'H*,MT*', '--blacklist-virtual', '--exclude-tracks',
                  '--exclude-nets', '--show-fields', 'Value,Footprint',
                  '--group-fields', 'Value,Footprint', '--no-normalize-field-case',
                  '--variant-field', 'Variant']
        good &= step('iBOM (student)', common + ['--name-format', 'ibom_student',
                                                 '--variants-whitelist', 'STUDENT',
                                                 '--variants-blacklist', 'IGNORE,ADV_SMT', PCB])
        good &= step('iBOM (all)', common + ['--name-format', 'ibom_all',
                                             '--variants-blacklist', 'IGNORE', PCB])
        if os.path.exists(os.path.join(ROOT, 'docs/bom/ibom_all.html')):
            shutil.copy(os.path.join(ROOT, 'docs/bom/ibom_all.html'),
                        os.path.join(ROOT, 'docs/bom/ibom.html'))
    print('  note  the pinout diagram is drawn by hand: update its board picture and '
          'labels in Inkscape if the board changed')
    print('  note  commit the regenerated files before the stale-output check can pass')
    return good


# ---------------------------------------------------------------- main

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--skip', default='',
                    help='comma-separated checks to skip: links,stale,parts,pinout,erc,drc')
    ap.add_argument('--external', action='store_true', help='also check external links')
    ap.add_argument('--regenerate', action='store_true',
                    help='rebuild generated outputs before checking (local only)')
    args = ap.parse_args()
    skip = {s.strip() for s in args.skip.split(',') if s.strip()}

    rep = Report()
    cli = kicad_cli()
    needs_cli = {'parts', 'pinout', 'erc', 'drc'} - skip or args.regenerate
    if needs_cli and not cli:
        print('kicad-cli not found: set KICAD_CLI or put it on PATH', file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        if args.regenerate and not regenerate(cli, tmp):
            rep.error('some outputs could not be regenerated (see above)')
        if 'links' not in skip:
            check_links(rep)
        if 'stale' not in skip:
            check_stale(rep)
        if 'parts' not in skip:
            check_parts(rep, bom_rows(cli, tmp))
        if 'pinout' not in skip:
            check_pinout(rep, netlist(cli, tmp))
        for kind in ('erc', 'drc'):
            if kind not in skip:
                check_kicad(rep, cli, tmp, kind)
        if args.external:
            check_external(rep)

    print(f'\n{rep.errors} error(s), {rep.warnings} warning(s)')
    return 1 if rep.errors else 0


if __name__ == '__main__':
    sys.exit(main())
