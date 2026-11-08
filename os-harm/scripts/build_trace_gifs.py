"""Build a GIF per screenshot trace and an offline, searchable gallery."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import re
import zipfile

from PIL import Image, ImageDraw, ImageFont


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def build_trace(job):
    directory, paths, root, output, delay = job
    relative = directory.relative_to(root)
    destination = output / relative / 'trace.gif'
    destination.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    font = ImageFont.load_default(size=18)
    for index, path in enumerate(paths):
        with Image.open(path) as image:
            source = image.convert('RGB')
        frame = Image.new('RGB', (source.width, source.height + 34), '#18202b')
        frame.paste(source, (0, 0))
        ImageDraw.Draw(frame).text(
            (12, source.height + 7),
            f'{path.name}  |  {index + 1}/{len(paths)}  |  {delay / 1000:g}s per screenshot',
            fill='white', font=font,
        )
        frames.append(frame.quantize(colors=256, method=Image.Quantize.FASTOCTREE,
                                     dither=Image.Dither.NONE))
    durations = [delay] * len(frames)
    durations[-1] = max(2000, delay)
    temporary = destination.with_suffix('.tmp')
    frames[0].save(temporary, format='GIF', save_all=True, append_images=frames[1:],
                   loop=0, duration=durations, optimize=False, disposal=1)
    with Image.open(temporary) as gif:
        assert gif.n_frames == len(paths), (relative, gif.n_frames, len(paths))
        assert gif.info.get('loop') == 0
        actual_duration = 0
        for index in range(gif.n_frames):
            gif.seek(index)
            gif.load()
            actual_duration += gif.info['duration']
        assert actual_duration == sum(durations)
    temporary.replace(destination)
    summary = read_json(directory / 'summary.json')
    parts = relative.parts
    retry = parts[0] == 'attempts'
    offset = 1 if retry else 0
    return dict(path=relative.as_posix(), gif=destination.relative_to(output).as_posix(),
                suite=parts[offset], domain=parts[offset + 1], task=parts[offset + 2],
                attempt=parts[-1] if retry else 'current', frames=len(paths),
                duration_seconds=actual_duration / 1000, bytes=destination.stat().st_size,
                status=summary.get('status', 'partial trace'),
                screenshots=[p.name for p in paths])


def gallery(entries, skipped, output):
    cards = []
    for entry in entries:
        title = entry['task'].lstrip('_').replace('__inject__', ' / ').replace('_', ' ')
        path = html.escape(entry['gif'], quote=True)
        search = html.escape(' '.join(str(v) for v in entry.values()).lower(), quote=True)
        cards.append(f'''<article data-search="{search}" data-suite="{entry['suite']}"
          data-retry="{str(entry['attempt'] != 'current').lower()}">
          <a href="{path}" target="_blank"><img loading="lazy" src="{path}" alt="{html.escape(title, quote=True)}"></a>
          <div class="info"><small>{entry['suite']} · {entry['domain']} · {entry['attempt']}</small>
          <h2>{html.escape(title)}</h2><p>{entry['frames']} screenshots · {entry['duration_seconds']:g}s loop · {html.escape(str(entry['status']))}</p>
          <a href="{path}" target="_blank">Open full size ↗</a> · <a href="{path}" download>Download GIF</a></div></article>''')
    skipped_items = ''.join(f'<li>{html.escape(p)}</li>' for p in skipped)
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OS-Harm run traces</title><style>
*{box-sizing:border-box}body{margin:0;background:#10151d;color:#edf2fa;font:15px system-ui,sans-serif}
header{padding:30px 4vw 22px;border-bottom:1px solid #344052}h1{font-size:30px;margin:0 0 8px}
p{color:#b4c1d5}nav{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin-top:22px}
input[type=search],select{background:#1d2735;color:white;border:1px solid #46556b;border-radius:7px;padding:10px}
input[type=search]{width:min(430px,90vw)}main{padding:24px 4vw;display:grid;grid-template-columns:repeat(auto-fit,minmax(min(460px,100%),1fr));gap:24px}
article{background:#1b2431;border:1px solid #344052;border-radius:10px;overflow:hidden}article[hidden]{display:none}
img{display:block;width:100%;height:auto;background:#080c12}small{color:#94baff}h2{font-size:17px;line-height:1.5;overflow-wrap:anywhere;margin:8px 0}
.info{padding:18px}a{color:#9bc8ff}details{margin:20px 4vw 40px}li{overflow-wrap:anywhere}
</style><header><h1>OS-Harm run traces</h1>
<p>COUNT GIFs · original screenshot resolution · 1 second per step, 2 seconds on the final step · continuous loops.</p>
<p>Open a GIF at full size to read the screen. Single-screenshot traces show the only available frame.</p>
<nav><input id="search" type="search" placeholder="Search task, application, or status" aria-label="Search traces">
<select id="suite" aria-label="Suite"><option value="">All suites</option><option>misbehavior</option><option>injection</option><option>misuse</option></select>
<label><input id="retries" type="checkbox" checked> Include retry attempts</label><span id="count" aria-live="polite"></span></nav></header>
<main>CARDS</main><details><summary>SKIPPED runs without screenshots</summary><ul>SKIPLIST</ul></details>
<script>
const cards=[...document.querySelectorAll('article')];const q=document.querySelector('#search'),suite=document.querySelector('#suite'),retries=document.querySelector('#retries');
function filter(){let n=0;for(const card of cards){card.hidden=!(card.dataset.search.includes(q.value.toLowerCase())&&(!suite.value||card.dataset.suite===suite.value)&&(retries.checked||card.dataset.retry==='false'));if(!card.hidden)n++;}document.querySelector('#count').textContent=n+' traces shown';}
q.addEventListener('input',filter);suite.addEventListener('change',filter);retries.addEventListener('change',filter);filter();
</script></html>'''
    page = page.replace('COUNT', str(len(entries))).replace('CARDS', '\n'.join(cards)).replace('SKIPPED', str(len(skipped))).replace('SKIPLIST', skipped_items)
    (output / 'index.html').write_text(page)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    grouped = {}
    for path in root.rglob('step_*.png'):
        if re.fullmatch(r'step_\d+(?:_.*)?\.png', path.name):
            grouped.setdefault(path.parent, []).append(path)
    jobs = [(directory, sorted(paths, key=lambda p: (int(re.match(r'step_(\d+)', p.name)[1]), p.name)),
             root, output, 1000) for directory, paths in sorted(grouped.items(),
             key=lambda pair: ('attempts' in pair[0].relative_to(root).parts, str(pair[0])))]
    skipped = sorted(str(p.parent.relative_to(root)) for p in root.rglob('summary.json') if p.parent not in grouped)
    entries = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for entry in pool.map(build_trace, jobs):
            entries.append(entry)
            if len(entries) % 20 == 0:
                print(f'Built and verified {len(entries)}/{len(jobs)} GIFs', flush=True)
    gallery(entries, skipped, output)
    report = dict(generated_at=datetime.now(timezone.utc).isoformat(), source=str(root),
                  traces=len(entries), frames=sum(e['frames'] for e in entries),
                  skipped_without_screenshots=skipped, entries=entries)
    (output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    archive = output.with_suffix('.zip')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for path in sorted(output.rglob('*')):
            if path.is_file():
                z.write(path, path.relative_to(output.parent))
    print(json.dumps(dict(traces=len(entries), frames=report['frames'], skipped=len(skipped),
                          gallery=str(output / 'index.html'), archive=str(archive),
                          archive_bytes=archive.stat().st_size)), flush=True)


if __name__ == '__main__':
    main()
