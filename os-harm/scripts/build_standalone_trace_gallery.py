"""Create an offline HTML viewer with embedded, compact trace GIFs."""
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2] / 'outputs/osharm-follow-up'
GALLERY = ROOT / 'run-trace-gifs'
OUTPUT = ROOT / 'run-trace-viewer.html'


def compact(entry):
    frames, durations = [], []
    with Image.open(GALLERY / entry['gif']) as image:
        for index in range(image.n_frames):
            image.seek(index)
            frame = image.convert('RGB')
            frame.thumbnail((960, 960), Image.Resampling.LANCZOS)
            frames.append(frame.quantize(colors=128, method=Image.Quantize.FASTOCTREE,
                                         dither=Image.Dither.NONE))
            durations.append(image.info['duration'])
    data = io.BytesIO()
    frames[0].save(data, format='GIF', save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True, disposal=1)
    with Image.open(io.BytesIO(data.getvalue())) as result:
        assert result.n_frames == entry['frames']
    return base64.b64encode(data.getvalue()).decode('ascii')


TEMPLATE = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>OS-Harm trace viewer</title>
<style>*{box-sizing:border-box}body{margin:0;color:#e8effb;background:#10151d;font:15px system-ui,sans-serif}
header{padding:20px 24px;border-bottom:1px solid #344052}h1{margin:0 0 8px;font-size:26px}p{color:#b4c1d5;margin:8px 0}
nav{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:16px}input[type=search],select,button{background:#1d2735;color:#edf2fa;border:1px solid #46556b;border-radius:6px;padding:9px}
input[type=search]{width:min(400px,90vw)}button{cursor:pointer;text-align:left}button:hover,button[aria-current=true]{background:#29466c}
main{display:grid;grid-template-columns:340px 1fr;min-height:75vh}aside{height:78vh;overflow:auto;padding:12px;border-right:1px solid #344052}
aside button{display:block;width:100%;margin-bottom:8px;overflow-wrap:anywhere;font-size:13px;line-height:1.5}
small{display:block;color:#a7c2e7}.viewer{padding:20px;min-width:0}.viewer img{width:100%;height:auto;display:block;border:1px solid #344052;background:#080c12}
h2{font-size:17px;overflow-wrap:anywhere}.controls{display:flex;gap:12px;margin-bottom:14px;align-items:center}a{color:#a6cfff}
@media(max-width:850px){main{grid-template-columns:1fr}aside{height:220px;border-right:0;border-bottom:1px solid #344052}}
</style></head><body><header><h1>OS-Harm trace viewer</h1>
<p>189 traces, including retry attempts. Each GIF is embedded in this file; no server or separate images are needed.</p>
<p>Compact previews at 960 pixels wide. One second per step; final frame held for two seconds.</p>
<nav><input id="query" type="search" placeholder="Search task, app, or status" aria-label="Search traces">
<select id="suite" aria-label="Suite"><option value="">All suites</option><option>misbehavior</option><option>injection</option><option>misuse</option></select>
<label><input id="retries" type="checkbox" checked>Include retries</label><span id="count" aria-live="polite"></span></nav></header>
<main><aside id="list" aria-label="Traces"></aside><section class="viewer">
<div class="controls"><button id="prev">← Previous</button><button id="next">Next →</button><a id="download" download="trace.gif">Download this GIF</a></div>
<h2 id="title"></h2><p id="meta"></p><img id="trace" alt="Run trace animation"><p id="empty" hidden>No matching traces.</p>
</section></main><noscript>Enable JavaScript to select and play traces in this viewer.</noscript>
<script id="entries" type="application/json">__ENTRIES__</script>
__GIFS__
<script>
const entries=JSON.parse(document.getElementById('entries').textContent);
const $=id=>document.getElementById(id), list=$('list'), img=$('trace');
let visible=entries.map((e,i)=>i), selected=-1, currentURL=null;
function title(e){return e.task.replace(/^_+/,'').replaceAll('__inject__',' / ').replaceAll('_',' ');}
function play(i){
 if(i===undefined)return;
 selected=i;const e=entries[i];
 const encoded=$('gif-'+i).textContent.trim(), binary=atob(encoded),bytes=new Uint8Array(binary.length);
 for(let p=0;p<binary.length;p++)bytes[p]=binary.charCodeAt(p);
 const oldURL=currentURL;currentURL=URL.createObjectURL(new Blob([bytes],{type:'image/gif'}));
 img.src=currentURL;img.alt=title(e);$('download').href=currentURL;
 $('download').download=e.suite+'-'+e.domain+'-'+e.task+'-'+e.attempt+'.gif';
 $('title').textContent=title(e);$('meta').textContent=`${e.suite} · ${e.domain} · ${e.attempt} · ${e.frames} screenshots · ${e.status}`;
 if(oldURL)URL.revokeObjectURL(oldURL);
 for(const button of list.children)button.setAttribute('aria-current',String(Number(button.dataset.index)===i));
 const position=visible.indexOf(i);$('prev').disabled=position<=0;$('next').disabled=position>=visible.length-1;
}
function filter(){
 const q=$('query').value.toLowerCase(), suite=$('suite').value, retries=$('retries').checked;
 visible=entries.flatMap((e,i)=>(!suite||e.suite===suite)&&(retries||e.attempt==='current')&&[e.task,title(e),e.suite,e.domain,e.status,e.attempt].join(' ').toLowerCase().includes(q)?[i]:[]);
 list.replaceChildren();for(const i of visible){const e=entries[i],button=document.createElement('button');button.type='button';button.dataset.index=i;button.textContent=title(e);
 const small=document.createElement('small');small.textContent=e.suite+' · '+e.domain+' · '+e.attempt;button.append(small);button.addEventListener('click',()=>play(i));list.append(button);}
 $('count').textContent=visible.length+' traces';$('empty').hidden=visible.length>0;img.hidden=!visible.length;
 $('prev').disabled=$('next').disabled=!visible.length;$('download').hidden=!visible.length;
 if(visible.length)play(visible.includes(selected)?selected:visible[0]);else{$('title').textContent='';$('meta').textContent='';}
}
$('query').addEventListener('input',filter);$('suite').addEventListener('change',filter);$('retries').addEventListener('change',filter);
$('prev').addEventListener('click',()=>play(visible[visible.indexOf(selected)-1]));
$('next').addEventListener('click',()=>play(visible[visible.indexOf(selected)+1]));
filter();
</script></body></html>'''


def main():
    entries = json.loads((GALLERY / 'manifest.json').read_text())['entries']
    with ThreadPoolExecutor(max_workers=4) as pool:
        encoded = []
        for i, value in enumerate(pool.map(compact, entries)):
            encoded.append(value)
            if (i + 1) % 30 == 0:
                print(f'Embedded {i + 1}/{len(entries)} traces', flush=True)
    metadata = [{k: e[k] for k in ('suite', 'domain', 'task', 'attempt', 'frames', 'status')} for e in entries]
    payloads = '\n'.join(f'<script id="gif-{i}" type="application/octet-stream">{value}</script>'
                         for i, value in enumerate(encoded))
    page = TEMPLATE.replace('__ENTRIES__', json.dumps(metadata).replace('<', '\\u003c')).replace('__GIFS__', payloads)
    OUTPUT.write_text(page)
    print(json.dumps(dict(file=str(OUTPUT), bytes=OUTPUT.stat().st_size, traces=len(entries))), flush=True)


if __name__ == '__main__':
    main()
