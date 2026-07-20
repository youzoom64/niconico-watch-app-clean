"""Step12の軽量スマホ向けアーカイブHTML生成。"""

from __future__ import annotations

import html
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TIMELINE_BLOCKS_PER_SHARD = 30
COMMENTS_PER_PAGE = 100
ESTIMATED_BLOCK_HEIGHT = 420
MAX_CACHED_SHARDS = 6


def mobile_html_filename(pc_filename: str) -> str:
    """PC版の実ファイル名から、200文字以内の対応するスマホ版名を作る。"""
    pc_path = Path(pc_filename)
    suffix = "_mobile.html"
    stem_limit = max(1, 200 - len(suffix))
    return f"{pc_path.stem[:stem_limit]}{suffix}"


def mobile_data_dirname(lv_value: str) -> str:
    """長い放送タイトルを避け、Windowsでも安全なデータディレクトリ名にする。"""
    safe_lv = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(lv_value))
    return f"{safe_lv or 'broadcast'}_mobile_data"


def inject_mobile_switch_link(pc_html: str, mobile_filename: str) -> str:
    """互換用。端末に関係なくPC版HTMLをそのまま返す。"""
    del mobile_filename
    return pc_html


def build_mobile_archive(
    *,
    broadcast_dir: str | Path,
    lv_value: str,
    pc_filename: str,
    broadcast_data: dict[str, Any],
    transcript_data: dict[str, Any],
    comments_data: dict[str, Any],
    ranking_data: dict[str, Any],
    timeline_data: dict[str, Any],
    audio_source: str,
    pc_html: str | None = None,
    preserve_existing_html: bool = True,
) -> dict[str, Any]:
    """軽量HTMLと、操作時だけ読むclassic JS shardを生成する。"""
    output_dir = Path(broadcast_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    mobile_name = mobile_html_filename(pc_filename)
    data_dir_name = mobile_data_dirname(lv_value)
    data_dir = output_dir / data_dir_name
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)

    comments = _normalise_comments(comments_data.get("comments", []))
    comment_blocks = _comments_by_block(comments)
    transcripts = _normalise_transcripts(transcript_data.get("transcripts", []))
    transcript_blocks = _transcripts_by_block(transcripts)
    timeline_blocks = _normalise_timeline(
        timeline_data.get("transcript_blocks", []), comment_blocks, transcript_blocks
    )
    timeline_shards = []
    for index in range(0, len(timeline_blocks), TIMELINE_BLOCKS_PER_SHARD):
        shard_index = index // TIMELINE_BLOCKS_PER_SHARD
        shard_name = f"timeline_{shard_index:03d}.js"
        _write_assignment(data_dir / shard_name, f"timeline_{shard_index}", timeline_blocks[index:index + TIMELINE_BLOCKS_PER_SHARD])
        timeline_shards.append(shard_name)

    _write_assignment(data_dir / "comments.js", "comments", comments)
    emotion = _emotion_payload(
        timeline_blocks,
        timeline_data.get("recording_segment_timeline") or {},
    )
    _write_assignment(data_dir / "emotion.js", "emotion", emotion)

    overview = _overview_payload(broadcast_data, ranking_data, comments, transcripts)
    mobile_path = output_dir / mobile_name
    mobile_html_preserved = bool(preserve_existing_html and mobile_path.is_file())
    if not mobile_html_preserved:
        if pc_html:
            try:
                mobile_html = _render_pc_virtual_html(
                    pc_html=pc_html,
                    pc_filename=pc_filename,
                    lv_value=lv_value,
                    data_dir_name=data_dir_name,
                    timeline_blocks=timeline_blocks,
                    timeline_shards=timeline_shards,
                    overview=overview,
                )
            except ValueError:
                mobile_html = _render_mobile_html(
                    lv_value=lv_value,
                    pc_filename=pc_filename,
                    data_dir_name=data_dir_name,
                    overview=overview,
                    audio_source=audio_source,
                    timeline_shards=timeline_shards,
                    timeline_block_count=len(timeline_blocks),
                )
        else:
            mobile_html = _render_mobile_html(
                lv_value=lv_value,
                pc_filename=pc_filename,
                data_dir_name=data_dir_name,
                overview=overview,
                audio_source=audio_source,
                timeline_shards=timeline_shards,
                timeline_block_count=len(timeline_blocks),
            )
        mobile_path.write_text(mobile_html, encoding="utf-8")
    return {
        "mobile_html_file": str(mobile_path),
        "mobile_data_dir": str(data_dir),
        "mobile_html_bytes": mobile_path.stat().st_size,
        "mobile_html_preserved": mobile_html_preserved,
        "comment_count": len(comments),
        "timeline_block_count": len(timeline_blocks),
        "timeline_shard_count": len(timeline_shards),
    }


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _normalise_comments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        seconds = _as_float(row.get("broadcast_seconds"))
        user_id = str(row.get("user_id") or "")
        anonymous = bool(row.get("anonymity", False))
        if not user_id or len(user_id) <= 4:
            icon_url = f"https://secure-dcdn.cdn.nimg.jp/nicoaccount/usericon/{user_id}.jpg"
        else:
            icon_url = (
                "https://secure-dcdn.cdn.nimg.jp/nicoaccount/usericon/"
                f"{user_id[:-4]}/{user_id}.jpg"
            )
        result.append({
            "no": _as_int(row.get("no")),
            "index": _as_int(row.get("no")),
            "seconds": seconds,
            "time": _format_seconds(seconds),
            "userId": user_id,
            "userName": str(row.get("user_name") or user_id or "匿名"),
            "anonymous": anonymous,
            "userUrl": "" if anonymous or not user_id else f"https://www.nicovideo.jp/user/{user_id}",
            "iconUrl": icon_url,
            "text": str(row.get("text") or ""),
        })
    result.sort(key=lambda item: (item["seconds"], item["no"]))
    return result


def _normalise_transcripts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        start = _as_float(row.get("start", row.get("timestamp")))
        result.append({
            "start": start,
            "end": _as_float(row.get("end", start)),
            "time": _format_seconds(start),
            "speaker": str(row.get("speaker") or ""),
            "text": str(row.get("text") or ""),
            "center": round(_as_float(row.get("center_score")), 3),
            "positive": round(_as_float(row.get("positive_score")), 3),
            "negative": round(_as_float(row.get("negative_score")), 3),
        })
    result.sort(key=lambda item: item["start"])
    return result


def _comments_by_block(comments: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for item in comments:
        grouped.setdefault(int(item["seconds"] // 10) * 10, []).append(item)
    return grouped


def _transcripts_by_block(transcripts: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for item in transcripts:
        grouped.setdefault(int(item["start"] // 10) * 10, []).append(item)
    return grouped


def _normalise_timeline(
    source_blocks: list[dict[str, Any]],
    comments: dict[int, list[dict[str, Any]]],
    transcripts: dict[int, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    result = []
    for source in source_blocks:
        start = _as_int(source.get("start_seconds"))
        result.append({
            "start": start,
            "end": _as_int(source.get("end_seconds", start + 10)),
            "label": str(source.get("time_range") or f"{_format_seconds(start)} - {_format_seconds(start + 10)}"),
            "screenshot": str(source.get("screenshot_path") or ""),
            "center": round(_as_float(source.get("center_score")), 3),
            "positive": round(_as_float(source.get("positive_score")), 3),
            "negative": round(_as_float(source.get("negative_score")), 3),
            "transcripts": transcripts.get(start, []),
            "comments": comments.get(start, []),
        })
    return result


def _emotion_payload(
    blocks: list[dict[str, Any]],
    recording_segment_timeline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    points: list[tuple[float, float | None, float | None, float | None]] = []
    for block in blocks:
        for transcript in block.get("transcripts") or []:
            second = _as_float(transcript.get("start"))
            points.append((
                second,
                _as_float(transcript.get("center")),
                _as_float(transcript.get("positive")),
                _as_float(transcript.get("negative")),
            ))
    for gap in (recording_segment_timeline or {}).get("gaps") or []:
        start = _as_float(gap.get("timeline_start_seconds"))
        end = _as_float(gap.get("timeline_end_seconds"))
        if end <= start:
            continue
        points.append((start, None, None, None))
        points.append((max(start, end - 0.000001), None, None, None))
    points.sort(key=lambda point: point[0])
    return {
        "seconds": [round(point[0], 6) for point in points],
        "labels": [_format_seconds(point[0]) for point in points],
        "center": [point[1] for point in points],
        "positive": [point[2] for point in points],
        "negative": [point[3] for point in points],
    }


def _overview_payload(
    broadcast: dict[str, Any],
    ranking: dict[str, Any],
    comments: list[dict[str, Any]],
    transcripts: list[dict[str, Any]],
) -> dict[str, Any]:
    ranking_rows = []
    for row in (ranking.get("ranking") or [])[:10]:
        ranking_rows.append({
            "rank": _as_int(row.get("rank")),
            "userId": str(row.get("user_id") or ""),
            "userName": str(row.get("user_name") or row.get("user_id") or "匿名"),
            "count": _as_int(row.get("comment_count")),
        })
    words = []
    for row in (broadcast.get("word_ranking") or [])[:20]:
        words.append({"word": str(row.get("word") or ""), "count": _as_int(row.get("count"))})
    return {
        "title": str(broadcast.get("live_title") or "タイトル不明"),
        "broadcaster": str(broadcast.get("broadcaster") or broadcast.get("owner_name") or "不明"),
        "date": _format_start_time(broadcast.get("start_time")),
        "summary": str(broadcast.get("summary_text") or "要約はありません。"),
        "duration": str(broadcast.get("elapsed_time") or "") or _format_seconds(broadcast.get("video_duration")),
        "viewerCount": str(broadcast.get("watch_count") or "").strip(),
        "commentCount": len(comments),
        "transcriptCount": len(transcripts),
        "ranking": ranking_rows,
        "words": words,
    }


def _format_start_time(value: Any) -> str:
    if value in (None, ""):
        return "不明"
    try:
        raw = float(value)
        return datetime.fromtimestamp(raw, tz=timezone(timedelta(hours=9))).strftime("%Y/%m/%d %H:%M")
    except (TypeError, ValueError, OSError):
        return str(value)


def _format_seconds(value: Any) -> str:
    seconds = max(0, _as_int(value))
    return f"{seconds // 3600:02d}:{(seconds // 60) % 60:02d}:{seconds % 60:02d}"


def _safe_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _write_assignment(path: Path, key: str, value: Any) -> None:
    payload = _safe_json(value)
    path.write_text(
        "window.__NICO_MOBILE_DATA__=window.__NICO_MOBILE_DATA__||{};"
        f"window.__NICO_MOBILE_DATA__[{json.dumps(key)}]={payload};\n",
        encoding="utf-8",
    )


def _render_legacy_mobile_html(
    *,
    lv_value: str,
    pc_filename: str,
    data_dir_name: str,
    overview: dict[str, Any],
    audio_source: str,
    timeline_shards: list[str],
    timeline_block_count: int,
) -> str:
    title = html.escape(overview["title"])
    broadcaster = html.escape(overview["broadcaster"])
    date = html.escape(overview["date"])
    summary = html.escape(overview["summary"]).replace("\n", "<br>")
    pc_link = html.escape(pc_filename, quote=True)
    audio_link = html.escape(audio_source, quote=True)
    words = "".join(
        f'<span class="word">{html.escape(item["word"])} <b>{item["count"]}</b></span>'
        for item in overview["words"]
    ) or '<span class="muted">データなし</span>'
    ranking = "".join(
        '<li><button class="user-detail" data-user-id="{}">{}位 {} <b>{}件</b></button></li>'.format(
            html.escape(item["userId"], quote=True), item["rank"], html.escape(item["userName"]), item["count"]
        )
        for item in overview["ranking"]
    ) or '<li class="muted">データなし</li>'
    manifest = {
        "dataDir": data_dir_name,
        "timelineShards": timeline_shards,
        "blocksPerShard": TIMELINE_BLOCKS_PER_SHARD,
        "timelineBlockCount": timeline_block_count,
        "estimatedBlockHeight": ESTIMATED_BLOCK_HEIGHT,
        "maxCachedShards": MAX_CACHED_SHARDS,
    }
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{title} - スマホ版</title>
<style>
:root{{--bg:#07111f;--panel:#102033;--line:#28405a;--text:#eef6ff;--muted:#9eb0c3;--accent:#2dd4bf}}
*{{box-sizing:border-box}}html,body{{margin:0;max-width:100%;overflow-x:hidden;background:var(--bg);color:var(--text);font-family:system-ui,sans-serif}}
body{{padding:12px 12px 70px}}main{{width:min(100%,760px);margin:auto}}h1{{font-size:1.25rem;line-height:1.4;margin:.4rem 0}}h2{{font-size:1.05rem;margin:.2rem 0 .8rem}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px;margin:12px 0;overflow-wrap:anywhere}}.meta,.muted{{color:var(--muted)}}
.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}.stat{{text-align:center;background:#0a1727;border-radius:10px;padding:10px 4px}}.stat b{{display:block;font-size:1.2rem}}
audio{{width:100%;margin-top:10px}}.words{{display:flex;flex-wrap:wrap;gap:7px}}.word{{background:#0a1727;border-radius:999px;padding:5px 9px}}
ol{{padding-left:1.4rem}}.user-detail,.tab,.pager button{{width:100%;text-align:left;border:1px solid var(--line);background:#0a1727;color:var(--text);padding:10px;border-radius:9px;margin:3px 0}}
.tabs{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}}.tab{{text-align:center;background:#12304a}}.tab:focus-visible,button:focus-visible,a:focus-visible{{outline:3px solid var(--accent)}}
#heavy-panel:empty{{display:none}}.toolbar,.pager{{display:flex;gap:8px;align-items:center;justify-content:space-between;margin-bottom:10px}}.pager button{{width:auto}}
.row,.block{{border-top:1px solid var(--line);padding:10px 0}}.time{{color:var(--accent);font-variant-numeric:tabular-nums}}.shot{{display:block;width:150px;max-width:100%;height:auto;margin:8px 0;border-radius:7px}}
.graph{{display:block;width:100%;height:260px;background:#06101b;border-radius:8px}}.pc-link{{position:fixed;right:12px;bottom:12px;background:#2563eb;color:#fff;padding:11px 15px;border-radius:999px;text-decoration:none;box-shadow:0 3px 12px #0008}}
@media(max-width:420px){{.stats{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<main>
<section class="card"><div class="meta">{html.escape(lv_value)} / {date}</div><h1>{title}</h1><div class="meta">配信者: {broadcaster}</div><audio id="archive-audio" controls preload="none" src="{audio_link}"></audio></section>
<section class="card"><h2>AI要約</h2><div>{summary}</div></section>
<section class="card"><h2>概要</h2><div class="stats"><div class="stat"><b>{overview['commentCount']:,}</b>コメント</div><div class="stat"><b>{overview['transcriptCount']:,}</b>文字起こし</div><div class="stat"><b>{html.escape(overview['duration'] or '不明')}</b>放送時間</div></div></section>
<section class="card"><h2>コメントランキング</h2><ol>{ranking}</ol></section>
<section class="card"><h2>主要ワード</h2><div class="words">{words}</div></section>
<section class="card"><h2>詳細（開いた時だけ読み込み）</h2><div class="tabs"><button class="tab" data-action="comments">コメント一覧</button><button class="tab" data-action="timeline">タイムライン</button><button class="tab" data-action="emotion">感情グラフ</button><button class="tab" data-action="clear">閉じる</button></div><div id="heavy-panel" aria-live="polite"></div></section>
</main>
<a class="pc-link" href="{pc_link}">PC版を開く</a>
<script>
const MANIFEST={_safe_json(manifest)};window.__NICO_MOBILE_DATA__=window.__NICO_MOBILE_DATA__||{{}};
const panel=document.getElementById('heavy-panel'),audio=document.getElementById('archive-audio');
const loaded=new Map();let commentPage=0,currentFilter='',timelineShard=0;
function el(tag,cls,text){{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n}}
function script(name){{if(loaded.has(name))return loaded.get(name);const p=new Promise((ok,fail)=>{{const s=document.createElement('script');s.src=MANIFEST.dataDir+'/'+name;s.onload=ok;s.onerror=()=>fail(new Error(name+' を読み込めません'));document.head.appendChild(s)}});loaded.set(name,p);return p}}
function status(text){{panel.replaceChildren(el('div','card',text))}}
async function comments(filter='',page=0){{status('コメントを読み込み中…');await script('comments.js');currentFilter=filter;commentPage=Math.max(0,page);const all=window.__NICO_MOBILE_DATA__.comments||[];const rows=filter?all.filter(x=>x.userId===filter):all;const pages=Math.max(1,Math.ceil(rows.length/MANIFEST.commentsPerPage));commentPage=Math.min(commentPage,pages-1);const root=el('div','card');const bar=el('div','toolbar');bar.append(el('b','',filter?'ユーザー詳細':'コメント一覧'),el('span','muted',`${{rows.length.toLocaleString()}}件 / ${{commentPage+1}}/${{pages}}頁`));root.append(bar);for(const x of rows.slice(commentPage*MANIFEST.commentsPerPage,(commentPage+1)*MANIFEST.commentsPerPage)){{const row=el('div','row');row.append(el('div','time',x.time+' '+x.userName),el('div','',x.text));root.append(row)}}const nav=el('div','pager');const prev=el('button','','← 前');prev.disabled=commentPage===0;prev.onclick=()=>comments(filter,commentPage-1);const next=el('button','','次 →');next.disabled=commentPage>=pages-1;next.onclick=()=>comments(filter,commentPage+1);nav.append(prev,next);root.append(nav);panel.replaceChildren(root)}}
async function timeline(index=0){{if(!MANIFEST.timelineShards.length){{status('タイムラインはありません');return}}timelineShard=Math.max(0,Math.min(index,MANIFEST.timelineShards.length-1));status('タイムラインを読み込み中…');await script(MANIFEST.timelineShards[timelineShard]);const blocks=window.__NICO_MOBILE_DATA__['timeline_'+timelineShard]||[];const root=el('div','card');const bar=el('div','toolbar');bar.append(el('b','','タイムライン'),el('span','muted',`${{timelineShard+1}}/${{MANIFEST.timelineShards.length}}`));root.append(bar);for(const b of blocks){{const block=el('div','block');const jump=el('button','time',b.label);jump.onclick=()=>{{audio.currentTime=b.start;audio.play()}};block.append(jump);if(b.screenshot){{const image=el('img','shot');image.loading='lazy';image.decoding='async';image.alt=b.label;image.src=b.screenshot;block.append(image)}}for(const t of b.transcripts)block.append(el('div','',`${{t.time}} ${{t.speaker}}: ${{t.text}}`));if(b.comments.length)block.append(el('div','muted',`コメント ${{b.comments.length}}件`));root.append(block)}}const nav=el('div','pager');const prev=el('button','','← 前');prev.disabled=timelineShard===0;prev.onclick=()=>timeline(timelineShard-1);const next=el('button','','次 →');next.disabled=timelineShard>=MANIFEST.timelineShards.length-1;next.onclick=()=>timeline(timelineShard+1);nav.append(prev,next);root.append(nav);panel.replaceChildren(root)}}
async function emotion(){{status('感情データを読み込み中…');await script('emotion.js');const d=window.__NICO_MOBILE_DATA__.emotion||{{labels:[]}};const root=el('div','card');root.append(el('b','','感情グラフ'));const canvas=el('canvas','graph');canvas.width=Math.max(320,Math.min(900,innerWidth*2));canvas.height=520;root.append(canvas);panel.replaceChildren(root);const ctx=canvas.getContext('2d'),pad=30,w=canvas.width-pad*2,h=canvas.height-pad*2;ctx.strokeStyle='#28405a';ctx.strokeRect(pad,pad,w,h);const seconds=d.seconds||[];const maxSecond=Math.max(1,...seconds.map(Number).filter(Number.isFinite));[['center','#94a3b8'],['positive','#2dd4bf'],['negative','#fb7185']].forEach(([key,color])=>{{const values=d[key]||[];let drawing=false;ctx.beginPath();ctx.strokeStyle=color;ctx.lineWidth=3;values.forEach((v,i)=>{{if(v===null||!Number.isFinite(Number(v))){{drawing=false;return}}const second=Number(seconds[i]??i),x=pad+(second/maxSecond)*w,y=pad+(1-(Number(v)+1)/2)*h;if(drawing)ctx.lineTo(x,y);else ctx.moveTo(x,y);drawing=true}});ctx.stroke()}})}}
document.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>{{const a=b.dataset.action;if(a==='comments')comments();if(a==='timeline')timeline();if(a==='emotion')emotion();if(a==='clear')panel.replaceChildren()}});
document.querySelectorAll('.user-detail').forEach(b=>b.onclick=()=>comments(b.dataset.userId||'',0));
</script>
</body>
</html>
"""


def _render_pc_virtual_container(timeline_blocks: list[dict[str, Any]]) -> str:
    left_blocks = []
    right_blocks = []
    for index, block in enumerate(timeline_blocks):
        start = _as_int(block.get("start"))
        label = html.escape(str(block.get("label") or _format_seconds(start)))
        shared = (
            f'class="time-block virtual-time-block" id="time_block_{start}" '
            f'data-virtual-index="{index}" data-virtual-second="{start}" '
            f'data-virtual-label="{html.escape(str(block.get("label") or _format_seconds(start)), quote=True)}" '
            'data-loaded="false" aria-hidden="true" style="position: relative; height: 180px;"'
        )
        left_blocks.append(f'<div {shared}><strong>{label}</strong></div>')
        right_blocks.append(
            f'<div {shared}><strong>{label}</strong><div class="comment-list"></div></div>'
        )
    return """
    <div class="container" data-virtual-timeline="true">
        <!-- 放送者タイムライン -->
        <div class="timeline" id="timeline1">
            <h2>放送者文字おこしのタイムライン</h2>
            %s
        </div>

        <!-- コメントタイムライン -->
        <div class="timeline" id="timeline2">
            <h2>コメントのタイムライン</h2>
            %s
        </div>
    </div>
""" % ("\n".join(left_blocks), "\n".join(right_blocks))


def _pc_virtual_client_script() -> str:
    return r"""
(function(){
  const loadedScripts=new Map();
  const shardCache=new Map();
  const shardAge=new Map();
  const loadedPairs=new Set();
  const pendingPairs=new Set();
  let ageCounter=0;
  let commentOffsetState={offsetSeconds:0,confirmed:false};
  try{
    const stateNode=document.getElementById('nico-comment-offset-state');
    if(stateNode)commentOffsetState=JSON.parse(stateNode.textContent||'{}');
  }catch(_error){}
  let commentOffset=Number.parseInt(commentOffsetState.offsetSeconds,10)||0;
  let allComments=null;
  let manualMinHeight=180;

  function el(tag,cls,text){
    const node=document.createElement(tag);
    if(cls)node.className=cls;
    if(text!==undefined)node.textContent=String(text);
    return node;
  }
  function loadScript(name){
    if(loadedScripts.has(name))return loadedScripts.get(name);
    const promise=new Promise((resolve,reject)=>{
      const script=document.createElement('script');
      script.src=NICO_VIRTUAL.dataDir+'/'+name;
      script.onload=()=>{script.remove();resolve();};
      script.onerror=()=>{script.remove();loadedScripts.delete(name);reject(new Error(name+' を読み込めません'));};
      document.head.appendChild(script);
    });
    loadedScripts.set(name,promise);
    return promise;
  }
  function activeShards(){
    const result=new Set();
    for(const index of loadedPairs)result.add(Math.floor(index/NICO_VIRTUAL.blocksPerShard));
    return result;
  }
  function evictShards(protectedIndex){
    const active=activeShards();
    active.add(protectedIndex);
    const candidates=[...shardCache.keys()]
      .filter(index=>!active.has(index))
      .sort((a,b)=>(shardAge.get(a)||0)-(shardAge.get(b)||0));
    while(shardCache.size>NICO_VIRTUAL.maxCachedShards&&candidates.length){
      const index=candidates.shift();
      const name=NICO_VIRTUAL.timelineShards[index];
      shardCache.delete(index);
      shardAge.delete(index);
      loadedScripts.delete(name);
      delete window.__NICO_MOBILE_DATA__['timeline_'+index];
    }
  }
  async function loadShard(index){
    if(shardCache.has(index)){
      shardAge.set(index,++ageCounter);
      return shardCache.get(index);
    }
    const name=NICO_VIRTUAL.timelineShards[index];
    if(!name)return [];
    await loadScript(name);
    const blocks=window.__NICO_MOBILE_DATA__['timeline_'+index]||[];
    shardCache.set(index,blocks);
    shardAge.set(index,++ageCounter);
    evictShards(index);
    return blocks;
  }
  function appendTextWithBreaks(parent,text){
    String(text||'').split(/\r?\n/).forEach((part,index)=>{
      if(index)parent.append(document.createElement('br'));
      parent.append(document.createTextNode(part));
    });
  }
  function renderTranscriptBlock(node,block,onLayout){
    const strong=el('strong','',block.label);
    node.replaceChildren(strong);
    for(const row of block.transcripts||[]){
      const meta=[row.time,row.speaker].filter(Boolean).join(' ');
      if(meta)node.append(el('div','virtual-transcript-meta',meta));
      const paragraph=el('p','comment transcript-comment');
      appendTextWithBreaks(paragraph,row.text);
      node.append(paragraph);
    }
    const scores=el('div','score-container');
    scores.append(
      el('span','center-score','center:'+block.center),
      el('span','positive-score','positive:'+block.positive),
      el('span','negative-score','negative:'+block.negative)
    );
    node.append(scores);
    const play=el('div','play-button','PLAY▶');
    play.onclick=()=>{
      const audio=document.getElementById('audioPlayer');
      if(audio){audio.currentTime=block.start;audio.play();}
    };
    node.append(play);
    if(block.screenshot){
      const container=el('div','img_container');
      const image=el('img','');
      image.loading='lazy';
      image.decoding='async';
      image.src=block.screenshot;
      image.alt='動画のスクリーンショット '+block.start+'秒';
      image.addEventListener('load',onLayout,{once:true});
      container.append(image);
      node.append(container);
    }
    const jump=el('div','nico-jump');
    const button=el('button','','タイムシフトにジャンプ');
    button.type='button';
    button.onclick=()=>window.open('https://live.nicovideo.jp/watch/'+NICO_VIRTUAL.lv+'#'+block.start,'_blank');
    jump.append(button);
    node.append(jump);
  }
  function commentsForBlock(block,second){
    if(commentOffset===0||!allComments)return block.comments||[];
    return allComments.filter(row=>{
      const shifted=Number(row.seconds||0)+commentOffset;
      return shifted>=0&&Math.floor(shifted/10)*10===second;
    });
  }
  function renderCommentBlock(node,block){
    node.replaceChildren(el('strong','',block.label));
    const list=el('div','comment-list');
    const rows=commentsForBlock(block,block.start);
    if(!rows.length){
      const empty=el('p','comment-empty','コメントなし');
      empty.style.color='#999';
      empty.style.fontStyle='italic';
      empty.style.textAlign='center';
      empty.style.marginTop='50px';
      list.append(empty);
    }else{
      for(const row of rows){
        const item=el('p','comment-item');
        item.dataset.commentSeconds=String(row.seconds||0);
        item.append(document.createTextNode((row.index||row.no||0)+' | '+row.time+' - '));
        if(row.userUrl){
          const user=el('a','',row.userName);
          user.href=row.userUrl;
          user.target='_blank';
          item.append(user);
        }else{
          item.append(document.createTextNode(row.userName||''));
        }
        item.append(document.createTextNode(' : '));
        const icon=el('img','');
        icon.loading='lazy';
        icon.decoding='async';
        icon.src=row.iconUrl||'';
        icon.style.cssText='width:20px;height:20px;vertical-align:middle;margin-left:5px';
        icon.onerror=()=>{icon.onerror=null;icon.src='https://secure-dcdn.cdn.nimg.jp/nicoaccount/usericon/defaults/blank.jpg';};
        item.append(icon,document.createTextNode(' '+(row.text||'')));
        list.append(item);
      }
    }
    node.append(list);
  }
  function equalizePair(left,right){
    requestAnimationFrame(()=>{
      left.style.height='auto';
      right.style.height='auto';
      left.style.minHeight='';
      right.style.minHeight='';
      const target=Math.max(
        manualMinHeight,
        left.scrollHeight,
        right.scrollHeight,
        Math.ceil(left.getBoundingClientRect().height),
        Math.ceil(right.getBoundingClientRect().height)
      );
      left.style.height='auto';
      right.style.height='auto';
      left.style.minHeight=target+'px';
      right.style.minHeight=target+'px';
    });
  }
  async function renderPair(index){
    if(loadedPairs.has(index)||pendingPairs.has(index))return;
    const left=document.querySelector('#timeline1 [data-virtual-index="'+index+'"]');
    const right=document.querySelector('#timeline2 [data-virtual-index="'+index+'"]');
    if(!left||!right)return;
    pendingPairs.add(index);
    try{
      const shardIndex=Math.floor(index/NICO_VIRTUAL.blocksPerShard);
      const blocks=await loadShard(shardIndex);
      const block=blocks[index%NICO_VIRTUAL.blocksPerShard];
      if(!block)return;
      renderTranscriptBlock(left,block,()=>equalizePair(left,right));
      renderCommentBlock(right,block);
      left.dataset.loaded='true';
      right.dataset.loaded='true';
      left.removeAttribute('aria-hidden');
      right.removeAttribute('aria-hidden');
      loadedPairs.add(index);
      equalizePair(left,right);
    }finally{
      pendingPairs.delete(index);
    }
  }
  function resetBlock(node,isComment){
    const label=node.dataset.virtualLabel||'';
    node.replaceChildren(el('strong','',label));
    if(isComment)node.append(el('div','comment-list'));
    node.dataset.loaded='false';
    node.setAttribute('aria-hidden','true');
    node.style.minHeight='';
    node.style.height='180px';
  }
  function unrenderPair(index){
    if(!loadedPairs.has(index))return;
    const left=document.querySelector('#timeline1 [data-virtual-index="'+index+'"]');
    const right=document.querySelector('#timeline2 [data-virtual-index="'+index+'"]');
    if(!left||!right)return;
    resetBlock(left,false);
    resetBlock(right,true);
    loadedPairs.delete(index);
    evictShards(-1);
  }
  function prune(){
    const upper=-1800;
    const lower=innerHeight+1800;
    for(const index of [...loadedPairs]){
      const left=document.querySelector('#timeline1 [data-virtual-index="'+index+'"]');
      if(!left)continue;
      const rect=left.getBoundingClientRect();
      if(rect.bottom<upper||rect.top>lower)unrenderPair(index);
    }
  }
  async function ensureAllComments(){
    if(allComments)return allComments;
    await loadScript('comments.js');
    allComments=window.__NICO_MOBILE_DATA__.comments||[];
    return allComments;
  }
  async function applyVirtualOffset(value){
    commentOffset=Number.parseInt(value,10)||0;
    if(commentOffset!==0)await ensureAllComments();
    for(const index of [...loadedPairs]){
      const shardIndex=Math.floor(index/NICO_VIRTUAL.blocksPerShard);
      const blocks=await loadShard(shardIndex);
      const block=blocks[index%NICO_VIRTUAL.blocksPerShard];
      const right=document.querySelector('#timeline2 [data-virtual-index="'+index+'"]');
      const left=document.querySelector('#timeline1 [data-virtual-index="'+index+'"]');
      if(block&&right){
        renderCommentBlock(right,block);
        if(left)equalizePair(left,right);
      }
    }
    const status=document.getElementById('commentOffsetStatus');
    if(status){
      const sign=commentOffset>0?'+':'';
      status.textContent=sign+commentOffset+'秒 表示'+NICO_VIRTUAL.commentCount+'件';
    }
  }
  document.addEventListener('DOMContentLoaded',()=>{
    window.__NICO_MOBILE_DATA__=window.__NICO_MOBILE_DATA__||{};
    const leftBlocks=[...document.querySelectorAll('#timeline1 [data-virtual-index]')];
    const observer=new IntersectionObserver(entries=>{
      for(const entry of entries){
        const index=Number(entry.target.dataset.virtualIndex);
        if(entry.isIntersecting)renderPair(index);
        else unrenderPair(index);
      }
    },{rootMargin:'1800px 0px 1800px 0px',threshold:0});
    leftBlocks.forEach(block=>observer.observe(block));
    let scheduled=false;
    addEventListener('scroll',()=>{
      if(scheduled)return;
      scheduled=true;
      requestAnimationFrame(()=>{scheduled=false;prune();});
    },{passive:true});
    const number=document.getElementById('commentOffsetSeconds');
    const range=document.getElementById('commentOffsetRange');
    if(number)number.addEventListener('input',()=>applyVirtualOffset(number.value));
    if(range)range.addEventListener('input',()=>applyVirtualOffset(range.value));
    const reset=document.getElementById('commentOffsetReset');
    if(reset)reset.addEventListener('click',()=>applyVirtualOffset(0));
    const gauge=document.getElementById('gaugeBar');
    if(gauge)gauge.addEventListener('input',()=>{
      manualMinHeight=Number.parseInt(gauge.value,10)||180;
      for(const index of loadedPairs){
        const left=document.querySelector('#timeline1 [data-virtual-index="'+index+'"]');
        const right=document.querySelector('#timeline2 [data-virtual-index="'+index+'"]');
        if(left&&right)equalizePair(left,right);
      }
    });
    const status=document.getElementById('commentOffsetStatus');
    if(status){
      const sign=commentOffset>0?'+':'';
      status.textContent=sign+commentOffset+'秒 表示'+NICO_VIRTUAL.commentCount+'件';
    }
    applyVirtualOffset(commentOffset);
    if(document.fonts&&document.fonts.ready){
      document.fonts.ready.then(()=>{
        for(const index of loadedPairs){
          const left=document.querySelector('#timeline1 [data-virtual-index="'+index+'"]');
          const right=document.querySelector('#timeline2 [data-virtual-index="'+index+'"]');
          if(left&&right)equalizePair(left,right);
        }
      });
    }
    setTimeout(prune,500);
  });
})();
"""


def _render_pc_virtual_html(
    *,
    pc_html: str,
    pc_filename: str,
    lv_value: str,
    data_dir_name: str,
    timeline_blocks: list[dict[str, Any]],
    timeline_shards: list[str],
    overview: dict[str, Any],
) -> str:
    start = pc_html.find('<div class="container">')
    if start < 0:
        raise ValueError("PC timeline container was not found")
    end_candidates = [
        pc_html.find('<div class="section ai-chat-section">', start),
        pc_html.find('<div class="section metadata-section">', start),
    ]
    end_candidates = [index for index in end_candidates if index >= 0]
    if not end_candidates:
        raise ValueError("PC timeline end marker was not found")
    end = min(end_candidates)
    virtual_container = _render_pc_virtual_container(timeline_blocks)
    result = pc_html[:start] + virtual_container + pc_html[end:]
    result = re.sub(
        r"<script\b[^>]*>(.*?)</script>",
        lambda match: ""
        if "matchMedia('(max-width: 760px)')" in match.group(1)
        and "location.replace(target.href)" in match.group(1)
        else match.group(0),
        result,
        flags=re.DOTALL,
    )
    duration = html.escape(str(overview.get("duration") or "不明"))
    viewer_count = html.escape(str(overview.get("viewerCount") or "不明"))
    result = re.sub(
        r'(<strong>配信時間:</strong>)\s*(?=</div>)',
        rf'\1 {duration}\n                    ',
        result,
        count=1,
    )
    result = re.sub(
        r'(<strong>来場者数:</strong>)\s*人(?=\s*</div>)',
        rf'\1 {viewer_count}',
        result,
        count=1,
    )
    pc_href = html.escape(pc_filename, quote=True) + "?desktop=1"
    result = re.sub(
        r'(<a\s+class="mobile-version-link"\s+href=")[^"]*("[^>]*>)[^<]*(</a>)',
        rf'\1{pc_href}\2PC版を開く\3',
        result,
        count=1,
    )
    virtual_style = """
<style id="nico-virtual-timeline-style">
html, body {
  max-width: 100% !important;
  overflow-x: hidden !important;
}
.header, .section, #controls-container, .container {
  width: 100% !important;
  max-width: 100% !important;
  box-sizing: border-box !important;
}
.mobile-version-link {
  position: static !important;
  display: block !important;
  width: max-content !important;
  max-width: calc(100% - 40px) !important;
  margin: 12px 20px 0 auto !important;
}
.container {
  display: flex !important;
  flex-direction: row !important;
  align-items: flex-start !important;
}
.container > #timeline1,
.container > #timeline2 {
  flex: 1 1 0 !important;
  min-width: 0 !important;
  width: auto !important;
}
.virtual-time-block[data-loaded="false"] { content-visibility: auto; contain-intrinsic-size: 180px; }
.virtual-time-block[data-loaded="true"] {
  content-visibility: visible !important;
  height: auto !important;
  min-height: 180px;
  max-height: none !important;
  overflow: visible !important;
}
.virtual-time-block[data-loaded="true"] .comment,
.virtual-time-block[data-loaded="true"] .comment-list,
.virtual-time-block[data-loaded="true"] .comment-item {
  height: auto !important;
  max-height: none !important;
  overflow: visible !important;
  white-space: pre-wrap !important;
  overflow-wrap: anywhere !important;
}
.virtual-time-block[data-loaded="true"] .score-container,
.virtual-time-block[data-loaded="true"] .play-button,
.virtual-time-block[data-loaded="true"] .img_container,
.virtual-time-block[data-loaded="true"] .nico-jump {
  position: static !important;
  inset: auto !important;
  float: none !important;
  max-width: 100% !important;
  margin: 8px 0 !important;
}
.virtual-time-block[data-loaded="true"] .score-container {
  display: flex !important;
  flex-wrap: wrap !important;
  gap: 4px 8px !important;
}
.virtual-time-block[data-loaded="true"] .img_container img,
.summary-image, .summary-image a, .summary-image img {
  width: auto !important;
  max-width: 100% !important;
  height: auto !important;
}
.virtual-transcript-meta {
  margin-top: 7px;
  color: #9eb0c3;
  font-size: .78rem;
  line-height: 1.4;
  overflow-wrap: anywhere;
}
#controls-container {
  display: flex !important;
  flex-wrap: wrap !important;
  gap: 6px !important;
  position: static !important;
  inset: auto !important;
  transform: none !important;
  margin: 12px 0 !important;
  z-index: auto !important;
}
#audioPlayer, #seekbar, .comment-offset-control {
  min-width: 0 !important;
  max-width: 100% !important;
}
.comment-offset-control {
  width: 100% !important;
  display: flex !important;
  flex-wrap: wrap !important;
  align-items: center !important;
  gap: 4px !important;
}
.comment-offset-control input[type="range"] { flex: 1 1 160px !important; min-width: 0 !important; }
.emotion-graph-scroll { max-width: 100% !important; overflow-x: auto !important; }
.virtual-time-block .img_container img,
.virtual-time-block .comment-item img { content-visibility: auto; }
</style>
"""
    manifest = {
        "lv": lv_value,
        "dataDir": data_dir_name,
        "timelineShards": timeline_shards,
        "blocksPerShard": TIMELINE_BLOCKS_PER_SHARD,
        "maxCachedShards": MAX_CACHED_SHARDS,
        "commentCount": sum(len(block.get("comments") or []) for block in timeline_blocks),
    }
    result = result.replace("</head>", virtual_style + "</head>", 1)
    virtual_script = (
        f'<script>const NICO_VIRTUAL={_safe_json(manifest)};'
        + _pc_virtual_client_script()
        + "</script>"
    )
    return result.replace("</body>", virtual_script + "\n</body>", 1)


def _adaptive_client_script() -> str:
    return r"""
window.__NICO_MOBILE_DATA__=window.__NICO_MOBILE_DATA__||{};
const timeline=document.getElementById('virtual-timeline');
const timelineStatus=document.getElementById('timeline-status');
const audio=document.getElementById('archive-audio');
const slots=[];
const measuredHeights=new Map();
const rendered=new Set();
const pending=new Set();
const shardCache=new Map();
const shardAge=new Map();
const loadedScripts=new Map();
let ageCounter=0;
let currentSlot=-1;

function el(tag,cls,text){
  const node=document.createElement(tag);
  if(cls)node.className=cls;
  if(text!==undefined)node.textContent=String(text);
  return node;
}
function updateStatus(){
  timelineStatus.textContent='画面付近 '+rendered.size+'ブロックを描画 / 全'+MANIFEST.timelineBlockCount+'ブロック';
}
function loadScript(name){
  if(loadedScripts.has(name))return loadedScripts.get(name);
  const promise=new Promise((resolve,reject)=>{
    const scriptNode=document.createElement('script');
    scriptNode.src=MANIFEST.dataDir+'/'+name;
    scriptNode.onload=()=>{scriptNode.remove();resolve();};
    scriptNode.onerror=()=>{
      scriptNode.remove();
      loadedScripts.delete(name);
      reject(new Error(name+' を読み込めません'));
    };
    document.head.appendChild(scriptNode);
  });
  loadedScripts.set(name,promise);
  return promise;
}
function activeShardIndexes(){
  const active=new Set();
  for(const index of rendered)active.add(Math.floor(index/MANIFEST.blocksPerShard));
  return active;
}
function evictShards(protectedIndex){
  const active=activeShardIndexes();
  active.add(protectedIndex);
  const candidates=[...shardCache.keys()]
    .filter(index=>!active.has(index))
    .sort((a,b)=>(shardAge.get(a)||0)-(shardAge.get(b)||0));
  while(shardCache.size>MANIFEST.maxCachedShards&&candidates.length){
    const index=candidates.shift();
    const name=MANIFEST.timelineShards[index];
    shardCache.delete(index);
    shardAge.delete(index);
    loadedScripts.delete(name);
    delete window.__NICO_MOBILE_DATA__['timeline_'+index];
  }
}
async function loadShard(index){
  if(shardCache.has(index)){
    shardAge.set(index,++ageCounter);
    return shardCache.get(index);
  }
  const name=MANIFEST.timelineShards[index];
  if(!name)return [];
  await loadScript(name);
  const blocks=window.__NICO_MOBILE_DATA__['timeline_'+index]||[];
  shardCache.set(index,blocks);
  shardAge.set(index,++ageCounter);
  evictShards(index);
  return blocks;
}
function appendEntries(lane,rows,kind){
  if(!rows.length){
    lane.append(el('div','muted','データなし'));
    return;
  }
  for(const row of rows){
    const entry=el('div','entry '+kind+'-entry');
    if(kind==='transcript'){
      entry.append(el('div','entry-meta',row.time+' '+(row.speaker||'')),el('div','entry-text',row.text));
    }else{
      entry.append(el('div','entry-meta',row.time+' '+row.userName),el('div','entry-text',row.text));
    }
    lane.append(entry);
  }
}
async function renderBlock(index){
  const slot=slots[index];
  if(!slot||rendered.has(index)||pending.has(index))return;
  pending.add(index);
  slot.setAttribute('aria-busy','true');
  try{
    const shardIndex=Math.floor(index/MANIFEST.blocksPerShard);
    const blocks=await loadShard(shardIndex);
    const block=blocks[index%MANIFEST.blocksPerShard];
    if(!block)return;
    const article=el('article','timeline-block');
    const head=el('div','block-head');
    const jump=el('button','time-jump',block.label);
    jump.type='button';
    jump.onclick=()=>{audio.currentTime=block.start;audio.play();};
    head.append(jump,el('span','muted','コメント '+block.comments.length+'件 / 文字起こし '+block.transcripts.length+'件'));
    article.append(head);
    if(block.screenshot){
      const image=el('img','block-shot');
      image.loading='lazy';
      image.decoding='async';
      image.alt=block.label;
      image.src=block.screenshot;
      article.append(image);
    }
    const lanes=el('div','timeline-lanes');
    const transcriptLane=el('section','lane transcript-lane');
    const commentLane=el('section','lane comment-lane');
    transcriptLane.append(el('h3','','文字起こし'));
    commentLane.append(el('h3','','コメント'));
    appendEntries(transcriptLane,block.transcripts||[],'transcript');
    appendEntries(commentLane,block.comments||[],'comment');
    lanes.append(transcriptLane,commentLane);
    article.append(lanes);
    slot.style.height='auto';
    slot.style.minHeight=(measuredHeights.get(index)||160)+'px';
    slot.replaceChildren(article);
    slot.dataset.rendered='true';
    slot.removeAttribute('aria-hidden');
    slot.setAttribute('aria-label','タイムライン '+(index+1));
    rendered.add(index);
    resizeObserver.observe(slot);
    requestAnimationFrame(()=>{
      const height=Math.ceil(slot.getBoundingClientRect().height);
      if(height>0){
        measuredHeights.set(index,height);
        slot.style.minHeight=height+'px';
      }
      pruneDistantBlocks();
    });
  }catch(error){
    slot.replaceChildren(el('div','card','読み込み失敗: '+error.message));
    slot.style.height='auto';
  }finally{
    pending.delete(index);
    slot.setAttribute('aria-busy','false');
    updateStatus();
  }
}
function unrenderBlock(index){
  const slot=slots[index];
  if(!slot||!rendered.has(index))return;
  const height=Math.max(1,Math.ceil(slot.getBoundingClientRect().height));
  measuredHeights.set(index,height);
  resizeObserver.unobserve(slot);
  slot.replaceChildren();
  slot.style.minHeight='';
  slot.style.height=height+'px';
  slot.dataset.rendered='false';
  slot.setAttribute('aria-hidden','true');
  rendered.delete(index);
  updateStatus();
  evictShards(-1);
}
const resizeObserver=new ResizeObserver(entries=>{
  for(const entry of entries){
    const index=Number(entry.target.dataset.index);
    if(rendered.has(index))measuredHeights.set(index,Math.ceil(entry.contentRect.height));
  }
});
const observer=new IntersectionObserver(entries=>{
  for(const entry of entries){
    const index=Number(entry.target.dataset.index);
    if(entry.isIntersecting)renderBlock(index);
    else unrenderBlock(index);
  }
},{rootMargin:'1600px 0px 1600px 0px',threshold:0});
let pruneScheduled=false;
function pruneDistantBlocks(){
  pruneScheduled=false;
  const upper=-2400;
  const lower=innerHeight+2400;
  for(const index of [...rendered]){
    const rect=slots[index].getBoundingClientRect();
    if(rect.bottom<upper||rect.top>lower)unrenderBlock(index);
  }
}
addEventListener('scroll',()=>{
  if(pruneScheduled)return;
  pruneScheduled=true;
  requestAnimationFrame(pruneDistantBlocks);
},{passive:true});
function initialiseTimeline(){
  if(!MANIFEST.timelineBlockCount){
    timeline.append(el('div','muted','タイムラインはありません'));
    updateStatus();
    return;
  }
  const fragment=document.createDocumentFragment();
  for(let index=0;index<MANIFEST.timelineBlockCount;index++){
    const slot=el('section','timeline-slot');
    slot.dataset.index=String(index);
    slot.dataset.rendered='false';
    slot.style.height=MANIFEST.estimatedBlockHeight+'px';
    slot.setAttribute('aria-hidden','true');
    slots.push(slot);
    fragment.append(slot);
  }
  timeline.append(fragment);
  for(const slot of slots)observer.observe(slot);
  updateStatus();
  setTimeout(pruneDistantBlocks,500);
}
audio.addEventListener('timeupdate',()=>{
  if(!slots.length)return;
  const next=Math.max(0,Math.min(slots.length-1,Math.floor(audio.currentTime/10)));
  if(next===currentSlot)return;
  if(currentSlot>=0)slots[currentSlot].classList.remove('is-current');
  currentSlot=next;
  slots[currentSlot].classList.add('is-current');
});
document.getElementById('jump-current').onclick=()=>{
  const index=Math.max(0,Math.min(slots.length-1,Math.floor(audio.currentTime/10)));
  renderBlock(index);
  if(slots[index])slots[index].scrollIntoView({behavior:'smooth',block:'center'});
};
async function showUserComments(userId){
  const panel=document.getElementById('user-panel');
  panel.hidden=false;
  panel.replaceChildren(el('div','muted','ユーザーコメントを読み込み中…'));
  try{
    await loadScript('comments.js');
    const rows=(window.__NICO_MOBILE_DATA__.comments||[]).filter(row=>row.userId===userId);
    const root=el('div','');
    root.append(el('h3','',rows.length+'件のコメント'));
    for(const row of rows){
      const entry=el('div','entry');
      entry.append(el('div','entry-meta',row.time+' '+row.userName),el('div','entry-text',row.text));
      root.append(entry);
    }
    panel.replaceChildren(root);
  }catch(error){
    panel.replaceChildren(el('div','muted','読み込み失敗: '+error.message));
  }
}
document.querySelectorAll('.user-detail').forEach(button=>{
  button.onclick=()=>showUserComments(button.dataset.userId||'');
});
document.getElementById('emotion-button').onclick=async()=>{
  const panel=document.getElementById('emotion-panel');
  if(!panel.hidden){
    panel.hidden=true;
    panel.replaceChildren();
    return;
  }
  panel.hidden=false;
  panel.replaceChildren(el('div','muted','感情データを読み込み中…'));
  try{
    await loadScript('emotion.js');
    const data=window.__NICO_MOBILE_DATA__.emotion||{labels:[]};
    const canvas=el('canvas','graph');
    canvas.width=Math.max(640,Math.min(1800,innerWidth*2));
    canvas.height=520;
    panel.replaceChildren(canvas);
    const ctx=canvas.getContext('2d'),pad=30,width=canvas.width-pad*2,height=canvas.height-pad*2;
    ctx.strokeStyle='#28405a';
    ctx.strokeRect(pad,pad,width,height);
    for(const pair of [['center','#94a3b8'],['positive','#2dd4bf'],['negative','#fb7185']]){
      const values=data[pair[0]]||[];
      ctx.beginPath();
      ctx.strokeStyle=pair[1];
      ctx.lineWidth=3;
      values.forEach((value,index)=>{
        const x=pad+(values.length<2?0:index/(values.length-1))*width;
        const y=pad+(1-(Number(value)+1)/2)*height;
        if(index)ctx.lineTo(x,y);else ctx.moveTo(x,y);
      });
      ctx.stroke();
    }
  }catch(error){
    panel.replaceChildren(el('div','muted','読み込み失敗: '+error.message));
  }
};
initialiseTimeline();
"""


def _render_mobile_html(
    *,
    lv_value: str,
    pc_filename: str,
    data_dir_name: str,
    overview: dict[str, Any],
    audio_source: str,
    timeline_shards: list[str],
    timeline_block_count: int,
) -> str:
    title = html.escape(overview["title"])
    broadcaster = html.escape(overview["broadcaster"])
    date = html.escape(overview["date"])
    summary = html.escape(overview["summary"]).replace("\n", "<br>")
    pc_link = html.escape(pc_filename, quote=True)
    audio_link = html.escape(audio_source, quote=True)
    words = "".join(
        f'<span class="word">{html.escape(item["word"])} <b>{item["count"]}</b></span>'
        for item in overview["words"]
    ) or '<span class="muted">データなし</span>'
    ranking = "".join(
        '<li><button class="user-detail" data-user-id="{}">{}位 {} <b>{}件</b></button></li>'.format(
            html.escape(item["userId"], quote=True),
            item["rank"],
            html.escape(item["userName"]),
            item["count"],
        )
        for item in overview["ranking"]
    ) or '<li class="muted">データなし</li>'
    manifest = {
        "dataDir": data_dir_name,
        "timelineShards": timeline_shards,
        "blocksPerShard": TIMELINE_BLOCKS_PER_SHARD,
        "timelineBlockCount": timeline_block_count,
        "estimatedBlockHeight": ESTIMATED_BLOCK_HEIGHT,
        "maxCachedShards": MAX_CACHED_SHARDS,
    }
    client_script = _adaptive_client_script()
    return f"""<!doctype html>
<html lang="ja" data-view="adaptive">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{title} - 軽量アーカイブ</title>
<style>
:root{{--bg:#07111f;--panel:#102033;--panel2:#0a1727;--line:#28405a;--text:#eef6ff;--muted:#9eb0c3;--accent:#2dd4bf;--comment:#fbbf24}}
*{{box-sizing:border-box}}html,body{{margin:0;max-width:100%;overflow-x:hidden;background:var(--bg);color:var(--text);font-family:system-ui,sans-serif}}
body{{padding:12px 12px 74px}}main{{width:min(100%,1480px);margin:auto}}h1{{font-size:clamp(1.2rem,2.4vw,2rem);line-height:1.4;margin:.4rem 0}}h2{{font-size:1.1rem;margin:.2rem 0 .8rem}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px;margin:12px 0;overflow-wrap:anywhere}}.meta,.muted{{color:var(--muted)}}
.overview-grid,.insight-grid{{display:grid;gap:12px}}.stats{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}}.stat{{text-align:center;background:var(--panel2);border-radius:10px;padding:10px 4px}}.stat b{{display:block;font-size:1.2rem}}
audio{{width:100%;margin-top:10px}}.words{{display:flex;flex-wrap:wrap;gap:7px}}.word{{background:var(--panel2);border-radius:999px;padding:5px 9px}}ol{{padding-left:1.4rem}}button{{font:inherit}}
.user-detail,.control{{width:100%;text-align:left;border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:10px;border-radius:9px;margin:3px 0}}button:focus-visible,a:focus-visible{{outline:3px solid var(--accent)}}
.timeline-heading{{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}}.timeline-tools{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}.timeline-tools .control{{width:auto;margin:0;text-align:center}}
.virtual-timeline{{position:relative;contain:layout style}}.timeline-slot{{overflow-anchor:none;border-top:1px solid var(--line);contain:layout style;transition:border-color .15s}}.timeline-slot.is-current{{border:2px solid var(--accent);border-radius:12px}}
.timeline-block{{padding:12px 0}}.block-head{{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:10px}}.time-jump{{border:1px solid var(--accent);background:#063b3c;color:#dffff9;border-radius:999px;padding:7px 11px;cursor:pointer;font-variant-numeric:tabular-nums}}
.block-shot{{display:block;width:220px;max-width:100%;height:auto;margin:0 0 10px;border-radius:8px;background:#06101b}}.timeline-lanes{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);align-items:stretch;gap:10px}}.lane{{min-width:0;height:100%;background:var(--panel2);border-radius:10px;padding:10px}}.lane h3{{font-size:.98rem;margin:0 0 8px}}.transcript-lane h3{{color:var(--accent)}}.comment-lane h3{{color:var(--comment)}}
.entry{{padding:7px 0;border-top:1px solid #1b3147;line-height:1.55}}.entry:first-of-type{{border-top:0}}.entry-meta{{font-size:.82rem;color:var(--muted);font-variant-numeric:tabular-nums}}.entry-text{{white-space:pre-wrap;overflow-wrap:anywhere}}
.user-panel[hidden],.emotion-panel[hidden]{{display:none}}.user-panel{{max-height:70vh;overflow:auto}}.graph{{display:block;width:100%;height:260px;background:#06101b;border-radius:8px}}
.legacy-link{{position:fixed;right:12px;bottom:12px;background:#2563eb;color:#fff;padding:11px 15px;border-radius:999px;text-decoration:none;box-shadow:0 3px 12px #0008;z-index:100}}
@media (min-width:900px){{.overview-grid{{grid-template-columns:minmax(0,2fr) minmax(320px,1fr)}}.insight-grid{{grid-template-columns:1fr 1fr}}}}
@media (max-width:899px){{body{{padding:8px 8px 72px}}.card{{padding:11px;margin:8px 0;border-radius:11px}}.stats{{grid-template-columns:1fr}}.timeline-lanes{{gap:6px}}.lane{{padding:8px}}.entry{{font-size:.9rem;padding:6px 0}}.entry-meta{{font-size:.72rem}}.block-shot{{width:100%;max-height:38vh;object-fit:contain}}.legacy-link{{font-size:.82rem;padding:9px 12px}}}}
</style>
</head>
<body>
<main>
<section class="card"><div class="meta">{html.escape(lv_value)} / {date}</div><h1>{title}</h1><div class="meta">配信者: {broadcaster}</div><audio id="archive-audio" controls preload="none" src="{audio_link}"></audio></section>
<div class="overview-grid"><section class="card"><h2>AI要約</h2><div>{summary}</div></section><section class="card"><h2>概要</h2><div class="stats"><div class="stat"><b>{overview['commentCount']:,}</b>コメント</div><div class="stat"><b>{overview['transcriptCount']:,}</b>文字起こし</div><div class="stat"><b>{html.escape(overview['duration'] or '不明')}</b>放送時間</div></div></section></div>
<div class="insight-grid"><section class="card"><h2>コメントランキング</h2><ol>{ranking}</ol><div id="user-panel" class="user-panel" hidden></div></section><section class="card"><h2>主要ワード</h2><div class="words">{words}</div><button id="emotion-button" class="control" type="button">感情グラフを表示</button><div id="emotion-panel" class="emotion-panel" hidden></div></section></div>
<section class="card"><div class="timeline-heading"><div><h2>文字起こし＋コメント</h2><div class="muted">全時間を連続表示。実際に描画するのは画面付近だけです。</div></div><div class="timeline-tools"><button id="jump-current" class="control" type="button">再生位置へ</button><span id="timeline-status" class="muted"></span></div></div><div id="virtual-timeline" class="virtual-timeline" aria-live="polite"></div></section>
</main>
<a class="legacy-link" href="{pc_link}?desktop=1">従来PC版</a>
<script>const MANIFEST={_safe_json(manifest)};{client_script}</script>
</body>
</html>
"""
