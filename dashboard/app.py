#!/usr/bin/env python3
"""BullBearBot health dashboard — three-page: / overview, /a1 detail, /a2 detail."""

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Config ────────────────────────────────────────────────────────────────────
BOT_DIR = Path(__file__).parent.parent
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "bullbearbot")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))
ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_KEY_OPT = os.getenv("ALPACA_API_KEY_OPTIONS", "")
ALPACA_SECRET_OPT = os.getenv("ALPACA_SECRET_KEY_OPTIONS", "")

ET_OFFSET = timedelta(hours=-4)  # EDT; adjust to -5 in winter

app = Flask(__name__)

if DASHBOARD_USER == "admin" and DASHBOARD_PASSWORD == "bullbearbot":
    app.logger.warning("Default credentials — set DASHBOARD_USER/DASHBOARD_PASSWORD in .env")

# ── Shared CSS (plain string — NOT an f-string — so {} are real CSS braces) ──
SHARED_CSS = """
:root {
  --bg-base: #0d0e1f;
  --bg-card: #10112a;
  --bg-card-2: #0d0e1f;
  --bg-input: #1a1b35;
  --border: #1e2040;
  --border-subtle: #1a1b35;
  --text-primary: #e8ecff;
  --text-secondary: #c8d0e8;
  --text-muted: #4a5080;
  --text-dim: #3a4070;
  --text-ghost: #2a3060;
  --accent-blue: #4facfe;
  --accent-green: #00e676;
  --accent-red: #ff5050;
  --accent-amber: #ffaa20;
  --accent-purple: #a855f7;
  --grad-a1: linear-gradient(135deg, #1a2a4a 0%, #0d1a35 100%);
  --grad-a2: linear-gradient(135deg, #1a3a2a 0%, #0d2a1a 100%);
  --grad-combo: linear-gradient(135deg, #2a1a4a 0%, #1a0d35 100%);
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg-base); color: var(--text-secondary); font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 13px; line-height: 1.5; }
a { color: var(--accent-blue); text-decoration: none; }
a:hover { text-decoration: underline; }
details > summary { cursor: pointer; }
details > summary::-webkit-details-marker { display: none; }

.container { padding: 12px 24px 72px; }

/* Nav */
.nav { background: var(--bg-card); border-bottom: 1px solid var(--border); padding: 0 16px; display: flex; align-items: center; gap: 0; position: sticky; top: 0; z-index: 100; min-height: 52px; }
.nav-brand { font-size: 14px; color: var(--text-primary); margin-right: 16px; white-space: nowrap; flex-shrink: 0; display: flex; flex-direction: column; gap: 1px; }
.nav-brand .bear { color: var(--accent-blue); }
.nav-subtitle { font-size: 9px; color: var(--text-ghost); letter-spacing: 0.3px; white-space: nowrap; font-weight: 400; }
.nav-tabs { display: flex; align-items: stretch; height: 44px; gap: 0; }
.nav-tab { display: flex; align-items: center; padding: 0 13px; font-size: 11px; color: var(--text-muted); border-bottom: 2px solid transparent; white-space: nowrap; text-decoration: none; transition: color 0.12s, border-color 0.12s; }
.nav-tab:hover { color: var(--text-secondary); text-decoration: none; }
.nav-tab.active { color: var(--accent-blue); border-bottom-color: var(--accent-blue); }
.nav-pills { display: flex; align-items: center; gap: 8px; margin-left: 10px; }
.npill { font-size: 12px; padding: 5px 14px; border-radius: 4px; border: 2px solid; letter-spacing: 0.4px; font-weight: 700; }
.npill-g { background: rgba(0,230,118,.1); border-color: rgba(0,230,118,.3); color: var(--accent-green); }
.npill-a { background: rgba(255,170,32,.1); border-color: rgba(255,170,32,.3); color: var(--accent-amber); }
.npill-r { background: rgba(255,80,80,.1); border-color: rgba(255,80,80,.3); color: var(--accent-red); }
.nav-right { margin-left: auto; font-size: 10px; color: var(--text-ghost); white-space: nowrap; flex-shrink: 0; }

/* Ticker */
.ticker { background: var(--bg-card); border-top: 1px solid var(--border); padding: 6px 16px; font-size: 9px; color: var(--text-muted); display: flex; align-items: center; gap: 10px; position: fixed; bottom: 0; left: 0; right: 0; z-index: 100; overflow: hidden; white-space: nowrap; }
.tk-sep { color: var(--text-dim); user-select: none; }
.tk-sym { color: var(--text-muted); letter-spacing: 0.3px; }
.tk-val { color: var(--text-secondary); }
.tk-g { color: var(--accent-green); }
.tk-r { color: var(--accent-red); }
.tk-dim { color: var(--text-dim); }

/* Section titles */
.section-label { font-size: 10px; letter-spacing: 1.5px; text-transform: uppercase; color: var(--text-dim); margin: 18px 0 8px; }

/* Cards */
.card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 14px; margin-bottom: 10px; }
.card-2 { background: var(--bg-card-2); border: 1px solid var(--border-subtle); border-radius: 8px; padding: 8px; }
.card-row { display: flex; justify-content: space-between; align-items: center; padding: 5px 0; border-bottom: 1px solid var(--border-subtle); }
.card-row:last-child { border-bottom: none; }
.card-label { font-size: 12px; color: var(--text-muted); }
.card-val { font-size: 12px; color: var(--text-secondary); }

/* Hero gradient cards */
.hero-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-bottom: 12px; }
@media (max-width: 760px) { .hero-grid { grid-template-columns: 1fr; } }
.hero-card { border-radius: 12px; border: 1px solid var(--border); padding: 16px; }
.hero-card-a1 { background: var(--grad-a1); }
.hero-card-a2 { background: var(--grad-a2); }
.hero-card-combo { background: var(--grad-combo); }
.hero-inner { display: flex; justify-content: space-between; align-items: flex-start; }
.hero-lbl { font-size: 10px; letter-spacing: 1.5px; text-transform: uppercase; color: var(--text-muted); margin-bottom: 6px; }
.hero-num { font-size: 26px; font-weight: 600; letter-spacing: -1px; line-height: 1; }
.hero-sub { font-size: 12px; color: var(--text-muted); margin-top: 5px; }
.hero-badge { display: inline-block; font-size: 10px; padding: 2px 8px; border-radius: 3px; border: 1px solid; margin-top: 8px; }
.hero-badge-g { background: rgba(0,230,118,.12); border-color: rgba(0,230,118,.3); color: var(--accent-green); }
.hero-badge-r { background: rgba(255,80,80,.12); border-color: rgba(255,80,80,.3); color: var(--accent-red); }
.hero-mini-stats { margin-top: 10px; display: flex; flex-direction: column; gap: 5px; }
.hero-mini-row { display: flex; justify-content: space-between; font-size: 11px; }
.hero-mini-lbl { color: var(--text-muted); }
.hero-mini-val { color: var(--text-secondary); }

/* Range bars */
.range-track { height: 5px; background: var(--bg-input); border-radius: 3px; position: relative; margin: 4px 0; overflow: visible; }
.range-fill { position: absolute; top: 0; height: 100%; border-radius: 3px; }

/* Tables */
.table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table.data-table { width: 100%; border-collapse: collapse; font-size: 12px; white-space: nowrap; }
table.data-table th { background: var(--bg-card-2); color: var(--text-muted); font-size: 10px; font-weight: 500; text-align: right; padding: 8px 10px; text-transform: uppercase; letter-spacing: 0.8px; border-bottom: 1px solid var(--border); }
table.data-table th:first-child { text-align: left; }
table.data-table td { padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--border-subtle); color: var(--text-secondary); }
table.data-table td:first-child { text-align: left; color: var(--text-primary); }
table.data-table tr:last-child td { border-bottom: none; }
table.data-table tr:hover td { background: rgba(79,172,254,.03); }

/* qs-table kept for kv widgets */
.qs-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.qs-table th { background: var(--bg-card-2); color: var(--text-muted); font-size: 10px; padding: 7px 10px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.8px; text-align: left; border-bottom: 1px solid var(--border); }
.qs-table td { padding: 7px 10px; border-bottom: 1px solid var(--border-subtle); font-size: 12px; color: var(--text-secondary); }
.qs-table tr:last-child td { border-bottom: none; }
.qs-table td:first-child { color: var(--text-muted); }
.qs-table td:not(:first-child) { text-align: right; }
.qs-table th:not(:first-child) { text-align: right; }

/* pos-table alias for backward compat */
table.pos-table { width: 100%; border-collapse: collapse; font-size: 12px; white-space: nowrap; }
table.pos-table th { background: var(--bg-card-2); color: var(--text-muted); font-weight: 500; text-align: right; padding: 8px 10px; font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px; border-bottom: 1px solid var(--border); }
table.pos-table th:first-child { text-align: left; }
table.pos-table td { padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--border-subtle); color: var(--text-secondary); }
table.pos-table td:first-child { text-align: left; color: var(--text-primary); }
table.pos-table tr:last-child td { border-bottom: none; }
table.pos-table tr:hover td { background: rgba(79,172,254,.03); }

/* Badges */
.badge { display: inline-block; font-size: 10px; padding: 2px 8px; border-radius: 3px; border: 1px solid; letter-spacing: 0.3px; vertical-align: middle; }
.badge-g { background: rgba(0,230,118,.1); border-color: rgba(0,230,118,.3); color: var(--accent-green); }
.badge-r { background: rgba(255,80,80,.1); border-color: rgba(255,80,80,.3); color: var(--accent-red); }
.badge-a { background: rgba(255,170,32,.1); border-color: rgba(255,170,32,.3); color: var(--accent-amber); }
.badge-b { background: rgba(79,172,254,.1); border-color: rgba(79,172,254,.3); color: var(--accent-blue); }
.badge-p { background: rgba(168,85,247,.1); border-color: rgba(168,85,247,.3); color: var(--accent-purple); }
.badge-x { background: rgba(74,80,128,.1); border-color: rgba(74,80,128,.3); color: var(--text-muted); }

/* Flag badges (legacy compat) */
.flag { display: inline-block; font-size: 9px; padding: 1px 5px; border-radius: 3px; margin-left: 4px; vertical-align: middle; }
.flag-earn { background: rgba(255,170,32,.15); color: var(--accent-amber); }
.flag-over { background: rgba(255,80,80,.15); color: var(--accent-red); }
.flag-warn { background: rgba(255,170,32,.12); color: var(--accent-amber); }
.flag-trail { background: rgba(0,230,118,.12); color: var(--accent-green); }
.flag-be { background: rgba(79,172,254,.12); color: var(--accent-blue); }

/* Alert/warning */
.warn-critical { background: rgba(255,80,80,.08); border: 1px solid rgba(255,80,80,.25); color: var(--accent-red); border-radius: 8px; padding: 10px 14px; margin-bottom: 8px; font-size: 11px; }
.warn-orange { background: rgba(255,170,32,.08); border: 1px solid rgba(255,170,32,.25); color: var(--accent-amber); border-radius: 8px; padding: 10px 14px; margin-bottom: 8px; font-size: 11px; }
.alert { padding: 9px 12px; border-radius: 8px; margin-bottom: 6px; font-size: 11px; }
.alert-green { background: rgba(0,230,118,.07); border: 1px solid rgba(0,230,118,.2); color: var(--accent-green); }
.alert-orange { background: rgba(255,170,32,.07); border: 1px solid rgba(255,170,32,.2); color: var(--accent-amber); }
.alert-red { background: rgba(255,80,80,.07); border: 1px solid rgba(255,80,80,.2); color: var(--accent-red); }

/* Stat boxes */
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; }
.stat-box { background: var(--bg-card-2); border: 1px solid var(--border-subtle); border-radius: 8px; padding: 10px 12px; }
.stat-label { font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.8px; }
.stat-val { font-size: 18px; font-weight: 600; margin-top: 3px; color: var(--text-primary); letter-spacing: -0.5px; }

/* kv rows */
.kv { display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid var(--border-subtle); font-size: 12px; }
.kv:last-child { border-bottom: none; }
.kv-label { color: var(--text-muted); font-size: 12px; }
.kv-val { color: var(--text-secondary); text-align: right; font-size: 12px; }

/* Reasoning */
.reasoning { background: var(--bg-card-2); border-left: 2px solid var(--accent-blue); padding: 10px 14px; border-radius: 0 8px 8px 0; font-size: 11px; color: var(--text-secondary); font-style: italic; margin: 8px 0; }
.log-line { font-family: "SF Mono", "Fira Code", monospace; font-size: 10px; padding: 2px 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); }

/* Thesis cards */
.thesis-card { border: 1px solid var(--border-subtle); border-radius: 8px; padding: 10px 12px; margin-bottom: 8px; }
.thesis-card:last-child { margin-bottom: 0; }

/* Watch bullets */
.watch-bullet { padding: 5px 0; border-bottom: 1px solid var(--border-subtle); font-size: 12px; }
.watch-bullet:last-child { border-bottom: none; }

/* Compact grids */
.compact-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
@media (max-width: 600px) { .compact-grid { grid-template-columns: 1fr; } }
.tri-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }
@media (max-width: 760px) { .tri-grid { grid-template-columns: 1fr; } }

/* Trail table */
.trail-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.trail-table th { background: var(--bg-card-2); color: var(--text-muted); font-weight: 500; padding: 6px 10px; text-align: left; font-size: 9px; text-transform: uppercase; letter-spacing: 0.8px; border-bottom: 1px solid var(--border); }
.trail-table td { padding: 6px 10px; border-bottom: 1px solid var(--border-subtle); }
.trail-table tr:last-child td { border-bottom: none; }

/* Progress bar */
.progress-wrap { background: var(--bg-input); border-radius: 3px; height: 5px; margin: 4px 0 2px; overflow: hidden; }
.progress-fill { height: 5px; border-radius: 3px; }

/* Dec panel */
.dec-panel { max-height: 340px; overflow-y: auto; }

/* Acct summary bar (horizontal) */
.acct-bar { display: flex; gap: 0; background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; margin-bottom: 10px; }
.acct-bar-item { padding: 12px 16px; flex: 1; border-right: 1px solid var(--border-subtle); }
.acct-bar-item:last-child { border-right: none; }
.acct-bar-lbl { font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 4px; }
.acct-bar-val { font-size: 13px; color: var(--text-primary); }

/* Legacy acct rows */
.acct-title { font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 10px; }
.acct-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid var(--border-subtle); font-size: 12px; }
.acct-row:last-child { border-bottom: none; }
.acct-label { color: var(--text-muted); font-size: 12px; }
.acct-val { color: var(--text-secondary); }

/* Color utilities */
.green { color: var(--accent-green); }
.red { color: var(--accent-red); }
.orange { color: var(--accent-amber); }
.blue { color: var(--accent-blue); }
.purple { color: var(--accent-purple); }
.muted { color: var(--text-muted); }
.primary { color: var(--text-primary); }
"""

# Plain string — no escaping needed when injected via {_COUNTDOWN_JS} in f-strings
_COUNTDOWN_JS = """<script>
var secs = 60, el = document.getElementById("cd");
setInterval(function() { secs -= 1; if (secs <= 0) secs = 60; if (el) el.textContent = secs; }, 1000);
</script>"""

# Command palette — ⌘K / Ctrl+K global search
_COMMAND_PALETTE_HTML = """<div id="bbb-cmd-ov" style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.6);align-items:flex-start;justify-content:center;padding-top:80px"><div style="width:min(600px,calc(100vw - 32px));max-height:480px;background:var(--bbb-surface,#13151D);border:1px solid var(--bbb-border,#1F2330);border-radius:8px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 24px 48px rgba(0,0,0,0.5)"><div style="display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid var(--bbb-border,#1F2330);gap:10px;flex-shrink:0"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" style="flex-shrink:0;color:var(--bbb-ai,#9B5DE5)"><circle cx="6.5" cy="6.5" r="4.5" stroke="currentColor" stroke-width="1.5"/><line x1="10" y1="10" x2="14" y2="14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg><input id="bbb-cmd-inp" type="text" autocomplete="off" spellcheck="false" placeholder="Search trades, cycles, symbols, pages…" style="flex:1;background:transparent;border:none;outline:none;font-family:var(--bbb-font-mono,monospace);font-size:16px;color:var(--bbb-fg,#E8EAF0);caret-color:var(--bbb-ai,#9B5DE5)"><span style="font-family:var(--bbb-font-mono,monospace);font-size:11px;color:var(--bbb-fg-dim,#4A4F60);padding:2px 6px;border:1px solid var(--bbb-border,#1F2330);border-radius:4px;flex-shrink:0">esc</span></div><div id="bbb-cmd-res" style="overflow-y:auto;flex:1;min-height:0"></div><div style="padding:8px 16px;border-top:1px solid var(--bbb-border,#1F2330);font-family:var(--bbb-font-mono,monospace);font-size:11px;color:var(--bbb-fg-dim,#4A4F60);flex-shrink:0">&#x21B5; go &nbsp;&middot;&nbsp; &#x2191;&#x2193; navigate &nbsp;&middot;&nbsp; esc close</div></div></div>
<script>
(function(){
  var ov,inp,res,cidx=-1,citems=[],ctmr=null;
  function cinit(){
    ov=document.getElementById('bbb-cmd-ov');
    inp=document.getElementById('bbb-cmd-inp');
    res=document.getElementById('bbb-cmd-res');
    document.addEventListener('keydown',function(e){
      if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();copen();return;}
      if(!ov||ov.style.display==='none')return;
      if(e.key==='Escape'){e.preventDefault();cclose();}
      else if(e.key==='ArrowDown'){e.preventDefault();cmove(1);}
      else if(e.key==='ArrowUp'){e.preventDefault();cmove(-1);}
      else if(e.key==='Enter'){e.preventDefault();cgo();}
    });
    ov.addEventListener('click',function(e){if(e.target===ov)cclose();});
    inp.addEventListener('input',function(){clearTimeout(ctmr);ctmr=setTimeout(function(){cfetch(inp.value);},150);});
  }
  function copen(){ov.style.display='flex';inp.value='';cidx=-1;cfetch('');setTimeout(function(){inp.focus();},30);}
  function cclose(){ov.style.display='none';}
  function cfetch(q){fetch('/api/search?q='+encodeURIComponent(q)).then(function(r){return r.json();}).then(function(d){crender(d,q);}).catch(function(){});}
  function cfz(s,q){if(!q)return true;var a=s.toLowerCase(),b=q.toLowerCase(),j=0;for(var i=0;i<a.length&&j<b.length;i++){if(a[i]===b[j])j++;}return j===b.length;}
  var CICONS={pages:'&#9723;',symbols:'&#9670;',trades:'&#10231;',cycles:'&#8857;'};
  var CLBLS={pages:'Pages',symbols:'Symbols',trades:'Trades',cycles:'Cycles'};
  function cesc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
  function crender(data,q){
    citems=[];var html='',tot=0;
    ['pages','symbols','trades','cycles'].forEach(function(sec){
      var arr=(data[sec]||[]).filter(function(it){return cfz((it.label||'')+' '+(it.subtitle||''),q);});
      if(!arr.length||tot>=8)return;
      arr=arr.slice(0,8-tot);
      html+='<div style="padding:4px 0">';
      html+='<div style="padding:5px 16px 3px;font-family:var(--bbb-font-mono,monospace);font-size:12px;letter-spacing:0.08em;text-transform:uppercase;color:var(--bbb-fg-dim,#4A4F60)">'+CLBLS[sec]+'</div>';
      arr.forEach(function(it){
        var ci=citems.length;
        citems.push({it:it,sec:sec});
        html+='<div class="bcr" data-ci="'+ci+'" style="height:40px;padding:0 16px;display:flex;align-items:center;cursor:pointer;gap:10px">';
        html+='<span style="font-size:13px;color:var(--bbb-fg-dim,#4A4F60);flex-shrink:0;width:16px;text-align:center">'+CICONS[sec]+'</span>';
        html+='<div style="flex:1;min-width:0">';
        html+='<div style="font-family:var(--bbb-font-mono,monospace);font-size:14px;color:var(--bbb-fg,#E8EAF0);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+cesc(it.label||'')+'</div>';
        if(it.subtitle)html+='<div style="font-family:var(--bbb-font-mono,monospace);font-size:12px;color:var(--bbb-fg-muted,#7B8090);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+cesc(it.subtitle)+'</div>';
        html+='</div>';
        html+='<span class="bcr-r" style="display:none;font-family:var(--bbb-font-mono,monospace);font-size:11px;color:var(--bbb-fg-dim,#4A4F60);padding:2px 5px;border:1px solid var(--bbb-border,#1F2330);border-radius:3px;flex-shrink:0">&#x21B5;</span>';
        html+='</div>';
        tot++;
      });
      html+='</div>';
    });
    if(!citems.length)html='<div style="padding:32px;text-align:center;font-family:var(--bbb-font-mono,monospace);font-size:13px;color:var(--bbb-fg-dim,#4A4F60)">No results</div>';
    res.innerHTML=html;
    cidx=citems.length?0:-1;
    cupd();
    res.querySelectorAll('.bcr').forEach(function(row){
      row.addEventListener('mouseenter',function(){cidx=parseInt(row.dataset.ci);cupd();});
      row.addEventListener('click',function(){cidx=parseInt(row.dataset.ci);cgo();});
    });
  }
  function cupd(){
    res.querySelectorAll('.bcr').forEach(function(row){
      var on=parseInt(row.dataset.ci)===cidx;
      row.style.background=on?'var(--bbb-surface-2,#181B26)':'';
      var r=row.querySelector('.bcr-r');if(r)r.style.display=on?'inline':'none';
    });
  }
  function cmove(d){
    if(!citems.length)return;
    cidx=(cidx+d+citems.length)%citems.length;
    cupd();
    var el=res.querySelector('.bcr[data-ci="'+cidx+'"]');
    if(el)el.scrollIntoView({block:'nearest'});
  }
  function cgo(){
    if(cidx<0||cidx>=citems.length)return;
    var e=citems[cidx],it=e.it,sec=e.sec,url='';
    if(sec==='pages')url=it.url;
    else if(sec==='symbols')url=it.url||('/trades?symbol='+encodeURIComponent(it.label||''));
    else if(sec==='trades')url=it.url||'/trades';
    else if(sec==='cycles')url=it.url||('/theater?cycle='+encodeURIComponent(it.cycle_id||''));
    if(url){cclose();window.location.href=url;}
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',cinit);
  else cinit();
})();
function bbbFlash(el){
  el.style.opacity='0.4';
  el.style.transition='opacity 200ms ease';
  requestAnimationFrame(function(){el.style.opacity='1.0';});
}
document.addEventListener('visibilitychange',function(){
  if(!document.hidden){
    document.querySelectorAll('.bbb-hero-number').forEach(function(el){bbbFlash(el);});
  }
});
</script>"""

# ── In-memory cache ───────────────────────────────────────────────────────────
_cache: dict = {}


def _cached(key: str, ttl: int = 60):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            now = time.time()
            entry = _cache.get(key)
            if entry and now - entry["ts"] < ttl:
                return entry["data"]
            result = fn(*args, **kwargs)
            _cache[key] = {"ts": now, "data": result}
            return result
        return wrapper
    return decorator


# ── HTTP Basic Auth ───────────────────────────────────────────────────────────
def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USER or auth.password != DASHBOARD_PASSWORD:
            return Response("Authentication required", 401,
                            {"WWW-Authenticate": 'Basic realm="BullBearBot"'})
        return f(*args, **kwargs)
    return decorated


# ── File helpers ──────────────────────────────────────────────────────────────
def _rj(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else {}


def _jsonl_last(path: Path, n: int = 1):
    try:
        lines = [ln for ln in path.read_text().strip().splitlines() if ln.strip()]
        return [json.loads(ln) for ln in lines[-n:]]
    except Exception:
        return []


# ── Alpaca data (cached 60 s) ─────────────────────────────────────────────────
@_cached("a1", ttl=60)
def _alpaca_a1():
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import OrderSide, OrderStatus, QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        c = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
        acc = c.get_account()
        pos = c.get_all_positions()
        orders = []
        try:
            orders = c.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200))
        except Exception:
            pass
        buys_today = sells_today = 0
        recent_orders = []
        try:
            today_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            closed = c.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED,
                                                    after=today_midnight, limit=200))
            filled = [o for o in closed if o.status == OrderStatus.FILLED]
            buys_today = sum(1 for o in filled if o.side == OrderSide.BUY)
            sells_today = sum(1 for o in filled if o.side == OrderSide.SELL)
        except Exception:
            pass
        try:
            recent_orders = list(c.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=20)))
        except Exception:
            pass
        return {"ok": True, "account": acc, "positions": pos, "orders": orders,
                "buys_today": buys_today, "sells_today": sells_today, "recent_orders": recent_orders}
    except Exception as e:
        return {"ok": False, "error": str(e), "account": None, "positions": [], "orders": [],
                "buys_today": 0, "sells_today": 0, "recent_orders": []}


@_cached("a2", ttl=60)
def _alpaca_a2():
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        c = TradingClient(ALPACA_KEY_OPT, ALPACA_SECRET_OPT, paper=True)
        acc = c.get_account()
        pos = c.get_all_positions()
        recent_orders = []
        try:
            recent_orders = list(c.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=15)))
        except Exception:
            pass
        return {"ok": True, "account": acc, "positions": pos, "recent_orders": recent_orders}
    except Exception as e:
        return {"ok": False, "error": str(e), "account": None, "positions": [], "recent_orders": []}


@_cached("pnl_a1", ttl=60)
def _today_pnl_a1() -> tuple:
    try:
        import requests as req
        r = req.get(
            "https://paper-api.alpaca.markets/v2/account/portfolio/history"
            "?period=1D&timeframe=1Min&extended_hours=true",
            headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            timeout=10,
        )
        eq = r.json().get("equity", [])
        if len(eq) >= 2 and eq[0]:
            pnl = eq[-1] - eq[0]
            return pnl, pnl / eq[0] * 100
    except Exception:
        pass
    return 0.0, 0.0


@_cached("pnl_a2", ttl=60)
def _today_pnl_a2() -> tuple:
    try:
        import requests as req
        r = req.get(
            "https://paper-api.alpaca.markets/v2/account/portfolio/history"
            "?period=1D&timeframe=1Min",
            headers={"APCA-API-KEY-ID": ALPACA_KEY_OPT, "APCA-API-SECRET-KEY": ALPACA_SECRET_OPT},
            timeout=10,
        )
        eq = r.json().get("equity", [])
        if len(eq) >= 2 and eq[0]:
            pnl = eq[-1] - eq[0]
            return pnl, pnl / eq[0] * 100
    except Exception:
        pass
    return 0.0, 0.0


# ── Bot file readers ──────────────────────────────────────────────────────────
def _last_decision():
    try:
        decisions = json.loads((BOT_DIR / "memory/decisions.json").read_text())
        for dec in reversed(decisions):
            r = dec.get("reasoning", "")
            if r and "gate skipped" not in r:
                return dec
        return decisions[-1] if decisions else {}
    except Exception:
        return {}


def _last_n_a1_decisions(n: int = 10) -> list:
    try:
        decisions = json.loads((BOT_DIR / "memory/decisions.json").read_text())
        valids = [d for d in decisions
                  if d.get("reasoning", "") and "gate skipped" not in d.get("reasoning", "")]
        return list(reversed(valids[-n:]))
    except Exception:
        return []


def _last_n_a2_decisions(n: int = 10) -> list:
    try:
        dec_dir = BOT_DIR / "data/account2/decisions"
        files = sorted(dec_dir.glob("a2_dec_*.json"))[-n:]
        result = []
        for f in reversed(files):
            try:
                result.append(json.loads(f.read_text()))
            except Exception:
                pass
        return result
    except Exception:
        return []


def _a2_churn_check() -> str:
    """Return warning HTML if A2 pipeline is looping on same symbol 5+ consecutive times."""
    try:
        decs = _last_n_a2_decisions(30)
        # Get top candidate symbol for each cycle (newest first from _last_n_a2_decisions)
        syms = []
        for d in decs:
            csets = d.get("candidate_sets", [])
            sel = d.get("selected_candidate")
            if sel and isinstance(sel, dict):
                syms.append(sel.get("symbol"))
            elif csets:
                syms.append(csets[0].get("symbol") if isinstance(csets[0], dict) else None)
            else:
                syms.append(None)
        # syms is newest-first; find trailing run from index 0
        if not syms or syms[0] is None:
            return ""
        trail_sym = syms[0]
        run = 0
        for s in syms:
            if s == trail_sym:
                run += 1
            else:
                break
        if run >= 5:
            return (
                f'<div style="background:rgba(251,191,36,.10);border:1px solid var(--bbb-warn);'
                f'border-radius:var(--bbb-r-2);padding:6px 14px;margin-bottom:8px;'
                f'font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-warn);'
                f'display:inline-block">&#x26A0; A2: {trail_sym} submitted {run}&times; '
                f'&mdash; possible pipeline loop</div>'
            )
        return ""
    except Exception:
        return ""


def _a2_last_cycle() -> dict:
    try:
        dec_dir = BOT_DIR / "data/account2/decisions"
        files = sorted(dec_dir.glob("a2_dec_*.json"))
        return json.loads(files[-1].read_text()) if files else {}
    except Exception:
        return {}


def _a2_structures() -> list:
    try:
        raw = json.loads((BOT_DIR / "data/account2/positions/structures.json").read_text())
        structs = [s for s in raw if isinstance(s, dict)]
        active_lc = {"fully_filled", "open", "submitted", "proposed"}
        return [s for s in structs if s.get("lifecycle") in active_lc]
    except Exception:
        return []


def _morning_brief() -> dict:
    try:
        return json.loads((BOT_DIR / "data/market/morning_brief.json").read_text())
    except Exception:
        return {}


def _morning_brief_time() -> str:
    try:
        mtime = (BOT_DIR / "data/market/morning_brief.json").stat().st_mtime
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc) + ET_OFFSET
        return dt.strftime("%-I:%M %p ET")
    except Exception:
        return "?"


def _todays_trades():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = []
    try:
        for line in (BOT_DIR / "logs/trades.jsonl").read_text().strip().splitlines():
            try:
                t = json.loads(line)
                if str(t.get("ts", "")).startswith(today):
                    out.append(t)
            except Exception:
                pass
    except Exception:
        pass
    return out


def _recent_errors(n_lines: int = 300, max_out: int = 5):
    try:
        lines = (BOT_DIR / "logs/bot.log").read_text().splitlines()[-n_lines:]
        errs = [ln.strip() for ln in lines
                if any(k in ln for k in ("  ERROR  ", "  WARNING  ", "  CRITICAL  "))]
        return errs[-max_out:]
    except Exception:
        return []


def _git_hash():
    try:
        head = (BOT_DIR / ".git/HEAD").read_text().strip()
        if head.startswith("ref: "):
            full = (BOT_DIR / ".git" / head[5:]).read_text().strip()
        else:
            full = head
        return full[:7] if len(full) >= 7 else full
    except Exception:
        try:
            r = subprocess.run(["/usr/bin/git", "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True, cwd=BOT_DIR, timeout=5)
            return r.stdout.strip() or "unknown"
        except Exception:
            return "unknown"


def _bbb_build_pill() -> str:
    _PILL_STYLE = (
        'background:var(--bbb-surface-2,#181B26);'
        'font-family:var(--bbb-font-mono,monospace);'
        'font-size:11px;color:var(--bbb-fg-muted,#7B8090);'
        'padding:2px 7px;border-radius:4px;display:inline-block'
    )
    try:
        r1 = subprocess.run(["/usr/bin/git", "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True, cwd=BOT_DIR, timeout=5)
        short_hash = r1.stdout.strip("\n").strip() if r1.returncode == 0 else "unknown"
        if not short_hash:
            short_hash = "unknown"
        r2 = subprocess.run(["/usr/bin/git", "log", "-1", "--format=%ci"],
                            capture_output=True, text=True, cwd=BOT_DIR, timeout=5)
        commit_date_str = r2.stdout.strip("\n").strip() if r2.returncode == 0 else ""
        days_ago: int | str = "?"
        if commit_date_str:
            import re as _re
            m = _re.match(r"(\d{4}-\d{2}-\d{2})", commit_date_str)
            if m:
                commit_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                today = datetime.now(timezone.utc).date()
                days_ago = (today - commit_date).days
        text = f"v0.4.2 · {short_hash} · shipped {days_ago}d ago"
        return f'<span style="{_PILL_STYLE}">{text}</span>'
    except Exception:
        return f'<span style="{_PILL_STYLE}">build unknown</span>'


def _service_uptime():
    try:
        r = subprocess.run(["systemctl", "show", "trading-bot", "--property=ActiveEnterTimestamp"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip().split("=", 1)[-1]
    except Exception:
        return "unknown"


def _earnings_flags():
    cal = _rj(BOT_DIR / "data/market/earnings_calendar.json", default={})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    flags: dict[str, str] = {}
    events = cal if isinstance(cal, list) else cal.get("events", cal.get("earnings", []))
    for ev in (events if isinstance(events, list) else []):
        sym = ev.get("symbol", ev.get("ticker", ""))
        date_str = ev.get("date", ev.get("report_date", ""))
        if sym and date_str:
            if date_str.startswith(today):
                flags[sym] = "EARNINGS TODAY"
            elif date_str.startswith(tomorrow):
                flags[sym] = "EARNINGS TOMORROW"
    return flags


def _stop_map(orders):
    stops: dict[str, float] = {}
    for o in orders:
        try:
            side = str(getattr(o, "side", "")).lower()
            otype = str(getattr(o, "type", "")).lower()
            sym = getattr(o, "symbol", "")
            sp = getattr(o, "stop_price", None)
            if "sell" in side and ("stop" in otype or "trail" in otype) and sp:
                stops[sym] = float(sp)
        except Exception:
            pass
    return stops


def _tp_map(orders):
    tps: dict[str, float] = {}
    for o in orders:
        try:
            side = str(getattr(o, "side", "")).lower()
            otype = str(getattr(o, "type", "")).lower()
            sym = getattr(o, "symbol", "")
            lp = getattr(o, "limit_price", None)
            if "sell" in side and otype == "limit" and lp:
                lp_f = float(lp)
                # keep the highest limit price per symbol (TP, not a trim)
                if sym not in tps or lp_f > tps[sym]:
                    tps[sym] = lp_f
        except Exception:
            pass
    return tps


def _qualitative_context() -> dict:
    try:
        return json.loads((BOT_DIR / "data/market/qualitative_context.json").read_text())
    except Exception:
        return {}


# ── Thesis helpers ────────────────────────────────────────────────────────────
def _a1_top_theses(decisions: list, qctx: dict) -> list:
    seen: dict[str, dict] = {}
    for d in decisions:
        actions = d.get("actions", d.get("ideas", []))
        ts = d.get("ts", "")
        for a in actions:
            sym = a.get("symbol", "")
            intent = (a.get("action") or a.get("intent") or "").upper()
            if sym and sym not in seen and intent not in ("HOLD", "WATCH", "MONITOR", "OBSERVE"):
                seen[sym] = {"symbol": sym, "intent": intent, "ts": ts}
    sym_ctx = qctx.get("symbol_context", {}) if isinstance(qctx, dict) else {}
    result = []
    for sym, info in list(seen.items())[:8]:
        ctx = (sym_ctx.get(sym) or {}) if isinstance(sym_ctx, dict) else {}
        narrative = (ctx.get("narrative", "") or "")[:220]
        tags = ctx.get("thesis_tags", []) or []
        result.append({
            "symbol": sym,
            "intent": info["intent"],
            "ts": info["ts"],
            "narrative": narrative,
            "tags": tags[:4],
            "catalyst_active": ctx.get("catalyst_active", False),
        })
        if len(result) >= 5:
            break
    return result


def _a2_top_theses(a2_decs: list) -> list:
    result = []
    for d in a2_decs[:5]:
        cand = d.get("selected_candidate") or {}
        if not isinstance(cand, dict) or not cand:
            continue
        sym = cand.get("symbol", "")
        strategy = cand.get("structure_type", cand.get("strategy", ""))
        debate = d.get("debate_parsed") or {}
        conf = debate.get("confidence", "?") if isinstance(debate, dict) else "?"
        reasons = debate.get("reasons", []) if isinstance(debate, dict) else []
        if isinstance(reasons, str):
            reasons = [reasons]
        result.append({
            "symbol": sym,
            "strategy": strategy,
            "confidence": conf,
            "reasons": (reasons or [])[:2],
            "ts": d.get("built_at", ""),
            "result": d.get("execution_result", "?"),
        })
    return result


# ── Formatting helpers ────────────────────────────────────────────────────────
def _to_et(ts_str: str) -> str:
    if not ts_str:
        return "—"
    try:
        s = ts_str.replace("Z", "+00:00")
        if "T" not in s:
            s = s[:19].replace(" ", "T") + "+00:00"
        ts = datetime.fromisoformat(s)
        et = ts + ET_OFFSET
        return et.strftime("%-m/%-d %-I:%M %p ET")
    except Exception:
        return ts_str[:16]


def _freshness_stamp(ts_str: str, warn_min: int = 60, crit_min: int = 240) -> str:
    """Relative-time freshness chip: '5m ago' in --bbb-fg-dim / --bbb-warn / --bbb-loss."""
    if not ts_str:
        return ""
    try:
        s = ts_str.replace("Z", "+00:00")
        if "T" not in s:
            s = s[:19].replace(" ", "T") + "+00:00"
        age_s = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(s)).total_seconds())
        age_m = int(age_s / 60)
        if age_m < 60:
            label = f"{age_m}m ago"
        elif age_m < 1440:
            h, m = divmod(age_m, 60)
            label = f"{h}h {m}m ago" if m else f"{h}h ago"
        else:
            label = f"{age_m // 1440}d ago"
        if age_m < warn_min:
            color = "var(--bbb-fg-dim)"
        elif age_m < crit_min:
            color = "var(--bbb-warn)"
        else:
            color = "var(--bbb-loss)"
        return (
            '<span style="font-family:var(--bbb-font-mono);font-size:11px;color:' + color + '">'
            + label + '</span>'
        )
    except Exception:
        return ""


def _now_et() -> str:
    return (datetime.now(timezone.utc) + ET_OFFSET).strftime("%-m/%-d %-I:%M:%S %p ET")


def _fm(v, prefix="$") -> str:
    try:
        f = float(v)
        sign = "-" if f < 0 else ""
        return f"{sign}{prefix}{abs(f):,.2f}"
    except Exception:
        return "N/A"


def _fp(v, decimals: int = 2) -> str:
    try:
        return f"{float(v):.{decimals}f}%"
    except Exception:
        return "N/A"


def _mode_color(mode_str: str) -> str:
    m = mode_str.lower()
    if m == "normal":
        return "#3fb950"
    if "halt" in m:
        return "#f85149"
    if "risk" in m or "contain" in m or "reconcile" in m:
        return "#d29922"
    return "#8b949e"


def _trail_status_badge(entry: float, stop: float) -> str:
    if not entry or not stop:
        return ""
    ratio = stop / entry
    if ratio >= 1.001:
        return '<span class="flag flag-trail">PROFIT TRAIL</span>'
    if ratio >= 0.998:
        return '<span class="flag flag-be">BREAKEVEN</span>'
    return ""


def _is_market_hours() -> bool:
    et = datetime.now(timezone.utc) + ET_OFFSET
    if et.weekday() >= 5:
        return False
    h, m = et.hour, et.minute
    return (h == 9 and m >= 30) or (10 <= h < 16)


# ── A2 qualitative helpers ────────────────────────────────────────────────────
def _iv_env_label(iv_rank) -> str:
    if iv_rank is None:
        return "unknown"
    r = float(iv_rank)
    if r < 15:   return "very cheap"
    if r < 35:   return "cheap"
    if r < 65:   return "neutral"
    if r < 80:   return "expensive"
    return "very expensive"


def _parse_net_debit(structure: dict):
    for entry in structure.get("audit_log", []):
        msg = entry.get("msg", "")
        if "net_debit=" in msg:
            try:
                return float(msg.split("net_debit=")[1].split()[0])
            except Exception:
                pass
    return None


def _calc_dte(expiry_str: str):
    try:
        return (date.fromisoformat(expiry_str) - date.today()).days
    except Exception:
        return None


def _build_a2_position_cards(structures: list, a2_live_positions: list) -> list:
    occ_pnl: dict[str, float] = {}
    for p in a2_live_positions:
        sym = getattr(p, "symbol", "")
        unreal = float(getattr(p, "unrealized_pl", 0) or 0)
        if sym:
            occ_pnl[sym] = unreal

    cards = []
    seen = set()
    for struct in structures:
        sid = struct.get("structure_id", "")
        if sid in seen:
            continue
        underlying = struct.get("underlying", "")
        strategy = struct.get("strategy", "")
        expiry_str = struct.get("expiration", "")
        long_strike = struct.get("long_strike")
        short_strike = struct.get("short_strike")
        max_cost = struct.get("max_cost_usd")
        max_profit = struct.get("max_profit_usd")
        iv_rank = struct.get("iv_rank")
        direction = struct.get("direction", "")
        legs = struct.get("legs", [])

        net_pnl = 0.0
        matched = False
        for leg in legs:
            occ = leg.get("occ_symbol", "")
            if occ in occ_pnl:
                net_pnl += occ_pnl[occ]
                matched = True
        if not matched:
            continue
        seen.add(sid)

        net_debit = _parse_net_debit(struct)
        dte = _calc_dte(expiry_str)
        dte_str = f"{dte} DTE" if dte is not None else "?"
        iv_env = _iv_env_label(iv_rank)
        iv_rank_str = f"{iv_rank:.1f}" if iv_rank is not None else "?"
        max_loss_str = _fm(max_cost) if max_cost else "N/A"
        is_single = "single" in strategy
        max_gain_str = _fm(max_profit) if max_profit else ("unlimited" if is_single else "N/A")
        net_pnl_pct = (net_pnl / max_cost * 100) if max_cost and max_cost > 0 else 0.0
        pnl_sign = "+" if net_pnl >= 0 else ""
        pnl_color = "#3fb950" if net_pnl >= 0 else "#f85149"
        pnl_str = f"{pnl_sign}{_fm(net_pnl)} ({pnl_sign}{net_pnl_pct:.1f}%)"
        s = strategy
        ls = long_strike
        ss = short_strike

        if "call_debit_spread" in s or ("debit" in s and "call" in s):
            breakeven = (ls + net_debit) if ls and net_debit else None
            s_range = f"${ls:.0f}/${ss:.0f}" if ss else f"${ls:.0f}"
            title = f"{underlying} {s_range} Call Debit Spread — {expiry_str} ({dte_str})"
            profit_line = (f"Profit if {underlying} &gt; ${breakeven:.2f} (breakeven)"
                           if breakeven else f"Profit if {underlying} rises above ${ss:.0f}" if ss else f"Profit if {underlying} rises")
            rationale = f"IV rank {iv_rank_str} ({iv_env}) — debit call spread. Max risk = premium paid."
        elif "put_debit_spread" in s or ("debit" in s and "put" in s):
            breakeven = (ls - net_debit) if ls and net_debit else None
            s_range = f"${ls:.0f}/${ss:.0f}" if ss else f"${ls:.0f}"
            title = f"{underlying} {s_range} Put Debit Spread — {expiry_str} ({dte_str})"
            profit_line = (f"Profit if {underlying} &lt; ${breakeven:.2f} (breakeven)"
                           if breakeven else f"Profit if {underlying} falls")
            rationale = f"IV rank {iv_rank_str} ({iv_env}) — put debit spread. Bearish thesis."
        elif "call_credit_spread" in s:
            s_range = f"${ls:.0f}/${ss:.0f}" if ss else f"${ls:.0f}"
            title = f"{underlying} {s_range} Call Credit Spread — {expiry_str} ({dte_str})"
            profit_line = f"Profit if {underlying} stays below ${ss:.0f}" if ss else "Profit if flat/down"
            rationale = f"IV rank {iv_rank_str} ({iv_env}) — selling call premium when vol is elevated."
        elif "put_credit_spread" in s:
            s_range = f"${ls:.0f}/${ss:.0f}" if ss else f"${ls:.0f}"
            title = f"{underlying} {s_range} Put Credit Spread — {expiry_str} ({dte_str})"
            profit_line = f"Profit if {underlying} stays above ${ss:.0f}" if ss else "Profit if flat/up"
            rationale = f"IV rank {iv_rank_str} ({iv_env}) — selling put premium."
        elif "single_call" in s:
            breakeven = (ls + net_debit) if ls and net_debit else None
            title = f"{underlying} ${ls:.0f} Call — {expiry_str} ({dte_str})"
            profit_line = (f"Profit if {underlying} &gt; ${breakeven:.2f}" if breakeven
                           else f"Profit if {underlying} rises above ${ls:.0f}")
            rationale = f"IV rank {iv_rank_str} ({iv_env}) — long call, {direction} thesis."
        elif "single_put" in s:
            breakeven = (ls - net_debit) if ls and net_debit else None
            title = f"{underlying} ${ls:.0f} Put — {expiry_str} ({dte_str})"
            profit_line = (f"Profit if {underlying} &lt; ${breakeven:.2f}" if breakeven
                           else f"Profit if {underlying} falls below ${ls:.0f}")
            rationale = f"IV rank {iv_rank_str} ({iv_env}) — long put, bearish/protective."
        else:
            title = f"{underlying} {s.replace('_', ' ').title()} — {expiry_str} ({dte_str})"
            profit_line = f"{direction.title()} thesis on {underlying}"
            rationale = f"IV rank {iv_rank_str} ({iv_env})"

        cards.append({
            "title": title, "strategy_label": s.replace("_", " ").title(),
            "iv_env": iv_env, "iv_rank_str": iv_rank_str,
            "profit_line": profit_line, "max_gain_str": max_gain_str, "max_loss_str": max_loss_str,
            "pnl_str": pnl_str, "pnl_color": pnl_color, "rationale": rationale,
            "progress_html": _a2_position_progress_html(net_pnl, max_cost, max_profit),
        })
    return cards[:10]


def _fmt_orders_html(recent_orders, is_options: bool = False, limit: int = 6) -> str:
    html = ""
    count = 0
    for o in (recent_orders or []):
        if count >= limit:
            break
        try:
            sym = getattr(o, "symbol", None) or "MLEG"
            side_raw = str(getattr(o, "side", "")).lower()
            qty = getattr(o, "qty", "?")
            filled_price = getattr(o, "filled_avg_price", None)
            status_raw = str(getattr(o, "status", "")).lower()
            created_raw = str(getattr(o, "created_at", ""))
            status_str = status_raw.split(".")[-1]
            side_str = side_raw.split(".")[-1].upper()
            ts_et = _to_et(created_raw)
            unit = "ct" if is_options else "sh"
            if status_str == "filled" and side_str == "BUY":
                icon, color = "&#x2705;", "#3fb950"
                price_part = f"@ {_fm(filled_price)}" if filled_price else ""
            elif status_str == "filled" and side_str in ("SELL", "SELL_SHORT"):
                icon, color = "&#x1F534;", "#f85149"
                price_part = f"@ {_fm(filled_price)}" if filled_price else ""
            elif status_str in ("canceled", "cancelled", "rejected"):
                icon, color, price_part = "&#x26A0;&#xFE0F;", "#d29922", status_str.upper()
            elif status_str in ("new", "held", "accepted", "pending_new", "partially_filled"):
                icon, color, price_part = "&#x23F3;", "#8b949e", "pending"
            else:
                icon, color, price_part = "&middot;", "#8b949e", status_str
            html += (
                f'<div style="font-size:12px;color:{color};padding:4px 0;'
                f'border-bottom:1px solid #21262d;font-family:monospace">'
                f'{icon} {side_str} {sym} {qty}{unit} {price_part} '
                f'<span style="color:#8b949e">[{ts_et}]</span></div>'
            )
            count += 1
        except Exception:
            pass
    return html or '<div style="color:#8b949e;font-size:13px">No recent orders</div>'


# ── New UX helpers ────────────────────────────────────────────────────────────
def _morning_brief_mtime_float() -> float:
    try:
        return (BOT_DIR / "data/market/morning_brief.json").stat().st_mtime
    except Exception:
        return 0.0


def _intelligence_brief_full() -> dict:
    try:
        return json.loads((BOT_DIR / "data/market/morning_brief_full.json").read_text())
    except Exception:
        return {}


def _brief_staleness_html(mtime_float: float) -> str:
    if not mtime_float or not _is_market_hours():
        return ""
    age_h = (time.time() - mtime_float) / 3600
    if age_h < 2:
        return ""
    if age_h < 6:
        return f' <span style="color:#d29922;font-size:11px">&#x26A0;&#xFE0F; {age_h:.0f}h ago</span>'
    return f' <span style="color:#f85149;font-size:11px">&#x1F534; {age_h:.0f}h ago (stale)</span>'


def _a2_position_progress_html(net_pnl: float, max_cost, max_profit) -> str:
    try:
        if not max_cost or max_cost <= 0:
            return ""
        max_l = float(max_cost)
        max_p = float(max_profit) if max_profit else max_l
        span = max_l + max_p
        if span <= 0:
            return ""
        clamped = max(-max_l, min(max_p, net_pnl))
        pos_pct = (clamped + max_l) / span * 100
        stop_pct = (max_l * 0.5) / span * 100
        be_pct = max_l / span * 100
        target_pct = (max_l + max_p * 0.8) / span * 100
        fill_color = "#3fb950" if net_pnl >= 0 else "#f85149"
        if net_pnl >= 0:
            fill_from, fill_to = be_pct, pos_pct
        else:
            fill_from, fill_to = pos_pct, be_pct
        fill_width = abs(fill_to - fill_from)
        dist_to_target = max_p * 0.8 - net_pnl
        dist_to_stop = net_pnl - (-max_l * 0.5)
        dist_to_stop_sign = "+" if dist_to_stop >= 0 else ""
        dist_to_target_arrow = "&#x2191; " if dist_to_target > 0 else "&#x2713; "
        return (
            f'<div style="position:relative;height:18px;background:#21262d;border-radius:4px;margin:8px 0;overflow:hidden">'
            f'<div style="position:absolute;left:{fill_from:.1f}%;width:{fill_width:.1f}%;height:100%;background:{fill_color};opacity:0.6"></div>'
            f'<div style="position:absolute;left:{stop_pct:.1f}%;top:0;width:2px;height:100%;background:#f85149;opacity:0.8" title="50% stop"></div>'
            f'<div style="position:absolute;left:{be_pct:.1f}%;top:0;width:2px;height:100%;background:#58a6ff;opacity:0.8" title="breakeven"></div>'
            f'<div style="position:absolute;left:{target_pct:.1f}%;top:0;width:2px;height:100%;background:#3fb950;opacity:0.8" title="80% target"></div>'
            f'<div style="position:absolute;left:calc({pos_pct:.1f}% - 3px);top:3px;width:6px;height:12px;background:#fff;border-radius:2px;opacity:0.9"></div>'
            f'</div>'
            f'<div style="font-size:10px;color:#8b949e;display:flex;justify-content:space-between;margin-bottom:3px">'
            f'<span style="color:#f85149">-{_fm(max_l)}</span>'
            f'<span style="color:#58a6ff">BE</span>'
            f'<span style="color:#3fb950">+{_fm(max_p*0.8)}</span>'
            f'<span>+{_fm(max_p)}</span>'
            f'</div>'
            f'<div style="font-size:11px;color:#8b949e">'
            f'{dist_to_target_arrow}{_fm(abs(dist_to_target))} to target &nbsp;|&nbsp; '
            f'{dist_to_stop_sign}{_fm(abs(dist_to_stop))} margin above stop'
            f'</div>'
        )
    except Exception:
        return ""


def _trail_table_html(positions: list, trail_tiers: list) -> str:
    if not positions:
        return '<div style="color:#8b949e;font-size:13px">No open positions.</div>'
    rows = ""
    for p in positions:
        entry = p.get("entry", 0.0)
        current = p.get("current", 0.0)
        stop = p.get("stop")
        if not entry or not current:
            continue
        sym = p["symbol"]
        gain_pct = (current - entry) / entry * 100
        current_tier_idx = -1
        for i, tier in enumerate(trail_tiers):
            if gain_pct >= tier.get("gain_pct", 0) * 100:
                current_tier_idx = i
        if gain_pct < 0 or current_tier_idx < 0:
            tier_label = "No trail"
            tier_color = "#8b949e"
        else:
            stop_floor = trail_tiers[current_tier_idx].get("stop_pct", 0) * 100
            tier_label = f"T{current_tier_idx+1} (stop &ge;+{stop_floor:.0f}%)"
            tier_color = "#3fb950"
        next_idx = current_tier_idx + 1
        if next_idx < len(trail_tiers):
            nt = trail_tiers[next_idx]
            trig_price = entry * (1 + nt.get("gain_pct", 0))
            next_trigger = f"${trig_price:.2f} (+{nt.get('gain_pct',0)*100:.0f}%)"
        elif trail_tiers and current_tier_idx < 0:
            t1 = trail_tiers[0]
            trig_price = entry * (1 + t1.get("gain_pct", 0))
            next_trigger = f"${trig_price:.2f} (+{t1.get('gain_pct',0)*100:.0f}%)"
        else:
            next_trigger = "max tier"
        if stop is None:
            stop_str, stop_color = "—", "#f85149"
        elif stop >= entry * 1.001:
            stop_str, stop_color = f"${stop:.2f}", "#3fb950"
        elif stop >= entry * 0.998:
            stop_str, stop_color = f"${stop:.2f}", "#58a6ff"
        else:
            stop_str, stop_color = f"${stop:.2f}", "#d29922"
        gain_color = "#3fb950" if gain_pct >= 0 else "#f85149"
        gain_sign = "+" if gain_pct >= 0 else ""
        rows += (
            f'<tr>'
            f'<td><b>{sym}</b></td>'
            f'<td style="color:{gain_color}">{gain_sign}{gain_pct:.1f}%</td>'
            f'<td style="color:{tier_color}">{tier_label}</td>'
            f'<td style="color:#8b949e">{next_trigger}</td>'
            f'<td style="color:{stop_color}">{stop_str}</td>'
            f'</tr>'
        )
    if not rows:
        return '<div style="color:#8b949e;font-size:13px">No position data.</div>'
    return (
        '<div class="table-wrap"><table class="trail-table">'
        '<thead><tr><th>Symbol</th><th>Gain %</th><th>Trail Tier</th><th>Next Trigger</th><th>Stop</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )


def _allocator_shadow_compact() -> str:
    try:
        path = BOT_DIR / "data/analytics/portfolio_allocator_shadow.jsonl"
        entries = _jsonl_last(path, n=1)
        if not entries:
            return ""
        entry = entries[0]
        actions = entry.get("proposed_actions", [])
        if not actions:
            return ""
        ts = entry.get("timestamp", "")
        ts_label = ""
        if ts:
            try:
                s = ts.replace("Z", "+00:00")
                if "T" not in s:
                    s = s[:19].replace(" ", "T") + "+00:00"
                dt = datetime.fromisoformat(s)
                et = dt + ET_OFFSET
                ts_label = et.strftime("%-I:%M %p")
            except Exception:
                ts_label = ""
        parts = [f"{a.get('action','').upper()} {a.get('symbol','')}"
                 for a in actions if a.get("action","").upper() != "HOLD" and a.get("symbol")]
        if not parts:
            parts = [f"{a.get('action','').upper()} {a.get('symbol','')}"
                     for a in actions[:3] if a.get("symbol")]
        action_str = " | ".join(parts[:5])
        if len(actions) > 5:
            action_str += f" +{len(actions)-5}"
        ts_part = (f' <span style="color:#8b949e">[updated {ts_label}]</span>' if ts_label else "")
        return (
            f'<div style="font-size:13px;padding:8px 0">'
            f'<span style="color:#d29922;font-weight:600;font-family:monospace">ALLOCATOR (shadow):</span> '
            f'<span style="color:#c9d1d9;font-family:monospace">{action_str}</span>{ts_part}'
            f'</div>'
        )
    except Exception:
        return ""


def _allocator_chart_data() -> dict:
    try:
        path = BOT_DIR / "data/analytics/portfolio_allocator_shadow.jsonl"
        entries = _jsonl_last(path, n=1)
        if not entries:
            return {}
        entry = entries[0]
        holdings = entry.get("current_holdings_snapshot", [])
        if not holdings:
            return {}
        target_weights = entry.get("target_weights", {})
        proposed_actions = entry.get("proposed_actions", [])
        ts = entry.get("timestamp", "")
        action_map = {a.get("symbol", ""): a.get("action", "HOLD").upper()
                      for a in proposed_actions if a.get("symbol")}
        symbols = [h["symbol"] for h in holdings if h.get("symbol")]
        current_pct = {h["symbol"]: round(float(h.get("account_pct", 0)), 2) for h in holdings}
        target_pct = {sym: round(float(target_weights.get(sym, 0)) * 100, 1) for sym in symbols}
        actions = {sym: action_map.get(sym, "HOLD") for sym in symbols}
        ts_label = ""
        if ts:
            try:
                s = ts.replace("Z", "+00:00")
                if "T" not in s:
                    s = s[:19].replace(" ", "T") + "+00:00"
                dt = datetime.fromisoformat(s)
                et = dt + ET_OFFSET
                ts_label = et.strftime("%-I:%M %p ET")
            except Exception:
                pass
        return {"symbols": symbols, "current_pct": current_pct,
                "target_pct": target_pct, "actions": actions, "ts_label": ts_label}
    except Exception:
        return {}


def _alloc_chart_html(data: dict, fallback_text: str = "") -> str:
    if not data or not data.get("symbols"):
        if fallback_text:
            return (
                '<div class="section-label">Allocator</div>'
                '<div class="card" style="padding:10px 14px;font-family:var(--bbb-font-mono);'
                'font-size:12px;color:var(--bbb-fg-muted)">'
                + fallback_text +
                '</div>'
            )
        return ""
    symbols = data["symbols"]
    cur = data["current_pct"]
    tgt = data["target_pct"]
    actions = data["actions"]
    ts_label = data.get("ts_label", "")
    import json as _json
    labels_js = _json.dumps(symbols)
    cur_js = _json.dumps([cur.get(s, 0) for s in symbols])
    tgt_js = _json.dumps([tgt.get(s, 0) for s in symbols])
    act_color = {"ADD": "#34D399", "TRIM": "#F87171", "HOLD": "#7B8090", "REPLACE": "#FBBF24"}
    act_arrow = {"ADD": "\u2191", "TRIM": "\u2193", "HOLD": "\u2192", "REPLACE": "\u21bb"}
    tiles = ""
    for sym in symbols:
        act = actions.get(sym, "HOLD")
        col = act_color.get(act, "#7B8090")
        arrow = act_arrow.get(act, "\u2192")
        tiles += (
            f'<div style="display:inline-flex;align-items:center;gap:3px;'
            f'padding:2px 7px;border-radius:3px;border:1px solid {col}44;margin:2px 2px 0 0">'
            f'<span style="font-family:var(--bbb-font-mono);font-size:11px;color:{col}">{arrow}</span>'
            f'<span style="font-family:var(--bbb-font-mono);font-size:11px;'
            f'color:var(--bbb-fg-muted);margin-left:2px">{sym}</span>'
            f'</div>'
        )
    ts_part = (
        f'<span style="font-size:10px;color:var(--bbb-fg-dim);margin-left:8px">{ts_label}</span>'
        if ts_label else ""
    )
    chart_h = max(160, len(symbols) * 28 + 40)
    return (
        '<div class="section-label" style="display:flex;align-items:center;gap:8px;margin-bottom:6px">' +
        'Allocator' +
        '<span style="font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-fg-dim);' +
        'text-transform:none;letter-spacing:0.03em">shadow \u2014 not executing</span>' +
        ts_part +
        '</div>' +
        '<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);' +
        'border-radius:var(--bbb-r-3);padding:12px 16px 10px;margin-bottom:var(--bbb-s-4)">' +
        f'<div style="position:relative;height:{chart_h}px;width:100%"><canvas id="allocChart"></canvas></div>' +
        f'<div style="margin-top:8px;display:flex;flex-wrap:wrap">{tiles}</div>' +
        '</div>' +
        f'<script>window.addEventListener("load",function(){{' +
        f'if(window._acd)return;window._acd=true;' +
        f'var ctx=document.getElementById("allocChart");if(!ctx)return;' +
        f'new Chart(ctx,{{type:"bar",data:{{labels:{labels_js},' +
        f'datasets:[{{label:"Current %",data:{cur_js},backgroundColor:"rgba(96,165,250,0.65)",' +
        f'borderColor:"rgba(96,165,250,0.9)",borderWidth:1,borderRadius:2,borderSkipped:false}},' +
        f'{{label:"Target ceiling %",data:{tgt_js},backgroundColor:"rgba(96,165,250,0.08)",' +
        f'borderColor:"rgba(96,165,250,0.45)",borderWidth:2,borderRadius:2,borderSkipped:false}}]}},' +
        f'options:{{indexAxis:"y",responsive:true,maintainAspectRatio:false,' +
        f'plugins:{{legend:{{display:true,labels:{{color:"#7B8090",' +
        f'font:{{family:"JetBrains Mono,monospace",size:10}},boxWidth:10,padding:12}}}},' +
        f'tooltip:{{callbacks:{{label:function(c){{return c.dataset.label+": "+c.raw.toFixed(1)+"%";}}}},' +
        f'backgroundColor:"#181B26",borderColor:"#1F2330",borderWidth:1}}}},' +
        f'scales:{{x:{{max:25,grid:{{color:"rgba(31,35,48,0.8)"}},' +
        f'ticks:{{color:"#4A4F60",font:{{family:"JetBrains Mono,monospace",size:10}},' +
        f'callback:function(v){{return v+"%";}}}}}},' +
        f'y:{{grid:{{display:false}},' +
        f'ticks:{{color:"#7B8090",font:{{family:"JetBrains Mono,monospace",size:11}}}}}}}}}}}}' +
        f'}});</script>'
    )



@_cached("equity_curve", ttl=3600)
def _equity_curve_data() -> dict:
    """Fetch A1+A2 daily equity from Alpaca portfolio history. Returns {dates, a1, a2, combined}."""
    try:
        import requests as _req
        from datetime import datetime as _dt
        LAUNCH_TS = 1776038400  # 2026-04-13 00:00 UTC
        rows = {}  # date_str -> {a1, a2, ts}
        for name, k, s in [("a1", ALPACA_KEY, ALPACA_SECRET), ("a2", ALPACA_KEY_OPT, ALPACA_SECRET_OPT)]:
            r = _req.get(
                "https://paper-api.alpaca.markets/v2/account/portfolio/history",
                params={"period": "1M", "timeframe": "1D"},
                headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s},
                timeout=10
            )
            if not r.ok:
                continue
            d = r.json()
            for ts, eq in zip(d.get("timestamp", []), d.get("equity", [])):
                if ts < LAUNCH_TS:
                    continue
                label = _dt.fromtimestamp(ts).strftime("%b %-d")
                if label not in rows:
                    rows[label] = {"a1": 0.0, "a2": 0.0, "ts": ts}
                rows[label][name] = float(eq or 0)
        if not rows:
            return {}
        ordered = sorted(rows.items(), key=lambda x: x[1]["ts"])
        return {
            "dates":    [k for k, _ in ordered],
            "a1":       [v["a1"] for _, v in ordered],
            "a2":       [v["a2"] for _, v in ordered],
            "combined": [v["a1"] + v["a2"] for _, v in ordered],
        }
    except Exception:
        return {}


def _equity_curve_html(data: dict) -> str:
    if not data or not data.get("dates"):
        return ""
    import json as _j
    labels_js = _j.dumps(data["dates"])
    a1_js = _j.dumps(data["a1"])
    a2_js = _j.dumps(data["a2"])
    comb_js = _j.dumps(data["combined"])
    # Find y-axis min (floor to nearest $1000 below min)
    all_vals = [v for v in data["combined"] + data["a1"] + data["a2"] if v > 0]
    y_min = (int(min(all_vals) / 1000) * 1000 - 1000) if all_vals else 190000
    y_max = (int(max(all_vals) / 1000) * 1000 + 2000) if all_vals else 220000
    return (
        '<div style="margin-bottom:var(--bbb-s-4)">'
        '<div class="section-label">Portfolio Equity Curve'
        '<span style="font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-fg-dim);'
        'text-transform:none;letter-spacing:0.03em;margin-left:8px">daily · inception to today</span>'
        '</div>'
        '<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
        'border-radius:var(--bbb-r-3);padding:12px 16px">'
        '<div style="position:relative;height:180px;width:100%"><canvas id="equityChart"></canvas></div>'
        f'<script>window.addEventListener("load",function(){{'
        f'if(window._ecd)return;window._ecd=true;'
        f'var ctx=document.getElementById("equityChart");if(!ctx)return;'
        f'new Chart(ctx,{{type:"line",'
        f'data:{{labels:{labels_js},'
        f'datasets:['
        f'{{label:"Combined",data:{comb_js},borderColor:"#E8EAF0",borderWidth:1.5,'
        f'pointRadius:0,tension:0.2,fill:false}},'
        f'{{label:"A1",data:{a1_js},borderColor:"#60A5FA",borderWidth:1,pointRadius:0,tension:0.2,fill:false}},'
        f'{{label:"A2",data:{a2_js},borderColor:"#FBBF24",borderWidth:1,pointRadius:0,tension:0.2,fill:false}}'
        f']}},'
        f'options:{{responsive:true,maintainAspectRatio:false,'
        f'plugins:{{legend:{{display:true,labels:{{color:"#7B8090",'
        f'font:{{family:"JetBrains Mono,monospace",size:10}},boxWidth:8,padding:10}}}},'
        f'tooltip:{{callbacks:{{label:function(c){{var v=c.raw;'
        f'return c.dataset.label+": $"+(v?v.toLocaleString("en-US",{{minimumFractionDigits:0,maximumFractionDigits:0}}):"0");}}}},'
        f'backgroundColor:"#181B26",borderColor:"#1F2330",borderWidth:1}}}},'
        f'scales:{{x:{{grid:{{color:"rgba(31,35,48,0.5)"}},'
        f'ticks:{{color:"#4A4F60",font:{{family:"JetBrains Mono,monospace",size:9}}}}}},'
        f'y:{{min:{y_min},max:{y_max},grid:{{color:"rgba(31,35,48,0.8)"}},'
        f'ticks:{{color:"#4A4F60",font:{{family:"JetBrains Mono,monospace",size:9}},'
        f'callback:function(v){{return "$"+(v/1000).toFixed(0)+"K";}}}}}}}}}}}}'
        f'}});</script>'
        '</div></div>'
    )


@_cached("intraday_bars", ttl=300)
def _intraday_bars_a1() -> dict:
    """Fetch today's 5-min bars for all A1 positions. Returns {bars:{sym:[prices]}, label:str}."""
    try:
        import requests as _req
        d = _alpaca_a1()
        positions = d.get("positions", [])
        if not positions:
            return {"bars": {}, "label": "today"}
        syms = [p.symbol for p in positions]
        now_utc = datetime.now(timezone.utc)
        now_et  = now_utc + ET_OFFSET
        market_open_et  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
        market_close_et = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
        market_open_utc = market_open_et - ET_OFFSET

        # Determine date range.  IEX requires an explicit end to return all symbols.
        market_is_open = market_open_et <= now_et < market_close_et
        if market_is_open:
            start_iso = market_open_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            end_iso   = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            label = "today"
        else:
            # Pre-market → previous trading day.  Post-market → today's session.
            if now_et >= market_close_et:
                candidate = market_open_utc           # today's open (session just ended)
            else:
                candidate = market_open_utc - timedelta(days=1)
            while candidate.weekday() >= 5:           # skip weekends back
                candidate -= timedelta(days=1)
            start_iso = candidate.strftime("%Y-%m-%dT%H:%M:%SZ")
            end_iso   = (candidate + timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
            label = "prior close"

        hdrs = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        r = _req.get(
            "https://data.alpaca.markets/v2/stocks/bars",
            params={"symbols": ",".join(syms), "timeframe": "5Min",
                    "start": start_iso, "end": end_iso, "limit": 1000, "feed": "iex"},
            headers=hdrs, timeout=10,
        )
        bars_data: dict = {}
        if r.ok:
            for sym, bars in r.json().get("bars", {}).items():
                prices = [float(b["c"]) for b in bars if "c" in b]
                if prices:
                    bars_data[sym] = prices
        # If today returned nothing, fall back to prior close with explicit window
        if not bars_data and label == "today":
            prior = market_open_utc - timedelta(days=1)
            while prior.weekday() >= 5:
                prior -= timedelta(days=1)
            r2 = _req.get(
                "https://data.alpaca.markets/v2/stocks/bars",
                params={"symbols": ",".join(syms), "timeframe": "5Min",
                        "start": prior.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "end":   (prior + timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "limit": 1000, "feed": "iex"},
                headers=hdrs, timeout=10,
            )
            if r2.ok:
                for sym, bars in r2.json().get("bars", {}).items():
                    prices = [float(b["c"]) for b in bars if "c" in b]
                    if prices:
                        bars_data[sym] = prices
            label = "prior close"
        return {"bars": bars_data, "label": label}
    except Exception:
        return {"bars": {}, "label": "today"}


def _pos_intraday_spark(prices: list, entry: float, height: int = 52) -> str:
    """SVG sparkline with entry as dashed horizontal line. Uses preserveAspectRatio=none for full-width stretch."""
    if not prices or len(prices) < 2:
        return ""
    all_p = prices + ([entry] if entry else [])
    mn, mx = min(all_p), max(all_p)
    mid = (mn + mx) / 2
    rng = max(mx - mn, mid * 0.025) or 0.01
    mn, mx = mid - rng / 2, mid + rng / 2

    n = len(prices)
    line_color = "#34D399" if prices[-1] >= prices[0] else "#F87171"

    def py(p):
        return (1 - (p - mn) / (mx - mn)) * (height - 4) + 2

    def px(i):
        return i / (n - 1) * 100

    pts = " ".join(f"{px(i):.2f},{py(p):.2f}" for i, p in enumerate(prices))
    price_line = f'<polyline points="{pts}" fill="none" stroke="{line_color}" stroke-width="1.5"/>' 

    ey = py(entry)
    entry_line = (
        f'<line x1="0" y1="{ey:.2f}" x2="100" y2="{ey:.2f}" '
        f'stroke="#4A4F60" stroke-width="0.8" stroke-dasharray="3,2"/>' 
    )
    return (
        f'<svg viewBox="0 0 100 {height}" width="100%" height="{height}" '
        f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg" style="display:block">'
        f'{entry_line}{price_line}</svg>'
    )


def _pos_card_html(p: dict, bars: list, label: str = "today") -> str:
    """Full position card: header + intraday sparkline + value line."""
    sym = p.get("symbol", "")
    qty = int(p.get("qty", 0))
    entry = float(p.get("entry") or 0)
    current = float(p.get("current") or 0)
    pl = float(p.get("unreal_pl") or 0)
    plpc = float(p.get("unreal_plpc") or 0)
    stop = p.get("stop")
    tp = p.get("tp")
    market_val = float(p.get("market_val") or 0)
    gap = p.get("gap_to_stop")
    pct_cap = float(p.get("pct_capacity") or 0)
    oversize = p.get("oversize", False)
    earnings_flag = p.get("earnings", "")

    pl_color = "#34D399" if pl >= 0 else "#F87171"
    pl_sign = "+" if pl >= 0 else ""

    # Badges
    badges = ""
    if earnings_flag:
        badges += f' <span style="background:#2d2208;color:#d29922;font-size:9px;padding:1px 4px;border-radius:3px">{earnings_flag}</span>'
    if oversize == "critical":
        badges += ' <span style="background:#3d0c0c;color:#f85149;font-size:9px;padding:1px 4px;border-radius:3px">OVERSIZE!</span>'
    elif oversize in ("core", "dynamic"):
        badges += ' <span style="background:#2d2208;color:#d29922;font-size:9px;padding:1px 4px;border-radius:3px">OVERSIZE</span>'

    # Row background if near stop
    near_stop = gap is not None and gap < 2.0
    row_bg = "background:#1a0f02;" if near_stop else ""

    header = (
        f'<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:3px">'
        f'<span>'
        f'<span style="color:var(--accent-blue);font-weight:600;font-size:13px">{sym}</span>'
        f'{badges}'
        f'<span style="font-family:monospace;font-size:10px;color:var(--text-muted);margin-left:6px">'
        f'{qty} sh · {_fm(entry)} → {_fm(current)}'
        f'</span>'
        f'</span>'
        f'<span style="font-family:monospace;font-size:11px;color:{pl_color}">'
        f'{pl_sign}{_fm(pl)} ({pl_sign}{plpc:.1f}%)'
        f'</span>'
        f'</div>'
    )

    spark_svg = _pos_intraday_spark(bars, entry) if bars else ""

    if stop:
        stop_lbl = (
            f'<div style="text-align:right;font-family:monospace;font-size:9px;'
            f'color:#F87171;line-height:1.3;min-width:54px">stop<br>{_fm(stop)}</div>'
        )
    else:
        stop_lbl = '<div style="min-width:54px"></div>'

    if tp:
        tp_lbl = (
            f'<div style="font-family:monospace;font-size:9px;color:#34D399;'
            f'line-height:1.3;min-width:50px">{_fm(tp)}</div>'
        )
    else:
        tp_lbl = (
            f'<div style="font-family:monospace;font-size:9px;color:#FBBF24;'
            f'line-height:1.3;min-width:50px">no TP</div>'
        )

    prior_note = (' <span style="font-family:monospace;font-size:9px;color:var(--text-muted)">prior close</span>'
                  if label == "prior close" else "")

    if spark_svg:
        chart_row = (
            f'<div style="display:flex;align-items:center;gap:4px;margin:3px 0">'
            f'{stop_lbl}'
            f'<div style="flex:1;overflow:hidden">{spark_svg}{prior_note}</div>'
            f'{tp_lbl}'
            f'</div>'
        )
    else:
        chart_row = (
            f'<div style="display:flex;align-items:center;gap:4px;margin:3px 0">'
            f'{stop_lbl}'
            f'<div style="flex:1;height:36px;border:1px dashed #21262d;border-radius:3px;'
            f'display:flex;align-items:center;justify-content:center">'
            f'<span style="font-size:9px;color:var(--text-muted)">no chart data</span>'
            f'</div>'
            f'{tp_lbl}'
            f'</div>'
        )

    gap_str = f"gap {gap:.1f}%" if gap is not None else "—"
    val_row = (
        f'<div style="font-family:monospace;font-size:10px;color:var(--text-muted);margin-top:2px">'
        f'{qty} sh × {_fm(current)} = {_fm(market_val)}'
        f'<span style="color:#4A4F60;margin-left:8px">{gap_str} · {pct_cap:.1f}% cap</span>'
        f'</div>'
    )

    return (
        f'<div style="padding:8px 0;border-bottom:1px solid var(--border-subtle,#21262d);{row_bg}">'
        f'{header}{chart_row}{val_row}'
        f'</div>'
    )


def _a2_pipeline_today() -> dict:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = {"total": 0, "submitted": 0, "fully_filled": 0, "cancelled": 0, "proposed": 0}
    try:
        raw = json.loads((BOT_DIR / "data/account2/positions/structures.json").read_text())
        for s in raw:
            if not isinstance(s, dict):
                continue
            opened = str(s.get("opened_at", ""))
            if not opened.startswith(today_str):
                continue
            out["total"] += 1
            lc = s.get("lifecycle", "")
            if lc in out:
                out[lc] += 1
    except Exception:
        pass
    return out


def _detect_churn(decisions: list, n_cycles: int = 3) -> set:
    """Return symbols that appear in both a buy and a sell within the last n_cycles decisions."""
    buys: set = set()
    sells: set = set()
    for d in decisions[:n_cycles]:
        for a in d.get("actions", d.get("ideas", [])):
            sym = a.get("symbol", "")
            intent = (a.get("action") or a.get("intent") or "").upper()
            if sym and intent in ("BUY", "ADD"):
                buys.add(sym)
            elif sym and intent in ("SELL", "EXIT", "TRIM"):
                sells.add(sym)
    return buys & sells


def _a1_decisions_compact_html(decisions: list, churn_syms: set | None = None) -> str:
    if not decisions:
        return '<div style="color:#8b949e;font-size:12px">No decisions yet.</div>'
    if churn_syms is None:
        churn_syms = _detect_churn(decisions)
    rows = ""
    for d in decisions[:10]:
        ts = _to_et(d.get("ts", ""))
        regime = d.get("regime", d.get("regime_view", "?"))
        score = d.get("regime_score", "")
        actions = d.get("actions", d.get("ideas", []))
        act_parts = []
        for a in actions[:4]:
            sym = a.get("symbol", "")
            intent = (a.get("action") or a.get("intent") or "").upper()
            if sym and intent:
                c = "#3fb950" if intent in ("BUY", "ADD") else ("#f85149" if intent in ("SELL", "EXIT", "TRIM") else "#8b949e")
                churn_tag = (' <span style="color:#ffaa20;font-size:9px">&#x21BA; churn</span>'
                             if sym in churn_syms else "")
                act_parts.append(f'<span style="color:{c}">{intent} {sym}{churn_tag}</span>')
        acts_line = " &middot; ".join(act_parts) if act_parts else '<span style="color:#8b949e">HOLD</span>'
        score_str = f"({score})" if score not in ("", None) else ""
        rc = "#3fb950" if "risk_on" in str(regime) or "bullish" in str(regime) else (
             "#f85149" if "risk_off" in str(regime) or "bearish" in str(regime) else "#d29922")
        rows += (
            f'<div style="font-size:12px;padding:3px 0;border-bottom:1px solid #21262d;'
            f'font-family:monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
            f'<span style="color:#8b949e">[{ts}]</span> '
            f'<span style="color:{rc}">{regime}</span><span style="color:#8b949e">{score_str}</span> '
            f'{acts_line}</div>'
        )
    return rows


def _prettify_rejection(reason: str) -> str:
    """Convert machine-readable veto/rejection strings to human-readable text."""
    import re as _re
    if not reason:
        return ""
    if "duplicate_submission_blocked" in reason:
        return "duplicate — identical structure submitted recently"
    m = _re.match(r"max_loss=([0-9.]+)>equity\*([0-9.]+)=([0-9.]+)", reason)
    if m:
        ml = float(m.group(1)); pct = float(m.group(2)); eq = float(m.group(3))
        return f"max loss ${ml:,.0f} exceeds {int(round(pct*100))}% risk limit (${eq:,.0f})"
    m = _re.match(r"spread_too_wide=([0-9.]+)%?>([0-9.]+)%?", reason)
    if m:
        return f"spread {float(m.group(1)):.1f}% exceeds {float(m.group(2)):.0f}% max"
    m = _re.match(r"iv_rank_too_low=([0-9.]+)<([0-9.]+)", reason)
    if m:
        return f"IV rank {float(m.group(1)):.0f} below min {float(m.group(2)):.0f}"
    if reason in ("no_surviving_candidates", "no_trade"):
        return "no candidates passed veto gates"
    if "gate_blocked" in reason:
        return "cycle gate blocked — no material state change"
    return reason.replace("_", " ")


def _a2_decisions_compact_html(decs: list) -> str:
    if not decs:
        return '<div style="color:#8b949e;font-size:12px">No A2 decisions yet.</div>'
    rows = ""
    for d in decs[:10]:
        ts = _to_et(d.get("built_at", ""))
        result = d.get("execution_result", "?")
        cand = d.get("selected_candidate") or {}
        sym = cand.get("symbol", "") if isinstance(cand, dict) else ""
        st = cand.get("structure_type", "") if isinstance(cand, dict) else ""
        cand_str = f"{sym} {st}".strip() if sym else "—"
        reason = d.get("no_trade_reason", "") or ""
        rc = "#3fb950" if result == "submitted" else ("#d29922" if result == "no_trade" else "#8b949e")
        reason_part = f' <span style="color:#8b949e">({_prettify_rejection(reason)[:55]})</span>' if reason else ""
        rows += (
            f'<div style="font-size:12px;padding:3px 0;border-bottom:1px solid #21262d;'
            f'font-family:monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
            f'<span style="color:#8b949e">[{ts}]</span> '
            f'<span style="color:{rc}">{result}</span>{reason_part} '
            f'<span style="color:#8b949e">{cand_str}</span></div>'
        )
    return rows


def _watch_now_bullets(status: dict) -> list:
    bullets = []

    # 1. Earnings binary events
    for p in status.get("positions", []):
        earn = p.get("earnings", "")
        if earn:
            sym = p["symbol"]
            entry = p.get("entry", 1)
            current = p.get("current", entry)
            gain = (current - entry) / entry * 100 if entry else 0
            bullets.append(("critical",
                f"<b>{sym}</b>: {earn} &mdash; {gain:+.1f}% open. Consider sizing before binary event."))
            if len(bullets) >= 6:
                return bullets

    # 2. Trail triggers — within 1% of next tier
    trail_tiers = status.get("trail_tiers", [])
    for p in status.get("positions", []):
        entry = p.get("entry", 0)
        current = p.get("current", 0)
        if not entry or not current:
            continue
        gain_pct = (current - entry) / entry * 100
        for i, tier in enumerate(trail_tiers):
            tier_gain = tier.get("gain_pct", 0) * 100
            if 0 < tier_gain - gain_pct < 1.0:
                trig = entry * (1 + tier.get("gain_pct", 0))
                bullets.append(("orange",
                    f"<b>{p['symbol']}</b>: {gain_pct:.1f}% gain &mdash; {tier_gain-gain_pct:.1f}% from T{i+1} trail trigger at ${trig:.2f}"))
                break
        if len(bullets) >= 6:
            return bullets

    # 3. Entries primed — most recent decision has BUY/ADD
    a1_decs = status.get("a1_decisions", [])
    if a1_decs:
        d = a1_decs[0]
        actions = d.get("actions", d.get("ideas", []))
        buys = [a.get("symbol", "") for a in actions
                if (a.get("action") or a.get("intent") or "").upper() in ("BUY", "ADD") and a.get("symbol")]
        if buys:
            bullets.append(("orange", f"Bot eyeing entries: <b>{', '.join(buys[:3])}</b> &mdash; last decision has BUY/ADD intent"))
            if len(bullets) >= 6:
                return bullets

    # 4. A2 duplicate-block streak
    a2_decs = status.get("a2_decisions", [])
    dup_count = sum(1 for d in a2_decs if d.get("no_trade_reason") == "duplicate_submission_blocked")
    if dup_count >= 2:
        bullets.append(("orange",
            f"A2: {dup_count} of last 5 cycles blocked as duplicate submissions &mdash; possible stale structure"))
        if len(bullets) >= 6:
            return bullets

    # 5. Cost burn
    costs = status.get("costs", {})
    daily_cost = float(costs.get("daily_cost", 0) or 0)
    proj = daily_cost * 22
    if proj > 400:
        bullets.append(("critical", f"Cost burn &#x1F534; <b>{_fm(proj)}/month</b> projected ({_fm(daily_cost)}/day &times; 22 days)"))
    elif proj > 250:
        bullets.append(("orange", f"Cost burn &#x26A0;&#xFE0F; <b>{_fm(proj)}/month</b> projected ({_fm(daily_cost)}/day &times; 22 days)"))
    if len(bullets) >= 6:
        return bullets

    # 6. Regime score
    decision = status.get("decision", {})
    regime = decision.get("regime", decision.get("regime_view", ""))
    try:
        rs = float(decision.get("regime_score", 50) or 50)
        if rs < 25:
            bullets.append(("critical", f"Regime score {rs:.0f}/100 &mdash; risk-off: <b>{regime}</b>"))
        elif rs < 40:
            bullets.append(("orange", f"Regime score {rs:.0f}/100 &mdash; defensive: <b>{regime}</b>"))
    except Exception:
        pass

    return bullets[:6]


# ── Warning helpers ───────────────────────────────────────────────────────────
def _build_warnings(status: dict) -> list:
    warnings = []
    a1_mode = status["a1_mode"].get("mode", "normal").upper()
    a2_mode = status["a2_mode"].get("mode", "normal").upper()
    if a1_mode != "NORMAL":
        detail = status["a1_mode"].get("reason_detail", "")[:100]
        # Find pending SELL/TRIM actions from recent decisions
        pending_exits = []
        for d in status.get("a1_decisions", [])[:3]:
            for a in d.get("actions", d.get("ideas", [])):
                intent = (a.get("action") or a.get("intent") or "").upper()
                sym = a.get("symbol", "")
                if intent in ("SELL", "EXIT", "TRIM") and sym:
                    pending_exits.append(f"{intent} {sym}")
        blocked_str = ""
        if pending_exits:
            blocked_str = f" | Bot wants: {', '.join(pending_exits[:3])}"
        since = status["a1_mode"].get("entered_at", "")
        since_str = f" since {_to_et(since)}" if since else ""
        warnings.append(("critical", f"&#x26A0; A1 MODE: {a1_mode} &mdash; {detail}{since_str}{blocked_str}"))
    if a2_mode != "NORMAL":
        # Check for A2 duplicate-block streak
        a2_decs = status.get("a2_decisions", [])
        dup_count = sum(1 for d in a2_decs if d.get("no_trade_reason") == "duplicate_submission_blocked")
        dup_note = f" ({dup_count} consecutive duplicate-blocks)" if dup_count >= 2 else ""
        warnings.append(("orange", f"&#x26A0; A2 MODE: {a2_mode}{dup_note}"))
    if not status["a1"].get("ok"):
        warnings.append(("critical", f"&#x26A0; A1 API ERROR: {status['a1'].get('error','?')[:80]}"))
    if not status["a2"].get("ok"):
        warnings.append(("orange", f"&#x26A0; A2 API ERROR: {status['a2'].get('error','?')[:80]}"))
    for p in status["positions"]:
        ov = p.get("oversize", False)
        if ov == "critical":
            warnings.append(("critical", f"&#x26A1; {p['symbol']}: OVERSIZE CRITICAL ({_fp(p['pct_capacity'])} of cap)"))
        elif ov in ("core", "dynamic"):
            warnings.append(("orange", f"&#x26A1; {p['symbol']}: OVERSIZE {ov.upper()} ({_fp(p['pct_capacity'])} of cap)"))
        if p.get("gap_to_stop") is not None and p["gap_to_stop"] < 2.0:
            warnings.append(("orange", f"&#x1F534; {p['symbol']}: near stop &mdash; gap {p['gap_to_stop']:.1f}%"))
        if p.get("earnings"):
            warnings.append(("orange", f"&#x1F4C5; {p['symbol']}: {p['earnings']}"))
    return warnings


def _warnings_html(warnings: list) -> str:
    if not warnings:
        return ""
    parts = []
    for severity, msg in warnings:
        cls = "warn-critical" if severity == "critical" else "warn-orange"
        parts.append(f'<div class="{cls}">{msg}</div>')
    return "\n".join(parts)


# ── Ring SVG helper ───────────────────────────────────────────────────────────
def _ring_svg(pct: float, color: str = "#4facfe") -> str:
    circ = 138
    fill = min(circ, max(0, pct / 100 * circ))
    gap = circ - fill
    label = f"{int(round(pct))}%"
    return (
        f'<svg width="56" height="56" viewBox="0 0 56 56">'
        f'<circle cx="28" cy="28" r="22" fill="none" stroke="#1e2040" stroke-width="5"/>'
        f'<circle cx="28" cy="28" r="22" fill="none" stroke="{color}" stroke-width="5"'
        f' stroke-dasharray="{fill:.1f} {gap:.1f}" stroke-dashoffset="34" stroke-linecap="round"/>'
        f'<text x="28" y="32" text-anchor="middle" font-size="10" fill="{color}">{label}</text>'
        f'</svg>'
    )


# ── Ticker builder ────────────────────────────────────────────────────────────
def _build_ticker_html(positions: list, vix_str: str = "—") -> str:
    items = []
    for p in positions[:8]:
        sym = p.get("symbol", "")
        cur = p.get("current", 0)
        pct = p.get("unreal_plpc", 0)
        sign = "+" if pct >= 0 else ""
        cls = "tk-g" if pct >= 0 else "tk-r"
        items.append(
            f'<span class="tk-sym">{sym}</span>'
            f' <span class="tk-val">${cur:,.2f}</span>'
            f' <span class="{cls}">{sign}{pct:.1f}%</span>'
        )
    parts = ['<span class="tk-sep"> | </span>'.join(
        f'<span class="ticker-item">{i}</span>' for i in items
    )]
    parts.append('<span class="tk-sep"> | </span>')
    parts.append(f'<span class="tk-sym">VIX</span> <span class="tk-val">{vix_str}</span>')
    return (
        '<div class="ticker">'
        + '<span class="tk-dim" style="margin-right:6px;letter-spacing:1px;font-size:8px">LIVE</span>'
        + "".join(parts)
        + '</div>'
    )


# ── Navigation ────────────────────────────────────────────────────────────────
def _nav_html(active_page: str, now_et: str, a1_mode: str = "NORMAL", a2_mode: str = "NORMAL",
              session_label: str = "") -> str:
    pages = [
        ("overview", "/", "Overview"),
        ("a1", "/a1", "A1 Equity"),
        ("a2", "/a2", "A2 Options"),
        ("board", "/board", "Strategy Room"),
        ("brief", "/brief", "Intelligence"),
        ("trades", "/trades", "Trades"),
        ("gallery", "/gallery", "Trade Log"),
        ("transparency", "/transparency", "Transparency"),
        ("theater", "/theater", "Decision Theater"),
        ("social", "/social", "Social"),
    ]
    tabs = ""
    for pid, href, label in pages:
        cls = "nav-tab active" if pid == active_page else "nav-tab"
        tabs += f'<a href="{href}" class="{cls}">{label}</a>'

    a1_pill_cls = "npill-g" if a1_mode == "NORMAL" else ("npill-r" if "HALT" in a1_mode else "npill-a")
    a2_pill_cls = "npill-g" if a2_mode == "NORMAL" else ("npill-r" if "HALT" in a2_mode else "npill-a")
    sess_cls = "npill-a" if session_label and session_label != "MARKET" else "npill-g"
    sess_pill = f' <span class="npill {sess_cls}">{session_label}</span>' if session_label else ""

    return (
        f'<div class="nav">'
        f'<div class="nav-brand">'
        f'<div>Bull<span class="bear">Bear</span>Bot</div>'
        f'<div class="nav-subtitle">Autonomous AI trading system &middot; paper trading since April&nbsp;13,&nbsp;2026</div>'
        f'</div>'
        f'<div class="nav-tabs">{tabs}</div>'
        f'<div class="nav-pills">'
        f'<span class="npill {a1_pill_cls}">A1 {a1_mode}</span>'
        f'<span class="npill {a2_pill_cls}">A2 {a2_mode}</span>'
        f'{sess_pill}'
        f'</div>'
        f'<div class="nav-right">'
        f'<span style="font-size:10px;color:var(--text-muted);margin-right:12px">Paper trading &middot; not financial advice</span>'
        f'{now_et}&nbsp;&nbsp;&#x21BB;&nbsp;<span id="cd">60</span>s'
        f'</div>'
        f'</div>'
    )


def _page_shell(title: str, nav: str, body: str, ticker: str = "") -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="refresh" content="60">'
        f'<title>{title} — BullBearBot</title>'
        '<style>' + SHARED_CSS + '</style>'
        '<link rel="stylesheet" href="/static/theme.css">'
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>'
        '</head><body>'
        + nav + body + ticker + _COUNTDOWN_JS + _COMMAND_PALETTE_HTML +
        '</body></html>'
    )


# ── Performance widget helpers ────────────────────────────────────────────────
def _insuf_data_card(days: int) -> str:
    return (
        '<div class="card"><div style="color:#8b949e;font-size:13px">'
        f'Insufficient data ({days} day{"s" if days != 1 else ""} of outcomes — need 3+)</div></div>'
    )


def _pct_clr(pct: float | None, good: float = 55.0, warn: float = 45.0) -> str:
    if pct is None:
        return "#8b949e"
    return "#3fb950" if pct >= good else ("#d29922" if pct >= warn else "#f85149")


def _kv_row(label: str, val_html: str) -> str:
    return (
        f'<div class="kv"><span class="kv-label">{label}</span>'
        f'<span class="kv-val">{val_html}</span></div>'
    )


def _perf_overview_html(ps: dict) -> str:
    """One-liner performance card for the overview page."""
    days = ps.get("data_days", 0)
    if days < 3:
        return _insuf_data_card(days)
    si = ps.get("trade_ideas", {})
    al = ps.get("allocator", {})
    a2 = ps.get("a2_structures", {})
    parts = []
    apr_1d = si.get("approved_profitable_1d_pct")
    if apr_1d is not None:
        clr = _pct_clr(apr_1d)
        parts.append(f'Ideas approved 1d: <span style="color:{clr};font-weight:600">{apr_1d:.0f}%</span>')
    follow_pct = al.get("follow_rate_pct")
    if follow_pct is not None:
        parts.append(f'Alloc follow: <span style="color:#c9d1d9">{follow_pct:.0f}%</span>')
    a2_win = a2.get("win_rate_pct")
    if a2_win is not None:
        clr = _pct_clr(a2_win)
        parts.append(f'A2 win: <span style="color:{clr}">{a2_win:.0f}%</span>')
    body = " &nbsp;|&nbsp; ".join(parts) if parts else "No outcome data yet."
    return (
        f'<div class="card"><div style="font-size:13px;color:#c9d1d9">'
        f'<span style="font-size:11px;color:#8b949e;text-transform:uppercase;'
        f'letter-spacing:0.5px;margin-right:8px">7d</span>'
        f'{body}</div></div>'
    )


def _perf_a1_decisions_html(ps: dict) -> str:
    """A1 Decision Quality widget for the A1 detail page."""
    days = ps.get("data_days", 0)
    if days < 3:
        return _insuf_data_card(days)
    si = ps.get("trade_ideas", {})
    al = ps.get("allocator", {})
    lines = []

    n_ideas = si.get("total_ideas_7d")
    if n_ideas is not None:
        lines.append(_kv_row("Ideas logged (7d)", f'<span style="color:#c9d1d9">{n_ideas}</span>'))
    apr_pct = si.get("approved_pct")
    if apr_pct is not None:
        lines.append(_kv_row("Approval rate", f'<span style="color:#c9d1d9">{apr_pct:.0f}%</span>'))
    apr_1d = si.get("approved_profitable_1d_pct")
    if apr_1d is not None:
        clr = _pct_clr(apr_1d)
        lines.append(_kv_row("Approved profitable (1d)", f'<span style="color:{clr}">{apr_1d:.0f}%</span>'))
    apr_5d = si.get("approved_profitable_5d_pct")
    if apr_5d is not None:
        clr = _pct_clr(apr_5d)
        lines.append(_kv_row("Approved profitable (5d)", f'<span style="color:{clr}">{apr_5d:.0f}%</span>'))
    rej_1d = si.get("rejected_wouldve_been_profitable_1d_pct")
    if rej_1d is not None:
        clr = "#f85149" if rej_1d > 55 else "#8b949e"
        lines.append(_kv_row("False kernel rejection (1d)", f'<span style="color:{clr}">{rej_1d:.0f}%</span>'))

    n_alloc = al.get("total_recommendations_7d")
    if n_alloc is not None:
        lines.append(_kv_row("Allocator recs (7d)", f'<span style="color:#c9d1d9">{n_alloc}</span>'))
    follow_pct = al.get("follow_rate_pct")
    if follow_pct is not None:
        clr = "#3fb950" if follow_pct >= 50 else "#8b949e"
        lines.append(_kv_row("Alloc follow rate", f'<span style="color:{clr}">{follow_pct:.0f}%</span>'))
    add_1d = al.get("add_accuracy_1d_pct")
    if add_1d is not None:
        clr = _pct_clr(add_1d)
        lines.append(_kv_row("ADD accuracy (1d)", f'<span style="color:{clr}">{add_1d:.0f}%</span>'))

    if not lines:
        return '<div class="card"><div style="color:#8b949e;font-size:13px">No decision quality data yet.</div></div>'
    return '<div class="card">' + "".join(lines) + '</div>'


def _perf_a2_strategies_html(ps: dict) -> str:
    """A2 Strategy Performance widget for the A2 detail page."""
    days = ps.get("data_days", 0)
    if days < 3:
        return _insuf_data_card(days)
    a2 = ps.get("a2_structures", {})
    lines = []

    n_sub = a2.get("total_submitted_7d")
    if n_sub is not None:
        lines.append(_kv_row("Structures submitted (7d)", f'<span style="color:#c9d1d9">{n_sub}</span>'))
    fill_pct = a2.get("fill_rate_pct")
    if fill_pct is not None:
        clr = "#3fb950" if fill_pct >= 70 else "#d29922"
        lines.append(_kv_row("Fill rate", f'<span style="color:{clr}">{fill_pct:.0f}%</span>'))
    win_pct = a2.get("win_rate_pct")
    if win_pct is not None:
        clr = _pct_clr(win_pct)
        lines.append(_kv_row("Win rate", f'<span style="color:{clr}">{win_pct:.0f}%</span>'))
    avg_pnl = a2.get("avg_pnl_pct_of_max_gain")
    if avg_pnl is not None:
        clr = "#3fb950" if avg_pnl >= 0 else "#f85149"
        sign = "+" if avg_pnl >= 0 else ""
        lines.append(_kv_row("Avg P&amp;L (% of max gain)", f'<span style="color:{clr}">{sign}{avg_pnl:.1f}%</span>'))

    by_strat = a2.get("by_strategy", {})
    if by_strat:
        lines.append(
            '<div style="margin-top:8px;font-size:11px;color:#8b949e;'
            'text-transform:uppercase;letter-spacing:0.5px">By Strategy</div>'
        )
        for strat, sv in by_strat.items():
            n = sv.get("count", 0)
            wr = sv.get("win_rate_pct")
            wr_str = f'{wr:.0f}%' if wr is not None else "?"
            wr_clr = _pct_clr(wr)
            lines.append(
                f'<div class="kv"><span class="kv-label" style="color:#8b949e">'
                f'{strat.replace("_", " ")}</span>'
                f'<span class="kv-val">{n} trades &nbsp;'
                f'<span style="color:{wr_clr}">{wr_str} WR</span></span></div>'
            )
    if not lines:
        return '<div class="card"><div style="color:#8b949e;font-size:13px">No A2 strategy performance data yet.</div></div>'
    return '<div class="card">' + "".join(lines) + '</div>'


# ── New design-system components (Hero strip + Cycle Pulse + Bot Voice) ───────
# All output uses --bbb-* tokens from /static/theme.css.
# Zero changes to existing page helpers or SHARED_CSS.

_INITIAL_CAPITAL = 200_000.0   # two paper accounts, $100k each, seeded 2026-04-13
_LAUNCH_DATE = date(2026, 4, 13)

_BBB_PIPELINE_STAGES = ["Regime", "Signals", "Scratchpad", "Gate", "Sonnet", "Kernel", "Exec", "A2"]


def _bbb_pnl_color(val: float) -> str:
    return "bbb-pos" if val >= 0 else "bbb-neg"


def _bbb_sign(val: float) -> str:
    return "+" if val >= 0 else ""


def _countdown_strip_html() -> str:
    today = date.today()
    launch = date(2026, 4, 13)
    target = date(2026, 5, 16)
    days_since = (today - launch).days
    days_to = (target - today).days
    if today >= target:
        live_day = (today - target).days + 1
        text = f'Live trading &middot; Day {live_day}'
        color = "var(--bbb-profit)"
    else:
        text = (f'Day {days_since} of paper trading'
                f' &nbsp;&middot;&nbsp; {days_to} day{"s" if days_to != 1 else ""} to real-money target'
                f' &nbsp;&middot;&nbsp; May 16, 2026')
        color = "var(--bbb-fg-muted)"
    return (
        f'<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
        f'border-radius:var(--bbb-r-2);padding:7px 16px;margin-bottom:var(--bbb-s-3);'
        f'font-family:var(--bbb-font-mono);font-size:12px;color:{color};'
        f'letter-spacing:0.02em">{text}</div>'
    )


def _bbb_hero_strip_html(status: dict) -> str:
    """Three-pane Hero strip: Cumulative Return | A1 Equity | A2 Equity."""
    a1d = status["a1"]
    a2d = status["a2"]
    a1_acc = a1d.get("account")
    a2_acc = a2d.get("account")

    a1_equity = float(a1_acc.equity or 0) if a1_acc else 0.0
    a2_equity = float(a2_acc.equity or 0) if a2_acc else 0.0
    combined_equity = a1_equity + a2_equity

    # Cumulative return since launch
    cum_dollars = combined_equity - _INITIAL_CAPITAL
    cum_pct = cum_dollars / _INITIAL_CAPITAL * 100 if _INITIAL_CAPITAL else 0.0
    cum_cls = _bbb_pnl_color(cum_dollars)
    cum_sign = _bbb_sign(cum_dollars)
    days_running = (date.today() - _LAUNCH_DATE).days

    # Today's P&L
    a1_pnl, a1_pnl_pct = status.get("today_pnl_a1", (0.0, 0.0))
    a2_pnl, a2_pnl_pct = status.get("today_pnl_a2", (0.0, 0.0))
    combined_pnl = a1_pnl + a2_pnl
    cpnl_cls = _bbb_pnl_color(combined_pnl)
    cpnl_sign = _bbb_sign(combined_pnl)

    # Regime string: "risk_on(62)"
    dec = status.get("decision", {})
    regime_raw = dec.get("regime_view", dec.get("regime", "")) or ""
    try:
        regime_score = float(dec.get("regime_score") or 50)
    except (TypeError, ValueError):
        regime_score = 50.0
    regime_str = f"{regime_raw}({regime_score:.0f})" if regime_raw else "—"

    # Cycle count (Sonnet calls today)
    gate = status.get("gate") or {}
    cycles_today = int(gate.get("total_calls_today") or 0)
    cycles_str = f"{cycles_today} cycle{'s' if cycles_today != 1 else ''}"

    # Projected monthly cost
    _costs = status.get("costs") or {}
    _daily_cost = float(_costs.get("daily_cost", 0) or 0)
    _proj_monthly = _daily_cost * 22
    _proj_c = "var(--bbb-loss)" if _proj_monthly > 400 else ("var(--bbb-warn)" if _proj_monthly > 250 else "var(--bbb-fg-muted)")

    # A1 pane data
    a1_pos_count = len(status.get("positions", []))
    a1_invested = sum(p.get("market_val", 0) for p in status.get("positions", []))
    _a1_lmv = float(a1_acc.long_market_value or 0) if a1_acc else a1_invested
    _a1_dtbp = float(a1_acc.daytrading_buying_power or 0) if a1_acc else 0.0
    _a1_regt = float(a1_acc.regt_buying_power or 0) if a1_acc else 0.0
    _a1_tcap = _a1_lmv + _a1_dtbp + _a1_regt
    a1_util = (_a1_lmv / _a1_tcap * 100) if _a1_tcap else 0.0
    a1_leverage = (_a1_lmv / a1_equity) if a1_equity else 0.0
    a1_pnl_cls = _bbb_pnl_color(a1_pnl)
    a1_sign = _bbb_sign(a1_pnl)
    a1_pos_str = f"{a1_pos_count} position{'s' if a1_pos_count != 1 else ''}"

    # A2 pane data
    a2_pos_count = len(a2d.get("positions", []))
    a2_pipe_total = status.get("a2_pipeline", {}).get("total", 0)
    a2_pnl_cls = _bbb_pnl_color(a2_pnl)
    a2_sign = _bbb_sign(a2_pnl)
    a2_struct_str = f"{a2_pos_count} structure{'s' if a2_pos_count != 1 else ''}"

    # Sparklines
    sp_a1_eq = status.get("spark_a1_eq") or []
    sp_a1_pnl = status.get("spark_a1_pnl") or []
    sp_a2_eq = status.get("spark_a2_eq") or []
    spark_cum = _bbb_sparkline_svg(sp_a1_pnl, _spark_color(sp_a1_pnl))
    spark_a1 = _bbb_sparkline_svg(sp_a1_eq, _spark_color(sp_a1_eq))
    spark_a2 = _bbb_sparkline_svg(sp_a2_eq, _spark_color(sp_a2_eq))

    return (
        f'<div class="bbb-hero-strip">'

        # — Left pane: Cumulative Return —
        f'<div class="bbb-hero-pane">'
        f'<div class="bbb-hero-label">Cumulative return</div>'
        f'<div class="bbb-hero-num {cum_cls}">{cum_sign}{_fm(cum_dollars)}</div>'
        f'<div class="bbb-hero-meta">'
        f'<span class="{cum_cls}">{cum_sign}{cum_pct:.2f}%</span>'
        f' combined since {_LAUNCH_DATE} · day {days_running} of 30'
        f'</div>'
        f'<div class="bbb-hero-divider"></div>'
        f'<div class="bbb-hero-meta">'
        f'<span class="{cpnl_cls}">{cpnl_sign}{_fm(combined_pnl)}</span> today'
        f' · <span style="color:{_proj_c}">{_fm(_proj_monthly)}/mo</span> projected'
        f' · {cycles_str}'
        f' · {_freshness_stamp(dec.get("ts", ""), 30, 90)}'
        f'</div>'
        f'{spark_cum}'
        f'</div>'

        # — Middle pane: A1 Equity —
        f'<div class="bbb-hero-pane">'
        f'<div class="bbb-hero-label">A1 equity</div>'
        f'<div class="bbb-hero-num-sm">{_fm(a1_equity)}</div>'
        f'<div class="bbb-hero-meta">'
        f'<span class="{a1_pnl_cls}">{a1_sign}{a1_pnl_pct:.2f}%</span>'
        f' day · {a1_pos_str} · {a1_util:.0f}% utilized · {a1_leverage:.2f}x leverage'
        f'</div>'
        f'{spark_a1}'
        f'</div>'

        # — Right pane: A2 Equity —
        f'<div class="bbb-hero-pane">'
        f'<div class="bbb-hero-label">A2 equity</div>'
        f'<div class="bbb-hero-num-sm">{_fm(a2_equity)}</div>'
        f'<div class="bbb-hero-meta">'
        f'<span class="{a2_pnl_cls}">{a2_sign}{a2_pnl_pct:.2f}%</span>'
        f' day · {a2_struct_str} · {a2_pipe_total} today'
        f'</div>'
        f'{spark_a2}'
        f'</div>'

        f'</div>'
    )


def _bbb_cycle_pulse_html(status: dict) -> str:
    """Cycle Pulse strip: dot | outcome text | approx cost | stage breadcrumbs."""
    gate = status.get("gate") or {}
    costs = status.get("costs") or {}
    cycles_today = int(gate.get("total_calls_today") or 0)

    # Approx per-cycle cost
    daily_cost = float(costs.get("daily_cost") or 0)
    per_cycle_cost = (daily_cost / cycles_today) if cycles_today > 0 else 0.0
    cost_str = f"cost {_fm(per_cycle_cost)}" if per_cycle_cost > 0 else ""

    # Last cycle outcome: read last raw decision entry (including gate-skips)
    last_outcome = "—"
    try:
        raw_decisions = json.loads((BOT_DIR / "memory/decisions.json").read_text())
        if raw_decisions:
            last_dec = raw_decisions[-1]
            reasoning = last_dec.get("reasoning", "") or ""
            # First sentence only, max 90 chars
            first_sent = reasoning.split(".")[0].strip()
            if first_sent:
                last_outcome = (first_sent[:87] + "…") if len(first_sent) > 90 else first_sent
    except Exception:
        pass

    cycle_label = f"Cycle {cycles_today}" if cycles_today > 0 else "No cycles today"
    text = f"{cycle_label} — {last_outcome}" if last_outcome != "—" else cycle_label

    # Stage breadcrumbs — all in completed/idle state (no live active stage this session)
    stages_html = "".join(
        f'<span class="bbb-stage is-done">{s}</span>' for s in _BBB_PIPELINE_STAGES
    )

    cost_span = f'<span class="bbb-pulse-cost">{cost_str}</span>' if cost_str else ""
    last_cycle_ts = (status.get("decision") or {}).get("ts", "")
    stamp_html = _freshness_stamp(last_cycle_ts, 30, 90)
    stamp_span = f'<span class="bbb-pulse-cost">{stamp_html}</span>' if stamp_html else ""

    return (
        f'<div class="bbb-pulse-strip">'
        f'<span class="bbb-pulse-dot is-idle"></span>'
        f'<span class="bbb-pulse-text">{text}</span>'
        f'{cost_span}'
        f'{stamp_span}'
        f'<div class="bbb-pulse-stages">{stages_html}</div>'
        f'</div>'
    )


def _bbb_voice_strip_html(status: dict, now_et: str) -> str:
    """Bot voice strip: first-person reasoning, violet left border."""
    dec = status.get("decision", {})
    reasoning = dec.get("reasoning", "") or ""

    # Skip gate-skip boilerplate; find first real sentence
    voice = ""
    for sent in reasoning.split("."):
        s = sent.strip()
        if s and "gate skipped" not in s.lower() and len(s) > 15:
            voice = s
            break

    if not voice:
        return ""   # no voice strip if there's no substantive reasoning

    # Truncate at word boundary, max 180 chars
    if len(voice) > 180:
        voice = voice[:177].rsplit(" ", 1)[0] + "…"

    # Attribution timestamp: use now_et if available
    attr_time = now_et.split(" ")[0] if now_et else "—"

    return (
        f'<div class="bbb-voice-strip">'
        f'<span class="bbb-voice-quote">{voice}</span>'
        f'<span class="bbb-voice-attr">BOT · {attr_time}</span>'
        f'</div>'
    )


# ── Gate Tray ─────────────────────────────────────────────────────────────────
def _gate_tray_data() -> dict:
    """Aggregate today's gate/kernel rejections for the Gate Tray drawer."""
    gate = _rj(BOT_DIR / "data/market/gate_state.json")
    gate_skips = int(gate.get("total_skips_today") or 0)
    # Use the gate's own trading-day date (ET), not UTC date
    today_str = gate.get("date_str") or date.today().isoformat()

    rejections = []
    try:
        lines = (BOT_DIR / "data/analytics/near_miss_log.jsonl").read_text().splitlines()
        for line in reversed(lines):
            try:
                e = json.loads(line)
                if today_str not in e.get("ts", ""):
                    continue
                if e.get("event_type") != "rejected_by_risk_kernel":
                    continue
                det = e.get("details") or {}
                reason_raw = det.get("rejection_reason", "") or ""
                rejections.append({
                    "ts": e.get("ts", ""),
                    "symbol": e.get("symbol", "?"),
                    "action": (det.get("intended_action") or "buy").upper(),
                    "reason": reason_raw,
                })
            except Exception:
                pass
    except Exception:
        pass

    def _cat(r: str) -> str:
        rl = r.lower()
        if "headroom" in rl or "exposure" in rl or "budget" in rl or "max_position" in rl or "reallocate" in rl:
            return "risk_budget"
        if "correlation" in rl:
            return "correlation"
        return "other"

    def _reason_short(r: str) -> str:
        rl = r.lower()
        if "headroom" in rl or "exposure" in rl:
            return "exposure cap"
        if "budget" in rl:
            return "budget"
        if "max_position" in rl:
            return "position cap"
        if "reallocate" in rl:
            return "realloc failed"
        if "correlation" in rl:
            return "correlation"
        return r[:40] if r else "blocked"

    n_budget = sum(1 for r in rejections if _cat(r["reason"]) == "risk_budget")
    n_corr = sum(1 for r in rejections if _cat(r["reason"]) == "correlation")
    n_other = len(rejections) - n_budget - n_corr
    total = gate_skips + len(rejections)

    return {
        "total": total,
        "gate_skips": gate_skips,
        "kernel_blocked": len(rejections),
        "rejections": [dict(r, reason_short=_reason_short(r["reason"])) for r in rejections[:20]],
        "cat_low_conviction": gate_skips,
        "cat_risk_budget": n_budget,
        "cat_correlation": n_corr,
        "cat_other": n_other,
    }


def _gate_tray_html(data: dict) -> str:
    """Fixed right-edge tab + 300px drawer for today's gate rejection log."""
    total = data["total"]
    gate_skips = data["gate_skips"]
    kernel_blocked = data["kernel_blocked"]
    rejections = data["rejections"]
    cat_lc = data["cat_low_conviction"]
    cat_rb = data["cat_risk_budget"]
    cat_co = data["cat_correlation"]
    cat_ot = data["cat_other"]

    denom = max(total, 1)
    pct_lc = cat_lc / denom * 100
    pct_rb = cat_rb / denom * 100
    pct_co = cat_co / denom * 100
    pct_ot = cat_ot / denom * 100

    # Stacked bar segments (4px tall)
    bar_html = (
        '<div style="display:flex;height:4px;border-radius:2px;overflow:hidden;margin:10px 0 6px">'
        + (f'<div style="width:{pct_lc:.1f}%;background:var(--bbb-fg-dim)" title="low_conviction"></div>' if cat_lc else '')
        + (f'<div style="width:{pct_rb:.1f}%;background:var(--bbb-warn)" title="risk_budget"></div>' if cat_rb else '')
        + (f'<div style="width:{pct_co:.1f}%;background:var(--bbb-info)" title="correlation"></div>' if cat_co else '')
        + (f'<div style="width:{pct_ot:.1f}%;background:var(--bbb-fg-muted)" title="other"></div>' if cat_ot else '')
        + '</div>'
    )

    # Legend chips
    legend_parts = []
    chip_sty = "font-family:var(--bbb-font-mono);font-size:10px;white-space:nowrap"
    if cat_lc:
        legend_parts.append(f'<span style="{chip_sty};color:var(--bbb-fg-dim)">● low_conviction {cat_lc}</span>')
    if cat_rb:
        legend_parts.append(f'<span style="{chip_sty};color:var(--bbb-warn)">● risk_budget {cat_rb}</span>')
    if cat_co:
        legend_parts.append(f'<span style="{chip_sty};color:var(--bbb-info)">● correlation {cat_co}</span>')
    if cat_ot:
        legend_parts.append(f'<span style="{chip_sty};color:var(--bbb-fg-muted)">● other {cat_ot}</span>')
    legend_html = '<div style="display:flex;flex-wrap:wrap;gap:8px">' + " ".join(legend_parts) + '</div>'

    # Rejection rows
    rows_html = ""
    if not rejections:
        rows_html = '<div style="font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg-dim);padding:8px 0">No kernel rejections today</div>'
    else:
        for r in rejections:
            sym = r["symbol"]
            act = r["action"]
            reason = r["reason_short"]
            ts = _to_et(r["ts"])
            act_color = "var(--bbb-profit)" if act == "BUY" else "var(--bbb-loss)"
            rows_html += (
                '<div style="display:grid;grid-template-columns:54px 32px 1fr auto;'
                'gap:6px;align-items:center;padding:5px 0;'
                'border-bottom:1px solid var(--bbb-border)">'
                '<span style="font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg)">' + sym + '</span>'
                '<span style="font-family:var(--bbb-font-mono);font-size:9px;letter-spacing:.05em;'
                'color:' + act_color + '">' + act + '</span>'
                '<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted);'
                'overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + r["reason"][:120] + '">'
                + reason + '</span>'
                '<span style="font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-fg-dim);'
                'white-space:nowrap">' + ts + '</span>'
                '</div>'
            )

    # Gate skip summary row
    skip_row = (
        '<div style="padding:6px 0;border-bottom:1px solid var(--bbb-border)">'
        '<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-dim)">'
        + str(gate_skips) + ' cycle gate skip' + ('s' if gate_skips != 1 else '') + ' — no material state change'
        + '</span></div>'
        if gate_skips else ''
    )

    today_label = date.today().strftime("%-m/%-d")

    return (
        # Tab (always visible, collapses drawer on click)
        '<div id="bbb-gate-tab" onclick="bbbGateToggle()" style="'
        'position:fixed;right:0;top:50%;transform:translateY(-50%);'
        'width:24px;padding:14px 5px;'
        'background:var(--bbb-surface);'
        'border:1px solid var(--bbb-border);border-right:none;'
        'border-left:2px solid var(--bbb-ai);'
        'border-radius:var(--bbb-r-2) 0 0 var(--bbb-r-2);'
        'cursor:pointer;z-index:200;user-select:none;'
        'transition:right var(--bbb-dur-base) var(--bbb-ease)">'
        '<span style="font-family:var(--bbb-font-mono);font-size:10px;letter-spacing:.08em;'
        'color:var(--bbb-fg-muted);writing-mode:vertical-lr;text-transform:uppercase">'
        'GATE ▸ ' + str(total) +
        '</span>'
        '</div>'

        # Drawer panel
        + '<div id="bbb-gate-drawer" style="'
        'position:fixed;right:-300px;top:0;bottom:0;width:300px;'
        'background:var(--bbb-surface);'
        'border-left:1px solid var(--bbb-border);'
        'z-index:199;overflow-y:auto;'
        'transition:right var(--bbb-dur-base) var(--bbb-ease)">'

        # Drawer header
        '<div style="padding:14px 16px 10px;border-bottom:1px solid var(--bbb-border);'
        'display:flex;align-items:baseline;gap:8px">'
        '<span style="font-family:var(--bbb-font-mono);font-size:11px;letter-spacing:.08em;'
        'text-transform:uppercase;color:var(--bbb-fg-muted)">Gate Rejections</span>'
        '<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-dim)">' + today_label + '</span>'
        '<span style="margin-left:auto;font-family:var(--bbb-font-mono);font-size:20px;'
        'font-weight:500;color:var(--bbb-fg)">' + str(total) + '</span>'
        '</div>'

        # Sub-header counts
        + '<div style="padding:8px 16px;border-bottom:1px solid var(--bbb-border);'
        'display:flex;gap:16px">'
        '<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-dim)">'
        + str(gate_skips) + ' gate skips</span>'
        '<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-warn)">'
        + str(kernel_blocked) + ' kernel blocks</span>'
        '</div>'

        # Rejection rows
        + '<div style="padding:0 16px">'
        + skip_row
        + rows_html
        + '</div>'

        # Stacked bar + legend
        + '<div style="padding:12px 16px;border-top:1px solid var(--bbb-border);'
        'position:sticky;bottom:0;background:var(--bbb-surface)">'
        + bar_html
        + legend_html
        + '</div>'

        '</div>'

        # JS: toggle, Escape, click-outside
        + '<script>'
        'function bbbGateToggle(){'
        'var d=document.getElementById("bbb-gate-drawer");'
        'var t=document.getElementById("bbb-gate-tab");'
        'var open=d.style.right==="0px";'
        'd.style.right=open?"-300px":"0px";'
        't.style.right=open?"0px":"300px";'
        '}'
        'document.addEventListener("keydown",function(e){'
        'if(e.key==="Escape"){'
        'var d=document.getElementById("bbb-gate-drawer");'
        'if(d.style.right==="0px")bbbGateToggle();'
        '}});'
        'document.addEventListener("click",function(e){'
        'var d=document.getElementById("bbb-gate-drawer");'
        'var t=document.getElementById("bbb-gate-tab");'
        'if(d.style.right==="0px"&&!d.contains(e.target)&&!t.contains(e.target))bbbGateToggle();'
        '});'
        '</script>'
    )


# ── Overview page ─────────────────────────────────────────────────────────────
def _page_overview(status: dict, now_et: str) -> str:
    a1d = status["a1"]
    a2d = status["a2"]
    a1_acc = a1d.get("account")
    a2_acc = a2d.get("account")
    a1_mode = status["a1_mode"].get("mode", "unknown").upper()
    a2_mode = status["a2_mode"].get("mode", "unknown").upper()
    a1_color = _mode_color(a1_mode)
    a2_color = _mode_color(a2_mode)
    nav = _nav_html("overview", now_et, a1_mode, a2_mode)
    warn_html = _warnings_html(status.get("warnings", []))

    a1_pnl, a1_pnl_pct = status.get("today_pnl_a1", (0.0, 0.0))
    a2_pnl, a2_pnl_pct = status.get("today_pnl_a2", (0.0, 0.0))
    a1_pnl_color = "#3fb950" if a1_pnl >= 0 else "#f85149"
    a2_pnl_color = "#3fb950" if a2_pnl >= 0 else "#f85149"
    a1_pnl_sign = "+" if a1_pnl >= 0 else ""
    a2_pnl_sign = "+" if a2_pnl >= 0 else ""

    if a1_acc:
        a1_equity = float(a1_acc.equity or 0)
        a1_pos_count = len(status["positions"])
        a1_unreal = sum(p["unreal_pl"] for p in status["positions"])
        a1_unreal_c = "#3fb950" if a1_unreal >= 0 else "#f85149"
        a1_unreal_s = "+" if a1_unreal >= 0 else ""
        a1_invested = sum(p["market_val"] for p in status["positions"])
        a1_util = min(100.0, a1_invested / a1_equity * 100) if a1_equity else 0.0
    else:
        a1_equity = a1_invested = a1_util = 0.0
        a1_pos_count = 0
        a1_unreal = 0.0; a1_unreal_c = "#8b949e"; a1_unreal_s = ""

    if a2_acc:
        a2_equity = float(a2_acc.equity or 0)
        a2_pos_count = len(a2d.get("positions", []))
    else:
        a2_equity = 0.0
        a2_pos_count = 0

    costs = status["costs"]
    gate = status["gate"]
    daily_cost = float(costs.get("daily_cost", 0) or 0)
    proj_monthly = daily_cost * 22
    if proj_monthly > 400:
        proj_color = "#f85149"
        proj_icon = "&#x1F534; "
    elif proj_monthly > 250:
        proj_color = "#d29922"
        proj_icon = "&#x26A0;&#xFE0F; "
    else:
        proj_color = "#3fb950"
        proj_icon = ""
    sonnet_calls = gate.get("total_calls_today", "—")
    buys = status["buys_today"]
    sells = status["sells_today"]

    # Top-3 callers by cost
    by_caller = costs.get("by_caller", {}) if isinstance(costs, dict) else {}
    top_callers = sorted(by_caller.items(), key=lambda x: float(x[1].get("cost", 0) if isinstance(x[1], dict) else 0), reverse=True)[:3]
    callers_html = ""
    for caller, cdata in top_callers:
        c_cost = float(cdata.get("cost", 0) if isinstance(cdata, dict) else 0)
        c_calls = cdata.get("calls", 0) if isinstance(cdata, dict) else 0
        callers_html += (
            f'<div style="font-size:11px;color:#8b949e;padding:2px 0">'
            f'<span style="color:#c9d1d9">{caller}</span>: {_fm(c_cost)} ({c_calls} calls)</div>'
        )

    flags_list = []
    for p in status["positions"]:
        if p.get("earnings"):
            flags_list.append(f'<div class="alert alert-orange">&#x1F4C5; {p["symbol"]}: {p["earnings"]}</div>')
        ov = p.get("oversize", False)
        if ov == "critical":
            flags_list.append(f'<div class="alert alert-red">&#x26A1; {p["symbol"]}: OVERSIZE CRITICAL ({_fp(p["pct_capacity"])} of cap)</div>')
        elif ov == "core":
            flags_list.append(f'<div class="alert alert-orange">&#x26A1; {p["symbol"]}: OVERSIZE ({_fp(p["pct_capacity"])} of cap &gt; 20%)</div>')
        elif ov == "dynamic":
            flags_list.append(f'<div class="alert alert-orange">&#x26A1; {p["symbol"]}: OVER DYN TIER ({_fp(p["pct_capacity"])} of cap &gt; 15%)</div>')
        if p.get("gap_to_stop") is not None and p["gap_to_stop"] < 2.0:
            flags_list.append(f'<div class="alert alert-orange">&#x1F534; {p["symbol"]}: stop gap {p["gap_to_stop"]:.1f}%</div>')
    if not flags_list:
        flags_list = ['<div class="alert alert-green">&#x2713; No active flags</div>']
    flags_html = "\n".join(flags_list)

    a2_dec = status.get("a2_decision") or {}
    a2_ts = _to_et(a2_dec.get("built_at", "")) if a2_dec else "—"
    git_hash = status["git_hash"]
    svc_uptime = status["service_uptime"]
    build_pill = _bbb_build_pill()

    # Earnings calendar staleness
    try:
        import sys as _sys  # noqa: PLC0415
        _bot_dir = str(BOT_DIR)
        if _bot_dir not in _sys.path:
            _sys.path.insert(0, _bot_dir)
        import data_warehouse as _dw  # noqa: PLC0415
        _ec_stale = _dw.get_earnings_calendar_staleness()
    except Exception:
        _ec_stale = {"stale": False, "warning": False, "hours_old": None, "entry_count": 0}
    _ec_hours = _ec_stale.get("hours_old")
    _ec_count = _ec_stale.get("entry_count", 0)
    if _ec_stale.get("warning"):
        _ec_icon  = "&#x1F534;"
        _ec_color = "#f85149"
        _ec_label = f"{_ec_hours:.0f}h ago" if _ec_hours is not None else "unknown age"
    elif _ec_stale.get("stale"):
        _ec_icon  = "&#x26A0;&#xFE0F;"
        _ec_color = "#d29922"
        _ec_label = f"{_ec_hours:.0f}h ago" if _ec_hours is not None else "unknown age"
    else:
        _ec_icon  = "&#x2705;"
        _ec_color = "#3fb950"
        _ec_label = f"updated {_ec_hours:.0f}h ago" if _ec_hours is not None else "fresh"
    earnings_cal_html = (
        f'<div class="kv"><span class="kv-label">Earnings cal</span>'
        f'<span class="kv-val" style="color:{_ec_color}">'
        f'{_ec_icon} {_ec_label} ({_ec_count} entries)</span></div>'
    )

    # Performance summary widget
    perf_7d_html = _perf_overview_html(status.get("perf_summary", {}))

    # P&L hero
    combined_pnl = a1_pnl + a2_pnl
    combined_color = "#3fb950" if combined_pnl >= 0 else "#f85149"
    combined_sign = "+" if combined_pnl >= 0 else ""

    # Trail table
    trail_tiers = status.get("trail_tiers", [])
    trail_html = _trail_table_html(status["positions"], trail_tiers)

    # Allocator compact line
    allocator_line = status.get("allocator_line", "") or ""
    alloc_data = status.get("allocator_data", {})

    # Equity curve
    equity_curve_html = _equity_curve_html(status.get("equity_curve", {}))

    # A2 churn warning
    a2_churn_warn = _a2_churn_check()

    # a1_comp computed later after churn detection; a2_comp here
    a2_comp = _a2_decisions_compact_html(status.get("a2_decisions", []))

    # A2 pipeline today
    a2_pipe = status.get("a2_pipeline", {})
    a2_pipe_total = a2_pipe.get("total", 0)
    a2_pipe_str = (
        f'Today: {a2_pipe_total} structures &mdash; '
        f'{a2_pipe.get("fully_filled",0)} filled / '
        f'{a2_pipe.get("submitted",0)} submitted / '
        f'{a2_pipe.get("cancelled",0)} cancelled / '
        f'{a2_pipe.get("proposed",0)} proposed'
    ) if a2_pipe_total else "No A2 structures today."

    # VIX for ticker
    _vix_for_ticker = "—"
    try:
        pf_entries_t = _jsonl_last(BOT_DIR / "data/status/preflight_log.jsonl", n=1)
        if pf_entries_t:
            for chk in pf_entries_t[0].get("checks", []):
                if chk.get("name") == "vix_gate" and "VIX=" in chk.get("message", ""):
                    _vix_for_ticker = chk["message"].split("VIX=")[1].split()[0]
    except Exception:
        pass
    ticker = _build_ticker_html(status["positions"], _vix_for_ticker)

    # Build positions compact HTML for overview
    _intraday = status.get("intraday_bars", {"bars": {}, "label": "today"})
    _intraday_bars = _intraday.get("bars", {})
    _intraday_label = _intraday.get("label", "today")
    _pos_rows = ""
    for p in status["positions"][:8]:
        _pos_rows += _pos_card_html(p, _intraday_bars.get(p["symbol"], []), _intraday_label)
    if not _pos_rows:
        _pos_rows = '<div style="color:var(--text-muted);font-size:11px">No open positions.</div>'

    # conviction picks from morning brief
    _brief = status.get("morning_brief") or {}
    _picks = _brief.get("conviction_picks", [])
    _conv_rows = ""
    for _pk in _picks[:6]:
        _psym = _pk.get("symbol", "")
        _pdir = _pk.get("direction", "long")
        _pconv = _pk.get("conviction", "medium")
        _pcat = _pk.get("catalyst", {})
        _ptxt = (_pcat.get("short_text", "") if isinstance(_pcat, dict) else str(_pcat or ""))[:55]
        _tcls = "var(--accent-green)" if _pconv == "high" else ("var(--accent-amber)" if _pconv == "medium" else "var(--text-muted)")
        _dicon = "&#x2191;" if _pdir == "long" else "&#x2193;"
        _dirc = "var(--accent-green)" if _pdir == "long" else "var(--accent-red)"
        _conv_rows += (
            f'<div style="display:flex;align-items:baseline;gap:6px;padding:5px 0;border-bottom:1px solid var(--border-subtle)">'
            f'<span style="color:{_tcls};font-size:9px;min-width:28px">{_pconv.upper()[:3]}</span>'
            f'<span style="color:{_dirc};font-size:9px">{_dicon}</span>'
            f'<span style="color:var(--accent-blue);font-size:11px;min-width:38px">{_psym}</span>'
            f'<span style="color:var(--text-muted);font-size:10px">{_ptxt}</span>'
            f'</div>'
        )
    if not _conv_rows:
        _conv_rows = '<div style="color:var(--text-muted);font-size:10px">Brief not yet generated.</div>'

    # regime score for combo card
    _decision = status.get("decision", {})
    _regime_score = _decision.get("regime_score") or 50
    try:
        _regime_score = float(_regime_score)
    except Exception:
        _regime_score = 50.0
    _regime_view = _decision.get("regime_view", _decision.get("regime", "—"))

    # Days running since launch
    _launch_date = date(2026, 4, 13)
    _days_running = (date.today() - _launch_date).days

    # Combined equity
    _combined_equity = a1_equity + a2_equity

    # Claude cost extended data for widget
    all_time_cost = float(costs.get("all_time_cost", 0) or 0)
    total_api_calls = int(costs.get("daily_calls", 0) or 0)
    _sonnet_calls_int = gate.get("total_calls_today", 0)
    try:
        _sonnet_calls_int = int(_sonnet_calls_int)
    except (TypeError, ValueError):
        _sonnet_calls_int = 0
    haiku_calls = max(0, total_api_calls - _sonnet_calls_int)

    # Cost sparkline
    _sp_cost = status.get("spark_cost") or []
    cost_spark = _bbb_sparkline_svg(_sp_cost, "var(--bbb-warn)", width=80)

    # Pluralization helpers
    _a1_pos_str = f"{a1_pos_count} position{'s' if a1_pos_count != 1 else ''}"
    _a2_pos_str = f"{a2_pos_count} structure{'s' if a2_pos_count != 1 else ''}"

    # Churn detection for decisions panel
    _churn_syms = _detect_churn(status.get("a1_decisions", []))
    a1_comp = _a1_decisions_compact_html(status.get("a1_decisions", []), churn_syms=_churn_syms)

    # Top callers for cost widget
    top_callers_widget = ""
    for caller, cdata in sorted(by_caller.items(), key=lambda x: -float(x[1].get("cost", 0) if isinstance(x[1], dict) else 0))[:4]:
        c_cost = float(cdata.get("cost", 0) if isinstance(cdata, dict) else 0)
        c_calls = cdata.get("calls", 0) if isinstance(cdata, dict) else 0
        top_callers_widget += (
            f'<div class="kv"><span class="kv-label" style="font-size:10px">{caller}</span>'
            f'<span class="kv-val" style="font-size:10px">{_fm(c_cost)} &middot; {c_calls} calls</span></div>'
        )

    body = f"""
<div class="container">

{warn_html}
{a2_churn_warn}

{_countdown_strip_html()}
{_bbb_hero_strip_html(status)}
{_bbb_cycle_pulse_html(status)}
{_bbb_voice_strip_html(status, now_et)}

{equity_curve_html}

<div class="tri-grid">
  <div>
    <div class="section-label">Open Positions</div>
    <div class="card" style="padding:10px 14px">{_pos_rows}</div>
    <div class="section-label">Trail Status</div>
    <div class="card" style="padding:0 0 4px">{trail_html}</div>
  </div>
  <div>
    <div class="section-label">Claude Cost</div>
    <div class="card">
      <div class="stat-grid" style="margin-bottom:10px">
        <div class="stat-box"><div class="stat-label">Today</div><div class="stat-val" style="font-size:18px;color:{proj_color}">{_fm(daily_cost)}</div>{cost_spark}</div>
        <div class="stat-box"><div class="stat-label">All-time</div><div class="stat-val" style="font-size:16px;color:var(--text-secondary)">{_fm(all_time_cost)}</div></div>
        <div class="stat-box"><div class="stat-label">Sonnet calls</div><div class="stat-val" style="font-size:18px">{sonnet_calls}</div></div>
        <div class="stat-box"><div class="stat-label">Haiku calls</div><div class="stat-val" style="font-size:18px">{haiku_calls}</div></div>
      </div>
      <div class="kv"><span class="kv-label">Projected/month (22d)</span><span class="kv-val" style="color:{proj_color}">{proj_icon}{_fm(proj_monthly)}</span></div>
      {top_callers_widget}
    </div>
    {_alloc_chart_html(alloc_data, allocator_line)}
  </div>
  <div>
    <div class="section-label">Conviction</div>
    <div class="card" style="padding:10px 14px">{_conv_rows}</div>
  </div>
</div>

<div class="compact-grid">
  <div>
    <div class="section-label">Recent Decisions</div>
    <div class="card" style="padding:10px 12px">
      <div style="font-size:9px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.8px;margin-bottom:6px">A1</div>
      {a1_comp}
      <div style="font-size:9px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.8px;margin:8px 0 6px">A2</div>
      {a2_comp}
    </div>
  </div>
  <div>
    <div class="section-label">System &amp; Performance</div>
    <div class="card">
      <div class="card-row"><span class="card-label">Build</span><span class="card-val">{build_pill}</span></div>
      <div class="card-row"><span class="card-label">Service up since</span><span class="card-val muted" style="font-size:10px">{svc_uptime[:30] if svc_uptime != "unknown" else "unknown"}</span></div>
      {earnings_cal_html}
    </div>
    <div class="section-label">Active Flags</div>
    {flags_html}
  </div>
</div>

<div class="section-label">Performance (7d)</div>
{perf_7d_html}

</div>"""
    body += _gate_tray_html(_gate_tray_data())
    return _page_shell("Overview", nav, body, ticker)


# ── A1 detail page ────────────────────────────────────────────────────────────
def _page_a1(status: dict, now_et: str, debug: bool = False) -> str:
    a1d = status["a1"]
    a1_acc = a1d.get("account")
    a1_mode = status["a1_mode"].get("mode", "unknown").upper()
    a2_mode = status["a2_mode"].get("mode", "unknown").upper()
    nav = _nav_html("a1", now_et, a1_mode, a2_mode)
    warn_html = _warnings_html(status.get("warnings", []))
    ticker = _build_ticker_html(status["positions"])

    a1_pnl, a1_pnl_pct = status.get("today_pnl_a1", (0.0, 0.0))
    a1_pnl_color = "var(--accent-green)" if a1_pnl >= 0 else "var(--accent-red)"
    a1_pnl_sign = "+" if a1_pnl >= 0 else ""

    if a1_acc:
        a1_equity = float(a1_acc.equity or 0)
        a1_cash = float(a1_acc.cash or 0)
        a1_bp = float(a1_acc.buying_power or 0)
        a1_pos_count = len(status["positions"])
        a1_unreal = sum(p["unreal_pl"] for p in status["positions"])
        a1_unreal_c = "#3fb950" if a1_unreal >= 0 else "#f85149"
        a1_unreal_s = "+" if a1_unreal >= 0 else ""
        a1_invested = sum(p["market_val"] for p in status["positions"])
        _lmv = float(a1_acc.long_market_value or 0)
        _dtbp = float(a1_acc.daytrading_buying_power or 0)
        _regt = float(a1_acc.regt_buying_power or 0)
        _tcap = _lmv + _dtbp + _regt
        a1_util = (_lmv / _tcap * 100) if _tcap else 0.0
        a1_leverage = (_lmv / a1_equity) if a1_equity else 0.0
        a1_util_c = "#f85149" if a1_util > 70 else ("#d29922" if a1_util > 50 else "#3fb950")
    else:
        a1_equity = a1_cash = a1_bp = a1_invested = a1_util = a1_leverage = 0.0
        a1_pos_count = 0
        a1_unreal = 0.0; a1_unreal_c = "#8b949e"; a1_unreal_s = ""
        a1_util_c = "#8b949e"

    # A1 equity sparkline
    _sp_a1_eq = status.get("spark_a1_eq") or []
    a1_eq_spark = _bbb_sparkline_svg(_sp_a1_eq, _spark_color(_sp_a1_eq), width=80)

    # Morning brief
    brief = status.get("morning_brief", {})
    brief_time_str = status.get("morning_brief_time", "?")
    if brief:
        tone = brief.get("market_tone", "?").upper()
        picks = brief.get("conviction_picks", [])
        avoid_syms = brief.get("avoid_today", [])
        tl = tone.lower()
        tone_color = "#3fb950" if "bull" in tl else ("#f85149" if "bear" in tl else "#d29922")
        picks_html = ""
        long_picks = [p for p in picks if p.get("direction", "long") == "long"]
        short_picks = [p for p in picks if p.get("direction", "long") == "short"]
        for pick in (long_picks + short_picks)[:8]:
            sym = pick.get("symbol", "")
            direction = pick.get("direction", "long")
            cat_raw = pick.get("catalyst", {})
            cat_text = cat_raw.get("short_text", "") if isinstance(cat_raw, dict) else str(cat_raw or "")
            conviction = pick.get("conviction", "")
            dir_icon = "&#x2191;" if direction == "long" else "&#x2193;"
            dir_color = "#3fb950" if direction == "long" else "#f85149"
            conv_badge = ""
            if conviction == "high":
                conv_badge = ' <span style="font-size:10px;background:#0d2018;color:#3fb950;padding:1px 4px;border-radius:3px">HIGH</span>'
            elif conviction == "medium":
                conv_badge = ' <span style="font-size:10px;background:#2d2208;color:#d29922;padding:1px 4px;border-radius:3px">MED</span>'
            picks_html += (
                f'<div style="padding:5px 0;border-bottom:1px solid #21262d;font-size:13px">'
                f'<b style="color:{dir_color}">{dir_icon} {sym}</b>{conv_badge}'
                f' <span style="color:#8b949e">— {cat_text[:90]}</span></div>'
            )
        if avoid_syms:
            avoid_chips = " ".join(
                f'<span style="font-size:11px;background:#1a1208;color:#8b949e;padding:2px 6px;border-radius:3px;border:1px solid #2d2208">{s}</span>'
                for s in avoid_syms[:8]
            )
            picks_html += (
                f'<div style="padding:6px 0 2px;font-size:11px;color:#4a5080">'
                f'<span style="text-transform:uppercase;letter-spacing:0.06em;margin-right:6px">Avoid today</span>'
                f'{avoid_chips}</div>'
            )
        stale_html = _brief_staleness_html(status.get("morning_brief_mtime", 0))
        brief_html = (
            f'<div style="font-size:12px;color:#8b949e;margin-bottom:8px">'
            f'Tone: <b style="color:{tone_color}">{tone}</b> &nbsp;|&nbsp; {len(long_picks)}↑ {len(short_picks)}↓'
            f' &nbsp;|&nbsp; {brief_time_str}{stale_html}</div>{picks_html}'
        )
    else:
        brief_html = '<div style="color:#8b949e;font-size:13px">Morning brief not yet generated.</div>'

    # Active theses from recent non-HOLD decisions (needs wide lookback — decisions run ~5/min so 50 spans several hours)
    a1_theses = status.get("a1_theses", [])
    if a1_theses:
        theses_html = ""
        for th in a1_theses:
            intent_color = "#3fb950" if th["intent"] in ("BUY", "ADD") else (
                "#f85149" if th["intent"] in ("SELL", "EXIT", "TRIM") else "#d29922")
            tags_html = " ".join(
                f'<span style="font-size:10px;background:#21262d;color:#8b949e;padding:1px 5px;border-radius:3px">{t}</span>'
                for t in th["tags"]
            )
            cat_dot = ' <span style="color:#d29922;font-size:10px">&#x25CF; catalyst active</span>' if th["catalyst_active"] else ""
            theses_html += (
                f'<div class="thesis-card">'
                f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
                f'<b style="color:#58a6ff;font-size:15px">{th["symbol"]}</b>'
                f'<span style="font-size:11px;font-weight:700;color:{intent_color}">{th["intent"]}</span>'
                f'{cat_dot}'
                f'<span style="margin-left:auto;font-size:11px;color:#8b949e">{_to_et(th["ts"])}</span>'
                f'</div>'
                f'<div style="font-size:13px;color:#c9d1d9;margin-bottom:6px">{th["narrative"] or "Signal active — no catalyst narrative recorded."}</div>'
                f'<div style="display:flex;gap:4px;flex-wrap:wrap">{tags_html}</div>'
                f'</div>'
            )
    else:
        theses_html = (
            '<div style="color:#8b949e;font-size:12px">No active theses found in recent decisions.</div>'
            '<div style="color:#4a5080;font-size:11px;margin-top:6px">Populated from non-HOLD Sonnet decisions · '
            'source: <code>memory/decisions.json</code></div>'
        )

    # Compact decisions summary
    a1_comp = _a1_decisions_compact_html(status.get("a1_decisions", []))

    # Expanded last-5 decisions — dim HOLD cycles off-hours (no signal value after close)
    a1_decs = status.get("a1_decisions", [])
    is_mkt = _is_market_hours()
    a1_decs_html = ""
    for d in a1_decs[:5]:
        ts = _to_et(d.get("ts", ""))
        regime = d.get("regime", d.get("regime_view", "?"))
        score = d.get("regime_score")
        score_disp = f"({score})" if score not in (None, "", "?") else ""
        actions = d.get("actions", d.get("ideas", []))
        r_raw = d.get("reasoning", "")
        r_short = r_raw[:140] + ("…" if len(r_raw) > 140 else "")
        act_strs = [f"{a.get('symbol','')} {(a.get('action') or a.get('intent') or '').upper()}"
                    for a in actions[:4] if a.get("symbol")]
        is_hold = not act_strs
        acts_label = "—" if is_hold else ", ".join(act_strs)
        signal_color = "#3fb950" if not is_hold else "#8b949e"
        dim_style = "opacity:0.45;" if (is_hold and not is_mkt) else ""
        regime_color = "#3fb950" if "risk_on" in regime or "bullish" in regime else (
            "#f85149" if "risk_off" in regime or "bearish" in regime else "#d29922")
        a1_decs_html += (
            f'<div style="padding:8px 0;border-bottom:1px solid #21262d;{dim_style}">'
            f'<div style="font-size:12px"><span style="color:#8b949e">[{ts}]</span> '
            f'<span style="color:{regime_color};font-weight:600">{regime}</span> '
            f'<span style="color:#8b949e">{score_disp}</span></div>'
            f'<div style="font-size:12px;color:#c9d1d9;margin:2px 0;font-style:italic">{r_short}</div>'
            f'<div style="font-size:12px;color:{signal_color}">Signals: {acts_label}</div>'
            f'</div>'
        )
    if not a1_decs_html:
        a1_decs_html = '<div style="color:#8b949e;font-size:13px">No decisions yet.</div>'

    # Positions — card design
    positions = status["positions"]
    pos_sorted = sorted(positions, key=lambda x: -abs(x.get("unreal_pl", 0)))
    pos_display = pos_sorted[:10]
    pos_extra = max(0, len(positions) - 10)
    _a1_intraday = status.get("intraday_bars", {"bars": {}, "label": "today"})
    _a1_bars = _a1_intraday.get("bars", {})
    _a1_bar_label = _a1_intraday.get("label", "today")
    positions_html = ""
    for p in pos_display:
        positions_html += _pos_card_html(p, _a1_bars.get(p.get("symbol", ""), []), _a1_bar_label)
    if not positions_html:
        positions_html = '<div style="color:#8b949e;font-size:11px;padding:10px">No open positions.</div>'
    pos_extra_note = (f'<div style="font-size:12px;color:#8b949e;padding:6px 10px">+{pos_extra} more not shown</div>'
                      if pos_extra else "")

    # Today's activity
    gate = status["gate"]
    costs = status["costs"]
    trades = status["trades"]
    decision = status["decision"]
    shadow = status["shadow"]
    sonnet_calls = gate.get("total_calls_today", "—")
    sonnet_skips = gate.get("total_skips_today", "—")
    last_sonnet_ts = _to_et(gate.get("last_sonnet_call_utc", ""))
    last_regime = gate.get("last_regime", "—")
    daily_cost = float(costs.get("daily_cost", 0) or 0)
    proj_monthly_a1 = daily_cost * 22
    if proj_monthly_a1 > 400:
        proj_color = "#f85149"
    elif proj_monthly_a1 > 250:
        proj_color = "#d29922"
    else:
        proj_color = "#3fb950"
    buys_today = status["buys_today"]
    sells_today = status["sells_today"]
    rejected = [t for t in trades if t.get("status") == "rejected"]
    trail_stops = [t for t in trades if t.get("event") == "trail_stop"]

    # VIX from preflight log
    last_vix_pf = "—"
    pf_entries = _jsonl_last(BOT_DIR / "data/status/preflight_log.jsonl", n=1)
    if pf_entries:
        for chk in pf_entries[0].get("checks", []):
            if chk.get("name") == "vix_gate" and "VIX=" in chk.get("message", ""):
                last_vix_pf = chk["message"].split("VIX=")[1].split()[0]

    regime_score = decision.get("regime_score") or "—"
    dec_session = decision.get("session", "—")
    reasoning_raw = decision.get("reasoning", "")
    sentences = reasoning_raw.split(". ")
    reasoning_2s = ". ".join(sentences[:2]) + ("." if len(sentences) > 1 else "")
    if len(reasoning_2s) > 280:
        reasoning_2s = reasoning_2s[:277] + "…"
    last_dec_ts = _to_et(decision.get("ts", ""))

    # Allocator shadow
    alloc = shadow.get("shadow_systems", {}).get("portfolio_allocator", {})
    alloc_st = alloc.get("status", "—")
    alloc_last = _to_et(alloc.get("last_run_at", ""))

    # Decision quality performance widget
    dec_quality_html = _perf_a1_decisions_html(status.get("perf_summary", {}))

    # Recent errors
    import html as _html
    log_errors = status["log_errors"]
    _err_lines = ""
    for err in log_errors:
        lc = "#f85149" if "  ERROR  " in err or "  CRITICAL  " in err else "#d29922"
        _err_lines += f'<div class="log-line" style="color:{lc}">{_html.escape(err[-180:])}</div>'
    if not _err_lines:
        _err_lines = '<div class="log-line" style="color:#3fb950">No recent warnings or errors</div>'
    errors_html = (
        f'<details><summary style="font-size:11px;color:var(--text-muted);cursor:pointer;padding:4px 0">'
        f'Recent system log &#x25BE;</summary>'
        f'<div style="margin-top:6px">{_err_lines}</div></details>'
    )

    a1_orders_html = _fmt_orders_html(a1d.get("recent_orders", []), is_options=False, limit=6)

    debug_section = (
        f'<div class="section-label" style="margin-top:16px">System Log</div>'
        f'<div class="card" style="padding:10px 14px">{errors_html}</div>'
    ) if debug else (
        '<div style="text-align:right;padding:4px 0 16px;font-size:11px;color:#4a5080">'
        '<a href="/a1?debug=1" style="color:#4a5080;text-decoration:none">dev tools →</a></div>'
    )

    body = f"""
<div class="container">
{warn_html}
<div class="section-label">A1 Account Summary</div>
<div class="acct-bar">
  <div class="acct-bar-item"><div class="acct-bar-lbl">Equity</div><div class="acct-bar-val">{_fm(a1_equity)}</div>{a1_eq_spark}</div>
  <div class="acct-bar-item"><div class="acct-bar-lbl">Cash</div><div class="acct-bar-val">{_fm(a1_cash)}</div></div>
  <div class="acct-bar-item"><div class="acct-bar-lbl">Buying Power</div><div class="acct-bar-val">{_fm(a1_bp)}</div></div>
  <div class="acct-bar-item"><div class="acct-bar-lbl">Positions</div><div class="acct-bar-val">{a1_pos_count}</div></div>
  <div class="acct-bar-item"><div class="acct-bar-lbl">Today P&amp;L</div><div class="acct-bar-val" style="color:{a1_pnl_color}">{a1_pnl_sign}{_fm(a1_pnl)} ({a1_pnl_sign}{a1_pnl_pct:.2f}%)</div></div>
  <div class="acct-bar-item"><div class="acct-bar-lbl">Unrealized P&amp;L</div><div class="acct-bar-val" style="color:{a1_unreal_c}">{a1_unreal_s}{_fm(a1_unreal)}</div></div>
</div>
<div style="margin-bottom:10px">
  <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--text-muted);margin-bottom:3px">
    <span>Capital utilization</span><span style="color:{a1_util_c}">${a1_invested/1000:.0f}K deployed · {a1_util:.0f}% utilized · {a1_leverage:.2f}x leverage</span>
  </div>
  <div class="progress-wrap"><div class="progress-fill" style="width:{a1_util:.0f}%;background:{a1_util_c}"></div></div>
</div>

<div style="display:grid;grid-template-columns:3fr 2fr;gap:10px;align-items:start">
  <div>
    <div class="section-label">Morning Brief</div>
    <div class="card">{brief_html}</div>
  </div>
  <div>
    <div class="section-label">Active Theses</div>
    <div class="card" style="max-height:320px;overflow-y:auto">{theses_html}</div>
  </div>
</div>

<div class="section-label">Last 5 Decisions</div>
<div class="card" style="padding:10px 14px">
  <details>
    <summary style="font-size:10px;color:var(--text-muted);cursor:pointer;margin-bottom:6px">Expand with reasoning &#x25BE;</summary>
    <div class="dec-panel" style="margin-top:8px">{a1_decs_html}</div>
  </details>
  <div>{a1_comp}</div>
</div>

<div class="section-label">{a1_pos_count} open position{'s' if a1_pos_count != 1 else ''}</div>
<div class="card" style="padding:4px 12px 4px">
  {positions_html}
  {pos_extra_note}
</div>

<div class="section-label">Recent Orders (last 6)</div>
<div class="card">{a1_orders_html}</div>

<div class="section-label">Today&apos;s Activity</div>
<div class="card">
  <div class="stat-grid" style="margin-bottom:12px">
    <div class="stat-box"><div class="stat-label">Sonnet Calls</div><div class="stat-val">{sonnet_calls}</div></div>
    <div class="stat-box"><div class="stat-label">Skips</div><div class="stat-val muted">{sonnet_skips}</div></div>
    <div class="stat-box"><div class="stat-label">Buys</div><div class="stat-val green">{buys_today}</div></div>
    <div class="stat-box"><div class="stat-label">Sells</div><div class="stat-val red">{sells_today}</div></div>
    <div class="stat-box"><div class="stat-label">Rejected</div><div class="stat-val orange">{len(rejected)}</div></div>
    <div class="stat-box"><div class="stat-label">Stop Trails</div><div class="stat-val">{len(trail_stops)}</div></div>
    <div class="stat-box"><div class="stat-label">Cost Today</div><div class="stat-val" style="font-size:15px;color:{proj_color}">{_fm(daily_cost)}</div></div>
    <div class="stat-box"><div class="stat-label">Proj/Month</div><div class="stat-val" style="font-size:15px;color:{proj_color}">{_fm(proj_monthly_a1)}</div></div>
  </div>
  <div class="kv"><span class="kv-label">Last Sonnet</span><span class="kv-val">{_freshness_stamp(gate.get("last_sonnet_call_utc",""), 30, 90)} {last_sonnet_ts}</span></div>
  <div class="kv"><span class="kv-label">Regime</span><span class="kv-val">{last_regime} (score {regime_score}) &middot; {dec_session}</span></div>
  <div class="kv"><span class="kv-label">VIX</span><span class="kv-val">{last_vix_pf}</span></div>
  <div class="kv"><span class="kv-label">Last Decision</span><span class="kv-val muted">{_freshness_stamp(decision.get("ts",""), 30, 90)} {last_dec_ts}</span></div>
  {f'<div class="reasoning">{reasoning_2s}</div>' if reasoning_2s else ''}
</div>

<div class="compact-grid">
  <div>
    <div class="section-label">Decision Quality (7d)</div>
    {dec_quality_html}
  </div>
  <div>
    <div class="section-label">Allocator Shadow</div>
    <div class="card">
      <div class="kv"><span class="kv-label">Status</span><span class="kv-val">{alloc_st}</span></div>
      <div class="kv"><span class="kv-label">Last Run</span><span class="kv-val muted">{alloc_last}</span></div>
    </div>
  </div>
</div>
{debug_section}
</div>"""
    return _page_shell("A1 Equities", nav, body, ticker)


# ── A2 cinematic helpers ──────────────────────────────────────────────────────

def _a2_closed_structures() -> list:
    try:
        raw = json.loads((BOT_DIR / "data/account2/positions/structures.json").read_text())
        structs = [s for s in raw if isinstance(s, dict)]
        return [s for s in structs if s.get("lifecycle") in ("fully_exited", "closed", "expired")]
    except Exception:
        return []


def _a2_parse_debate(raw: str) -> dict:
    """Split debate_output_raw by agent section headers → {agent_name: text}."""
    if not raw or not isinstance(raw, str):
        return {}
    HEADERS = [
        "DIRECTIONAL ADVOCATE",
        "VOL/STRUCTURE ANALYST",
        "VOL ANALYST",
        "TAPE/FLOW SKEPTIC",
        "TAPE SKEPTIC",
        "RISK OFFICER",
        "SYNTHESIS",
    ]
    # Canonical names after stripping sub-labels
    CANON = {
        "VOL/STRUCTURE ANALYST": "VOL ANALYST",
        "TAPE/FLOW SKEPTIC": "TAPE SKEPTIC",
    }
    sections: dict = {}
    current: str = ""
    buf: list = []
    for line in raw.split("\n"):
        stripped = line.strip().strip("*#=- ").upper()
        matched = next((h for h in HEADERS if stripped == h or stripped.startswith(h + ":")), None)
        if matched:
            if current and buf:
                sections[current] = "\n".join(buf).strip()
            current = CANON.get(matched, matched)
            buf = []
        elif current:
            buf.append(line)
    if current and buf:
        sections[current] = "\n".join(buf).strip()
    return sections


def _strip_md(t: str) -> str:
    """Strip bold/italic markdown markers from text for raw-HTML display."""
    import re as _re
    if not t:
        return t
    t = _re.sub(r'\*\*(.+?)\*\*', r'\1', t)
    t = _re.sub(r'\*(.+?)\*', r'\1', t)
    return t


def _a2_snip(text: str, n: int = 2) -> str:
    """First n sentences from text, markdown stripped, max 220 chars."""
    import re as _re
    if not text:
        return ""
    text = _re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = _re.sub(r"\*(.+?)\*", r"\1", text)
    text = _re.sub(r"^[\-\*\d\.]+\s*", "", text, flags=_re.MULTILINE)
    sentences = _re.split(r"(?<=[.!?])\s+", text.strip())
    snippet = " ".join(s.strip() for s in sentences[:n] if s.strip())
    if len(snippet) > 220:
        snippet = snippet[:217] + "…"
    return snippet.strip()


def _a2_vote_tally(conf, reject) -> dict:
    """Derive 4-agent vote tally from debate_parsed confidence + reject flag."""
    try:
        c = float(conf) if conf is not None else 0.5
    except (TypeError, ValueError):
        c = 0.5
    if reject:
        per = {
            "DIRECTIONAL ADVOCATE": "FLAG",
            "VOL ANALYST": "REJECT",
            "TAPE SKEPTIC": "REJECT",
            "RISK OFFICER": "REJECT",
        }
        return {"verdict": "REJECT", "tally": "1–3", "per_agent": per}
    elif c >= 0.75:
        per = {
            "DIRECTIONAL ADVOCATE": "PROCEED",
            "VOL ANALYST": "PROCEED",
            "TAPE SKEPTIC": "PROCEED",
            "RISK OFFICER": "PROCEED",
        }
        return {"verdict": "PROCEED", "tally": "4–0", "per_agent": per}
    elif c >= 0.60:
        per = {
            "DIRECTIONAL ADVOCATE": "PROCEED",
            "VOL ANALYST": "PROCEED",
            "TAPE SKEPTIC": "PROCEED",
            "RISK OFFICER": "FLAG",
        }
        return {"verdict": "PROCEED", "tally": "3–1", "per_agent": per}
    else:
        per = {
            "DIRECTIONAL ADVOCATE": "FLAG",
            "VOL ANALYST": "FLAG",
            "TAPE SKEPTIC": "FLAG",
            "RISK OFFICER": "FLAG",
        }
        return {"verdict": "FLAG", "tally": "2–2", "per_agent": per}


def _a2_match_decision(struct: dict, all_decs: list) -> dict:
    """Find best matching decision for a structure by symbol + timestamp proximity."""
    underlying = struct.get("underlying", "")
    opened_at = struct.get("opened_at", "")
    if not underlying or not opened_at:
        return {}
    try:
        opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
    except Exception:
        return {}
    best = None
    best_delta = float("inf")
    for d in all_decs:
        sc = d.get("selected_candidate")
        if not isinstance(sc, dict):
            continue
        if sc.get("symbol", "") != underlying:
            continue
        built = d.get("built_at", "")
        try:
            built_dt = datetime.fromisoformat(built.replace("Z", "+00:00"))
            delta = abs((built_dt - opened_dt).total_seconds())
            if delta < best_delta:
                best_delta = delta
                best = d
        except Exception:
            continue
    return best or {}


def _a2_vpill(v: str) -> str:
    """PROCEED/FLAG/REJECT verdict pill HTML."""
    if v == "PROCEED":
        bg = "rgba(52,211,153,.15)"
        col = "var(--bbb-profit)"
    elif v == "FLAG":
        bg = "rgba(251,191,36,.15)"
        col = "var(--bbb-warn)"
    else:
        bg = "rgba(248,113,113,.15)"
        col = "var(--bbb-loss)"
    return (
        '<span style="background:' + bg + ';color:' + col + ';font-family:var(--bbb-font-mono);'
        'font-size:10px;letter-spacing:.06em;text-transform:uppercase;'
        'padding:2px 7px;border-radius:var(--bbb-r-1);white-space:nowrap">' + v + '</span>'
    )


def _a2_payoff_bar_html(max_loss, max_gain, breakeven) -> str:
    """16px payoff bar: red loss zone left, green gain zone right."""
    try:
        ml = float(max_loss) if max_loss else None
    except (TypeError, ValueError):
        ml = None
    try:
        mg = float(max_gain) if max_gain else None
    except (TypeError, ValueError):
        mg = None
    try:
        be = float(breakeven) if breakeven else None
    except (TypeError, ValueError):
        be = None

    is_unlimited = (mg is None or mg <= 0)

    if ml and not is_unlimited:
        total = ml + mg
        loss_pct = ml / total * 100
        gain_pct = mg / total * 100
        rr = mg / ml
        rr_str = "R/R " + f"{rr:.1f}" + "×"
    else:
        loss_pct = 38
        gain_pct = 62
        rr_str = ""

    loss_label = ("-$" + f"{ml:,.0f}") if ml else "−?"
    if is_unlimited:
        gain_label = "unlimited ↑"
        gain_color = "var(--bbb-profit)"
    else:
        gain_label = ("+$" + f"{mg:,.0f}") if mg else "+?"
        gain_color = "var(--bbb-profit)"

    be_html = ""
    if be:
        be_label = "BE $" + f"{be:,.0f}"
        be_html = (
            '<span style="position:absolute;left:50%;transform:translateX(-50%);'
            'font-family:var(--bbb-font-mono);font-size:9px;color:var(--bbb-fg-muted);'
            'white-space:nowrap;top:18px">' + be_label + '</span>'
        )

    right_marker = ""
    if is_unlimited:
        right_marker = (
            '<span style="position:absolute;right:0;top:0;bottom:0;width:2px;'
            'background:var(--bbb-profit)"></span>'
        )

    if is_unlimited:
        gain_bg = "background:linear-gradient(90deg,var(--bbb-profit),transparent)"
    else:
        gain_bg = "background:var(--bbb-profit)"

    return (
        '<div style="position:relative;margin-bottom:20px">'
        '<div style="display:flex;height:16px;border-radius:var(--bbb-r-1);overflow:hidden">'
        '<div style="width:' + f"{loss_pct:.0f}" + '%;background:var(--bbb-loss);opacity:.7"></div>'
        '<div style="width:' + f"{gain_pct:.0f}" + '%;' + gain_bg + ';opacity:.7;position:relative">'
        + right_marker + '</div>'
        '</div>'
        + be_html +
        '<div style="display:flex;justify-content:space-between;margin-top:3px">'
        '<span style="font-family:var(--bbb-font-mono);font-size:9px;color:var(--bbb-loss)">' + loss_label + '</span>'
        '<span style="font-family:var(--bbb-font-mono);font-size:9px;color:' + gain_color + '">' + gain_label + '</span>'
        '</div>'
        '</div>'
    )


def _a2_cinematic_card_html(struct: dict, dec: dict, occ_pnl: dict) -> str:
    """Full cinematic thesis card for one open A2 structure."""
    underlying = struct.get("underlying", "?")
    strategy = struct.get("strategy", "")
    expiry = struct.get("expiration", "")
    long_strike = struct.get("long_strike")
    short_strike = struct.get("short_strike")
    iv_rank = struct.get("iv_rank")
    lifecycle = struct.get("lifecycle", "")
    max_loss = struct.get("max_cost_usd")
    max_profit = struct.get("max_profit_usd")

    dte = _calc_dte(expiry)
    dte_str = (str(dte) + "d") if dte is not None else "?"
    dte_color = "var(--bbb-loss)" if (dte is not None and dte < 7) else "var(--bbb-fg-muted)"

    opened_at_str = struct.get("opened_at", "") or struct.get("built_at", "")
    held_chip = ""
    if opened_at_str:
        try:
            s = opened_at_str.replace("Z", "+00:00")
            if "T" not in s:
                s = s[:19].replace(" ", "T") + "+00:00"
            age_m = int(max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(s)).total_seconds()) / 60)
            held_label = (f"{age_m}m" if age_m < 60 else (f"{age_m // 60}h" if age_m < 1440 else f"{age_m // 1440}d"))
            held_color = "var(--bbb-fg-dim)" if age_m < 2880 else ("var(--bbb-warn)" if age_m < 7200 else "var(--bbb-loss)")
            held_chip = (
                '<span style="font-family:var(--bbb-font-mono);font-size:10px;color:' + held_color + ';'
                'padding:2px 6px;background:var(--bbb-surface-2);border-radius:var(--bbb-r-1)">'
                'held ' + held_label + '</span>'
            )
        except Exception:
            pass

    strat_label = strategy.replace("_", " ").upper() if strategy else "OPTION STRUCTURE"

    if long_strike and short_strike:
        strikes_str = "$" + f"{long_strike:.0f}" + "/$" + f"{short_strike:.0f}"
    elif long_strike:
        strikes_str = "$" + f"{long_strike:.0f}"
    else:
        strikes_str = ""

    lc_colors = {
        "fully_filled": ("rgba(52,211,153,.12)", "var(--bbb-profit)"),
        "open": ("rgba(96,165,250,.12)", "var(--bbb-info)"),
        "submitted": ("rgba(251,191,36,.12)", "var(--bbb-warn)"),
        "proposed": ("rgba(74,79,96,.25)", "var(--bbb-fg-muted)"),
    }
    lc_bg, lc_col = lc_colors.get(lifecycle, ("rgba(74,79,96,.25)", "var(--bbb-fg-muted)"))
    lc_pill = (
        '<span style="background:' + lc_bg + ';color:' + lc_col + ';font-family:var(--bbb-font-mono);'
        'font-size:10px;letter-spacing:.06em;text-transform:uppercase;'
        'padding:2px 7px;border-radius:var(--bbb-r-1)">' + lifecycle + '</span>'
    )

    dp = (dec.get("debate_parsed") or {}) if dec else {}
    if not isinstance(dp, dict):
        dp = {}
    conf = dp.get("confidence")
    reject = dp.get("reject", False)
    key_risks = dp.get("key_risks") or []
    reasons = dp.get("reasons") or []
    sc = (dec.get("selected_candidate") or {}) if dec else {}
    if not isinstance(sc, dict):
        sc = {}

    vote = _a2_vote_tally(conf, reject)
    verdict_pill = _a2_vpill(vote["verdict"])

    if reasons:
        if isinstance(reasons, list):
            thesis = _a2_snip(" ".join(str(r) for r in reasons[:3]), 2)
        else:
            thesis = _a2_snip(str(reasons), 2)
    else:
        thesis = ""
    if not thesis:
        thesis = sc.get("a1_primary_catalyst", "")
    if not thesis:
        thesis = struct.get("catalyst", "")
    thesis = thesis or "—"

    sc_ls = sc.get("long_strike")
    sc_ss = sc.get("short_strike")
    if sc_ls and sc_ss and long_strike and short_strike:
        try:
            if abs(float(sc_ls) - float(long_strike)) > 0.5 or abs(float(sc_ss) - float(short_strike)) > 0.5:
                thesis += (
                    ' <span style="font-family:var(--bbb-font-mono);font-size:10px;'
                    'color:var(--bbb-fg-dim)">'
                    '(strikes adjusted to $' + f"{float(long_strike):.0f}" + '/$' + f"{float(short_strike):.0f}" + ' at fill)</span>'
                )
        except (TypeError, ValueError):
            pass

    a1_score = sc.get("a1_score")
    a1_conv = sc.get("a1_conviction", "")
    conf_str = (f"{float(conf):.0%}") if conf is not None else "?"
    iv_env = _iv_env_label(iv_rank)
    iv_str = f"{iv_rank:.0f}" if iv_rank is not None else "?"

    chip_sty = (
        "font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-fg-muted);"
        "padding:2px 6px;background:var(--bbb-surface-2);border-radius:var(--bbb-r-1);white-space:nowrap"
    )
    chips_parts = []
    if a1_score is not None:
        chips_parts.append('<span style="' + chip_sty + '">A1 ' + f"{float(a1_score):.0f}" + '</span>')
    chips_parts.append('<span style="' + chip_sty + '">conf ' + conf_str + '</span>')
    if iv_rank is not None:
        chips_parts.append('<span style="' + chip_sty + '">IV ' + iv_str + ' · ' + iv_env + '</span>')
    if a1_conv:
        chips_parts.append('<span style="' + chip_sty + '">' + str(a1_conv) + '</span>')
    chips_html = " ".join(chips_parts)

    raw_debate = (dec.get("debate_output_raw") or "") if dec else ""
    sections = _a2_parse_debate(raw_debate)

    _agent_cfgs = [
        ("DIRECTIONAL ADVOCATE", "#378ADD", "#185FA5"),
        ("VOL ANALYST",          "#7F77DD", "#534AB7"),
        ("TAPE SKEPTIC",         "#BA7517", "#854F0B"),
        ("RISK OFFICER",         "#D85A30", "#993C1D"),
    ]
    per_agent = vote["per_agent"]
    debate_cards_html = ""
    for agent_key, border_col, name_col in _agent_cfgs:
        agent_text = sections.get(agent_key, "")
        snip = _a2_snip(agent_text, 2) if agent_text else "—"
        av = per_agent.get(agent_key, "FLAG")
        if av == "PROCEED":
            av_pill = (
                '<span style="background:#EAF3DE;color:#3B6D11;font-family:var(--bbb-font-mono);'
                'font-size:10px;padding:2px 6px;border-radius:var(--bbb-r-1);white-space:nowrap">'
                '&#10003; PROCEED</span>'
            )
        elif av == "REJECT":
            av_pill = (
                '<span style="background:#FCEBEB;color:#A32D2D;font-family:var(--bbb-font-mono);'
                'font-size:10px;padding:2px 6px;border-radius:var(--bbb-r-1);white-space:nowrap">'
                '&#10005; REJECT</span>'
            )
        else:
            av_pill = _a2_vpill(av)
        debate_cards_html += (
            '<div style="background:var(--bbb-surface-2);border:1px solid var(--bbb-border);'
            'border-left:3px solid ' + border_col + ';border-radius:0 12px 12px 0;'
            'padding:var(--bbb-s-2) var(--bbb-s-3);height:100%;display:flex;flex-direction:column">'
            '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">'
            '<span style="font-family:var(--bbb-font-mono);font-size:9px;letter-spacing:.06em;'
            'text-transform:uppercase;color:' + name_col + '">' + agent_key + '</span>'
            + av_pill +
            '</div>'
            '<div style="font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg);'
            'line-height:1.45">' + snip + '</div>'
            '</div>'
        )

    _cb_pass = vote["verdict"] == "PROCEED"
    _cb_rej  = vote["verdict"] == "REJECT"
    if _cb_pass:
        _cb_bg = "#EAF3DE"; _cb_bd = "#C0DD97"; _cb_fg = "#27500A"; _cb_icon = "&#9679;"
    elif _cb_rej:
        _cb_bg = "#FCEBEB"; _cb_bd = "#E8B4B4"; _cb_fg = "#A32D2D"; _cb_icon = "&#10005;"
    else:
        _cb_bg = "rgba(251,191,36,.10)"; _cb_bd = "rgba(251,191,36,.40)"; _cb_fg = "#854F0B"; _cb_icon = "&#9675;"
    _cb_label = (
        "Approved " + vote["tally"] if _cb_pass else
        "Rejected " + vote["tally"] if _cb_rej else
        "No consensus " + vote["tally"]
    )
    _cb_meta = "auto-vote · conf " + conf_str + " · IV " + iv_str + " " + iv_env
    consensus_bar_html = (
        '<div style="background:' + _cb_bg + ';border:1px solid ' + _cb_bd + ';'
        'border-radius:var(--bbb-r-1);padding:5px 10px;margin-top:6px;'
        'display:flex;align-items:center;gap:8px">'
        '<span style="font-family:var(--bbb-font-mono);font-size:11px;color:' + _cb_fg + ';flex:1">'
        + _cb_icon + ' Consensus &rarr; ' + _cb_label + '</span>'
        '<span style="font-family:var(--bbb-font-mono);font-size:10px;color:' + _cb_fg + ';'
        'opacity:.7;white-space:nowrap;flex:none">' + _cb_meta + '</span>'
        '</div>'
    )

    risks_html = ""
    for risk in (key_risks or [])[:2]:
        if risk:
            risks_html += (
                '<div style="font-family:var(--bbb-font-mono);font-size:11px;'
                'color:var(--bbb-warn);margin-top:3px;white-space:normal">⚑ ' + str(risk) + '</div>'
            )

    net_pnl = 0.0
    matched = False
    for leg in struct.get("legs", []):
        occ = leg.get("occ_symbol", "")
        if occ in occ_pnl:
            net_pnl += occ_pnl[occ]
            matched = True

    breakeven = sc.get("breakeven")
    payoff_html = _a2_payoff_bar_html(max_loss, max_profit, breakeven)

    if matched and max_loss:
        pnl_sign = "+" if net_pnl >= 0 else ""
        pnl_color = "var(--bbb-profit)" if net_pnl >= 0 else "var(--bbb-loss)"
        pnl_pct = net_pnl / float(max_loss) * 100
        pnl_pct_str = (("+" if pnl_pct >= 0 else "") + f"{pnl_pct:.1f}% of max loss")
        mtm_html = (
            '<div style="font-family:var(--bbb-font-mono);font-size:18px;color:' + pnl_color + ';'
            'font-weight:500;letter-spacing:-.01em">' + pnl_sign + '$' + f"{net_pnl:,.2f}" + '</div>'
            '<div style="font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-fg-muted);'
            'margin-top:2px">' + pnl_pct_str + '</div>'
        )
    else:
        mtm_html = '<div style="font-family:var(--bbb-font-mono);font-size:13px;color:var(--bbb-fg-dim)">P&amp;L: awaiting fill</div>'

    delta_v = sc.get("delta")
    theta_v = sc.get("theta")
    vega_v = sc.get("vega")

    def _gv(v):
        try:
            return f"{float(v):.3f}"
        except (TypeError, ValueError):
            return "—"

    greeks_html = (
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;margin-top:8px">'
        '<div><span style="font-family:var(--bbb-font-mono);font-size:9px;color:var(--bbb-fg-dim);'
        'text-transform:uppercase;letter-spacing:.06em">Δ delta</span><br>'
        '<span style="font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg)">' + _gv(delta_v) + '</span></div>'
        '<div><span style="font-family:var(--bbb-font-mono);font-size:9px;color:var(--bbb-fg-dim);'
        'text-transform:uppercase;letter-spacing:.06em">Θ /day</span><br>'
        '<span style="font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg)">' + _gv(theta_v) + '</span></div>'
        '<div><span style="font-family:var(--bbb-font-mono);font-size:9px;color:var(--bbb-fg-dim);'
        'text-transform:uppercase;letter-spacing:.06em">ν vega</span><br>'
        '<span style="font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg)">' + _gv(vega_v) + '</span></div>'
        '<div><span style="font-family:var(--bbb-font-mono);font-size:9px;color:var(--bbb-fg-dim);'
        'text-transform:uppercase;letter-spacing:.06em">IV rank</span><br>'
        '<span style="font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg)">' + iv_str + '</span></div>'
        '</div>'
    )

    stop_html = (
        '<span style="font-family:var(--bbb-font-mono);font-size:9px;color:var(--bbb-fg-dim);'
        'padding:1px 5px;background:var(--bbb-surface-2);border-radius:var(--bbb-r-1);margin-right:4px">'
        'stop 50% max loss</span>'
        '<span style="font-family:var(--bbb-font-mono);font-size:9px;color:var(--bbb-fg-dim);'
        'padding:1px 5px;background:var(--bbb-surface-2);border-radius:var(--bbb-r-1)">'
        'take 80% max gain</span>'
    )

    dec_id = (dec.get("decision_id") or "") if dec else ""
    is_unlimited = (max_profit is None or max_profit <= 0)
    footer_parts = []
    if breakeven:
        footer_parts.append("BE $" + f"{float(breakeven):,.0f}")
    if not is_unlimited and max_profit:
        footer_parts.append("+$" + f"{float(max_profit):,.0f}" + " max gain")
    else:
        footer_parts.append("unlimited ↑ max gain")
    if max_loss:
        footer_parts.append("−$" + f"{float(max_loss):,.0f}" + " max loss")
    if not is_unlimited and max_loss and max_profit:
        footer_parts.append("R/R " + f"{float(max_profit)/float(max_loss):.1f}" + "×")
    if dec_id:
        footer_parts.append(dec_id[:16])
    footer_str = " · ".join(footer_parts)

    strikes_cell = (
        '<span style="font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg-muted)">'
        + strikes_str + '</span> '
    ) if strikes_str else ""

    return (
        '<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
        'border-radius:var(--bbb-r-3);margin-bottom:var(--bbb-s-4);overflow:hidden">'
        # header
        '<div style="padding:12px 20px;border-bottom:1px solid var(--bbb-border);'
        'display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
        '<span style="font-family:var(--bbb-font-mono);font-size:22px;font-weight:500;'
        'color:var(--bbb-fg);letter-spacing:-.01em">' + underlying + '</span>'
        '<span style="font-family:var(--bbb-font-mono);font-size:11px;letter-spacing:.06em;'
        'text-transform:uppercase;color:var(--bbb-fg-muted)">' + strat_label + '</span>'
        + strikes_cell +
        '<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-dim)">' + expiry + '</span>'
        '<span style="font-family:var(--bbb-font-mono);font-size:10px;color:' + dte_color + ';'
        'padding:2px 6px;background:var(--bbb-surface-2);border-radius:var(--bbb-r-1)">' + dte_str + ' DTE</span>'
        + held_chip +
        '<span style="margin-left:auto;display:flex;gap:4px">' + lc_pill + ' ' + verdict_pill + '</span>'
        '</div>'
        # thesis + chips
        '<div style="padding:8px 20px;border-bottom:1px solid var(--bbb-border)">'
        '<div style="font-family:var(--bbb-font-sans);font-size:13px;color:var(--bbb-fg);margin-bottom:4px">' + thesis + '</div>'
        '<div style="display:flex;flex-wrap:wrap;gap:4px">' + chips_html + '</div>'
        '</div>'
        # body: debate + payoff
        '<div style="display:grid;grid-template-columns:1fr 260px;gap:12px;padding:12px 20px">'
        '<div>'
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px;align-items:stretch">'
        + debate_cards_html +
        '</div>'
        + consensus_bar_html
        + risks_html +
        '</div>'
        '<div>' + payoff_html + mtm_html +
        '<div style="margin-top:8px">' + stop_html + '</div>'
        + greeks_html +
        '</div>'
        '</div>'
        # footer
        '<div style="padding:4px 20px;border-top:1px solid var(--bbb-border);background:var(--bbb-surface-2)">'
        '<span style="font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-fg-dim)">' + footer_str + '</span>'
        '</div>'
        '</div>'
    )


def _a2_pipeline_section_html(all_decs: list) -> str:
    """Last 5 A2 candidate decisions as compressed cards."""
    if not all_decs:
        return (
            '<div style="color:var(--bbb-fg-muted);font-size:13px;'
            'font-family:var(--bbb-font-mono)">No A2 decisions yet.</div>'
        )
    recent = all_decs[:5]
    last = recent[0]
    cand_sets = last.get("candidate_sets") or []
    n_syms = len(cand_sets)
    n_gen = sum(len(cs.get("generated_candidates") or []) for cs in cand_sets if isinstance(cs, dict))
    n_surv = sum(len(cs.get("surviving_candidates") or []) for cs in cand_sets if isinstance(cs, dict))
    n_debated = 1 if last.get("selected_candidate") else 0
    n_filled = sum(1 for d in all_decs if d.get("execution_result") == "submitted")
    funnel = (
        str(n_syms) + " symbols → " + str(n_gen) + " generated → " +
        str(n_surv) + " passed veto → 1 debated → " +
        str(n_filled) + "/" + str(len(all_decs)) + " filled"
    )
    cards_html = ""
    for d in recent:
        sc = d.get("selected_candidate") or {}
        sym = sc.get("symbol", "") if isinstance(sc, dict) else ""
        if not sym:
            sym = (d.get("no_trade_reason") or "no_trade")[:8]
        strat = sc.get("structure_type", "") if isinstance(sc, dict) else ""
        dp = d.get("debate_parsed") or {}
        conf = dp.get("confidence") if isinstance(dp, dict) else None
        reject = dp.get("reject", False) if isinstance(dp, dict) else False
        vote = _a2_vote_tally(conf, reject)
        result = d.get("execution_result", "?")
        reason = d.get("no_trade_reason") or ""
        ts = _to_et(d.get("built_at", ""))
        cs_list = d.get("candidate_sets") or []
        veto_str = ""
        for cs in cs_list[:2]:
            if isinstance(cs, dict):
                for v in (cs.get("vetoed_candidates") or [])[:1]:
                    if isinstance(v, dict) and v.get("reason"):
                        veto_str = _prettify_rejection(v["reason"])[:60]
                        break
            if veto_str:
                break
        if not veto_str and reason:
            veto_str = _prettify_rejection(reason)[:60]
        result_color = "var(--bbb-profit)" if result == "submitted" else "var(--bbb-fg-muted)"
        cards_html += (
            '<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
            'border-radius:var(--bbb-r-2);padding:8px 12px">'
            '<div style="display:flex;align-items:center;gap:4px;margin-bottom:4px">'
            '<span style="font-family:var(--bbb-font-mono);font-size:14px;font-weight:500;color:var(--bbb-fg)">' + sym + '</span>'
            + _a2_vpill(vote["verdict"]) +
            '</div>'
            '<div style="font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-fg-muted);margin-bottom:2px">'
            + (strat.replace("_", " ").upper()[:20] if strat else "—") +
            '</div>'
            + ('<div style="font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-fg-dim);margin-bottom:2px">'
               + veto_str + '</div>' if veto_str else "") +
            '<div style="display:flex;justify-content:space-between;margin-top:4px">'
            '<span style="font-family:var(--bbb-font-mono);font-size:10px;color:' + result_color + '">' + result + '</span>'
            '<span style="font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-fg-dim)">' + ts + '</span>'
            '</div>'
            '</div>'
        )
    return (
        '<div style="margin-bottom:var(--bbb-s-4)">'
        '<div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">'
        '<span style="font-family:var(--bbb-font-sans);font-size:12px;font-weight:500;'
        'letter-spacing:.08em;text-transform:uppercase;color:var(--bbb-fg-muted)">Pending Pipeline</span>'
        '<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-dim)">' + funnel + '</span>'
        '</div>'
        '<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:8px">'
        + cards_html +
        '</div>'
        '</div>'
    )


def _a2_closed_section_html(structs: list, all_decs: list) -> str:
    """Closed/exited structures: what we said vs what happened."""
    if not structs:
        return ""
    cards_html = ""
    for struct in structs[:5]:
        underlying = struct.get("underlying", "?")
        strategy = struct.get("strategy", "")
        expiry = struct.get("expiration", "")
        closed_at = struct.get("closed_at")
        realized_pnl = struct.get("realized_pnl")

        dec = _a2_match_decision(struct, all_decs)
        dp = (dec.get("debate_parsed") or {}) if dec else {}
        if not isinstance(dp, dict):
            dp = {}
        sc = (dec.get("selected_candidate") or {}) if dec else {}
        if not isinstance(sc, dict):
            sc = {}

        reasons = dp.get("reasons") or []
        if isinstance(reasons, list):
            thesis = _a2_snip(" ".join(str(r) for r in reasons[:3]), 2)
        else:
            thesis = _a2_snip(str(reasons), 2)
        if not thesis:
            thesis = sc.get("a1_primary_catalyst") or struct.get("catalyst") or "—"

        conf = dp.get("confidence")
        reject = dp.get("reject", False)
        vote = _a2_vote_tally(conf, reject)
        conf_str = f"{float(conf):.0%}" if conf is not None else "?"

        if realized_pnl is not None:
            pnl_val = float(realized_pnl)
            if pnl_val > 0:
                v_text, v_bg, v_col = "THESIS CORRECT", "rgba(52,211,153,.12)", "var(--bbb-profit)"
            elif pnl_val < 0:
                v_text, v_bg, v_col = "THESIS BROKEN", "rgba(248,113,113,.12)", "var(--bbb-loss)"
            else:
                v_text, v_bg, v_col = "PENDING SETTLEMENT", "rgba(251,191,36,.12)", "var(--bbb-warn)"
        else:
            v_text, v_bg, v_col = "PENDING SETTLEMENT", "rgba(251,191,36,.12)", "var(--bbb-warn)"

        verdict_chip = (
            '<span style="background:' + v_bg + ';color:' + v_col + ';font-family:var(--bbb-font-mono);'
            'font-size:10px;letter-spacing:.06em;text-transform:uppercase;'
            'padding:2px 7px;border-radius:var(--bbb-r-1)">' + v_text + '</span>'
        )

        if realized_pnl is not None:
            pnl_val = float(realized_pnl)
            pnl_col = "var(--bbb-profit)" if pnl_val >= 0 else "var(--bbb-loss)"
            pnl_sign = "+" if pnl_val >= 0 else ""
            pnl_line = (
                '<span style="font-family:var(--bbb-font-mono);font-size:16px;color:' + pnl_col + ';font-weight:500">'
                + pnl_sign + '$' + f"{pnl_val:,.2f}" + '</span>'
            )
        else:
            pnl_line = (
                '<span style="font-family:var(--bbb-font-mono);font-size:13px;color:var(--bbb-fg-muted)">'
                'P&amp;L not recorded</span>'
            )

        audit_log = struct.get("audit_log") or []
        last_audit = ""
        if audit_log:
            last_entry = audit_log[-1]
            if isinstance(last_entry, dict):
                last_audit = last_entry.get("msg", "")
        audit_html = (
            '<div style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted);margin-top:4px">'
            + last_audit[:120] + '</div>'
        ) if last_audit else ""

        closed_str = _to_et(closed_at) if closed_at else "—"
        strat_label = strategy.replace("_", " ").upper() if strategy else "OPTION"

        cards_html += (
            '<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
            'border-radius:var(--bbb-r-3);margin-bottom:12px;overflow:hidden">'
            '<div style="padding:8px 20px;border-bottom:1px solid var(--bbb-border);'
            'display:flex;align-items:center;gap:8px">'
            '<span style="font-family:var(--bbb-font-mono);font-size:16px;font-weight:500;color:var(--bbb-fg)">' + underlying + '</span>'
            '<span style="font-family:var(--bbb-font-mono);font-size:10px;letter-spacing:.06em;'
            'text-transform:uppercase;color:var(--bbb-fg-muted)">' + strat_label + '</span>'
            '<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-dim)">' + expiry + '</span>'
            '<span style="margin-left:auto">' + verdict_chip + '</span>'
            '</div>'
            '<div style="display:grid;grid-template-columns:1fr 1fr">'
            '<div style="padding:12px 20px;border-right:1px solid var(--bbb-border)">'
            '<div style="font-family:var(--bbb-font-sans);font-size:10px;font-weight:500;letter-spacing:.08em;'
            'text-transform:uppercase;color:var(--bbb-fg-dim);margin-bottom:4px">What we said</div>'
            '<div style="font-family:var(--bbb-font-sans);font-size:12px;color:var(--bbb-fg);'
            'line-height:1.45;margin-bottom:4px">' + thesis + '</div>'
            '<div style="font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-fg-muted)">'
            'conf ' + conf_str + ' · ' + _a2_vpill(vote["verdict"]) + '</div>'
            '</div>'
            '<div style="padding:12px 20px">'
            '<div style="font-family:var(--bbb-font-sans);font-size:10px;font-weight:500;letter-spacing:.08em;'
            'text-transform:uppercase;color:var(--bbb-fg-dim);margin-bottom:4px">What happened</div>'
            + pnl_line + audit_html +
            '<div style="font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-fg-dim);margin-top:4px">'
            'closed ' + closed_str + '</div>'
            '</div>'
            '</div>'
            '</div>'
        )

    return (
        '<div style="margin-bottom:var(--bbb-s-4)">'
        '<div style="font-family:var(--bbb-font-sans);font-size:12px;font-weight:500;letter-spacing:.08em;'
        'text-transform:uppercase;color:var(--bbb-fg-muted);margin-bottom:8px">'
        'Closed Structures — What We Said vs What Happened</div>'
        + cards_html +
        '</div>'
    )


def _a2_hero_strip_html(status: dict) -> str:
    """5-pane hero strip + cycle pulse strip for A2."""
    a2d = status.get("a2") or {}
    a2_acc = a2d.get("account")
    a2_equity = float(a2_acc.equity or 0) if a2_acc else 0.0

    a2_pnl, a2_pnl_pct = status.get("today_pnl_a2", (0.0, 0.0))
    pnl_color = "var(--bbb-profit)" if a2_pnl >= 0 else "var(--bbb-loss)"
    pnl_sign = "+" if a2_pnl >= 0 else ""

    structs_all = _a2_structures()
    n_open = len(structs_all)

    a2_decs = status.get("a2_decisions") or []
    n_filled = sum(1 for d in a2_decs if d.get("execution_result") == "submitted")
    n_total = len(a2_decs)
    fill_rate = n_filled / n_total * 100 if n_total > 0 else 0.0
    fill_color = "var(--bbb-profit)" if fill_rate >= 25 else "var(--bbb-warn)"

    if structs_all:
        iv_vals = [s.get("iv_rank") for s in structs_all if s.get("iv_rank") is not None]
        if iv_vals:
            avg_iv = sum(float(v) for v in iv_vals) / len(iv_vals)
            iv_env_str = _iv_env_label(avg_iv)
            iv_num_str = f"{avg_iv:.0f}"
        else:
            iv_env_str, iv_num_str = "—", "—"
    else:
        iv_env_str, iv_num_str = "—", "—"

    last_dec = status.get("a2_decision") or {}
    last_ts = _to_et(last_dec.get("built_at", "")) if last_dec else "—"
    last_result = last_dec.get("execution_result", "") if last_dec else ""
    last_sc = (last_dec.get("selected_candidate") or {}) if last_dec else {}
    if isinstance(last_sc, dict) and last_sc.get("symbol"):
        sym = last_sc["symbol"]
        strat = last_sc.get("structure_type", "")
        if last_result == "submitted":
            last_action = "submitted " + sym + " " + strat.replace("_", " ")
        else:
            nr = last_dec.get("no_trade_reason") or ""
            last_action = (last_result + " — " + nr).strip("— ") if nr else (last_result or "—")
    else:
        nr = last_dec.get("no_trade_reason") or "" if last_dec else ""
        last_action = nr[:80] if nr else (last_result[:80] if last_result else "—")

    cand_sets = (last_dec.get("candidate_sets") or []) if last_dec else []
    n_syms = len(cand_sets)
    n_gen = sum(len(cs.get("generated_candidates") or []) for cs in cand_sets if isinstance(cs, dict))
    n_surv = sum(len(cs.get("surviving_candidates") or []) for cs in cand_sets if isinstance(cs, dict))
    n_deb = 1 if (last_dec.get("selected_candidate") if last_dec else None) else 0
    funnel_str = str(n_syms) + "→" + str(n_gen) + "→" + str(n_surv) + "→" + str(n_deb)

    elapsed = last_dec.get("elapsed_seconds") if last_dec else None
    elapsed_str = f"{float(elapsed):.1f}s" if elapsed else "—"

    stages = ["CANDIDATES", "FILTER", "DEBATE", "APPROVAL", "FILL"]
    stage_html = ""
    for i, s in enumerate(stages):
        stage_html += '<span class="bbb-stage is-done">' + s + '</span>'
        if i < len(stages) - 1:
            stage_html += '<span style="font-size:8px;color:var(--bbb-fg-dim)">›</span>'

    dot_class = "bbb-pulse-dot is-idle" if last_result else "bbb-pulse-dot"

    def _pane(label, num_html, meta_html=""):
        return (
            '<div class="bbb-hero-pane" style="padding:16px 20px">'
            '<div class="bbb-hero-label">' + label + '</div>'
            + num_html
            + (meta_html if meta_html else "") +
            '</div>'
        )

    def _num(val, color="var(--bbb-fg)"):
        return (
            '<span class="bbb-hero-num" style="font-size:28px;color:' + color + '">'
            + val + '</span>'
        )

    def _meta(text, color=""):
        sty = (';color:' + color) if color else ""
        return (
            '<span class="bbb-hero-meta" style="display:block' + sty + '">' + text + '</span>'
        )

    sp_a2_eq = status.get("spark_a2_eq") or []
    sp_a2_pnl = status.get("spark_a2_pnl") or []
    spark_eq_svg = _bbb_sparkline_svg(sp_a2_eq, _spark_color(sp_a2_eq))
    spark_pnl_svg = _bbb_sparkline_svg(sp_a2_pnl, _spark_color(sp_a2_pnl))

    panes_html = (
        _pane("A2 Equity", _num(_fm(a2_equity)), spark_eq_svg) +
        _pane("Today P&amp;L", _num(pnl_sign + _fm(a2_pnl), pnl_color), _meta(pnl_sign + f"{a2_pnl_pct:.2f}%") + spark_pnl_svg) +
        _pane("Fill Rate", _num(f"{fill_rate:.0f}%", fill_color), _meta("fill rate · target ≥ 25%", "var(--bbb-warn)")) +
        _pane("Open Structures", _num(str(n_open))) +
        _pane("IV Environment", _num(iv_num_str), _meta(iv_env_str))
    )

    return (
        '<div class="bbb-hero-strip" style="grid-template-columns:repeat(5,1fr);margin-bottom:12px">'
        + panes_html +
        '</div>'
        '<div class="bbb-pulse-strip" style="margin-bottom:var(--bbb-s-4)">'
        '<div class="' + dot_class + '"></div>'
        '<span class="bbb-pulse-text">' + last_action + '</span>'
        '<span class="bbb-pulse-cost">'
        + _freshness_stamp(last_dec.get("built_at", "") if last_dec else "", 20, 60)
        + ' · ' + elapsed_str + ' · ' + last_ts + '</span>'
        + stage_html +
        '<span style="font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-fg-dim);margin-left:8px">'
        + funnel_str + '</span>'
        '</div>'
    )


# ── A2 detail page ────────────────────────────────────────────────────────────
def _page_a2(status: dict, now_et: str) -> str:
    a2d = status["a2"]
    a1_mode = status["a1_mode"].get("mode", "unknown").upper()
    a2_mode = status["a2_mode"].get("mode", "unknown").upper()
    nav = _nav_html("a2", now_et, a1_mode, a2_mode)
    warn_html = _warnings_html(status.get("warnings", []))
    ticker = _build_ticker_html(status["positions"])

    # Build occ_pnl lookup from live A2 positions
    occ_pnl: dict = {}
    for p in a2d.get("positions", []):
        sym = getattr(p, "symbol", "")
        unreal = float(getattr(p, "unrealized_pl", 0) or 0)
        if sym:
            occ_pnl[sym] = unreal

    # Load enough decisions for matching: use up to 50 for the cinematic cards
    all_decs = _last_n_a2_decisions(50)

    # ── 1. Open structures — cinematic cards ──────────────────────────────────
    structs_open = _a2_structures()
    if structs_open:
        open_label = (
            '<div style="font-family:var(--bbb-font-sans);font-size:12px;font-weight:500;'
            'letter-spacing:.08em;text-transform:uppercase;color:var(--bbb-fg-muted);'
            'margin-bottom:8px">Open Structures (' + str(len(structs_open)) + ')</div>'
        )
        open_cards = "".join(
            _a2_cinematic_card_html(s, _a2_match_decision(s, all_decs), occ_pnl)
            for s in structs_open
        )
        open_html = open_label + open_cards
    else:
        open_html = (
            '<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
            'border-radius:var(--bbb-r-3);padding:24px 20px;margin-bottom:var(--bbb-s-4);'
            'font-family:var(--bbb-font-mono);font-size:13px;color:var(--bbb-fg-muted)">'
            'No open structures.</div>'
        )

    # ── 2. Pending pipeline ───────────────────────────────────────────────────
    pipeline_html = _a2_pipeline_section_html(all_decs)

    # ── 3. Closed structures ──────────────────────────────────────────────────
    closed_structs = _a2_closed_structures()
    closed_html = _a2_closed_section_html(closed_structs, all_decs)

    # ── 4. Strategy performance ───────────────────────────────────────────────
    a2_perf_html = _perf_a2_strategies_html(status.get("perf_summary", {}))
    perf_section = (
        '<div style="font-family:var(--bbb-font-sans);font-size:12px;font-weight:500;'
        'letter-spacing:.08em;text-transform:uppercase;color:var(--bbb-fg-muted);'
        'margin-bottom:8px">Strategy Performance (7d)</div>'
        + a2_perf_html
    )

    a2_hero_html = _a2_hero_strip_html(status)

    body = f"""
<div class="container">
{warn_html}
{a2_hero_html}
{open_html}
{pipeline_html}
{closed_html}
{perf_section}
<div style="height:24px"></div>
</div>"""
    return _page_shell("A2 Options", nav, body, ticker)


# ── Signal map data helper ────────────────────────────────────────────────────

def _signal_map_data() -> dict:
    """Load scored symbols from signal_scores.json. Returns {sym: {score, direction}} dict."""
    try:
        p = BOT_DIR / "data" / "market" / "signal_scores.json"
        if not p.exists():
            return {}
        with open(p) as f:
            d = json.load(f)
        return d.get("scored_symbols", {})
    except Exception:
        return {}


# ── Brief schedule helper ─────────────────────────────────────────────────────
# Mirrors the slot list in scheduler._maybe_run_intelligence_brief.
_BRIEF_SLOTS_ET = [
    (4, 0), (9, 25), (10, 30), (11, 30), (12, 30),
    (13, 30), (14, 30), (15, 30), (16, 30),
]


def _next_brief_slot_display() -> str:
    """Human-readable label for the next scheduled intelligence brief slot."""
    et = datetime.now(timezone.utc) + ET_OFFSET
    now_min = et.hour * 60 + et.minute
    if et.weekday() < 5:
        for h, m in _BRIEF_SLOTS_ET:
            if h * 60 + m > now_min:
                return et.replace(hour=h, minute=m, second=0, microsecond=0).strftime("%-I:%M %p ET today")
    # Past last slot or weekend — next is 4:00 AM next trading day
    nxt = et + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    nxt = nxt.replace(hour=4, minute=0, second=0, microsecond=0)
    day_label = "tomorrow" if (nxt.date() == (et + timedelta(days=1)).date()) else nxt.strftime("%A")
    return nxt.strftime(f"%-I:%M %p ET {day_label}")


# ── Thesis cluster map (force-directed graph for /brief) ──────────────────────

_THESIS_CLUSTERS: dict = {
    "AI/semis":           ["ai", "artificial intelligence", "semiconductor", "semis", "chip", "gpu",
                           "wafer", "blackwell", "data center", "machine learning", "neural", "cloud"],
    "tankers/energy":     ["tanker", "crude", "oil", "energy sector", "lng", "shipping",
                           "crude route", "upstream", "downstream", "refin"],
    "macro/currency":     ["yen", "usd/jpy", "jpy", "eur/usd", "dollar weakness", "dollar strength",
                           "fed", "inflation", "rate cut", "rate hike", "yields", "10-year", "currency"],
    "payments/financials":["payment", "consumer spend", "financial", "bank", "credit",
                           "fintech", "visa", "mastercard", "paypal"],
    "healthcare":         ["health", "biotech", "pharma", "drug", "medical", "clinical",
                           "fda", "trial", "oncology"],
    "defense":            ["defense", "military", "government", "contractor", "aerospace",
                           "lockheed", "raytheon"],
    "industrials":        ["industrial", "capex", "manufacturing", "construction",
                           "infrastructure", "logistics"],
}

_THESIS_MAP_JS = '''<script>
(function(){
  var NS='http://www.w3.org/2000/svg';
  var nodes=%%NODES%%;
  var links=%%LINKS%%;
  if(!nodes.length) return;

  function init(){
    var wrap=document.getElementById('tm-wrap');
    var svgEl=document.getElementById('tm-svg');
    var tip=document.getElementById('tm-tip');
    if(!svgEl||!wrap) return;
    var W=wrap.getBoundingClientRect().width;
    if(!W||W<50) W=680;
    var H=300;
    svgEl.setAttribute('width',W);
    svgEl.setAttribute('height',H);
    svgEl.style.width=W+'px';

    // Resolve link source/target id strings to node objects
    var nm={};
    nodes.forEach(function(n){ nm[n.id]=n; });
    links.forEach(function(l){ l.s=nm[l.source]; l.t=nm[l.target]; });

    // Seed positions evenly around a circle to minimise initial overlap
    nodes.forEach(function(n,i){
      var angle=(i/nodes.length)*Math.PI*2;
      var rad=Math.min(W,H)*0.28;
      n.x=W/2+Math.cos(angle)*rad+(Math.random()-0.5)*24;
      n.y=H/2+Math.sin(angle)*rad*(H/W*1.4)+(Math.random()-0.5)*16;
      n.vx=0; n.vy=0; n.fixed=false;
    });

    // SVG edge layer (rendered beneath nodes)
    var gE=document.createElementNS(NS,'g');
    svgEl.appendChild(gE);
    var lineEls=links.map(function(l){
      var el=document.createElementNS(NS,'line');
      el.setAttribute('stroke','#505870');
      el.setAttribute('stroke-width',(l.weight||1)+0.5);
      el.setAttribute('stroke-opacity','1');
      gE.appendChild(el);
      return el;
    });

    // SVG node layer
    var gN=document.createElementNS(NS,'g');
    svgEl.appendChild(gN);
    var groupEls=nodes.map(function(n){
      var g=document.createElementNS(NS,'g');
      g.style.cursor='grab';
      var c=document.createElementNS(NS,'circle');
      c.setAttribute('r',n.r);
      c.setAttribute('fill',n.color);
      c.setAttribute('fill-opacity','0.82');
      c.setAttribute('stroke',n.stroke);
      c.setAttribute('stroke-width','1.5');
      // Hover tooltip
      c.addEventListener('mouseover',function(){
        if(!tip) return;
        var dir=n.side==='long'?'▲ LONG':'▼ SHORT';
        var rrStr=n.rr?(' · R/R '+n.rr.toFixed(1)+'x'):'';
        var ezStr=n.entry_zone?(' · entry '+n.entry_zone):'';
        tip.innerHTML=
          '<div style="color:'+n.color+';font-weight:500;font-size:13px;margin-bottom:5px">'+n.symbol
          +' <span style="font-size:11px">'+dir+'</span></div>'
          +(n.cat?'<div style="color:#E8EAF0;font-size:12px;margin-bottom:4px">'+n.cat+'</div>':'')
          +'<div style="color:#7B8090;font-size:11px">score='+n.score.toFixed(0)+rrStr+ezStr+'</div>'
          +(n.conviction?'<div style="color:'+n.color+';font-size:10px;margin-top:3px;'
          +'text-transform:uppercase;letter-spacing:.04em">'+n.conviction+'</div>':'');
        tip.style.display='block';
      });
      c.addEventListener('mousemove',function(ev){
        if(!tip) return;
        var rc=wrap.getBoundingClientRect();
        tip.style.left=Math.min(ev.clientX-rc.left+14,W-260)+'px';
        tip.style.top=Math.max(ev.clientY-rc.top-36,4)+'px';
      });
      c.addEventListener('mouseout',function(){ if(tip) tip.style.display='none'; });
      // Drag
      c.addEventListener('mousedown',function(ev){
        n.fixed=true; ev.preventDefault(); g.style.cursor='grabbing';
        function mv(e){
          var rc=wrap.getBoundingClientRect();
          n.x=Math.max(n.r+4,Math.min(W-n.r-4,e.clientX-rc.left));
          n.y=Math.max(n.r+4,Math.min(H-n.r-4,e.clientY-rc.top));
          n.vx=0; n.vy=0; render();
        }
        function up(){
          n.fixed=false; g.style.cursor='grab';
          document.removeEventListener('mousemove',mv);
          document.removeEventListener('mouseup',up);
        }
        document.addEventListener('mousemove',mv);
        document.addEventListener('mouseup',up);
      });
      var txt=document.createElementNS(NS,'text');
      txt.textContent=n.symbol;
      txt.setAttribute('font-family','"JetBrains Mono",monospace');
      txt.setAttribute('font-size','10');
      txt.setAttribute('fill','#E8EAF0');
      txt.setAttribute('text-anchor','middle');
      txt.setAttribute('dominant-baseline','middle');
      txt.setAttribute('pointer-events','none');
      g.appendChild(c); g.appendChild(txt);
      gN.appendChild(g);
      return g;
    });

    function render(){
      links.forEach(function(l,i){
        if(!l.s||!l.t) return;
        lineEls[i].setAttribute('x1',l.s.x.toFixed(1));
        lineEls[i].setAttribute('y1',l.s.y.toFixed(1));
        lineEls[i].setAttribute('x2',l.t.x.toFixed(1));
        lineEls[i].setAttribute('y2',l.t.y.toFixed(1));
      });
      groupEls.forEach(function(g,i){
        g.setAttribute('transform','translate('+nodes[i].x.toFixed(1)+','+nodes[i].y.toFixed(1)+')');
      });
    }

    // Spring-electrical physics — no external dep
    var alpha=1.0;
    function tick(){
      alpha=Math.max(0.015,alpha*0.983);
      // Repulsion: every pair
      for(var i=0;i<nodes.length;i++){
        for(var j=i+1;j<nodes.length;j++){
          var ni=nodes[i],nj=nodes[j];
          var dx=nj.x-ni.x, dy=nj.y-ni.y;
          var d2=dx*dx+dy*dy||0.01, d=Math.sqrt(d2);
          // Hard overlap separation
          var minD=ni.r+nj.r+10;
          if(d<minD){
            var push=alpha*(minD-d)*0.3/d;
            if(!ni.fixed){ni.vx-=push*dx; ni.vy-=push*dy;}
            if(!nj.fixed){nj.vx+=push*dx; nj.vy+=push*dy;}
          }
          // Coulomb long-range repulsion
          var rep=alpha*3200/d2, rfx=rep*dx/d, rfy=rep*dy/d;
          if(!ni.fixed){ni.vx-=rfx; ni.vy-=rfy;}
          if(!nj.fixed){nj.vx+=rfx; nj.vy+=rfy;}
        }
      }
      // Spring attraction along edges
      links.forEach(function(l){
        if(!l.s||!l.t) return;
        var dx=l.t.x-l.s.x, dy=l.t.y-l.s.y;
        var d=Math.sqrt(dx*dx+dy*dy)||1;
        var k=alpha*0.055*(d-95)/d;
        if(!l.s.fixed){l.s.vx+=k*dx; l.s.vy+=k*dy;}
        if(!l.t.fixed){l.t.vx-=k*dx; l.t.vy-=k*dy;}
      });
      // Gravity toward canvas centre
      nodes.forEach(function(n){
        if(!n.fixed){
          n.vx+=(W/2-n.x)*0.014*alpha;
          n.vy+=(H/2-n.y)*0.014*alpha;
        }
      });
      // Integrate + dampen + clamp
      nodes.forEach(function(n){
        if(n.fixed) return;
        n.vx*=0.82; n.vy*=0.82;
        n.x+=n.vx; n.y+=n.vy;
        n.x=Math.max(n.r+4,Math.min(W-n.r-4,n.x));
        n.y=Math.max(n.r+4,Math.min(H-n.r-4,n.y));
      });
      render();
      if(alpha>0.015) requestAnimationFrame(tick);
    }
    render();
    requestAnimationFrame(tick);
  }

  // Defer until after layout pass so getBoundingClientRect returns real width
  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded',function(){ setTimeout(init,60); });
  } else {
    setTimeout(init,60);
  }
})();
</script>'''


def _bbb_thesis_cluster_map_html(longs: list, bears: list) -> str:
    """Force-directed thesis cluster map. Nodes = conviction entries; edges = shared keyword clusters."""
    if not longs and not bears:
        return ""

    def _clusters(cat: str) -> list:
        t = (cat or "").lower()
        return [name for name, kws in _THESIS_CLUSTERS.items() if any(kw in t for kw in kws)]

    nodes_raw: list = []
    for p in longs:
        sym = p.get("symbol", "")
        score = float(p.get("score") or 50)
        cat = p.get("catalyst", "")
        nodes_raw.append({
            "id": sym, "symbol": sym, "score": score,
            "r": round(8 + (score / 100) * 12, 1),
            "color": "#34D399", "stroke": "#1a8a63",
            "side": "long", "conviction": str(p.get("conviction", "") or ""),
            "cat": cat[:110], "entry_zone": str(p.get("entry_zone", "") or ""),
            "rr": round(float(p.get("risk_reward", 0) or 0), 1),
            "clusters": _clusters(cat),
        })
    for p in bears:
        sym = p.get("symbol", "")
        score = float(p.get("score") or 50)
        cat = p.get("catalyst", "")
        nodes_raw.append({
            "id": sym, "symbol": sym, "score": score,
            "r": round(8 + (score / 100) * 12, 1),
            "color": "#F87171", "stroke": "#c53030",
            "side": "short", "conviction": str(p.get("conviction", "") or ""),
            "cat": cat[:110], "entry_zone": str(p.get("entry_zone", "") or ""),
            "rr": round(float(p.get("risk_reward", 0) or 0), 1),
            "clusters": _clusters(cat),
        })

    links_raw: list = []
    for i, n1 in enumerate(nodes_raw):
        for n2 in nodes_raw[i + 1:]:
            shared = set(n1["clusters"]) & set(n2["clusters"])
            if shared:
                links_raw.append({"source": n1["id"], "target": n2["id"], "weight": min(3, len(shared))})

    import json as _json
    _strip = lambda n: {k: v for k, v in n.items() if k != "clusters"}
    nodes_json = _json.dumps([_strip(n) for n in nodes_raw])
    links_json = _json.dumps(links_raw)

    n_nodes = len(nodes_raw)
    n_edges = len(links_raw)
    meta = f"{n_nodes} theses · {n_edges} shared-cluster edges"

    container = (
        f'<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
        f'border-radius:var(--bbb-r-3);padding:16px;margin-bottom:16px">'
        f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">'
        f'<div style="font-family:var(--bbb-font-sans);font-size:12px;font-weight:500;'
        f'letter-spacing:.08em;text-transform:uppercase;color:var(--bbb-fg-muted)">Thesis Cluster Map</div>'
        f'<div style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-dim)">{meta}</div>'
        f'</div>'
        f'<div id="tm-wrap" style="position:relative;border-radius:var(--bbb-r-2);overflow:hidden">'
        f'<svg id="tm-svg" style="background:var(--bbb-surface-2);width:100%;height:300px;'
        f'display:block;cursor:grab"></svg>'
        f'<div id="tm-tip" style="display:none;position:absolute;background:#181B26;'
        f'border:1px solid #1F2330;border-radius:4px;padding:8px 12px;'
        f'font-family:JetBrains Mono,monospace;font-size:11px;color:#E8EAF0;'
        f'pointer-events:none;max-width:280px;line-height:1.5;z-index:10"></div>'
        f'</div>'
        f'</div>'
    )
    return container + _THESIS_MAP_JS.replace("%%NODES%%", nodes_json).replace("%%LINKS%%", links_json)


def _page_brief(status: dict, now_et: str) -> str:
    a1_mode = status["a1_mode"].get("mode", "unknown").upper()
    a2_mode = status["a2_mode"].get("mode", "unknown").upper()
    nav = _nav_html("brief", now_et, a1_mode, a2_mode)
    ticker = _build_ticker_html(status["positions"])

    brief = status.get("intelligence_brief", {})
    if not brief:
        body = (
            '<div style="padding:40px;font-family:var(--bbb-font-mono);font-size:13px;'
            'color:var(--bbb-fg-muted);text-align:center">'
            'Intelligence brief not yet generated.<br>'
            '<span style="color:var(--bbb-fg-dim)">Runs at 4:00 AM ET (premarket) and 9:25 AM ET (market open).</span>'
            '</div>'
        )
        return _page_shell("Intelligence Brief", nav, body, ticker)

    gen_at = brief.get("generated_at", "")
    brief_type = brief.get("brief_type", "?")
    gen_display = gen_at[:16].replace("T", " ") if gen_at else "?"
    try:
        next_slot_str = _next_brief_slot_display()
    except Exception:
        next_slot_str = "next market open"

    _type_map = {
        "premarket":       ("4 AM",        "var(--bbb-fg-muted)"),
        "market_open":     ("Market Open", "var(--bbb-info)"),
        "intraday_update": ("Intraday",    "var(--bbb-profit)"),
        "closing":         ("Closing",     "var(--bbb-warn)"),
    }
    type_label, type_color = _type_map.get(brief_type, (brief_type, "var(--bbb-fg-muted)"))

    # Staleness banner — shown whenever >90 min old (no market-hours gate)
    stale_html = ""
    if gen_at:
        try:
            gen_ts = datetime.fromisoformat(gen_at)
            if gen_ts.tzinfo is None:
                gen_ts = gen_ts.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - gen_ts.astimezone(timezone.utc)).total_seconds() / 60
            if age_min > 90:
                age_str = f"{age_min / 60:.1f}h" if age_min >= 120 else f"{age_min:.0f} min"
                stale_html = (
                    f'<div style="background:rgba(251,191,36,.08);border:1px solid var(--bbb-warn);'
                    f'border-radius:var(--bbb-r-3);padding:10px 16px;margin-bottom:12px;'
                    f'font-family:var(--bbb-font-mono);font-size:13px;color:var(--bbb-warn)">'
                    f'&#x26A0; Brief is {age_str} old &mdash; next refresh: {next_slot_str}'
                    f'</div>'
                )
        except Exception:
            pass

    timeline_js = '''
    <script>
    (function() {
      function fmtTime(iso) {
        if (!iso) return "?";
        var d = new Date(iso.replace("T", " ") + " UTC");
        if (isNaN(d)) d = new Date(iso);
        return d.toLocaleTimeString("en-US", {hour:"numeric", minute:"2-digit", hour12:true, timeZone:"America/New_York"});
      }
      function typeLabel(t) {
        return {premarket:"4 AM", market_open:"Market Open", intraday_update:"Intraday", closing:"Closing"}[t] || t || "?";
      }
      function typeColor(t) {
        return {premarket:"#7B8090", market_open:"#60A5FA", intraday_update:"#34D399", closing:"#FBBF24"}[t] || "#7B8090";
      }
      fetch("/api/briefs").then(r => r.json()).then(data => {
        var container = document.getElementById("brief-timeline");
        if (!container) return;
        var updates = data.updates || [];
        if (!updates.length) { container.style.display = "none"; return; }
        var pills = updates.map(function(u, i) {
          var t = u.brief_type || "?";
          var ts = fmtTime(u.generated_at);
          var color = typeColor(t);
          return '<span onclick="loadBrief(' + i + ')" id="pill-' + i + '"'
            + ' style="cursor:pointer;display:inline-block;padding:3px 10px;border-radius:12px;'
            + 'border:1px solid ' + color + ';color:' + color + ';font-family:var(--bbb-font-mono,monospace);'
            + 'font-size:11px;margin-right:6px;margin-bottom:4px">' + typeLabel(t) + ' ' + ts + '</span>';
        }).join("");
        container.innerHTML = '<div style="margin-bottom:14px">'
          + '<div style="font-family:var(--bbb-font-mono,monospace);font-size:11px;letter-spacing:.04em;'
          + 'text-transform:uppercase;color:#7B8090;margin-bottom:6px">Today\'s briefs</div>'
          + pills + '</div>';
        window._briefUpdates = updates;
        var pill = document.getElementById("pill-" + (updates.length - 1));
        if (pill) pill.style.fontWeight = "500";
      }).catch(function(){});
      window.loadBrief = function(idx) {
        (window._briefUpdates || []).forEach(function(_, i) {
          var p = document.getElementById("pill-" + i);
          if (p) p.style.fontWeight = i === idx ? "500" : "400";
        });
      };
    })();
    </script>'''

    header_html = f'''
    {timeline_js}
    <div id="brief-timeline"></div>
    <div class="bbb-voice-strip" style="margin-bottom:var(--bbb-s-4)">
      <div class="bbb-voice-quote">Generated {gen_display} &nbsp;&middot;&nbsp; <span style="color:{type_color}">{type_label}</span></div>
      <div class="bbb-voice-attr">Next: {next_slot_str}</div>
    </div>'''

    # ── Local style shortcuts ──────────────────────────────────────────────────
    S_CARD = ('background:var(--bbb-surface);border:1px solid var(--bbb-border);'
              'border-radius:var(--bbb-r-3);padding:16px;margin-bottom:16px')
    S_LBL  = ('font-family:var(--bbb-font-sans);font-size:12px;font-weight:500;'
              'letter-spacing:.08em;text-transform:uppercase;margin-bottom:10px')
    S_CHIP = 'background:var(--bbb-surface-2);padding:2px 8px;border-radius:var(--bbb-r-1);font-size:11px'
    S_MONO = 'font-family:var(--bbb-font-mono)'

    # Latest updates
    updates = brief.get("latest_updates", [])[:5]
    updates_html = ""
    if updates:
        rows = ""
        for u in updates:
            ts  = u.get("timestamp", "")[:16].replace("T", " ")
            cat = u.get("category", "?")
            sym = u.get("symbol", "")
            cat_c = "var(--bbb-warn)" if "catalyst" in cat else ("var(--bbb-info)" if "macro" in cat else "var(--bbb-fg-muted)")
            rows += (
                f'<div style="padding:6px 0;border-bottom:1px solid var(--bbb-border);{S_MONO};font-size:13px">'
                f'<span style="color:var(--bbb-fg-muted)">{ts}</span> '
                f'<span style="color:{cat_c}">[{cat}]</span> '
                f'<b style="color:var(--bbb-info)">{sym}</b> &mdash; '
                f'<span style="color:var(--bbb-fg)">{_strip_md(u.get("summary",""))}</span></div>'
            )
        updates_html = (
            f'<div style="background:rgba(251,191,36,.08);border:1px solid var(--bbb-warn);'
            f'border-radius:var(--bbb-r-3);padding:12px 16px;margin-bottom:16px">'
            f'<div style="{S_LBL};color:var(--bbb-warn)">&#x1F534; Latest Updates</div>'
            f'{rows}</div>'
        )

    # Market Regime
    mr      = brief.get("market_regime", {})
    regime  = mr.get("regime", "?")
    score   = mr.get("score", 0)
    conf    = mr.get("confidence", "?")
    vix     = mr.get("vix", 0)
    tone    = mr.get("tone", "")
    drivers = mr.get("key_drivers", [])
    events  = mr.get("todays_events", [])
    r_color = ("var(--bbb-profit)" if "risk_on" in regime
               else "var(--bbb-loss)" if ("risk_off" in regime or "defensive" in regime)
               else "var(--bbb-warn)")
    drv_html = "".join(f'<li style="margin:2px 0;color:var(--bbb-fg)">{d}</li>' for d in drivers)
    evt_html = ""
    for e in events[:4]:
        impact = e.get("impact", "low")
        ic = ("var(--bbb-loss)" if impact == "high"
              else "var(--bbb-warn)" if impact == "medium"
              else "var(--bbb-fg-muted)")
        evt_html += (
            f'<div style="padding:3px 0;{S_MONO};font-size:12px">'
            f'<span style="color:{ic}">&#x25CF;</span> '
            f'<b style="color:var(--bbb-fg)">{e.get("time","?")}</b> &mdash; '
            f'{e.get("event","?")} <span style="color:var(--bbb-fg-muted)">({impact})</span></div>'
        )
    regime_html = (
        f'<div style="{S_CARD}">'
        f'<div style="{S_LBL};color:var(--bbb-fg-muted)">Market Regime</div>'
        f'<div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:10px">'
        f'<span style="{S_MONO};font-size:22px;font-weight:500;color:{r_color}">{regime.replace("_"," ").upper()}</span>'
        f'<span style="{S_MONO};font-size:13px;color:var(--bbb-fg-muted)">({score}/100, {conf})</span>'
        f'<span style="{S_MONO};font-size:14px;color:var(--bbb-fg)">VIX <b style="color:var(--bbb-info)">{vix:.1f}</b></span>'
        f'</div>'
        f'<div style="{S_MONO};font-size:13px;color:var(--bbb-fg);margin-bottom:8px">{_strip_md(tone)}</div>'
        + (f'<ul style="margin:0;padding-left:20px;font-size:13px">{drv_html}</ul>' if drivers else '')
        + (f'<div style="margin-top:10px">{evt_html}</div>' if evt_html else '')
        + '</div>'
    )

    # Sector Snapshot
    sectors = brief.get("sector_snapshot", [])
    sec_rows = ""
    for sec in sectors:
        chg    = sec.get("etf_change_pct", 0) or 0
        st_val = sec.get("status", "NEUTRAL")
        st_c   = ("var(--bbb-profit)" if st_val in ("LEADING", "BULLISH")
                  else "var(--bbb-loss)" if st_val in ("BEARISH", "WEAK")
                  else "var(--bbb-fg-muted)")
        chg_c  = "var(--bbb-profit)" if chg >= 0 else "var(--bbb-loss)"
        news_str = " · ".join(sec.get("news", [])[:2])
        sec_rows += (
            f'<tr>'
            f'<td style="color:var(--bbb-fg);font-weight:500">{sec.get("sector","?")}</td>'
            f'<td style="color:var(--bbb-fg-muted);text-align:center">{sec.get("etf","?")}</td>'
            f'<td style="text-align:right;{S_MONO};color:{chg_c}">{chg:+.1f}%</td>'
            f'<td style="text-align:center"><span style="{S_CHIP};color:{st_c}">{st_val}</span></td>'
            f'<td style="color:var(--bbb-fg-muted);font-size:12px">{sec.get("summary","")[:80]}</td>'
            f'<td style="color:var(--bbb-fg-dim);font-size:11px">{news_str[:80]}</td>'
            f'</tr>'
        )
    sectors_html = (
        f'<div style="{S_CARD}">'
        f'<div style="{S_LBL};color:var(--bbb-fg-muted)">Sector Snapshot</div>'
        f'<div class="table-wrap"><table class="data-table">'
        f'<thead><tr><th>Sector</th><th style="text-align:center">ETF</th>'
        f'<th style="text-align:right">Change</th><th style="text-align:center">Status</th>'
        f'<th>Summary</th><th>News</th></tr></thead>'
        f'<tbody>{sec_rows}</tbody></table></div></div>'
    ) if sectors else ""

    # Signal Map
    _scored = _signal_map_data()
    signal_map_html = ""
    if _scored:
        _sm_tiles = ""
        for _sym, _info in sorted(_scored.items(), key=lambda x: -x[1].get("score", 0)):
            _sc = _info.get("score", 0)
            _dir = _info.get("direction", "neutral").lower()
            if _sc >= 85:
                _bg = "rgba(52,211,153,0.22)"; _bc = "rgba(52,211,153,0.45)"
            elif _sc >= 70:
                _bg = "rgba(52,211,153,0.12)"; _bc = "rgba(52,211,153,0.28)"
            elif _sc >= 50:
                _bg = "var(--bbb-surface-2)"; _bc = "var(--bbb-border)"
            elif _sc >= 40:
                _bg = "rgba(248,113,113,0.08)"; _bc = "rgba(248,113,113,0.22)"
            else:
                _bg = "rgba(248,113,113,0.18)"; _bc = "rgba(248,113,113,0.40)"
            if _dir == "bullish":
                _arrow = "&#x2191;"; _arrow_c = "var(--bbb-profit)"
            elif _dir == "bearish":
                _arrow = "&#x2193;"; _arrow_c = "var(--bbb-loss)"
            else:
                _arrow = "&#x2192;"; _arrow_c = "var(--bbb-fg-dim)"
            _sc_c = ("var(--bbb-profit)" if _sc >= 70 else
                     "var(--bbb-loss)" if _sc < 40 else "var(--bbb-fg-muted)")
            _sm_tiles += (
                f'<div style="background:{_bg};border:1px solid {_bc};border-radius:var(--bbb-r-2);'
                f'padding:7px 6px;width:76px;display:flex;flex-direction:column;'
                f'align-items:center;gap:2px;flex:none">'
                f'<div style="{S_MONO};font-size:12px;font-weight:500;color:var(--bbb-fg)">{_sym}</div>'
                f'<div style="{S_MONO};font-size:15px;color:{_sc_c};font-weight:500;line-height:1">{_sc:.0f}</div>'
                f'<div style="{S_MONO};font-size:12px;color:{_arrow_c};line-height:1">{_arrow}</div>'
                f'</div>'
            )
        _n = len(_scored)
        signal_map_html = (
            f'<div style="{S_CARD}">'
            f'<div style="{S_LBL};color:var(--bbb-fg-muted)">Signal Map'
            f'<span style="font-size:11px;font-weight:400;letter-spacing:0;text-transform:none;'
            f'color:var(--bbb-fg-dim);margin-left:8px">{_n} symbols</span></div>'
            f'<div style="display:flex;flex-wrap:wrap;gap:5px">{_sm_tiles}</div>'
            f'</div>'
        )

    # High Conviction Longs
    longs = brief.get("high_conviction_longs", [])
    long_cards = ""
    for p in longs:
        conv    = p.get("conviction", "MEDIUM")
        rr      = p.get("risk_reward", 0)
        conv_c  = "var(--bbb-profit)" if conv == "HIGH" else "var(--bbb-warn)"
        rr_c    = ("var(--bbb-profit)" if rr >= 2.0
                   else "var(--bbb-warn)" if rr >= 1.5
                   else "var(--bbb-fg-muted)")
        a2_note = p.get("a2_strategy_note", "")
        risk    = p.get("risk_note", "")[:80]
        long_cards += (
            f'<div style="background:var(--bbb-surface-2);border:1px solid var(--bbb-border);'
            f'border-left:3px solid var(--bbb-profit);border-radius:var(--bbb-r-2);'
            f'padding:10px 14px;margin-bottom:8px">'
            f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">'
            f'<span style="{S_MONO};color:var(--bbb-fg-dim);font-size:12px">#{p.get("rank",0)}</span>'
            f'<b style="{S_MONO};color:var(--bbb-profit);font-size:16px">{p.get("symbol","?")}</b>'
            f'<span style="{S_CHIP};color:{conv_c}">{conv}</span>'
            f'<span style="{S_MONO};color:var(--bbb-fg-muted);font-size:12px">score={p.get("score",0)}</span>'
            f'<span style="{S_MONO};color:{rr_c};font-size:13px;font-weight:500;margin-left:auto">R/R {rr:.1f}x</span>'
            f'</div>'
            f'<div style="{S_MONO};font-size:13px;color:var(--bbb-fg);margin-bottom:4px">{_strip_md(p.get("catalyst",""))[:100]}</div>'
            f'<div style="{S_MONO};font-size:12px;color:var(--bbb-fg-muted);margin-bottom:2px">'
            f'entry={p.get("entry_zone","?")} &nbsp; stop={p.get("stop",0)} &nbsp; target={p.get("target",0)}</div>'
            f'<div style="{S_MONO};font-size:12px;color:var(--bbb-fg-muted)">{_strip_md(p.get("technical_summary",""))[:80]}</div>'
            + (f'<div style="{S_MONO};font-size:11px;color:var(--bbb-info);margin-top:3px">{a2_note}</div>' if a2_note else '')
            + (f'<div style="{S_MONO};font-size:11px;color:var(--bbb-loss);margin-top:2px">Risk: {risk}</div>' if risk else '')
            + '</div>'
        )
    longs_html = (
        f'<div style="{S_CARD}">'
        f'<div style="{S_LBL};color:var(--bbb-profit)">High Conviction Longs ({len(longs)})</div>'
        + (long_cards or f'<div style="{S_MONO};font-size:13px;color:var(--bbb-fg-muted)">No high conviction longs.</div>')
        + '</div>'
    ) if longs is not None else ""

    # High Conviction Bearish
    bears = brief.get("high_conviction_bearish", [])
    bear_cards = ""
    for p in bears:
        conv   = p.get("conviction", "MEDIUM")
        rr     = p.get("risk_reward", 0)
        conv_c = "var(--bbb-loss)" if conv == "HIGH" else "var(--bbb-warn)"
        risk   = p.get("risk_note", "")[:80]
        bear_cards += (
            f'<div style="background:var(--bbb-surface-2);border:1px solid var(--bbb-border);'
            f'border-left:3px solid var(--bbb-loss);border-radius:var(--bbb-r-2);'
            f'padding:10px 14px;margin-bottom:8px">'
            f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">'
            f'<span style="{S_MONO};color:var(--bbb-fg-dim);font-size:12px">#{p.get("rank",0)}</span>'
            f'<b style="{S_MONO};color:var(--bbb-loss);font-size:16px">{p.get("symbol","?")}</b>'
            f'<span style="{S_CHIP};color:{conv_c}">{conv}</span>'
            f'<span style="{S_MONO};color:var(--bbb-fg-muted);font-size:12px">score={p.get("score",0)}</span>'
            f'<span style="{S_MONO};color:var(--bbb-fg-muted);font-size:13px;margin-left:auto">R/R {rr:.1f}x</span>'
            f'</div>'
            f'<div style="{S_MONO};font-size:13px;color:var(--bbb-fg);margin-bottom:4px">{_strip_md(p.get("catalyst",""))[:100]}</div>'
            f'<div style="{S_MONO};font-size:12px;color:var(--bbb-fg-muted);margin-bottom:2px">'
            f'entry={p.get("entry_zone","?")} &nbsp; stop={p.get("stop",0)} &nbsp; target={p.get("target",0)}</div>'
            + (f'<div style="{S_MONO};font-size:11px;color:var(--bbb-loss);margin-top:3px">Risk: {risk}</div>' if risk else '')
            + '</div>'
        )
    bears_html = (
        f'<div style="{S_CARD}">'
        f'<div style="{S_LBL};color:var(--bbb-loss)">High Conviction Bearish ({len(bears)})</div>'
        + (bear_cards or f'<div style="{S_MONO};font-size:13px;color:var(--bbb-fg-muted)">No bearish signals.</div>')
        + '</div>'
    ) if bears is not None else ""

    # Earnings Pipeline
    earnings = brief.get("earnings_pipeline", [])
    earn_rows = ""
    for e in earnings:
        timing = e.get("timing", "?")
        t_c    = ("var(--bbb-loss)" if "today" in timing
                  else "var(--bbb-warn)" if "tomorrow" in timing
                  else "var(--bbb-fg-muted)")
        held   = e.get("held_by_a1", False)
        held_b = (f' <span style="{S_CHIP};color:var(--bbb-profit)">HELD</span>' if held else "")
        iv     = e.get("iv_rank")
        earn_rows += (
            f'<tr>'
            f'<td><b style="color:var(--bbb-info)">{e.get("symbol","?")}</b>{held_b}</td>'
            f'<td><span style="color:{t_c}">{timing}</span></td>'
            f'<td style="text-align:right;{S_MONO}">{f"{iv:.0f}" if iv is not None else "—"}</td>'
            f'<td style="color:var(--bbb-fg-muted);font-size:12px">{e.get("beat_history","")[:40]}</td>'
            f'<td style="color:var(--bbb-fg-muted);font-size:12px">{e.get("a2_rule","")[:40]}</td>'
            f'</tr>'
        )
    earnings_html = (
        f'<div style="{S_CARD}">'
        f'<div style="{S_LBL};color:var(--bbb-fg-muted)">Earnings Pipeline</div>'
        f'<div class="table-wrap"><table class="data-table">'
        f'<thead><tr><th>Symbol</th><th>Timing</th>'
        f'<th style="text-align:right">IV Rank</th><th>Beat History</th><th>A2 Rule</th></tr></thead>'
        f'<tbody>{earn_rows}</tbody></table></div></div>'
    ) if earnings else ""

    # Macro Wire Alerts
    macro_alerts = brief.get("macro_wire_alerts", [])
    alert_cards = ""
    for a in macro_alerts:
        tier  = a.get("tier", "medium")
        tc    = ("var(--bbb-loss)" if tier == "critical"
                 else "var(--bbb-warn)" if tier == "high"
                 else "var(--bbb-fg-muted)")
        bg_a  = ("rgba(248,113,113,.08)" if tier == "critical"
                 else "rgba(251,191,36,.08)" if tier == "high"
                 else "var(--bbb-surface-2)")
        secs  = ", ".join(a.get("affected_sectors", [])[:3])
        alert_cards += (
            f'<div style="background:{bg_a};border:1px solid {tc};'
            f'border-radius:var(--bbb-r-2);padding:8px 12px;margin-bottom:6px">'
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
            f'<span style="{S_MONO};color:{tc};font-size:11px;text-transform:uppercase;letter-spacing:.04em">{tier}</span>'
            f'<span style="{S_MONO};color:var(--bbb-fg-muted);font-size:11px">score={a.get("score",0):.1f}</span>'
            + (f'<span style="{S_MONO};color:var(--bbb-fg-muted);font-size:11px">{secs}</span>' if secs else '')
            + f'</div>'
            f'<div style="{S_MONO};font-size:13px;color:var(--bbb-fg)">{a.get("headline","")[:120]}</div>'
            f'<div style="{S_MONO};font-size:12px;color:var(--bbb-fg-muted);margin-top:3px">{a.get("impact","")[:80]}</div>'
            f'</div>'
        )
    macro_html = (
        f'<div style="{S_CARD}">'
        f'<div style="{S_LBL};color:var(--bbb-fg-muted)">Macro Wire Alerts</div>'
        + (alert_cards or f'<div style="{S_MONO};font-size:13px;color:var(--bbb-fg-muted)">No significant macro alerts.</div>')
        + '</div>'
    ) if macro_alerts is not None else ""

    # Avoid List
    avoid = brief.get("avoid_list", [])
    avoid_rows = ""
    for a in avoid:
        avoid_rows += (
            f'<div style="display:flex;align-items:center;gap:12px;padding:8px 0;'
            f'border-bottom:1px solid var(--bbb-border)">'
            f'<b style="{S_MONO};color:var(--bbb-loss);font-size:14px;min-width:54px">{a.get("symbol","?")}</b>'
            f'<span style="{S_MONO};font-size:13px;color:var(--bbb-fg);flex:1">{a.get("reason","")[:80]}</span>'
            f'<span style="background:rgba(248,113,113,0.15);border:1px solid var(--bbb-loss);'
            f'color:var(--bbb-loss);{S_MONO};font-size:10px;font-weight:500;'
            f'letter-spacing:.06em;padding:2px 6px;border-radius:var(--bbb-r-1);flex:none">AVOID</span>'
            f'</div>'
        )
    avoid_html = (
        f'<div style="{S_CARD};border-left:3px solid var(--bbb-loss)">'
        f'<div style="{S_LBL};color:var(--bbb-loss)">Avoid List</div>'
        + (avoid_rows or f'<div style="{S_MONO};font-size:13px;color:var(--bbb-fg-muted)">No symbols flagged to avoid.</div>')
        + '</div>'
    ) if avoid is not None else ""

    # Insider Activity
    insider  = brief.get("insider_activity", {})
    hc       = insider.get("high_conviction", [])
    cong     = insider.get("congressional", [])
    f4       = insider.get("form4_purchases", [])
    ins_rows = "".join(
        f'<div style="{S_MONO};font-size:13px;color:var(--bbb-fg);padding:3px 0">{item}</div>'
        for item in (hc + cong + f4)[:8]
    )
    insider_html = (
        f'<div style="{S_CARD}">'
        f'<div style="{S_LBL};color:var(--bbb-fg-muted)">Insider Activity</div>'
        + (ins_rows or f'<div style="{S_MONO};font-size:13px;color:var(--bbb-fg-muted)">No insider activity.</div>')
        + '</div>'
    ) if (hc or cong or f4) else ""

    # Watch List
    watch      = brief.get("watch_list", [])[:10]
    watch_rows = ""
    for w in watch:
        dir_  = w.get("direction", "").lower()
        dir_c = ("var(--bbb-profit)" if dir_ == "bullish"
                 else "var(--bbb-loss)" if dir_ == "bearish"
                 else "var(--bbb-fg-muted)")
        watch_rows += (
            f'<tr>'
            f'<td><b style="color:var(--bbb-info)">{w.get("symbol","?")}</b></td>'
            f'<td style="text-align:right;{S_MONO}">{w.get("score",0)}</td>'
            f'<td><span style="color:{dir_c}">{w.get("direction","?")}</span></td>'
            f'<td style="color:var(--bbb-fg-muted);font-size:12px">{w.get("entry_trigger","")[:60]}</td>'
            f'</tr>'
        )
    watch_html = (
        f'<div style="{S_CARD}">'
        f'<div style="{S_LBL};color:var(--bbb-fg-muted)">Watch List</div>'
        f'<div class="table-wrap"><table class="data-table">'
        f'<thead><tr><th>Symbol</th><th style="text-align:right">Score</th>'
        f'<th>Direction</th><th>Entry Trigger</th></tr></thead>'
        f'<tbody>{watch_rows}</tbody></table></div></div>'
    ) if watch else ""

    cluster_map_html = _bbb_thesis_cluster_map_html(longs, bears)

    body = (
        '<div class="container">'
        + header_html + stale_html + updates_html
        + '<div class="compact-grid" style="gap:12px">'
        + '<div>' + regime_html + signal_map_html + sectors_html + earnings_html + insider_html + macro_html + '</div>'
        + '<div>' + longs_html + bears_html + watch_html + avoid_html + '</div>'
        + '</div>'
        + cluster_map_html
        + '</div>'
    )
    return _page_shell("Intelligence Brief", nav, body, ticker)


# ── Sparkline helpers ─────────────────────────────────────────────────────────

def _bbb_sparkline_svg(values: list, color: str, width: int = 60, height: int = 20) -> str:
    """60×20 inline SVG polyline sparkline. No fill, no axis, no label."""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return ""
    mn, mx = min(vals), max(vals)
    pad = 1.5
    if mn == mx:
        mid_y = height / 2
        pts = f"0,{mid_y:.1f} {width},{mid_y:.1f}"
    else:
        def _px(i):
            return pad + i / (len(vals) - 1) * (width - 2 * pad)
        def _py(v):
            return pad + (1 - (v - mn) / (mx - mn)) * (height - 2 * pad)
        pts = " ".join(f"{_px(i):.1f},{_py(v):.1f}" for i, v in enumerate(vals))
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'fill="none" xmlns="http://www.w3.org/2000/svg" '
        f'style="display:block;flex-shrink:0;margin-top:4px">'
        f'<polyline points="{pts}" stroke="{color}" stroke-width="1.5" '
        f'fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        f'</svg>'
    )


def _spark_color(values: list) -> str:
    """Return --bbb-profit or --bbb-loss based on net delta of the series."""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return "var(--bbb-profit)"
    return "var(--bbb-profit)" if vals[-1] >= vals[0] else "var(--bbb-loss)"


def _spark_a1_equity(n: int = 21) -> tuple:
    """(equity_list, pnl_list) from Alpaca A1 portfolio history — 1 month daily."""
    try:
        import requests as _req
        r = _req.get(
            "https://paper-api.alpaca.markets/v2/account/portfolio/history?period=1M&timeframe=1D",
            headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            timeout=10,
        )
        d = r.json()
        eq = [float(v) for v in d.get("equity", []) if v is not None and float(v or 0) > 0]
        pl = [float(v) for v in d.get("profit_loss", []) if v is not None]
        return eq[-n:], pl[-n:]
    except Exception:
        return [], []


def _spark_a2_portfolio_daily(n: int = 21) -> tuple:
    """(equity_list, pnl_list) from Alpaca A2 portfolio history — 1 month daily."""
    try:
        import requests as _req
        r = _req.get(
            "https://paper-api.alpaca.markets/v2/account/portfolio/history?period=1M&timeframe=1D",
            headers={"APCA-API-KEY-ID": ALPACA_KEY_OPT, "APCA-API-SECRET-KEY": ALPACA_SECRET_OPT},
            timeout=10,
        )
        d = r.json()
        eq = [float(v) for v in d.get("equity", []) if v is not None and float(v or 0) > 0]
        pl = [float(v) for v in d.get("profit_loss", []) if v is not None]
        return eq[-n:], pl[-n:]
    except Exception:
        return [], []


def _spark_claude_cost(n: int = 14) -> list:
    """Daily Claude cost aggregated from cost_attribution_spine.jsonl + A2 cost_log.jsonl."""
    from collections import defaultdict as _dd
    costs: dict = _dd(float)
    try:
        for line in (BOT_DIR / "data/analytics/cost_attribution_spine.jsonl").read_text().splitlines():
            try:
                e = json.loads(line)
                c = float(e.get("estimated_cost_usd") or 0)
                if c and e.get("ts"):
                    costs[e["ts"][:10]] += c
            except Exception:
                pass
    except Exception:
        pass
    _PR = {"claude-haiku-4-5-20251001": (8e-7, 4e-6), "claude-sonnet-4-6": (3e-6, 1.5e-5)}
    try:
        for line in (BOT_DIR / "data/account2/costs/cost_log.jsonl").read_text().splitlines():
            try:
                e = json.loads(line)
                ts = e.get("timestamp", "")[:10]
                if not ts:
                    continue
                pin, pout = _PR.get(e.get("model", ""), (3e-6, 1.5e-5))
                costs[ts] += float(e.get("input_tokens") or 0) * pin
                costs[ts] += float(e.get("output_tokens") or 0) * pout
            except Exception:
                pass
    except Exception:
        pass
    return [costs[d] for d in sorted(costs)][-n:]


# ── Build status dict ─────────────────────────────────────────────────────────
def _build_status() -> dict:
    a1d = _alpaca_a1()
    a2d = _alpaca_a2()
    a1_acc = a1d.get("account")
    earnings = _earnings_flags()

    positions = []
    if a1_acc:
        equity = float(a1_acc.equity or 0)
        buying_power = float(a1_acc.buying_power or 0)
        _raw_positions = a1d.get("positions", [])
        exposure_dollars = sum(float(p.market_value or 0) for p in _raw_positions)
        total_capacity = (exposure_dollars + buying_power) or equity
        stops = _stop_map(a1d.get("orders", []))
        tps = _tp_map(a1d.get("orders", []))
        for p in _raw_positions:
            sym = p.symbol
            qty = float(p.qty or 0)
            entry = float(p.avg_entry_price or 0)
            current = float(p.current_price or 0)
            market_val = float(p.market_value or 0)
            unreal_pl = float(p.unrealized_pl or 0)
            unreal_plpc = float(p.unrealized_plpc or 0) * 100
            pct_capacity = (market_val / total_capacity * 100) if total_capacity else 0
            stop = stops.get(sym)
            tp = tps.get(sym)
            gap = ((current - stop) / current * 100) if stop and current else None
            if pct_capacity > 25:
                oversize = "critical"
            elif pct_capacity > 20:
                oversize = "core"
            elif pct_capacity > 15:
                oversize = "dynamic"
            else:
                oversize = False
            positions.append({
                "symbol": sym, "qty": qty, "entry": entry, "current": current,
                "market_val": market_val, "unreal_pl": unreal_pl,
                "unreal_plpc": unreal_plpc, "pct_capacity": pct_capacity,
                "stop": stop, "tp": tp, "gap_to_stop": gap,
                "earnings": earnings.get(sym, ""),
                "oversize": oversize,
            })

    a2_dec = _a2_last_cycle()
    a2_structs = _a2_structures()
    a2_live_pos = a2d.get("positions", [])
    a1_decs = _last_n_a1_decisions(50)
    a2_decs = _last_n_a2_decisions(10)
    qctx = _qualitative_context()
    today_pnl_a1 = _today_pnl_a1()
    today_pnl_a2 = _today_pnl_a2()

    # Sparkline time-series (non-fatal — all return [] on error)
    spark_a1_eq, spark_a1_pnl = _spark_a1_equity()
    spark_a2_eq, spark_a2_pnl = _spark_a2_portfolio_daily()
    spark_cost = _spark_claude_cost()

    # Load trail tiers from strategy_config
    trail_tiers = []
    try:
        cfg = json.loads((BOT_DIR / "strategy_config.json").read_text())
        trail_tiers = cfg.get("exit_management", {}).get("trail_tiers", [])
    except Exception:
        pass

    st = {
        "a1": a1d, "a2": a2d,
        "positions": positions,
        "buys_today": a1d.get("buys_today", 0),
        "sells_today": a1d.get("sells_today", 0),
        "a1_mode": _rj(BOT_DIR / "data/runtime/a1_mode.json"),
        "a2_mode": _rj(BOT_DIR / "data/runtime/a2_mode.json"),
        "gate": _rj(BOT_DIR / "data/market/gate_state.json"),
        "costs": _rj(BOT_DIR / "data/costs/daily_costs.json"),
        "shadow": _rj(BOT_DIR / "data/reports/shadow_status_latest.json"),
        "decision": _last_decision(),
        "trades": _todays_trades(),
        "log_errors": _recent_errors(),
        "git_hash": _git_hash(),
        "service_uptime": _service_uptime(),
        "a2_decision": a2_dec,
        "morning_brief": _morning_brief(),
        "morning_brief_time": _morning_brief_time(),
        "morning_brief_mtime": _morning_brief_mtime_float(),
        "intelligence_brief": _intelligence_brief_full(),
        "a1_decisions": a1_decs,
        "a2_decisions": a2_decs,
        "a2_pos_cards": _build_a2_position_cards(a2_structs, a2_live_pos),
        "a1_theses": _a1_top_theses(a1_decs, qctx),
        "a2_theses": _a2_top_theses(a2_decs),
        "today_pnl_a1": today_pnl_a1,
        "today_pnl_a2": today_pnl_a2,
        "trail_tiers": trail_tiers,
        "a2_pipeline": _a2_pipeline_today(),
        "allocator_line": _allocator_shadow_compact(),
        "allocator_data": _allocator_chart_data(),
        "equity_curve": _equity_curve_data(),
        "intraday_bars": _intraday_bars_a1(),
        "perf_summary": _load_perf_summary(),
        "spark_a1_eq": spark_a1_eq,
        "spark_a1_pnl": spark_a1_pnl,
        "spark_a2_eq": spark_a2_eq,
        "spark_a2_pnl": spark_a2_pnl,
        "spark_cost": spark_cost,
    }
    st["warnings"] = _build_warnings(st)
    return st


def _load_perf_summary() -> dict:
    """Load performance_summary.json. Returns {} on any error (non-fatal)."""
    try:
        import sys  # noqa: PLC0415
        sys.path.insert(0, str(BOT_DIR))
        from performance_tracker import load_performance_summary  # noqa: PLC0415
        return load_performance_summary()
    except Exception:
        return {}


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    status = _build_status()
    return _page_overview(status, _now_et())


@app.route("/a1")
def page_a1():
    from flask import request as _req
    status = _build_status()
    debug = _req.args.get("debug", "0") == "1"
    return _page_a1(status, _now_et(), debug=debug)


@app.route("/a2")
def page_a2():
    status = _build_status()
    return _page_a2(status, _now_et())


@app.route("/brief")
def page_brief():
    status = _build_status()
    return _page_brief(status, _now_et())


@app.route("/api/briefs")
def api_briefs():
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    today_str = datetime.now(et).strftime("%Y%m%d")
    briefs_dir = BOT_DIR / "data" / "market" / "briefs"
    daily_file = briefs_dir / f"morning_brief_{today_str}.json"
    if not daily_file.exists():
        return jsonify({"date": today_str, "updates": []})
    try:
        data = json.loads(daily_file.read_text())
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e), "updates": []}), 500


@app.route("/api/status")
def api_status():
    status = _build_status()
    return jsonify({
        "a1_mode": status["a1_mode"],
        "a2_mode": status["a2_mode"],
        "gate": status["gate"],
        "costs": status["costs"],
        "decision": status["decision"],
        "git_hash": status["git_hash"],
        "service_uptime": status["service_uptime"],
        "positions_count": len(status["positions"]),
        "today_pnl_a1": status["today_pnl_a1"][0],
        "today_pnl_a2": status["today_pnl_a2"][0],
        "a1_error": status["a1"].get("error"),
        "a2_error": status["a2"].get("error"),
        "warnings": status.get("warnings", []),
    })


@app.route("/health")
def health():
    return "ok", 200


@app.route("/api/health")
def api_health():
    """Return JSON health status for all 7 bot health checks."""
    try:
        import sys as _sys
        _bot_dir = str(BOT_DIR)
        if _bot_dir not in _sys.path:
            _sys.path.insert(0, _bot_dir)
        import health_monitor
        status = health_monitor.get_health_status()
        code = 200 if status.get("all_ok") else 503
        return jsonify(status), code
    except Exception as exc:
        return jsonify({"all_ok": False, "error": str(exc), "checks": []}), 500


# ── Trade journal (cached 5 min — Alpaca API call) ───────────────────────────
@_cached("trades", ttl=300)
def _closed_trades() -> list[dict]:
    try:
        sys.path.insert(0, str(BOT_DIR))
        from trade_journal import build_bug_fix_log, build_closed_trades  # noqa: I001
        return build_closed_trades(), build_bug_fix_log()
    except Exception as e:
        app.logger.warning("trade_journal error: %s", e)
        return [], []


def _page_trades(now_et: str) -> str:
    a1 = _alpaca_a1()
    _a1_positions = [
        {"symbol": getattr(p, "symbol", ""), "current": float(getattr(p, "current_price", 0) or 0),
         "unreal_plpc": float(getattr(p, "unrealized_plpc", 0) or 0) * 100}
        for p in a1.get("positions", [])
    ]
    nav = _nav_html("trades", now_et)
    ticker = _build_ticker_html(_a1_positions)

    result = _closed_trades()
    trades, bug_log = result if isinstance(result, tuple) else (result, [])

    # ── statistics ────────────────────────────────────────────────────────
    n = len(trades)
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    losses = sum(1 for t in trades if t.get("outcome") == "loss")
    total_pnl = sum(t.get("pnl", 0.0) or 0.0 for t in trades)
    win_rate = (wins / n * 100) if n else 0.0

    running_pnl, peak_pnl, max_dd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda x: x.get("exit_time", "")):
        running_pnl += t.get("pnl", 0.0) or 0.0
        if running_pnl > peak_pnl:
            peak_pnl = running_pnl
        dd = peak_pnl - running_pnl
        if dd > max_dd:
            max_dd = dd

    pnl_pcts = [t.get("pnl_pct", 0.0) or 0.0 for t in trades]
    avg_ret = sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0.0
    holds = [h for h in [t.get("holding_days") for t in trades]
             if isinstance(h, (int, float)) and h >= 0]
    avg_hold_val = sum(holds) / len(holds) if holds else None

    wr_color = "var(--bbb-profit)" if win_rate >= 55 else ("var(--bbb-warn)" if win_rate >= 45 else "var(--bbb-loss)")

    # ── credibility strip ─────────────────────────────────────────────────
    def _sc(lbl, val_html):
        return (
            '<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
            'border-radius:var(--bbb-r-3);padding:var(--bbb-s-3) var(--bbb-s-4)">'
            '<div style="font-family:var(--bbb-font-sans);font-size:var(--bbb-t-label);'
            'font-weight:var(--bbb-w-medium);letter-spacing:0.08em;text-transform:uppercase;'
            'color:var(--bbb-fg-muted);margin-bottom:6px">' + lbl + '</div>'
            + val_html + '</div>'
        )

    def _sn(v, color="var(--bbb-fg)"):
        return (
            '<span style="font-family:var(--bbb-font-mono);font-size:22px;font-weight:500;'
            'font-variant-numeric:tabular-nums;color:' + color + '">' + v + '</span>'
        )

    pnl_sign = "+" if total_pnl >= 0 else ""
    pnl_color = "var(--bbb-profit)" if total_pnl >= 0 else "var(--bbb-loss)"
    ret_sign = "+" if avg_ret >= 0 else ""
    ret_color = "var(--bbb-profit)" if avg_ret >= 0 else "var(--bbb-loss)"
    wr_bar = (
        '<div style="margin-top:5px;height:3px;background:var(--bbb-border);border-radius:2px">'
        '<div style="width:' + f'{min(100.0, win_rate):.0f}' + '%;height:100%;background:'
        + wr_color + ';border-radius:2px"></div></div>'
    )

    cred_html = (
        '<div style="display:grid;grid-template-columns:repeat(6,1fr);gap:var(--bbb-s-3);margin-bottom:var(--bbb-s-4)">'
        + _sc("Closed Trades", _sn(str(n)))
        + _sc("Net Realized P&amp;L",
              _sn(f"{pnl_sign}${total_pnl:,.2f}", pnl_color)
              + ('<div style="font-family:var(--bbb-font-mono);font-size:10px;'
                 'color:var(--bbb-fg-dim);margin-top:4px">excl. BUG-DENOM-001 exits</div>'
                 if any(bool(t.get("bug_flags")) for t in trades) else ""))
        + _sc("Win Rate", _sn(f"{win_rate:.0f}%", wr_color) + wr_bar)
        + _sc("Max Drawdown", _sn(f"-${max_dd:,.2f}" if max_dd else "$0.00",
                                  "var(--bbb-loss)" if max_dd else "var(--bbb-fg-dim)"))
        + _sc("Avg Return", (_sn(f"{ret_sign}{avg_ret:.1f}%", ret_color)
                             if pnl_pcts else _sn("—", "var(--bbb-fg-dim)")))
        + _sc("Avg Hold", (_sn(f"{avg_hold_val:.1f}d", "var(--bbb-fg)")
                           if avg_hold_val is not None else _sn("—", "var(--bbb-fg-dim)")))
        + '</div>'
    )

    # ── trades table ──────────────────────────────────────────────────────
    BUG_CUTOFF = "2026-05-01"
    clean_trades = [t for t in trades
                    if (t.get("exit_time") or "") >= BUG_CUTOFF and not t.get("bug_flags")]
    legacy_trades = [t for t in trades
                     if (t.get("exit_time") or "") < BUG_CUTOFF or bool(t.get("bug_flags"))]

    _TH = ('style="padding:6px 12px;font-family:var(--bbb-font-sans);font-size:11px;'
           'font-weight:500;letter-spacing:0.06em;text-transform:uppercase;'
           'color:var(--bbb-fg-muted);border-bottom:1px solid var(--bbb-border);white-space:nowrap"')
    _THL = ('style="padding:6px 12px;font-family:var(--bbb-font-sans);font-size:11px;'
            'font-weight:500;letter-spacing:0.06em;text-transform:uppercase;'
            'color:var(--bbb-fg-muted);border-bottom:1px solid var(--bbb-border);'
            'text-align:left;white-space:nowrap"')
    _thead = (
        '<thead><tr style="background:var(--bbb-surface-2)">'
        f'<th {_THL}>Symbol</th>'
        f'<th {_THL}>Strategy</th>'
        f'<th {_TH}>Exit Date</th>'
        f'<th {_TH}>Entry → Exit</th>'
        f'<th {_TH}>Qty</th>'
        f'<th {_TH}>Hold</th>'
        f'<th {_THL}>Tier</th>'
        f'<th {_TH}>P&amp;L</th>'
        f'<th {_TH}>Return</th>'
        f'<th {_THL}>Catalyst</th>'
        '</tr></thead>'
    )

    def _trow(t, idx, dim=False):
        sym = t.get("symbol", "")
        pnl = t.get("pnl", 0.0) or 0.0
        pnl_pct = t.get("pnl_pct", 0.0) or 0.0
        outcome = t.get("outcome", "flat")
        clr = "var(--bbb-profit)" if outcome == "win" else ("var(--bbb-loss)" if outcome == "loss" else "var(--bbb-fg-dim)")
        s_pnl = "+" if pnl >= 0 else ""
        s_ret = "+" if pnl_pct >= 0 else ""
        entry = t.get("entry_price", 0) or 0
        exit_ = t.get("exit_price", 0) or 0
        qty = int(t.get("qty", 0) or 0)
        holding = t.get("holding_days")
        if holding is None:
            hold_s = "—"
        elif isinstance(holding, (int, float)):
            if holding < 0:
                hold_s = "< 1m"
            elif holding < 1 / 24:
                hold_s = f"{max(1, int(holding * 1440))}m"
            elif holding < 1:
                hold_s = f"{holding * 24:.1f}h"
            else:
                hold_s = f"{holding:.1f}d"
        else:
            hold_s = "< 1m" if str(holding).startswith("-") else str(holding)
        exit_t = t.get("exit_time", "")
        date_s = exit_t[:10] if exit_t else "—"
        flags = list(t.get("bug_flags") or [])
        # Strip embedded bug tag from symbol (e.g. "VBUG-DENOM-001" → sym="V")
        _bug_pos = sym.find("BUG-")
        if _bug_pos > 0:
            _inline_bug = sym[_bug_pos:]
            sym = sym[:_bug_pos]
            if _inline_bug not in flags:
                flags = [_inline_bug] + flags
        conviction = (t.get("tier") or t.get("conviction") or "").upper() or "—"
        cat = t.get("catalyst") or ""
        cat_s = (cat[:48] + "…") if len(cat) > 48 else cat
        reasoning = t.get("reasoning") or ""
        has_r = bool(reasoning)

        flag_pill = "".join(
            f' <span onclick="scrollToBug()" '
            f'style="cursor:pointer;font-family:var(--bbb-font-mono);font-size:10px;'
            f'background:rgba(251,191,36,.12);color:var(--bbb-warn);'
            f'border:1px solid rgba(251,191,36,.3);border-radius:2px;padding:1px 4px"'
            f' title="Jump to issue log">{fid} ⚠</span>'
            for fid in flags
        )
        expand = (
            f'<span onclick="tglR(\'r{idx}\')" '
            'style="cursor:pointer;color:var(--bbb-fg-dim);font-size:10px;margin-left:4px">▸</span>'
        ) if has_r else ""

        _dim = "opacity:0.5;" if dim else ""
        _m = 'style="font-family:var(--bbb-font-mono);font-size:13px;font-variant-numeric:tabular-nums;padding:7px 12px"'
        _mc = f'style="font-family:var(--bbb-font-mono);font-size:13px;font-variant-numeric:tabular-nums;padding:7px 12px;color:{clr}"'

        row = (
            f'<tr style="{_dim}border-bottom:1px solid var(--bbb-border)">'
            f'<td style="padding:7px 12px;font-family:var(--bbb-font-mono);font-size:13px;font-weight:500;color:var(--bbb-fg)">{sym}{flag_pill}{expand}</td>'
            f'<td style="padding:7px 12px;font-family:var(--bbb-font-sans);font-size:12px;color:var(--bbb-fg-muted)">A1 Equity</td>'
            f'<td {_m}>{date_s}</td>'
            f'<td {_m}>${entry:,.2f}&nbsp;→&nbsp;${exit_:,.2f}</td>'
            f'<td {_m}>{qty}</td>'
            f'<td {_m}>{hold_s}</td>'
            f'<td style="padding:7px 12px;font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg-muted)">{conviction}</td>'
            f'<td {_mc}>{s_pnl}${pnl:,.2f}</td>'
            f'<td {_mc}>{s_ret}{pnl_pct:.1f}%</td>'
            f'<td style="padding:7px 12px;font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted);max-width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{cat_s}</td>'
            f'</tr>'
        )
        if has_r:
            row += (
                f'<tr id="r{idx}" style="display:none">'
                f'<td colspan="10" style="padding:var(--bbb-s-3) var(--bbb-s-4);background:var(--bbb-surface-2);border-bottom:1px solid var(--bbb-border)">'
                f'<div style="font-family:var(--bbb-font-mono);font-size:13px;color:var(--bbb-fg);line-height:1.5">'
                f'{reasoning[:500]}{"..." if len(reasoning) > 500 else ""}'
                f'</div></td></tr>'
            )
        return row

    if not trades:
        trades_html = (
            '<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
            'border-radius:var(--bbb-r-3);padding:var(--bbb-s-5);text-align:center;'
            'font-family:var(--bbb-font-mono);font-size:13px;color:var(--bbb-fg-dim)">No closed trades yet.</div>'
        )
    else:
        _sec = (
            'style="padding:6px 12px;font-family:var(--bbb-font-sans);font-size:11px;'
            'font-weight:500;letter-spacing:0.08em;text-transform:uppercase;'
            'color:var(--bbb-fg-muted);background:var(--bbb-surface-2);'
            'border-bottom:1px solid var(--bbb-border)"'
        )
        table_rows = ""
        idx = 0
        if clean_trades:
            table_rows += f'<tr><td colspan="10" {_sec}>Clean · May 2026+</td></tr>'
            for t in clean_trades:
                table_rows += _trow(t, idx)
                idx += 1
        if legacy_trades:
            table_rows += (
                f'<tr><td colspan="10" {_sec}>'
                f'Bug Period · Apr 13–30 2026'
                f'<span style="margin-left:8px;font-family:var(--bbb-font-mono);font-size:10px;'
                f'background:rgba(251,191,36,.12);color:var(--bbb-warn);'
                f'border:1px solid rgba(251,191,36,.3);border-radius:2px;padding:1px 5px">RESULTS MAY BE UNRELIABLE</span>'
                f'</td></tr>'
            )
            for t in legacy_trades:
                table_rows += _trow(t, idx, dim=True)
                idx += 1

        trades_html = (
            '<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
            'border-radius:var(--bbb-r-3);overflow:hidden">'
            '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse">'
            + _thead
            + f'<tbody>{table_rows}</tbody>'
            + '</table></div></div>'
        )

    # ── known issue log ───────────────────────────────────────────────────
    bugs_section_html = ""
    for bug in bug_log:
        sev = bug.get("severity", "LOW")
        sev_color = "var(--bbb-loss)" if sev == "HIGH" else ("var(--bbb-warn)" if sev == "MEDIUM" else "var(--bbb-fg-muted)")
        is_resolved = bool(bug.get("resolution"))
        status_pill = (
            '<span style="font-family:var(--bbb-font-mono);font-size:10px;'
            'background:rgba(52,211,153,.12);color:var(--bbb-profit);'
            'border:1px solid rgba(52,211,153,.3);border-radius:2px;padding:1px 5px">RESOLVED</span>'
        ) if is_resolved else (
            '<span style="font-family:var(--bbb-font-mono);font-size:10px;'
            'background:rgba(248,113,113,.12);color:var(--bbb-loss);'
            'border:1px solid rgba(248,113,113,.3);border-radius:2px;padding:1px 5px">OPEN</span>'
        )
        desc = bug.get("description", "")
        resolution = bug.get("resolution", "")
        res_html = (
            f'<div style="margin-top:var(--bbb-s-2);font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg-muted)">'
            f'Fixed: {resolution[:140]}{"..." if len(resolution) > 140 else ""}</div>'
        ) if resolution else ""
        bugs_section_html += (
            f'<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
            f'border-radius:var(--bbb-r-3);padding:var(--bbb-s-3) var(--bbb-s-4);margin-bottom:var(--bbb-s-2)">'
            f'<div style="display:flex;align-items:center;gap:var(--bbb-s-2);margin-bottom:var(--bbb-s-2)">'
            f'<span style="font-family:var(--bbb-font-mono);font-size:12px;font-weight:500;color:var(--bbb-fg)">{bug.get("id","")}</span>'
            f'<span style="font-family:var(--bbb-font-mono);font-size:11px;color:{sev_color};text-transform:uppercase;font-weight:500">{sev}</span>'
            f'{status_pill}'
            f'<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-dim)">{bug.get("start","")} – {bug.get("end","")}</span>'
            f'</div>'
            f'<div style="font-family:var(--bbb-font-sans);font-size:13px;font-weight:500;color:var(--bbb-fg);margin-bottom:4px">{bug.get("title","")}</div>'
            f'<div style="font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg-muted)">{desc[:200]}{"..." if len(desc) > 200 else ""}</div>'
            f'{res_html}</div>'
        )

    # ── compose ───────────────────────────────────────────────────────────
    body = (
        '<div class="container">'
        + cred_html
        + '<div class="section-label">Closed Round-Trips</div>'
        + trades_html
        + '<div class="section-label" id="known-issue-log">Known Issue Log</div>'
        + (bugs_section_html or
           '<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
           'border-radius:var(--bbb-r-3);padding:var(--bbb-s-5);text-align:center;'
           'font-family:var(--bbb-font-mono);font-size:13px;color:var(--bbb-fg-dim)">No logged bugs.</div>')
        + '</div>'
        + '<script>'
        + 'function tglR(id){var e=document.getElementById(id);if(e){e.style.display=e.style.display==="none"?"table-row":"none";}}'
        + 'function scrollToBug(){var e=document.getElementById("known-issue-log");if(e){e.scrollIntoView({behavior:"smooth"});}}'
        + '</script>'
    )
    return _page_shell("Trade Journal", nav, body, ticker)


@app.route("/trades")
def page_trades():
    return _page_trades(_now_et())





# ── Gallery helpers ───────────────────────────────────────────────────────────

def _gallery_outcome_pill(outcome: str) -> str:
    cfg = {
        "win":  ("WIN",  "rgba(52,211,153,.15)", "var(--bbb-profit)", "rgba(52,211,153,.35)"),
        "loss": ("LOSS", "rgba(248,113,113,.15)", "var(--bbb-loss)",   "rgba(248,113,113,.35)"),
        "flat": ("FLAT", "rgba(155,93,229,.12)", "var(--bbb-fg-muted)","rgba(155,93,229,.25)"),
    }.get(outcome, ("?", "rgba(74,79,96,.2)", "var(--bbb-fg-dim)", "rgba(74,79,96,.3)"))
    label, bg, color, border = cfg
    return (
        f'<span style="font-family:var(--bbb-font-mono);font-size:10px;font-weight:500;'
        f'background:{bg};color:{color};border:1px solid {border};'
        f'border-radius:2px;padding:2px 6px;letter-spacing:0.04em">{label}</span>'
    )


def _gallery_tier_chip(tier: str) -> str:
    if not tier:
        return ""
    t = tier.upper()
    color = "var(--bbb-profit)" if t == "CORE" else ("var(--bbb-warn)" if t == "DYNAMIC" else "var(--bbb-fg-muted)")
    return (
        f'<span style="font-family:var(--bbb-font-mono);font-size:10px;color:{color};'
        f'letter-spacing:0.06em">{t}</span>'
    )


def _gallery_regime_chip(regime: str, score) -> str:
    if not regime:
        return ""
    r = str(regime).lower()
    color = "var(--bbb-profit)" if "risk_on" in r else ("var(--bbb-loss)" if "risk_off" in r else "var(--bbb-warn)")
    score_part = f"({score:.0f})" if score not in (None, "", "?") else ""
    try:
        score_part = f"({float(score):.0f})"
    except (TypeError, ValueError):
        score_part = ""
    return (
        f'<span style="font-family:var(--bbb-font-mono);font-size:10px;color:{color}">'
        f'{regime}{score_part}</span>'
    )


def _gallery_snip(text: str, sentences: int = 2) -> str:
    if not text:
        return ""
    parts = [s.strip() for s in text.replace("  ", " ").split(".") if s.strip()]
    snippet = ". ".join(parts[:sentences])
    if snippet and not snippet.endswith("."):
        snippet += "."
    return snippet[:280]


def _gallery_card_html(t: dict, idx: int) -> str:
    sym       = t.get("symbol", "")
    pnl       = t.get("pnl", 0.0) or 0.0
    pnl_pct   = t.get("pnl_pct", 0.0) or 0.0
    outcome   = t.get("outcome", "flat")
    tier      = t.get("tier") or ""
    regime    = t.get("regime") or t.get("regime_view") or ""
    score     = t.get("regime_score")
    entry     = t.get("entry_price", 0) or 0
    exit_     = t.get("exit_price", 0) or 0
    qty       = int(t.get("qty", 0) or 0)
    holding   = t.get("holding_days")
    hold_s    = f"{holding}d" if holding is not None else ""
    exit_t    = t.get("exit_time", "")
    date_s    = exit_t[:10] if exit_t else ""
    reasoning = t.get("reasoning") or ""
    flags     = t.get("bug_flags", [])

    sign      = "+" if pnl >= 0 else ""
    ret_sign  = "+" if pnl_pct >= 0 else ""
    pnl_color = "var(--bbb-profit)" if outcome == "win" else ("var(--bbb-loss)" if outcome == "loss" else "var(--bbb-fg-dim)")
    border_l  = "var(--bbb-profit)" if outcome == "win" else ("var(--bbb-loss)" if outcome == "loss" else "var(--bbb-border)")

    outcome_pill = _gallery_outcome_pill(outcome)
    tier_chip    = _gallery_tier_chip(tier)
    regime_chip  = _gallery_regime_chip(regime, score)
    snip         = _gallery_snip(reasoning)

    bug_pill = (
        '<span style="font-family:var(--bbb-font-mono);font-size:10px;'
        'background:rgba(251,191,36,.12);color:var(--bbb-warn);'
        'border:1px solid rgba(251,191,36,.3);border-radius:2px;padding:2px 5px">BUG</span>'
    ) if flags else ""

    meta_parts = []
    if entry and exit_:
        meta_parts.append(f'${entry:,.2f}&nbsp;→&nbsp;${exit_:,.2f}')
    if qty:
        meta_parts.append(f'{qty}sh')
    if hold_s:
        meta_parts.append(hold_s)
    if date_s:
        meta_parts.append(date_s)
    meta_str = ' · '.join(meta_parts)

    return (
        f'<div class="gallery-card" data-outcome="{outcome}" data-bot="a1" data-pnl="{pnl:.2f}" '
        f'style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
        f'border-left:3px solid {border_l};border-radius:var(--bbb-r-3);'
        f'padding:var(--bbb-s-3) var(--bbb-s-4);display:flex;flex-direction:column;gap:var(--bbb-s-2)">'

        # header row
        f'<div style="display:flex;align-items:center;gap:var(--bbb-s-2);flex-wrap:wrap">'
        f'<span style="font-family:var(--bbb-font-mono);font-size:14px;font-weight:500;color:var(--bbb-fg)">{sym}</span>'
        f'{outcome_pill}'
        f'<span style="font-family:var(--bbb-font-mono);font-size:13px;font-variant-numeric:tabular-nums;color:{pnl_color}">'
        f'{sign}${pnl:,.2f}&nbsp;({ret_sign}{pnl_pct:.1f}%)</span>'
        f'{bug_pill}'
        f'</div>'

        # chips row
        f'<div style="display:flex;align-items:center;gap:var(--bbb-s-3)">'
        + (tier_chip if tier_chip else "")
        + ('<span style="color:var(--bbb-fg-dim);font-size:10px">·</span>' if tier_chip and regime_chip else "")
        + (regime_chip if regime_chip else "")
        + f'</div>'

        # meta row
        + (f'<div style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted)">{meta_str}</div>'
           if meta_str else "")

        # reasoning snippet
        + (f'<div style="font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg);'
           f'line-height:1.5;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical">'
           f'{snip}</div>' if snip else "")

        + f'</div>'
    )


def _page_gallery(now_et: str) -> str:
    a1 = _alpaca_a1()
    _a1_positions = [
        {"symbol": getattr(p, "symbol", ""), "current": float(getattr(p, "current_price", 0) or 0),
         "unreal_plpc": float(getattr(p, "unrealized_plpc", 0) or 0) * 100}
        for p in a1.get("positions", [])
    ]
    nav    = _nav_html("gallery", now_et)
    ticker = _build_ticker_html(_a1_positions)

    result = _closed_trades()
    trades, _ = result if isinstance(result, tuple) else (result, [])
    n = len(trades)

    # ── filter bar ────────────────────────────────────────────────────────
    def _fpill(label, val, cls):
        return (
            f'<button onclick="gFilter(this,\'{cls}\')" data-val="{val}" '
            f'class="gf {cls}" '
            f'style="font-family:var(--bbb-font-mono);font-size:11px;font-weight:500;'
            f'letter-spacing:0.06em;text-transform:uppercase;cursor:pointer;'
            f'padding:4px 10px;border-radius:var(--bbb-r-2);border:1px solid var(--bbb-border);'
            f'background:var(--bbb-surface-2);color:var(--bbb-fg-muted);transition:all 120ms">'
            f'{label}</button>'
        )

    outcome_pills = (
        _fpill("All", "all", "gf-outcome") +
        _fpill("Win",  "win",  "gf-outcome") +
        _fpill("Loss", "loss", "gf-outcome") +
        _fpill("Flat", "flat", "gf-outcome")
    )

    filter_bar = (
        f'<div style="display:flex;align-items:center;gap:var(--bbb-s-2);flex-wrap:wrap;'
        f'margin-bottom:var(--bbb-s-4);padding:var(--bbb-s-3) var(--bbb-s-4);'
        f'background:var(--bbb-surface);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3)">'
        f'<span style="font-family:var(--bbb-font-sans);font-size:11px;font-weight:500;'
        f'letter-spacing:0.06em;text-transform:uppercase;color:var(--bbb-fg-muted);margin-right:4px">Outcome</span>'
        + outcome_pills +
        f'<span style="flex:1"></span>'
        f'<select id="gSort" onchange="gSort()" '
        f'style="font-family:var(--bbb-font-mono);font-size:11px;background:var(--bbb-surface-2);'
        f'color:var(--bbb-fg-muted);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-2);'
        f'padding:4px 8px;cursor:pointer">'
        f'<option value="newest">Newest first</option>'
        f'<option value="pnl_desc">Best P&amp;L</option>'
        f'<option value="pnl_asc">Worst P&amp;L</option>'
        f'</select>'
        f'<span id="gCount" style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-dim)">'
        f'{n} trades</span>'
        f'</div>'
    )

    # ── card grid ─────────────────────────────────────────────────────────
    if not trades:
        grid_html = (
            '<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
            'border-radius:var(--bbb-r-3);padding:var(--bbb-s-5);text-align:center;'
            'font-family:var(--bbb-font-mono);font-size:13px;color:var(--bbb-fg-dim)">No closed trades yet.</div>'
        )
    else:
        cards = "".join(_gallery_card_html(t, i) for i, t in enumerate(trades))
        grid_html = (
            f'<div id="gallery-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));'
            f'gap:var(--bbb-s-3)">'
            + cards +
            f'</div>'
        )

    gallery_js = """
<script>
(function(){
  var _outcome='all';
  function _apply(){
    var cards=Array.from(document.querySelectorAll('.gallery-card'));
    var shown=0;
    cards.forEach(function(c){
      var ok=(_outcome==='all'||c.dataset.outcome===_outcome);
      c.style.display=ok?'':'none';
      if(ok)shown++;
    });
    var el=document.getElementById('gCount');
    if(el)el.textContent=shown+' trade'+(shown===1?'':'s');
  }
  window.gFilter=function(btn,cls){
    document.querySelectorAll('.'+cls).forEach(function(b){
      b.style.background='var(--bbb-surface-2)';
      b.style.color='var(--bbb-fg-muted)';
      b.style.borderColor='var(--bbb-border)';
    });
    btn.style.background='var(--bbb-surface)';
    btn.style.color='var(--bbb-fg)';
    btn.style.borderColor='var(--bbb-fg-muted)';
    if(cls==='gf-outcome')_outcome=btn.dataset.val;
    _apply();
  };
  window.gSort=function(){
    var sel=document.getElementById('gSort');
    if(!sel)return;
    var grid=document.getElementById('gallery-grid');
    var cards=Array.from(grid.querySelectorAll('.gallery-card'));
    cards.sort(function(a,b){
      var v=sel.value;
      if(v==='pnl_desc')return parseFloat(b.dataset.pnl)-parseFloat(a.dataset.pnl);
      if(v==='pnl_asc')return parseFloat(a.dataset.pnl)-parseFloat(b.dataset.pnl);
      return 0;
    });
    cards.forEach(function(c){grid.appendChild(c);});
  };
  // activate All button by default
  var all=document.querySelector('.gf-outcome[data-val="all"]');
  if(all){all.style.background='var(--bbb-surface)';all.style.color='var(--bbb-fg)';all.style.borderColor='var(--bbb-fg-muted)';}
})();
</script>"""

    body = (
        '<div class="container">'
        + filter_bar
        + grid_html
        + '</div>'
        + gallery_js
    )
    return _page_shell("Trade Gallery", nav, body, ticker)


@app.route("/gallery")
def page_gallery():
    return _page_gallery(_now_et())


@app.route("/api/trades")
def api_trades():
    result = _closed_trades()
    trades, bug_log = result if isinstance(result, tuple) else (result, [])
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    losses = sum(1 for t in trades if t.get("outcome") == "loss")
    total_pnl = sum(t.get("pnl", 0.0) or 0.0 for t in trades)
    return jsonify({
        "trades": trades,
        "summary": {
            "total": len(trades),
            "wins": wins,
            "losses": losses,
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(wins / len(trades) * 100, 1) if trades else 0.0,
        },
        "bug_log": bug_log,
    })


def _page_transparency(now_et: str) -> str:
    a1 = _alpaca_a1()
    _a1_positions = [
        {"symbol": getattr(p, "symbol", ""), "current": float(getattr(p, "current_price", 0) or 0),
         "unreal_plpc": float(getattr(p, "unrealized_plpc", 0) or 0) * 100}
        for p in a1.get("positions", [])
    ]
    _a1_mode = _rj(BOT_DIR / "data/runtime/a1_mode.json", default={})
    _a2_mode = _rj(BOT_DIR / "data/runtime/a2_mode.json", default={})
    _t_a1_mode = (_a1_mode.get("mode") or "NORMAL").upper()
    _t_a2_mode = (_a2_mode.get("mode") or "NORMAL").upper()
    nav = _nav_html("transparency", now_et, _t_a1_mode, _t_a2_mode)
    ticker = _build_ticker_html(_a1_positions)

    # ── strategy config ────────────────────────────────────────────────────────
    try:
        _cfg = json.loads((BOT_DIR / "strategy_config.json").read_text())
    except Exception:
        _cfg = {}

    params = _cfg.get("parameters", {})
    _director_notes_raw = _cfg.get("director_notes", "")
    director_notes = (_director_notes_raw.strip() if isinstance(_director_notes_raw, str) else "")
    active_strategy = _cfg.get("active_strategy", "hybrid")
    ff = _cfg.get("feature_flags", {})
    shadow_flags = _cfg.get("shadow_flags", {})
    lab_flags = _cfg.get("lab_flags", {})
    _pa = _cfg.get("portfolio_allocator", {})
    _a2_enabled = _cfg.get("account2", {}).get("enabled", False)
    _a2rb = _cfg.get("a2_rollback", {})

    def _fr(name, val, desc, is_bool=True):
        if is_bool:
            enabled = bool(val)
            badge = (
                '<span style="font-family:var(--bbb-font-mono);font-size:9px;letter-spacing:.06em;'
                'text-transform:uppercase;padding:2px 6px;border-radius:var(--bbb-r-1);'
                'background:rgba(52,211,153,.12);color:var(--bbb-profit)">ON</span>'
                if enabled else
                '<span style="font-family:var(--bbb-font-mono);font-size:9px;letter-spacing:.06em;'
                'text-transform:uppercase;padding:2px 6px;border-radius:var(--bbb-r-1);'
                'background:rgba(74,79,96,.25);color:var(--bbb-fg-dim)">OFF</span>'
            )
        else:
            badge = (
                '<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg)">'
                + str(val) + '</span>'
            )
        return (
            '<tr>'
            f'<td style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted);padding:5px 12px">{name}</td>'
            f'<td style="padding:5px 12px">{badge}</td>'
            f'<td style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-dim);padding:5px 12px">{desc}</td>'
            '</tr>'
        )

    def _fsec(title):
        return (
            f'<tr><td colspan="3" style="font-family:var(--bbb-font-mono);font-size:9px;'
            f'letter-spacing:.08em;text-transform:uppercase;color:var(--bbb-fg-dim);'
            f'padding:10px 12px 4px;background:var(--bbb-surface-2)">{title}</td></tr>'
        )

    flags_html = (
        '<table class="data-table" style="width:100%"><thead><tr>'
        '<th style="font-size:10px">Flag</th>'
        '<th style="font-size:10px">State</th>'
        '<th style="font-size:10px">Description</th>'
        '</tr></thead><tbody>'

        + _fsec("Core Runtime")
        + _fr("hard_gate_claude_to_trading_window", ff.get("hard_gate_claude_to_trading_window", False),
              f"Claude API blocked outside {ff.get('trading_window_start_et','?')}–{ff.get('trading_window_end_et','?')} ET")
        + _fr("enable_model_tiering", ff.get("enable_model_tiering", False),
              "Haiku for low-signal cycles; Sonnet for high-signal")
        + _fr("enable_divergence_summarizer", ff.get("enable_divergence_summarizer", False),
              "WhatsApp alerts on live/paper account divergence")
        + _fr("enable_finnhub_news", ff.get("enable_finnhub_news", False),
              "Finnhub news feed wired into signal scoring")

        + _fsec("A2 Options")
        + _fr("account2.enabled", _a2_enabled,
              "A2 options cycle active")
        + _fr("a2_rollback.force_no_trade", _a2rb.get("force_no_trade", False),
              "Emergency A2 kill switch — forces no-trade regardless of signal")
        + _fr("a2_rollback.disable_bounded_debate", _a2rb.get("disable_bounded_debate", False),
              "Bypass 4-agent debate; fall through to no_trade")

        + _fsec("Allocator")
        + _fr("portfolio_allocator.enable_shadow", _pa.get("enable_shadow", False),
              "Shadow allocator runs and logs recommendations")
        + _fr("portfolio_allocator.enable_live", _pa.get("enable_live", False),
              "Live rebalancing execution — OFF until promoted")

        + _fsec("Memory & Learning")
        + _fr("enable_cost_attribution_spine", ff.get("enable_cost_attribution_spine", False),
              "Per-call cost attribution to pipeline stages")
        + _fr("enable_recommendation_memory", ff.get("enable_recommendation_memory", False),
              "Cross-cycle recommendation memory")
        + _fr("enable_abstention_contract", ff.get("enable_abstention_contract", False),
              "Structured hold reasoning required for every no-trade")
        + _fr("enable_experience_library", ff.get("enable_experience_library", False),
              "Curated outcome library injected as context")
        + _fr("vector_memory_ab_logging", ff.get("vector_memory_ab_logging", False),
              "A/B test logging for vector memory recall")

        + _fsec("Shadow / Experimental")
        + _fr("enable_replay_fork_debugger", shadow_flags.get("enable_replay_fork_debugger", False),
              "Shadow")
        + _fr("enable_context_compressor_shadow", shadow_flags.get("enable_context_compressor_shadow", False),
              "Shadow")
        + _fr("enable_confession_channel", lab_flags.get("enable_confession_channel", False),
              "Lab — bot explains every non-trade to a log")
        + _fr("enable_personality_forks", lab_flags.get("enable_personality_forks", False),
              "Lab — multiple reasoning personas in debate")
        + _fr("enable_dream_mode", lab_flags.get("enable_dream_mode", False),
              "Lab — overnight simulation runs")

        + '</tbody></table>'
    )

    # ── risk parameters — comprehensive 3-column table ────────────────────────
    _ps = _cfg.get("position_sizing", {})
    _sg = _cfg.get("sonnet_gate", {})
    _em = _cfg.get("exit_management", {})
    _a2 = _cfg.get("account2", {})
    _a2ps = _a2.get("position_sizing", {})
    _a2gr = _a2.get("greeks", {})
    _a2iv = _a2.get("iv_rules", {})
    _a2vix = _a2.get("vix_gates", {})
    _a2r = _cfg.get("a2_router", {})
    _pa = _cfg.get("portfolio_allocator", {})

    def _rr(label, val, notes=""):
        nd = f'<td style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-dim);padding:5px 12px">{notes}</td>' if notes else '<td></td>'
        return (
            f'<tr>'
            f'<td style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted);padding:5px 12px">{label}</td>'
            f'<td style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg);padding:5px 12px;white-space:nowrap">{val}</td>'
            f'{nd}'
            f'</tr>'
        )

    def _rsec(title):
        return (
            f'<tr><td colspan="3" style="font-family:var(--bbb-font-mono);font-size:9px;'
            f'letter-spacing:.08em;text-transform:uppercase;color:var(--bbb-fg-dim);'
            f'padding:10px 12px 4px;background:var(--bbb-surface-2)">{title}</td></tr>'
        )

    def _pct(v, mult=100): return f"{float(v)*mult:.0f}%" if v is not None else "—"
    def _x(v): return str(v) if v is not None else "—"

    trail_tiers = _em.get("trail_tiers", [])
    trail_rows = ""
    for t in trail_tiers:
        g = t.get("gain_pct", 0)
        s = t.get("stop_pct", 0)
        trail_rows += _rr(f"+{g*100:.0f}% gain", f"trail to +{s*100:.0f}% above entry")

    params_html = (
        '<table class="data-table" style="width:100%"><thead><tr>'
        '<th style="font-size:10px">Parameter</th>'
        '<th style="font-size:10px">Value</th>'
        '<th style="font-size:10px">Notes</th>'
        '</tr></thead><tbody>'

        + _rsec("Position Sizing")
        + _rr("Core tier per name", _pct(_ps.get("core_tier_pct")), "% of total_capacity")
        + _rr("Dynamic tier per name", _pct(_ps.get("dynamic_tier_pct")), "% of total_capacity")
        + _rr("Single-name cap (hard)", _pct(params.get("max_position_pct_capacity")), "risk_kernel upper bound")
        + _rr("Max deploy of buying power", _pct(params.get("max_deploy_pct_of_bp")), "prompt guidance; not hard gate")
        + _rr("Cash reserve floor", _pct(_ps.get("cash_reserve_pct")), "minimum idle cash")
        + _rr("Max open positions", _x(params.get("max_positions")), "A1 equity account")
        + _rr("Max sector exposure", _pct(params.get("max_sector_exposure_pct")), "per sector")
        + _rr("Max crypto exposure", _pct(params.get("max_crypto_exposure_pct")), "BTC/ETH combined")
        + _rr("Max overnight position", _pct(params.get("max_overnight_position_pct_equity")), "% of equity held overnight")
        + _rr("Margin authorized", "Yes — 4× max", "conviction-tiered: 1×→4×")

        + _rsec("Stop Loss Policy")
        + _rr("Stop loss · core", _pct(params.get("stop_loss_pct_core")), "% below entry")
        + _rr("Stop loss · overnight", _pct(params.get("stop_loss_pct_overnight")), "tighter for held-overnight")
        + _rr("Stop loss · intraday", _pct(params.get("stop_loss_pct_intraday")), "fastest exit")
        + _rr("Take-profit target", f"{_x(params.get('take_profit_multiple'))}× risk", "entry + N × initial_risk")
        + _rr("Trail trigger", f"+{float(_em.get('trail_trigger_r', 1.0)):.1f}R", "trail begins after 1R of gain")
        + _rr("Trail to breakeven+", _pct(_em.get("trail_to_breakeven_plus_pct")), "minimum trail floor")
        + _rr("Max trail failures", _x(_em.get("trail_replace_max_failures")), "before escalation")
        + _rr("Backstop days", _x(_em.get("backstop_days")), "max hold before forced review")

        + _rsec("Trail Stop Tiers")
        + trail_rows

        + _rsec("Drawdown Limits")
        + _rr("Max daily drawdown", _pct(params.get("max_daily_drawdown_pct")), "new entries blocked")
        + _rr("Position gate trigger", _pct(params.get("max_daily_drawdown_position_gate")), "individual positions gated first")
        + _rr("Max weekly drawdown", _pct(params.get("max_weekly_drawdown_pct")), "weekly portfolio limit")

        + _rsec("Regime Gates (VIX)")
        + _rr("VIX calm", f"< {_x(params.get('vix_calm_threshold'))}", "full sizing")
        + _rr("VIX elevated", f"< {_x(params.get('vix_elevated_threshold'))}", "normal")
        + _rr("VIX cautious", f"< {_x(params.get('vix_cautious_threshold'))}", "reduced sizing")
        + _rr("VIX stressed", f"≥ {_x(params.get('vix_stressed_threshold'))}", f"conviction floor ≥ {_x(params.get('vix_stressed_conviction_floor'))}")
        + _rr("Sonnet gate cooldown", f"{_x(_sg.get('cooldown_minutes'))} min", "minimum between full cycles")
        + _rr("Signal score threshold", f"≥ {_x(_sg.get('signal_score_threshold'))}", "gate opens at this level")
        + _rr("Max consecutive skips", _x(_sg.get("max_consecutive_skips")), "force-fire override")
        + _rr("Trading window", f"{_x(_cfg.get('feature_flags',{}).get('trading_window_start_et'))}–{_x(_cfg.get('feature_flags',{}).get('trading_window_end_et'))} ET", "hard gate on Claude API calls")

        + _rsec("A2 Options Sizing")
        + _rr("Core spread max", _pct(_a2ps.get("core_spread_max_pct")), "% equity per structure")
        + _rr("Dynamic max", _pct(_a2ps.get("dynamic_max_pct")), "% equity per structure")
        + _rr("Min DTE", f"{_x(_a2gr.get('min_dte'))}d", "no 0–4 DTE exposure")
        + _rr("Delta floor", f"≥ {_x(_a2gr.get('min_delta'))}", "absolute delta")
        + _rr("IV buy max rank", f"≤ {_x(_a2iv.get('buy_premium_rank_max'))}", "debit strategies only")
        + _rr("IV sell min rank", f"≥ {_x(_a2iv.get('sell_premium_rank_min'))}", "credit strategies only")
        + _rr("VIX crisis halt", f"≥ {_x(_a2vix.get('crisis_halt'))}", "all A2 suspended")
        + _rr("Max open structures", _x(_a2.get("max_open_positions")), "A2 account")
        + _rr("Equity floor", f"${_x(_a2.get('equity_floor')):}", "A2 minimum balance")
        + _rr("Earnings DTE blackout", f"≤ {_x(_a2r.get('earnings_dte_blackout'))}d", "no credit spreads near earnings")

        + _rsec("Conviction & Churn")
        + _rr("Min confidence (new entry)", _x(params.get("min_confidence_threshold")), "Sonnet must exceed this tier")
        + _rr("Add to position gate", f"≥ {_x(params.get('add_conviction_gate'))}", "minimum to scale into open position")
        + _rr("Catalyst required", "Yes", "entry blocked without valid catalyst tag")
        + _rr("Allocator mode", "Shadow only", "portfolio_allocator.enable_live = False")

        + '</tbody></table>'
    )

    # ── cost data — same source as Overview (_rj mirrors _build_status) ──────────
    _cost_data = _rj(BOT_DIR / "data/costs/daily_costs.json")
    daily_cost = float(_cost_data.get("daily_cost") or 0.0)
    all_time_cost_tr = float(_cost_data.get("all_time_cost") or 0.0)
    daily_calls_all = int(_cost_data.get("daily_calls") or 0)
    by_caller = _cost_data.get("by_caller") or {}
    _cost_date = _cost_data.get("date", "")

    # Sonnet calls from gate (same source as Overview)
    try:
        _gstate = json.loads((BOT_DIR / "data/market/gate_state.json").read_text())
        sonnet_calls_today = int(_gstate.get("total_calls_today") or 0)
    except Exception:
        sonnet_calls_today = 0
    haiku_calls_today = max(0, daily_calls_all - sonnet_calls_today)
    proj_monthly = daily_cost * 22

    caller_rows = ""
    for caller, info in sorted(by_caller.items(), key=lambda x: -x[1].get("cost", 0)):
        c = float(info.get("cost") or 0.0)
        n = int(info.get("calls") or 0)
        pct = (c / daily_cost * 100) if daily_cost else 0
        bar_w = f"{pct:.1f}%"
        caller_rows += (
            f'<tr>'
            f'<td style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted);padding:5px 12px">{caller}</td>'
            f'<td style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg);text-align:right;padding:5px 12px">{n}</td>'
            f'<td style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg);text-align:right;padding:5px 12px">${c:.3f}</td>'
            f'<td style="padding:5px 12px;width:80px">'
            f'<div style="height:3px;background:var(--bbb-surface-2);border-radius:2px">'
            f'<div style="height:3px;width:{bar_w};background:var(--bbb-warn);border-radius:2px"></div>'
            f'</div></td>'
            f'</tr>'
        )
    if not caller_rows:
        caller_rows = f'<tr><td colspan="4" style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-dim);padding:8px 12px">No cost data for {_cost_date or "today"}.</td></tr>'

    # ── time-bound actions ─────────────────────────────────────────────────────
    tba = _cfg.get("time_bound_actions", [])
    if tba:
        tba_rows = ""
        for action in tba:
            sym = action.get("symbol", "")
            deadline = action.get("deadline", "")
            reason = action.get("reason", "")
            tba_rows += (
                f'<tr><td style="font-family:monospace;font-size:11px">{sym}</td>'
                f'<td style="font-size:11px">{deadline}</td>'
                f'<td style="font-size:11px;color:var(--text-secondary)">{reason}</td></tr>'
            )
        tba_html = (
            '<div class="section-label">Time-Bound Actions</div>'
            '<div class="card" style="padding:0"><table class="data-table"><thead><tr>'
            '<th>Symbol</th><th>Deadline</th><th>Reason</th>'
            f'</tr></thead><tbody>{tba_rows}</tbody></table></div>'
        )
    else:
        tba_html = ""

    # ── left column: public context, architecture, bug log, learnings ─────────
    try:
        from trade_journal import build_bug_fix_log as _bfl  # noqa: I001
        _bugs = _bfl()
    except Exception:
        _bugs = []

    # Architecture pipeline diagram — 8-stage decision flow
    _pipe_stages = [
        ("REGIME",     "Haiku",    "Classifies market regime: risk_on / caution / risk_off using macro, VIX, sector, and breadth signals. Controls position sizing floors."),
        ("SIGNALS",    "Haiku",    "Scores each watchlist symbol 0–100 using L2 Python anchors (momentum, mean-reversion, volume) + L3 Haiku synthesis. Feeds Gate."),
        ("SCRATCHPAD", "Haiku",    "Per-symbol qualitative context sweep. Ingests news, earnings, insider activity, macro wires, Reddit sentiment into hot scratchpads."),
        ("GATE",       "Logic",    "State-change gate. Skips Sonnet if no material change (same positions hash, same catalyst hash, score below threshold, cooldown active)."),
        ("SONNET",     "Sonnet",   "Main decision call. Receives full context — regime, scores, scratchpads, memory, positions — and outputs buy/sell/hold with reasoning."),
        ("KERNEL",     "Python",   "Risk kernel enforces hard rules: position caps, exposure headroom, drawdown limits, stop placement. Rejects Sonnet actions that violate policy."),
        ("EXECUTION",  "Alpaca",   "Order router. Submits bracket orders, wires stop-loss and take-profit legs. Verifies fills. Escalates on partial fills or errors."),
        ("A2 OPTS",    "Sonnet×4", "Parallel options cycle. Runs 4-agent debate (Directional · Vol Analyst · Skeptic · Risk Officer) on top A1 candidates. Structures debit spreads and condors."),
    ]

    node_sty = (
        "display:inline-flex;flex-direction:column;align-items:center;gap:3px;"
        "padding:8px 14px;background:var(--bbb-surface-2);"
        "border:1px solid var(--bbb-border);border-radius:var(--bbb-r-2)"
    )
    arrow_sty = "font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg-dim);flex-shrink:0"

    # Stage pills row
    pills_html = '<div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap;margin-bottom:16px">'
    for i, (name, model, _) in enumerate(_pipe_stages):
        model_color = "var(--bbb-ai)" if "Sonnet" in model else ("var(--bbb-warn)" if "Haiku" in model else "var(--bbb-fg-dim)")
        pills_html += (
            f'<div style="{node_sty}">'
            f'<span style="font-family:var(--bbb-font-mono);font-size:10px;letter-spacing:.06em;'
            f'text-transform:uppercase;color:var(--bbb-fg)">{name}</span>'
            f'<span style="font-family:var(--bbb-font-mono);font-size:9px;color:{model_color}">{model}</span>'
            f'</div>'
        )
        if i < len(_pipe_stages) - 1:
            pills_html += f'<span style="{arrow_sty}">›</span>'
    pills_html += '</div>'

    # Stage description grid
    descs_html = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">'
    for name, model, desc in _pipe_stages:
        model_color = "var(--bbb-ai)" if "Sonnet" in model else ("var(--bbb-warn)" if "Haiku" in model else "var(--bbb-fg-dim)")
        descs_html += (
            f'<div style="padding:8px 12px;background:var(--bbb-surface-2);border-radius:var(--bbb-r-2);'
            f'border:1px solid var(--bbb-border)">'
            f'<div style="display:flex;align-items:baseline;gap:6px;margin-bottom:3px">'
            f'<span style="font-family:var(--bbb-font-mono);font-size:10px;letter-spacing:.06em;'
            f'text-transform:uppercase;color:var(--bbb-fg)">{name}</span>'
            f'<span style="font-family:var(--bbb-font-mono);font-size:9px;color:{model_color}">{model}</span>'
            f'</div>'
            f'<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted);'
            f'line-height:1.45">{desc}</span>'
            f'</div>'
        )
    descs_html += '</div>'

    arch_html = (
        '<div class="card" style="padding:16px 20px">'
        '<div style="display:flex;gap:24px;margin-bottom:12px;flex-wrap:wrap">'
        f'<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted)">strategy <span style="color:var(--bbb-fg);text-transform:uppercase">{active_strategy}</span></span>'
        '<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted)">accounts <span style="color:var(--bbb-fg)">A1 Equity · A2 Options</span></span>'
        '<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted)">cycle <span style="color:var(--bbb-fg)">5m market · 15m ext · 30m overnight</span></span>'
        '<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted)">paper <span style="color:var(--bbb-profit)">Alpaca — since 2026-04-13</span></span>'
        '</div>'
        + pills_html + descs_html +
        '</div>'
    )

    # Bug fix log
    bug_left_html = ""
    for b in _bugs:
        sev = b.get("severity", "LOW")
        sev_c = "#f85149" if sev == "HIGH" else ("#d29922" if sev == "MEDIUM" else "#8b949e")
        full_res = b.get("resolution", "")
        res = (full_res[:120] + "…") if len(full_res) > 120 else full_res
        res_div = f'<div style="font-size:11px;color:var(--text-muted)">&#x2714; {res}</div>' if res else ""
        bug_left_html += (
            f'<div style="padding:8px 0;border-bottom:1px solid var(--border-subtle)">'
            f'<div style="display:flex;gap:6px;align-items:baseline;margin-bottom:3px">'
            f'<span style="font-size:11px;font-weight:700;font-family:monospace;color:var(--accent-blue)">{b["id"]}</span>'
            f'<span style="font-size:10px;font-weight:700;color:{sev_c}">{sev}</span>'
            f'<span style="font-size:10px;color:var(--text-muted)">{b["start"]} – {b["end"]}</span>'
            f'</div>'
            f'<div style="font-size:12px;color:var(--text-primary);margin-bottom:2px">{b["title"]}</div>'
            f'{res_div}'
            f'</div>'
        )
    if not bug_left_html:
        bug_left_html = '<div style="color:var(--text-muted);font-size:12px">No bugs logged.</div>'

    # Director notes — read from weekly board meeting history (director_memo_history.json)
    _memo_history = _rj(BOT_DIR / "data/reports/director_memo_history.json", default=[])
    if not isinstance(_memo_history, list):
        _memo_history = []
    _memo_history = [m for m in _memo_history if isinstance(m, dict)]

    if _memo_history:
        _lm = _memo_history[-1]
        _lm_week = _lm.get("week", "")
        _lm_summary = (_lm.get("memo_summary") or "").strip()
        _lm_score = _lm.get("real_money_readiness_score")
        _lm_recs = [r for r in (_lm.get("key_recommendations") or []) if r.get("recommendation")][:4]
        _sc_col = ("var(--bbb-profit)" if (_lm_score or 0) >= 7
                   else ("var(--bbb-warn)" if (_lm_score or 0) >= 4 else "var(--bbb-loss)"))
        _score_badge = (
            f'<span style="font-family:var(--bbb-font-mono);font-size:9px;letter-spacing:.06em;'
            f'text-transform:uppercase;padding:2px 6px;border-radius:var(--bbb-r-1);'
            f'background:rgba(74,79,96,.2);color:{_sc_col}">readiness {float(_lm_score):.0f}/10</span>'
        ) if _lm_score is not None else ""
        _recs_html = ""
        for _r in _lm_recs:
            _rt = (_r.get("recommendation") or "")[:140]
            _rv = (_r.get("verdict") or "").upper()
            _rv_c = ("var(--bbb-profit)" if _rv == "RESOLVED"
                     else ("var(--bbb-warn)" if _rv == "PENDING" else "var(--bbb-fg-dim)"))
            _recs_html += (
                f'<div style="padding:4px 0;border-bottom:1px solid var(--bbb-border)">'
                f'<span style="font-family:var(--bbb-font-mono);font-size:9px;color:{_rv_c};'
                f'text-transform:uppercase;margin-right:6px">{_rv or "open"}</span>'
                f'<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted)">'
                f'{_rt}{"…" if len(_r.get("recommendation",""))>140 else ""}</span>'
                f'</div>'
            )
        _lm_summary_html = _md_to_html(_lm_summary)
        director_html = (
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">'
            f'<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg)">Week {_lm_week}</span>'
            f'{_score_badge}'
            f'</div>'
            f'<div class="bbb-md" style="max-height:260px;overflow-y:auto;padding-right:4px;margin-bottom:8px">{_lm_summary_html}</div>'
            + (
                '<div style="font-family:var(--bbb-font-mono);font-size:9px;letter-spacing:.06em;'
                'text-transform:uppercase;color:var(--bbb-fg-dim);margin-bottom:4px">Key Recommendations</div>'
                + _recs_html
            if _recs_html else "")
        )
    else:
        import datetime as _dt_dn
        _today_dn = _dt_dn.date.today()
        _days_to_sun = (6 - _today_dn.weekday()) % 7 or 7
        _next_sun = _today_dn + _dt_dn.timedelta(days=_days_to_sun)
        director_html = (
            '<p style="font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg-muted);margin:0 0 6px">'
            'Board meeting runs Sundays automatically.</p>'
            f'<p style="font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg-dim);margin:0">'
            f'Next meeting: {_next_sun}. Notes will appear here after the first run.</p>'
        )

    left_col = (
        '<div class="section-label">Bug Fix Log</div>'
        f'<div class="card" style="padding:10px 14px">{bug_left_html}</div>'
        '<div class="section-label">Strategy Director Notes</div>'
        f'<div class="card" style="padding:10px 14px">{director_html}</div>'
    )

    # Only show Feature Flags section if at least one flag is non-False
    _any_flag_on = any([
        bool(ff.get("hard_gate_claude_to_trading_window")), bool(ff.get("enable_model_tiering")),
        bool(ff.get("enable_divergence_summarizer")), bool(ff.get("enable_finnhub_news")),
        bool(_a2_enabled), bool(_a2rb.get("force_no_trade")), bool(_a2rb.get("disable_bounded_debate")),
        bool(_pa.get("enable_shadow")), bool(_pa.get("enable_live")),
        bool(ff.get("enable_cost_attribution_spine")), bool(ff.get("enable_recommendation_memory")),
        bool(ff.get("enable_abstention_contract")), bool(ff.get("enable_experience_library")),
        bool(ff.get("vector_memory_ab_logging")), bool(shadow_flags), bool(lab_flags),
    ])
    _flags_section = (
        '<div class="section-label">Feature Flags</div>'
        f'<div class="card" style="padding:0">{flags_html}</div>'
    ) if _any_flag_on else ""

    right_col = (
        # ── strategy overview ──────────────────────────────────────────────────
        '<div class="section-label">Risk Parameters</div>'
        f'<div class="card" style="padding:0">{params_html}</div>'
        + tba_html
        + _flags_section
        # ── claude cost breakdown ──────────────────────────────────────────────
        + f'<div class="section-label">Claude Cost — {_cost_date or "Today"}</div>'
        f'<div class="card">'
        f'<div class="stat-grid" style="margin-bottom:12px">'
        f'<div class="stat-box"><div class="stat-label">Daily Spend</div>'
        f'<div class="stat-val" style="color:var(--bbb-warn)">{_fm(daily_cost)}</div></div>'
        f'<div class="stat-box"><div class="stat-label">All-time Spend</div>'
        f'<div class="stat-val" style="font-size:15px;color:var(--bbb-fg-muted)">{_fm(all_time_cost_tr)}</div></div>'
        f'<div class="stat-box"><div class="stat-label">Sonnet Calls</div>'
        f'<div class="stat-val">{sonnet_calls_today}</div></div>'
        f'<div class="stat-box"><div class="stat-label">Haiku Calls</div>'
        f'<div class="stat-val" style="color:var(--bbb-fg-muted)">{haiku_calls_today}</div></div>'
        f'<div class="stat-box"><div class="stat-label">Monthly Est. (22d)</div>'
        f'<div class="stat-val" style="color:var(--bbb-fg-muted)">{_fm(proj_monthly)}</div></div>'
        f'</div>'
        '<table class="data-table" style="margin-top:8px"><thead><tr>'
        '<th style="font-size:10px">Caller</th>'
        '<th style="font-size:10px;text-align:right">Calls</th>'
        '<th style="font-size:10px;text-align:right">Cost</th>'
        '<th style="font-size:10px">Share</th>'
        f'</tr></thead><tbody>{caller_rows}</tbody></table>'
        f'</div>'
    )

    _trans_md_css = (
        '<style>'
        '.bbb-md{font-family:var(--bbb-font-mono);font-size:12px;color:var(--bbb-fg-muted);line-height:1.6}'
        '.bbb-md h1,.bbb-md h2{font-size:13px;font-weight:500;color:var(--bbb-fg);margin:12px 0 5px;font-family:var(--bbb-font-sans)}'
        '.bbb-md h3,.bbb-md h4{font-size:11px;font-weight:500;color:var(--bbb-fg-muted);text-transform:uppercase;letter-spacing:.06em;margin:8px 0 3px;font-family:var(--bbb-font-sans)}'
        '.bbb-md p{margin:0 0 6px}'
        '.bbb-md strong{color:var(--bbb-fg);font-weight:500}'
        '.bbb-md hr{border:none;border-top:1px solid var(--bbb-border);margin:8px 0}'
        '.bbb-md ul,.bbb-md ol{padding-left:16px;margin:3px 0 6px}'
        '.bbb-md li{margin-bottom:2px}'
        '.bbb-md code{background:var(--bbb-surface-2);border:1px solid var(--bbb-border);border-radius:2px;padding:0 3px;font-size:11px;color:var(--bbb-fg)}'
        '.bbb-md pre{background:var(--bbb-surface-2);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-2);padding:8px 10px;overflow-x:auto;margin:6px 0}'
        '.bbb-md pre code{background:none;border:none;padding:0}'
        '</style>'
    )
    body = (
        _trans_md_css
        + '<div class="container">'
        + '<div class="section-label">Decision Pipeline</div>'
        + arch_html
        + f'<div class="compact-grid">'
        f'<div>{left_col}</div>'
        f'<div>{right_col}</div>'
        f'</div>'
        '</div>'
    )
    return _page_shell("Transparency", nav, body, ticker)


@app.route("/transparency")
def page_transparency():
    return _page_transparency(_now_et())


def _bbb_cycle_wheel_svg() -> str:
    """280×280 SVG 24h clock frame. Dynamic dots injected by JS."""
    import math as _math
    cx, cy = 140, 140
    R_ring = 120   # outer clock ring
    R_dot  = 82    # cycle dot radius
    R_hand = 108   # current-time hand length

    ticks = []
    for h in range(24):
        angle = (h / 24) * 2 * _math.pi - _math.pi / 2
        r_in = R_ring - (10 if h % 6 == 0 else 5 if h % 3 == 0 else 2)
        x1 = cx + r_in * _math.cos(angle)
        y1 = cy + r_in * _math.sin(angle)
        x2 = cx + R_ring * _math.cos(angle)
        y2 = cy + R_ring * _math.sin(angle)
        ticks.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"'
            f' stroke="var(--bbb-border)" stroke-width="1"/>'
        )

    labels = []
    for h, lbl in [(0, "0"), (6, "6"), (12, "12"), (18, "18")]:
        angle = (h / 24) * 2 * _math.pi - _math.pi / 2
        r_lbl = R_ring - 22
        x = cx + r_lbl * _math.cos(angle)
        y = cy + r_lbl * _math.sin(angle)
        labels.append(
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle"'
            f' dominant-baseline="middle" font-family="var(--bbb-font-mono)"'
            f' font-size="9" fill="var(--bbb-fg-dim)">{lbl}</text>'
        )

    # Market-hours arc: 9:30 AM – 4:00 PM ET (arced along the ring)
    def arc_xy(h_float, r):
        a = (h_float / 24) * 2 * _math.pi - _math.pi / 2
        return cx + r * _math.cos(a), cy + r * _math.sin(a)

    mx1, my1 = arc_xy(9.5, R_ring)
    mx2, my2 = arc_xy(16.0, R_ring)
    market_arc = (
        f'<path d="M {mx1:.1f} {my1:.1f} A {R_ring} {R_ring} 0 0 1 {mx2:.1f} {my2:.1f}"'
        f' fill="none" stroke="rgba(52,211,153,0.18)" stroke-width="7" stroke-linecap="round"/>'
    )

    tick_str   = "\n  ".join(ticks)
    label_str  = "\n  ".join(labels)

    return (
        f'<svg id="cycle-wheel" width="280" height="280" viewBox="0 0 280 280"'
        f' style="display:block;margin:0 auto">\n'
        f'  <circle cx="{cx}" cy="{cy}" r="{R_ring}" fill="none"'
        f' stroke="var(--bbb-border)" stroke-width="1"/>\n'
        f'  {tick_str}\n'
        f'  {label_str}\n'
        f'  {market_arc}\n'
        f'  <g id="wheel-dots"></g>\n'
        f'  <circle id="wheel-sel-ring" cx="{cx}" cy="{cy}" r="0" fill="none"'
        f' stroke="var(--bbb-ai)" stroke-width="1.5"/>\n'
        f'  <circle id="wheel-sel-dot" cx="{cx}" cy="{cy}" r="0" fill="var(--bbb-ai)"/>\n'
        f'  <line id="wheel-hand" x1="{cx}" y1="{cy}" x2="{cx}" y2="{cy-R_hand:.1f}"'
        f' stroke="var(--bbb-fg-dim)" stroke-width="1" stroke-linecap="round"/>\n'
        f'  <circle cx="{cx}" cy="{cy}" r="2.5" fill="var(--bbb-fg-dim)"/>\n'
        f'  <text x="{cx}" y="{cy+16}" text-anchor="middle"'
        f' font-family="var(--bbb-font-mono)" font-size="8"'
        f' fill="var(--bbb-fg-dim)">ET</text>\n'
        f'</svg>'
    )


def _bbb_calibration_svg_frame() -> str:
    """
    Empty SVG frame for the calibration plot.
    Axes, reference line, and labels are static; dots injected by JS.
    X-axis: conviction (LOW=0.45  MED=0.65  HIGH=0.85)
    Y-axis: realized return % (clamped to ±20)
    """
    W, H = 560, 180
    PAD_L, PAD_R, PAD_T, PAD_B = 48, 24, 16, 36

    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B

    # X mapping: conviction 0.3 → 1.0
    x_min, x_max = 0.30, 1.00
    # Y mapping: return -20 → +20
    y_min, y_max = -20.0, 20.0

    def px(conv):
        return PAD_L + (conv - x_min) / (x_max - x_min) * plot_w

    def py(ret):
        return PAD_T + plot_h - (ret - y_min) / (y_max - y_min) * plot_h

    # Zero line (y=0)
    y0 = py(0)
    zero_line = (
        f'<line x1="{PAD_L}" y1="{y0:.1f}" x2="{W - PAD_R}" y2="{y0:.1f}"'
        f' stroke="var(--bbb-border)" stroke-width="1" stroke-dasharray="3 3"/>'
    )

    # Reference diagonal: "higher confidence → higher return"
    # From (LOW conviction, -5%) to (HIGH conviction, +10%)
    rx1, ry1 = px(0.45), py(-5)
    rx2, ry2 = px(0.85), py(10)
    ref_line = (
        f'<line x1="{rx1:.1f}" y1="{ry1:.1f}" x2="{rx2:.1f}" y2="{ry2:.1f}"'
        f' stroke="var(--bbb-fg-dim)" stroke-width="1" stroke-dasharray="4 4"/>'
    )
    # Reference label at right end
    ref_label = (
        f'<text x="{rx2+4:.1f}" y="{ry2:.1f}" font-family="var(--bbb-font-mono)"'
        f' font-size="8" fill="var(--bbb-fg-dim)" dominant-baseline="middle">'
        f'higher confidence → higher return</text>'
    )

    # X-axis ticks and labels
    x_ticks = ""
    for conv, lbl in [(0.45, "LOW"), (0.65, "MED"), (0.85, "HIGH")]:
        xp = px(conv)
        bot = PAD_T + plot_h
        x_ticks += (
            f'<line x1="{xp:.1f}" y1="{bot}" x2="{xp:.1f}" y2="{bot+4}"'
            f' stroke="var(--bbb-border)" stroke-width="1"/>'
            f'<text x="{xp:.1f}" y="{bot+13}" text-anchor="middle"'
            f' font-family="var(--bbb-font-mono)" font-size="9"'
            f' fill="var(--bbb-fg-dim)">{lbl}</text>'
        )

    # Y-axis ticks
    y_ticks = ""
    for ret in [-10, 0, 10]:
        yp = py(ret)
        sign = "+" if ret > 0 else ""
        y_ticks += (
            f'<line x1="{PAD_L-4}" y1="{yp:.1f}" x2="{PAD_L}" y2="{yp:.1f}"'
            f' stroke="var(--bbb-border)" stroke-width="1"/>'
            f'<text x="{PAD_L-7}" y="{yp:.1f}" text-anchor="end"'
            f' dominant-baseline="middle" font-family="var(--bbb-font-mono)"'
            f' font-size="9" fill="var(--bbb-fg-dim)">{sign}{ret}%</text>'
        )

    # Axis lines
    axes = (
        f'<line x1="{PAD_L}" y1="{PAD_T}" x2="{PAD_L}" y2="{PAD_T+plot_h}"'
        f' stroke="var(--bbb-border)" stroke-width="1"/>'
        f'<line x1="{PAD_L}" y1="{PAD_T+plot_h}" x2="{W-PAD_R}" y2="{PAD_T+plot_h}"'
        f' stroke="var(--bbb-border)" stroke-width="1"/>'
    )

    # X-axis label
    x_axis_label = (
        f'<text x="{PAD_L + plot_w/2:.1f}" y="{H-2}" text-anchor="middle"'
        f' font-family="var(--bbb-font-sans)" font-size="9" letter-spacing=".06em"'
        f' text-transform="uppercase" fill="var(--bbb-fg-muted)">Conviction at Entry</text>'
    )
    # Y-axis label (rotated)
    y_axis_label = (
        f'<text x="10" y="{PAD_T + plot_h/2:.1f}" text-anchor="middle"'
        f' font-family="var(--bbb-font-sans)" font-size="9" letter-spacing=".06em"'
        f' fill="var(--bbb-fg-muted)" transform="rotate(-90 10 {PAD_T + plot_h/2:.1f})">'
        f'Realized Return %</text>'
    )

    return (
        f'<svg id="calib-plot" width="{W}" height="{H}" viewBox="0 0 {W} {H}"'
        f' style="display:block;max-width:100%">\n'
        f'  {axes}\n'
        f'  {zero_line}\n'
        f'  {ref_line}\n'
        f'  {ref_label}\n'
        f'  {x_ticks}\n'
        f'  {y_ticks}\n'
        f'  {x_axis_label}\n'
        f'  {y_axis_label}\n'
        f'  <g id="calib-dots"></g>\n'
        f'  <!-- Brier score chip -->\n'
        f'  <text id="calib-brier" x="{W-PAD_R-2}" y="{PAD_T+10}" text-anchor="end"'
        f' font-family="var(--bbb-font-mono)" font-size="9" fill="var(--bbb-fg-dim)"></text>\n'
        f'  <!-- Empty state message -->\n'
        f'  <text id="calib-empty" x="{PAD_L+plot_w/2:.1f}" y="{PAD_T+plot_h/2:.1f}"'
        f' text-anchor="middle" dominant-baseline="middle"'
        f' font-family="var(--bbb-font-mono)" font-size="11" fill="var(--bbb-fg-dim)">'
        f'calibrating — data accumulates as trades close</text>\n'
        f'</svg>'
    )


def _page_theater(now_et: str) -> str:
    nav = _nav_html("theater", now_et)
    _init_idx = 0
    try:
        import sys as _sys  # noqa: PLC0415
        _sys.path.insert(0, str(BOT_DIR))
        from decision_theater import find_last_filled_cycle_index  # noqa: I001
        _init_idx = find_last_filled_cycle_index()
    except Exception:
        pass
    try:
        import sys as _sys  # noqa: PLC0415
        _sys.path.insert(0, str(BOT_DIR))
        from decision_theater import get_all_trades_summary, get_cycle_view  # noqa: I001
        cycle = get_cycle_view(_init_idx)
        trades_sum = get_all_trades_summary()
    except Exception:
        cycle = {"cycle_number": 0, "total_cycles": 0, "timestamp": "", "session": "unknown",
                 "decision_id": "", "outcome": "hold", "stages": {}}
        trades_sum = {"trades": [], "open_count": 0, "closed_count": 0, "total": 0}

    cycle_num = cycle.get("cycle_number", 0)
    total = cycle.get("total_cycles", 0)
    session = cycle.get("session", "")
    stages = cycle.get("stages", {})
    _milestone_badge = (
        f'<span style="font-family:var(--bbb-font-mono);font-size:9px;letter-spacing:.05em;'
        f'text-transform:uppercase;padding:2px 6px;border-radius:2px;'
        f'background:rgba(52,211,153,.12);color:#34D399;margin-left:6px">'
        f'Cycle #{total} · milestone</span>'
    ) if total > 0 and total % 500 == 0 else ""

    # ── Trade pills ────────────────────────────────────────────────────────────
    pills_html = ""
    for t in trades_sum["trades"][:20]:
        sym = t["symbol"]
        pnl_pct = t["pnl_pct"]
        sign = "+" if pnl_pct >= 0 else ""
        entry_date = t.get("entry_date", "")
        pc = t.get("pill_class", "tp-flat")
        pnl_color = "#34D399" if pnl_pct >= 0 else "#F87171"
        pills_html += (
            f'<button class="trade-pill {pc}" '
            f'onclick="loadTrade(\'{sym}\',\'{entry_date}\')">'
            f'{sym} · {t["status"]} · '
            f'<span style="color:{pnl_color}">{sign}{pnl_pct:.1f}%</span>'
            f'</button>\n'
        )
    if not pills_html:
        pills_html = '<span style="font-size:11px;color:var(--bbb-fg-muted)">No trades yet.</span>'

    # ── Pipeline node HTML ─────────────────────────────────────────────────────
    _STAGE_DEFS = [
        ("regime",     "🌡", "Regime"),
        ("signals",    "📡", "Sigs"),
        ("scratchpad", "📋", "Scratch"),
        ("gate",       "🚦", "Gate"),
        ("sonnet",     "🧠", "Sonnet"),
        ("kernel",     "⚙",  "Kernel"),
        ("execution",  "⚡", "Exec"),
        ("a2",         "📊", "A2"),
    ]
    nodes_html = ""
    for stage_id, icon, name in _STAGE_DEFS:
        st = stages.get(stage_id, {})
        st_status = st.get("status", "warn")
        # Node state CSS class
        if st_status == "ok":
            node_cls = "pipe-node complete"
        elif st_status == "skip":
            node_cls = "pipe-node skipped"
        else:
            node_cls = "pipe-node"
        metric = _theater_stage_metric(stage_id, st)
        summary = st.get("summary", "") or metric
        nodes_html += (
            f'<div class="{node_cls}" id="pipe-{stage_id}" '
            f'onclick="selectStage(\'{stage_id}\')" '
            f'data-summary="{summary[:60]}">'
            f'<span class="pipe-icon">{icon}</span>'
            f'<div class="pipe-name">{name}</div>'
            f'<div class="pipe-metric">{metric}</div>'
            f'</div>'
        )

    # ── Serialize for JS ───────────────────────────────────────────────────────
    cycle_json = json.dumps(cycle, default=str)
    trades_json = json.dumps(trades_sum, default=str)

    import base64 as _b64
    _auth_b64 = _b64.b64encode(f"{DASHBOARD_USER}:{DASHBOARD_PASSWORD}".encode()).decode()

    initial_reasoning = stages.get("sonnet", {}).get("reasoning_full") or \
                        stages.get("sonnet", {}).get("reasoning_excerpt") or ""
    initial_ideas_html = _theater_ideas_html(stages.get("sonnet", {}).get("ideas", []))

    body = f"""
<div id="auth-b64" data-val="{_auth_b64}" style="display:none"></div>
<div class="container" style="padding-bottom:60px">

  <!-- Mode toggle -->
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:var(--bbb-s-4)">
    <button class="th-mode-btn" id="btn-cycle" onclick="setMode('cycle')">Cycle View</button>
    <button class="th-mode-btn" id="btn-trade" onclick="setMode('trade')">Trade Lifecycle</button>
    <span id="th-loading" style="display:none;font-family:var(--bbb-font-mono);font-size:var(--bbb-t-caption);color:var(--bbb-ai)">loading…</span>
    <div style="margin-left:auto;display:flex;align-items:center;font-family:var(--bbb-font-mono);font-size:var(--bbb-t-caption);color:var(--bbb-fg-muted)" id="th-cycle-meta">
      {session} · cycle {cycle_num+1}/{total}{_milestone_badge}
    </div>
  </div>

  <!-- ══ CYCLE VIEW ══ -->
  <div id="panel-cycle" style="display:none">

    <!-- Scrubber -->
    <div class="th-scrubber-wrap" id="scrubber-wrap">
      <div class="th-scrubber-track" id="scrubber-track"></div>
      <div class="th-scrubber-cursor" id="scrubber-cursor"></div>
      <div class="th-scrubber-tooltip" id="scrubber-tooltip"></div>
    </div>
    <div style="display:flex;align-items:center;gap:var(--bbb-s-3);margin-top:var(--bbb-s-2);margin-bottom:var(--bbb-s-3)">
      <button class="th-ctrl-btn" id="btn-play" onclick="togglePlay()">▶ Play</button>
      <button class="th-ctrl-btn" id="btn-speed" onclick="cycleSpeed()">1×</button>
      <button class="th-ctrl-btn" onclick="jumpNow()">Now</button>
      <span style="font-family:var(--bbb-font-mono);font-size:var(--bbb-t-caption);color:var(--bbb-fg-muted)" id="scrubber-label">cycle {cycle_num+1}</span>
    </div>

    <!-- Pipeline + Reasoning + Wheel split -->
    <div class="th-split">

      <!-- Pipeline -->
      <div class="th-pipeline-col">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:var(--bbb-s-2)">
          <div class="bbb-hero-label" style="margin-bottom:0">Pipeline</div>
          <div style="margin-left:auto;display:flex;align-items:center;gap:5px">
            <button class="th-ctrl-btn" id="btn-replay-speed" onclick="replaySpeedChange()" title="Replay speed">1×</button>
            <button class="th-ctrl-btn" id="btn-replay" onclick="replayStart()" title="Animate through each pipeline stage of the last completed cycle">&#9654; Replay last cycle</button>
          </div>
        </div>
        <div class="th-pipe-row" id="pipe-row">
          {nodes_html}
        </div>
        <!-- Replay outcome banner (hidden until replay completes) -->
        <div id="replay-outcome-banner" style="display:none;margin-bottom:var(--bbb-s-3);
             padding:8px 12px;border-radius:var(--bbb-r-2);font-family:var(--bbb-font-mono);
             font-size:12px;display:none;align-items:center;gap:8px">
          <span id="replay-outcome-icon" style="font-size:14px"></span>
          <span style="color:var(--bbb-fg-muted)">Cycle complete &mdash;</span>
          <span id="replay-outcome-text" style="font-weight:500"></span>
        </div>
        <!-- Stage detail -->
        <div class="th-stage-detail" id="stage-detail-panel">
          <span style="color:var(--bbb-fg-dim);font-family:var(--bbb-font-mono);font-size:var(--bbb-t-caption)">click a stage</span>
        </div>
        <!-- Ideas table -->
        <div class="bbb-hero-label" style="margin-top:var(--bbb-s-4);margin-bottom:var(--bbb-s-2)">Ideas Generated</div>
        <div id="ideas-panel" style="background:var(--bbb-surface);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3)">
          {initial_ideas_html}
        </div>
      </div>

      <!-- Reasoning stream -->
      <div class="th-reasoning-col">
        <div class="bbb-hero-label" style="margin-bottom:var(--bbb-s-2)">Sonnet Reasoning</div>
        <div class="th-reasoning" id="reasoning-panel">
          <div class="th-reason-bar"></div>
          <div class="th-reason-body" id="reasoning-text"></div>
        </div>
      </div>

      <!-- 24h Cycle Wheel -->
      <div class="th-wheel-col">
        <div class="bbb-hero-label" style="margin-bottom:var(--bbb-s-2)">24h Activity</div>
        <div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3);padding:var(--bbb-s-3)">
          {_bbb_cycle_wheel_svg()}
          <!-- Outcome legend -->
          <div id="wheel-legend" style="margin-top:var(--bbb-s-2);display:flex;flex-wrap:wrap;gap:var(--bbb-s-2) var(--bbb-s-3);justify-content:center"></div>
          <!-- Selected cycle time -->
          <div id="wheel-sel-label" style="margin-top:var(--bbb-s-2);text-align:center;font-family:var(--bbb-font-mono);font-size:var(--bbb-t-caption);color:var(--bbb-fg-muted)"></div>
        </div>
      </div>

      <!-- Calibration Plot -->
      <div class="th-calib-col">
        <div class="bbb-hero-label" style="margin-bottom:var(--bbb-s-2)">Conviction Calibration</div>
        <div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3);padding:var(--bbb-s-3) var(--bbb-s-4);overflow:hidden">
          {_bbb_calibration_svg_frame()}
        </div>
      </div>

    </div>
  </div>

  <!-- ══ TRADE LIFECYCLE VIEW ══ -->
  <div id="panel-trade" style="display:none">

    <div class="bbb-hero-label" style="margin-bottom:var(--bbb-s-2)">Select a Trade</div>
    <div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3);padding:var(--bbb-s-3) var(--bbb-s-4);margin-bottom:var(--bbb-s-4)">
      <div style="display:flex;flex-wrap:wrap;gap:6px">{pills_html}</div>
    </div>

    <div id="trade-hero">
      <div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3);padding:24px;color:var(--bbb-fg-muted);font-family:var(--bbb-font-mono);font-size:var(--bbb-t-caption);text-align:center">
        ← Select a trade from the list to view its full journey
      </div>
    </div>

    <div id="price-journey-wrap" style="display:none;margin-top:var(--bbb-s-3)">
      <div class="bbb-hero-label" style="margin-bottom:var(--bbb-s-2)">Price Journey</div>
      <div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3);padding:var(--bbb-s-4)">
        <div id="price-journey-bar" style="position:relative;height:56px;background:var(--bbb-surface-2);border-radius:var(--bbb-r-2)"></div>
        <div id="price-journey-labels" style="display:flex;justify-content:space-between;margin-top:6px;font-family:var(--bbb-font-mono);font-size:var(--bbb-t-caption);color:var(--bbb-fg-muted)"></div>
      </div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:var(--bbb-s-3);margin-top:var(--bbb-s-3)">
      <div>
        <div class="bbb-hero-label" style="margin-bottom:var(--bbb-s-2)">Lifecycle Timeline</div>
        <div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3);padding:var(--bbb-s-3) var(--bbb-s-4);min-height:160px" id="lifecycle-timeline">
          <span style="font-family:var(--bbb-font-mono);font-size:var(--bbb-t-caption);color:var(--bbb-fg-muted)">← Select a trade from the list to view its full journey</span>
        </div>
      </div>
      <div>
        <div class="bbb-hero-label" style="margin-bottom:var(--bbb-s-2)">Entry Thesis &amp; Exit Scenarios</div>
        <div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3);padding:var(--bbb-s-3) var(--bbb-s-4);min-height:160px" id="trade-thesis">
          <span style="font-family:var(--bbb-font-mono);font-size:var(--bbb-t-caption);color:var(--bbb-fg-muted)">← Select a trade from the list to view its full journey</span>
        </div>
      </div>
    </div>

  </div>

</div>

<style>
/* ── Mode buttons ── */
.th-mode-btn {{
  font-family: var(--bbb-font-mono); font-size: var(--bbb-t-label);
  letter-spacing: 0.06em; text-transform: uppercase;
  background: var(--bbb-surface); border: 1px solid var(--bbb-border);
  color: var(--bbb-fg-muted); padding: 5px 14px;
  border-radius: var(--bbb-r-2); cursor: pointer;
  transition: border-color var(--bbb-dur-fast) var(--bbb-ease),
              color var(--bbb-dur-fast) var(--bbb-ease);
}}
.th-mode-btn.active {{
  border-color: var(--bbb-ai-border); color: var(--bbb-ai);
  background: var(--bbb-ai-soft);
}}
.th-ctrl-btn {{
  font-family: var(--bbb-font-mono); font-size: 10px;
  background: var(--bbb-surface-2); border: 1px solid var(--bbb-border);
  color: var(--bbb-fg-muted); padding: 3px 10px;
  border-radius: var(--bbb-r-2); cursor: pointer;
}}
.th-ctrl-btn:hover {{ color: var(--bbb-fg); border-color: var(--bbb-fg-dim); }}

/* ── Scrubber ── */
.th-scrubber-wrap {{
  position: relative; height: 12px;
  background: var(--bbb-surface); border: 1px solid var(--bbb-border);
  border-radius: var(--bbb-r-2); cursor: crosshair; overflow: hidden;
  margin-bottom: 0;
}}
.th-scrubber-track {{
  position: absolute; inset: 0; border-radius: var(--bbb-r-2);
  display: flex; align-items: stretch; gap: 1px; padding: 2px 1px;
}}
.th-scrub-tick {{
  flex: 1; min-width: 0; border-radius: 1px;
}}
.th-scrub-tick.t-filled  {{ background: var(--bbb-profit); }}
.th-scrub-tick.t-skipped {{ background: var(--bbb-fg-dim); }}
.th-scrub-tick.t-pending {{ background: var(--bbb-warn); }}
.th-scrub-tick.t-rejected{{ background: var(--bbb-loss); }}
.th-scrub-tick.t-hold    {{ background: var(--bbb-fg-dim); opacity: 0.4; }}
.th-scrubber-cursor {{
  position: absolute; top: 0; bottom: 0; width: 2px;
  background: var(--bbb-ai); border-radius: 1px;
  box-shadow: 0 0 5px var(--bbb-ai); pointer-events: none;
  transition: left 80ms var(--bbb-ease);
}}
.th-scrubber-tooltip {{
  position: absolute; bottom: 28px; transform: translateX(-50%);
  background: var(--bbb-surface-2); border: 1px solid var(--bbb-border);
  border-radius: var(--bbb-r-2); padding: 4px 8px;
  font-family: var(--bbb-font-mono); font-size: 10px; color: var(--bbb-fg);
  white-space: nowrap; pointer-events: none; display: none; z-index: 10;
}}

/* ── Pipeline ── */
.th-split {{
  display: grid; grid-template-columns: 1fr 1fr 280px 1fr; gap: var(--bbb-s-4);
  align-items: start;
}}
.th-pipeline-col {{ min-width: 0; }}
.th-reasoning-col {{ min-width: 0; }}
.th-wheel-col {{ width: 280px; flex: none; }}
.th-calib-col {{ min-width: 0; }}
.th-pipe-row {{
  display: flex; flex-wrap: nowrap; gap: 4px;
  margin-bottom: var(--bbb-s-3);
}}
.pipe-node {{
  display: flex; flex-direction: column; align-items: center;
  flex: 1 1 0; min-width: 0; padding: 8px 4px 6px;
  background: var(--bbb-surface); border: 1px solid var(--bbb-border);
  border-radius: var(--bbb-r-3); cursor: pointer; position: relative;
  transition: border-color var(--bbb-dur-fast) var(--bbb-ease);
  text-align: center;
}}
.pipe-node:hover {{ border-color: var(--bbb-fg-dim); }}
.pipe-node.active {{ border-color: var(--bbb-ai); background: var(--bbb-ai-soft); }}
.pipe-node.complete {{ border-color: var(--bbb-profit); opacity: 1; }}
.pipe-node.complete .pipe-icon {{ color: var(--bbb-profit); }}
.pipe-node.skipped {{ border-color: var(--bbb-border); opacity: 0.45; }}
.pipe-node.skipped::after {{
  content: ''; position: absolute; inset: 4px;
  background: repeating-linear-gradient(
    -45deg, var(--bbb-fg-dim) 0, var(--bbb-fg-dim) 1px, transparent 1px, transparent 5px
  );
  border-radius: 2px; pointer-events: none;
}}
/* ── Replay stage states ── */
.pipe-node.replay-future {{
  opacity: 0.28;
  transition: opacity var(--bbb-dur-base) var(--bbb-ease),
              border-color var(--bbb-dur-base) var(--bbb-ease);
}}
.pipe-node.replay-active {{
  border-color: var(--bbb-ai) !important;
  background: var(--bbb-ai-soft) !important;
  opacity: 1 !important;
  box-shadow: 0 0 0 1px var(--bbb-ai-border);
  animation: bbb-ai-pulse 1.2s ease-in-out infinite;
}}
.pipe-node.replay-done {{
  opacity: 0.55;
  border-color: var(--bbb-border);
  transition: opacity var(--bbb-dur-slow) var(--bbb-ease);
}}
.pipe-node.replay-done .pipe-icon::after {{
  content: ' ✓';
  font-size: 10px;
  color: var(--bbb-profit);
  vertical-align: super;
}}
.pipe-icon  {{ font-size: 15px; line-height: 1; }}
.pipe-name  {{
  font-family: var(--bbb-font-sans); font-size: 10px; font-weight: var(--bbb-w-medium);
  letter-spacing: 0.05em; text-transform: uppercase;
  color: var(--bbb-fg-muted); margin-top: 3px; line-height: 1.2;
  white-space: nowrap; overflow: visible;
}}
.pipe-metric {{
  font-family: var(--bbb-font-mono); font-size: 9px;
  color: var(--bbb-fg-dim); margin-top: 2px; line-height: 1.2;
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}}
.th-stage-detail {{
  background: var(--bbb-surface); border: 1px solid var(--bbb-border);
  border-radius: var(--bbb-r-3); padding: var(--bbb-s-3) var(--bbb-s-4);
  min-height: 80px;
}}

/* ── Reasoning stream ── */
.th-reasoning {{
  display: flex; gap: var(--bbb-s-3);
  background: var(--bbb-surface); border: 1px solid var(--bbb-border);
  border-radius: var(--bbb-r-3); padding: var(--bbb-s-3) var(--bbb-s-4);
  min-height: 320px; max-height: 520px; overflow-y: auto;
}}
.th-reason-bar {{
  flex: none; width: 2px; border-radius: 1px;
  background: var(--bbb-ai); align-self: stretch; min-height: 100%;
}}
.th-reason-body {{
  font-family: var(--bbb-font-mono); font-size: var(--bbb-t-reasoning);
  color: var(--bbb-fg); line-height: var(--bbb-lh-normal);
  white-space: pre-wrap; word-break: break-word; flex: 1; min-width: 0;
}}

/* ── Trade lifecycle ── */
.trade-pill {{
  background: var(--bbb-surface); border: 1px solid var(--bbb-border);
  color: var(--bbb-fg-muted); font-family: var(--bbb-font-mono);
  font-size: 10px; padding: 4px 10px;
  border-radius: 12px; cursor: pointer; white-space: nowrap;
  transition: border-color var(--bbb-dur-fast) var(--bbb-ease);
}}
.tp-open  {{ border-color: rgba(52,211,153,.35); color: var(--bbb-profit); }}
.tp-win   {{ border-color: rgba(96,165,250,.35); color: var(--bbb-info); }}
.tp-bug   {{ border-color: rgba(251,191,36,.35); color: var(--bbb-warn); }}
.tp-loss  {{ border-color: rgba(248,113,113,.35); color: var(--bbb-loss); }}
.tp-flat  {{ border-color: var(--bbb-border); color: var(--bbb-fg-muted); }}
.trade-pill:hover, .trade-pill.selected {{ border-color: var(--bbb-ai-border); color: var(--bbb-fg); }}
.tl-event {{ padding: 7px 0; border-bottom: 1px solid var(--bbb-border); font-size: 11px; }}
.tl-event:last-child {{ border-bottom: none; }}
.tl-dot {{
  display: inline-block; width: 8px; height: 8px;
  border-radius: 50%; margin-right: 6px; vertical-align: middle;
}}
</style>

<script>
var _cycleData  = {cycle_json};
var _tradesData = {trades_json};
var _currentCycle  = _cycleData;
var _totalCycles   = _cycleData.total_cycles || 0;
var _currentIdx    = {_init_idx};
var _selectedStage = null;
var _playTimer = null;
var _playSpeed = 1;   // 1 2 4
var _typingTimer = null;

function _authHeader() {{
  var el = document.getElementById('auth-b64');
  return 'Basic ' + (el ? el.getAttribute('data-val') : '');
}}

// ── Mode ──────────────────────────────────────────────────────────────────────
function setMode(m) {{
  document.getElementById('panel-cycle').style.display = m==='cycle' ? '' : 'none';
  document.getElementById('panel-trade').style.display  = m==='trade' ? '' : 'none';
  document.getElementById('btn-cycle').classList.toggle('active', m==='cycle');
  document.getElementById('btn-trade').classList.toggle('active', m==='trade');
}}

// ── Scrubber ──────────────────────────────────────────────────────────────────
function _outcomeClass(outcome) {{
  var map = {{filled:'t-filled',skipped:'t-skipped',pending:'t-pending',rejected:'t-rejected',hold:'t-hold'}};
  return map[outcome] || 't-hold';
}}

function buildScrubber(cycles) {{
  // cycles: array of {{outcome, timestamp}} for all cycles
  var track = document.getElementById('scrubber-track');
  var wrap  = document.getElementById('scrubber-wrap');
  track.innerHTML = '';
  var n = cycles.length;
  if (!n) return;
  var frag = document.createDocumentFragment();
  cycles.forEach(function(c, i) {{
    var tick = document.createElement('div');
    tick.className = 'th-scrub-tick ' + _outcomeClass(c.outcome);
    tick.dataset.idx = i;
    frag.appendChild(tick);
  }});
  track.appendChild(frag);
  moveCursor(_currentIdx, n);
  // Mouse interaction
  wrap.addEventListener('click', function(e) {{
    var rect = wrap.getBoundingClientRect();
    var pct = (e.clientX - rect.left) / rect.width;
    var idx = Math.round(pct * (n-1));
    idx = Math.max(0, Math.min(n-1, idx));
    selectCycle(idx);
  }});
  wrap.addEventListener('mousemove', function(e) {{
    var rect = wrap.getBoundingClientRect();
    var pct = (e.clientX - rect.left) / rect.width;
    var idx = Math.round(pct * (n-1));
    idx = Math.max(0, Math.min(n-1, idx));
    showTooltip(e, cycles[idx], idx);
  }});
  wrap.addEventListener('mouseleave', function() {{
    document.getElementById('scrubber-tooltip').style.display = 'none';
  }});
}}

function moveCursor(idx, total) {{
  var cursor = document.getElementById('scrubber-cursor');
  var n = total || _totalCycles;
  var pct = n > 1 ? (idx / (n-1)) * 100 : 0;
  cursor.style.left = pct + '%';
}}

function showTooltip(e, cycleInfo, idx) {{
  var tip = document.getElementById('scrubber-tooltip');
  var wrap = document.getElementById('scrubber-wrap');
  var rect = wrap.getBoundingClientRect();
  var pct = (e.clientX - rect.left) / rect.width * 100;
  tip.style.left = pct + '%';
  tip.style.display = 'block';
  var ts = (cycleInfo.ts || cycleInfo.timestamp || '').substring(0,16).replace('T',' ');
  var out = cycleInfo.outcome || '';
  tip.textContent = 'cycle ' + (idx+1) + ' · ' + out + (ts ? ' · '+ts : '');
}}

// ── Cycle select / fetch ──────────────────────────────────────────────────────
function selectCycle(idx) {{
  _currentIdx = idx;
  moveCursor(idx, _totalCycles);
  document.getElementById('scrubber-label').textContent = 'cycle ' + (idx+1);
  loadCycle(idx);
}}

function loadCycle(index) {{
  document.getElementById('th-loading').style.display = 'inline';
  fetch('/api/theater/cycle/' + index, {{credentials:'include',headers:{{'Authorization':_authHeader()}}}})
    .then(function(r){{ return r.json(); }})
    .then(function(data) {{
      _currentCycle = data;
      renderCycle(data, false);
      document.getElementById('th-loading').style.display = 'none';
    }})
    .catch(function() {{ document.getElementById('th-loading').style.display = 'none'; }});
}}

function renderCycle(data, isLive) {{
  var stages = data.stages || {{}};

  // Update pipeline nodes
  var stageIds = ['regime','signals','scratchpad','gate','sonnet','kernel','execution','a2'];
  stageIds.forEach(function(id) {{
    var el = document.getElementById('pipe-' + id);
    if (!el) return;
    var st = stages[id] || {{}};
    var status = st.status || 'warn';
    el.className = 'pipe-node' +
      (status==='ok' ? ' complete' : status==='skip' ? ' skipped' : '') +
      (_selectedStage===id ? ' active' : '');
    var metric = el.querySelector('.pipe-metric');
    if (metric) metric.textContent = st.summary || '';
  }});

  // Ideas
  var ideas = (stages.sonnet || {{}}).ideas || [];
  document.getElementById('ideas-panel').innerHTML = renderIdeas(ideas);

  // Reasoning stream
  var full = (stages.sonnet || {{}}).reasoning_full || (stages.sonnet || {{}}).reasoning_excerpt || '';
  if (isLive) {{
    typeReasoning(full);
  }} else {{
    clearTyping();
    document.getElementById('reasoning-text').textContent = full;
    var panel = document.getElementById('reasoning-panel');
    panel.scrollTop = 0;
  }}

  // Cycle meta
  var num = (data.cycle_number || 0) + 1;
  var total = data.total_cycles || 0;
  var session2 = data.session || '';
  var msTag = (total > 0 && total % 500 === 0)
    ? ' <span style="font-family:var(--bbb-font-mono);font-size:9px;letter-spacing:.05em;text-transform:uppercase;padding:2px 6px;border-radius:2px;background:rgba(52,211,153,.12);color:#34D399;margin-left:6px">Cycle #' + total + ' · milestone</span>'
    : '';
  document.getElementById('th-cycle-meta').innerHTML = session2 + ' · cycle ' + num + '/' + total + msTag;
  document.getElementById('scrubber-label').textContent = 'cycle ' + num;

  // Re-render stage detail if one is selected
  if (_selectedStage && stages[_selectedStage]) {{
    renderStageDetail(stages[_selectedStage], _selectedStage);
  }}
}}

// ── Typewriter ────────────────────────────────────────────────────────────────
function clearTyping() {{
  if (_typingTimer) {{ clearInterval(_typingTimer); _typingTimer = null; }}
}}

function typeReasoning(text) {{
  clearTyping();
  var el = document.getElementById('reasoning-text');
  el.textContent = '';
  var i = 0;
  var panel = document.getElementById('reasoning-panel');
  _typingTimer = setInterval(function() {{
    if (i >= text.length) {{ clearTyping(); return; }}
    el.textContent += text.slice(i, i+2);
    i += 2;
    // auto-scroll if not manually scrolled away from bottom
    if (panel.scrollHeight - panel.scrollTop - panel.clientHeight < 60) {{
      panel.scrollTop = panel.scrollHeight;
    }}
  }}, 30);
}}

// ── Stage detail ──────────────────────────────────────────────────────────────
function selectStage(name) {{
  document.querySelectorAll('.pipe-node').forEach(function(n) {{ n.classList.remove('active'); }});
  var el = document.getElementById('pipe-' + name);
  if (el) el.classList.add('active');
  _selectedStage = name;
  renderStageDetail(_currentCycle.stages[name] || {{}}, name);
}}

function renderStageDetail(st, name) {{
  var skip = new Set(['ideas','conviction_ranking','submitted','rejections','top_3',
                      'watching','blocking','actions','reasoning_full','reasoning_excerpt']);
  var html = '<div style="font-family:var(--bbb-font-sans);font-size:var(--bbb-t-label);letter-spacing:.07em;text-transform:uppercase;color:var(--bbb-fg-muted);margin-bottom:6px">' + name + '</div>';
  html += '<div style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg)">';
  for (var k in st) {{
    if (skip.has(k)) continue;
    var v = st[k];
    if (v === null || v === undefined) continue;
    if (typeof v === 'object') v = JSON.stringify(v).substring(0,80);
    html += '<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--bbb-border)">';
    html += '<span style="color:var(--bbb-fg-muted)">' + k + '</span>';
    html += '<span>' + String(v).substring(0,60) + '</span></div>';
  }}
  html += '</div>';
  if (st.rejections && st.rejections.length) {{
    html += '<div style="margin-top:6px;font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-warn)">Rejections:</div>';
    st.rejections.forEach(function(r) {{
      html += '<div style="font-family:var(--bbb-font-mono);font-size:10px;padding:2px 0;color:var(--bbb-fg-muted)">' + r.symbol + ': ' + r.reason + '</div>';
    }});
  }}
  document.getElementById('stage-detail-panel').innerHTML = html;
}}

// ── Play / speed ──────────────────────────────────────────────────────────────
function togglePlay() {{
  var btn = document.getElementById('btn-play');
  if (_playTimer) {{
    clearInterval(_playTimer);
    _playTimer = null;
    btn.textContent = '▶ Play';
  }} else {{
    btn.textContent = '⏸ Pause';
    _playTimer = setInterval(function() {{
      var next = _currentIdx + 1;
      if (next >= _totalCycles) {{ togglePlay(); return; }}
      selectCycle(next);
    }}, Math.max(400, 1200 / _playSpeed));
  }}
}}

function cycleSpeed() {{
  _playSpeed = _playSpeed === 1 ? 2 : (_playSpeed === 2 ? 4 : 1);
  document.getElementById('btn-speed').textContent = _playSpeed + '×';
  if (_playTimer) {{ togglePlay(); togglePlay(); }} // restart at new speed
}}

function jumpNow() {{
  if (_totalCycles > 0) selectCycle(_totalCycles - 1);
}}

// ── Stage replay ──────────────────────────────────────────────────────────────
var _replayTimer     = null;
var _replaySpeed     = 1;
var _replayStageIdx  = 0;
var _replayPaused    = false;
var _replayRunning   = false;
var _REPLAY_STAGES   = ['regime','signals','scratchpad','gate','sonnet','kernel','execution','a2'];
var _REPLAY_DELAY_MS = 1500;

function _replaySetStates(activeIdx) {{
  _REPLAY_STAGES.forEach(function(id, i) {{
    var el = document.getElementById('pipe-' + id);
    if (!el) return;
    el.classList.remove('replay-active', 'replay-done', 'replay-future');
    if (i < activeIdx)       el.classList.add('replay-done');
    else if (i === activeIdx) el.classList.add('replay-active');
    else                      el.classList.add('replay-future');
  }});
}}

function _replayClearStates() {{
  _REPLAY_STAGES.forEach(function(id) {{
    var el = document.getElementById('pipe-' + id);
    if (el) el.classList.remove('replay-active', 'replay-done', 'replay-future');
  }});
}}

function _replaySetBtn(label, fn) {{
  var btn = document.getElementById('btn-replay');
  if (!btn) return;
  btn.textContent = label;
  btn.onclick = fn;
}}

function _replayShowOutcome(outcome) {{
  var banner = document.getElementById('replay-outcome-banner');
  var text   = document.getElementById('replay-outcome-text');
  var icon   = document.getElementById('replay-outcome-icon');
  if (!banner || !text) return;
  var outcomeUpper = (outcome || 'hold').toUpperCase();
  var colorMap = {{
    FILLED: 'var(--bbb-profit)', BUY: 'var(--bbb-profit)', ADD: 'var(--bbb-profit)',
    SELL: 'var(--bbb-loss)', EXIT: 'var(--bbb-loss)', LOSS: 'var(--bbb-loss)',
    HOLD: 'var(--bbb-fg-muted)', SKIP: 'var(--bbb-fg-muted)', SKIPPED: 'var(--bbb-fg-muted)',
  }};
  var iconMap = {{
    FILLED: '✓', BUY: '↑', ADD: '↑',
    SELL: '↓', EXIT: '↓',
    HOLD: '—', SKIP: '—', SKIPPED: '—',
  }};
  var color = colorMap[outcomeUpper] || 'var(--bbb-fg-muted)';
  var iconChar = iconMap[outcomeUpper] || '·';
  text.textContent  = outcomeUpper;
  text.style.color  = color;
  if (icon) {{ icon.textContent = iconChar; icon.style.color = color; }}
  banner.style.background = outcomeUpper === 'FILLED' || outcomeUpper === 'BUY' || outcomeUpper === 'ADD'
    ? 'rgba(52,211,153,.08)' : (outcomeUpper === 'SELL' || outcomeUpper === 'EXIT' || outcomeUpper === 'LOSS'
    ? 'rgba(248,113,113,.08)' : 'rgba(123,128,144,.08)');
  banner.style.border = '1px solid ' + color;
  banner.style.display = 'flex';
}}

function _replayHideOutcome() {{
  var banner = document.getElementById('replay-outcome-banner');
  if (banner) banner.style.display = 'none';
}}

function _replayStep() {{
  if (!_replayRunning || _replayPaused) return;
  if (_replayStageIdx >= _REPLAY_STAGES.length) {{
    // All stages done — show outcome
    _replayClearStates();
    // Mark all done
    _REPLAY_STAGES.forEach(function(id) {{
      var el = document.getElementById('pipe-' + id);
      if (el) el.classList.add('replay-done');
    }});
    var outcome = (_currentCycle || {{}}).outcome || 'hold';
    _replayShowOutcome(outcome);
    _replayRunning = false;
    _replaySetBtn('► Replay last cycle', function() {{ replayStart(); }});
    return;
  }}
  var stageId = _REPLAY_STAGES[_replayStageIdx];
  _replaySetStates(_replayStageIdx);
  // Update the detail panel
  selectStage(stageId);
  // Advance after delay
  _replayTimer = setTimeout(function() {{
    _replayStageIdx++;
    _replayStep();
  }}, _REPLAY_DELAY_MS / _replaySpeed);
}}

function _replayBegin() {{
  _replayRunning  = true;
  _replayPaused   = false;
  _replayStageIdx = 0;
  _replayHideOutcome();
  _replaySetBtn('⏸ Pause', function() {{ replayPause(); }});
  _replayStep();
}}

function replayStart() {{
  // Stop any in-progress replay
  if (_replayTimer) {{ clearTimeout(_replayTimer); _replayTimer = null; }}
  _replayRunning = false;
  _replayClearStates();
  // Jump to last cycle, then fetch + animate
  if (_totalCycles > 0) {{
    var lastIdx = _totalCycles - 1;
    _currentIdx = lastIdx;
    moveCursor(lastIdx, _totalCycles);
    document.getElementById('scrubber-label').textContent = 'cycle ' + (lastIdx + 1);
    document.getElementById('th-loading').style.display = 'inline';
    _replaySetBtn('⏳ Loading…', null);
    fetch('/api/theater/cycle/' + lastIdx, {{credentials:'include',headers:{{'Authorization':_authHeader()}}}})
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        _currentCycle = data;
        renderCycle(data, false);
        document.getElementById('th-loading').style.display = 'none';
        _replayBegin();
      }})
      .catch(function() {{
        document.getElementById('th-loading').style.display = 'none';
        _replaySetBtn('► Replay last cycle', function() {{ replayStart(); }});
      }});
  }}
}}

function replayPause() {{
  if (_replayTimer) {{ clearTimeout(_replayTimer); _replayTimer = null; }}
  _replayPaused = true;
  _replaySetBtn('► Resume', function() {{ replayResume(); }});
}}

function replayResume() {{
  _replayPaused = false;
  _replaySetBtn('⏸ Pause', function() {{ replayPause(); }});
  _replayStep();
}}

function replaySpeedChange() {{
  _replaySpeed = _replaySpeed === 1 ? 2 : (_replaySpeed === 2 ? 4 : 1);
  document.getElementById('btn-replay-speed').textContent = _replaySpeed + '\xd7';
}}

// ── Ideas table ───────────────────────────────────────────────────────────────
function renderIdeas(ideas) {{
  if (!ideas || !ideas.length) {{
    return '<p style="padding:12px;font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted)">No ideas this cycle.</p>';
  }}
  var html = '<table class="data-table"><thead><tr><th>Symbol</th><th>Intent</th><th>Tier</th><th>Catalyst</th></tr></thead><tbody>';
  ideas.forEach(function(idea) {{
    var ic = (idea.intent==='buy'||idea.intent==='add') ? 'var(--bbb-profit)' :
             (idea.intent==='sell'||idea.intent==='exit') ? 'var(--bbb-loss)' : 'var(--bbb-fg-muted)';
    html += '<tr><td style="font-family:var(--bbb-font-mono);font-weight:500">' + (idea.symbol||'') + '</td>';
    html += '<td><span style="color:'+ic+';font-family:var(--bbb-font-mono);font-size:10px">' + (idea.intent||'—').toUpperCase() + '</span></td>';
    html += '<td>' + (idea.tier||'') + '</td>';
    html += '<td style="color:var(--bbb-fg-muted)">' + (idea.catalyst||'').substring(0,60) + '</td></tr>';
  }});
  return html + '</tbody></table>';
}}

// ── Trade lifecycle ───────────────────────────────────────────────────────────
function loadTrade(symbol, entryDate) {{
  document.querySelectorAll('.trade-pill').forEach(function(p){{ p.classList.remove('selected'); }});
  document.getElementById('th-loading').style.display = 'inline';
  fetch('/api/theater/trade/' + symbol + '?entry_date=' + entryDate,
        {{credentials:'include',headers:{{'Authorization':_authHeader()}}}})
    .then(function(r){{ return r.json(); }})
    .then(function(data) {{
      renderTradeLifecycle(data);
      document.getElementById('th-loading').style.display = 'none';
    }})
    .catch(function() {{ document.getElementById('th-loading').style.display = 'none'; }});
}}

function renderTradeLifecycle(data) {{
  if (!data || data.status === 'not_found') {{
    document.getElementById('trade-hero').innerHTML =
      '<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3);padding:24px;color:var(--bbb-fg-muted);font-family:var(--bbb-font-mono);font-size:11px;text-align:center">Trade not found.</div>';
    return;
  }}
  var pnl = data.pnl_usd || 0;
  var pnlPct = data.pnl_pct || 0;
  var pnlSign = pnl >= 0 ? '+' : '';
  var pnlCls  = pnl >= 0 ? 'bbb-pos' : 'bbb-neg';
  var statusLabel = data.status === 'open' ? 'OPEN' : (pnl >= 0 ? 'WIN' : 'LOSS');

  var hero = '<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3);padding:var(--bbb-s-4) var(--bbb-s-5)">';
  hero += '<div style="display:flex;justify-content:space-between;align-items:flex-start">';
  hero += '<div><span class="bbb-hero-num-sm" style="font-family:var(--bbb-font-mono)">' + data.symbol + '</span>';
  hero += '<div style="margin-top:4px;font-family:var(--bbb-font-mono);font-size:var(--bbb-t-label);letter-spacing:.06em;text-transform:uppercase;color:var(--bbb-fg-muted)">' + statusLabel + '</div></div>';
  hero += '<div style="text-align:right"><div class="bbb-hero-num-sm ' + pnlCls + '">' + pnlSign + '$' + Math.abs(pnl).toFixed(2) + '</div>';
  hero += '<div class="bbb-hero-meta ' + pnlCls + '">' + pnlSign + pnlPct.toFixed(2) + '% · ' + (data.pnl_status||'') + '</div></div></div>';
  hero += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:var(--bbb-s-3);margin-top:var(--bbb-s-3)">';
  [['Entry', data.entry_price],['Current',data.current_price],['Stop',data.stop_price],
   ['Exit', data.exit_price || (data.status==='open' ? null : null)]].forEach(function(m) {{
    var val = m[1] ? '$'+m[1].toFixed(2) : (m[0]==='Exit'&&data.status==='open'?'Open':'—');
    hero += '<div><div class="bbb-hero-label">' + m[0] + '</div>';
    hero += '<div class="bbb-hero-meta bbb-fg">' + val + '</div></div>';
  }});
  hero += '</div>';
  if (data.catalyst_at_entry) {{
    hero += '<div style="margin-top:var(--bbb-s-3);font-family:var(--bbb-font-mono);font-size:var(--bbb-t-reasoning);color:var(--bbb-fg-muted);line-height:var(--bbb-lh-normal)">' + data.catalyst_at_entry + '</div>';
  }}
  hero += '</div>';
  document.getElementById('trade-hero').innerHTML = hero;

  renderPriceJourney(data.price_journey);
  renderTimeline(data.lifecycle_events || []);
  renderThesis(data);
}}

function renderPriceJourney(pj) {{
  var wrap = document.getElementById('price-journey-wrap');
  if (!pj || !pj.entry) {{ wrap.style.display='none'; return; }}
  wrap.style.display = '';
  var bar = document.getElementById('price-journey-bar');
  var labels = document.getElementById('price-journey-labels');
  var html = '';
  if (pj.stop_pct!==null && pj.entry_pct!==null) {{
    var w=Math.max(0,pj.entry_pct-pj.stop_pct);
    html+='<div style="position:absolute;left:'+pj.stop_pct+'%;width:'+w+'%;height:100%;background:rgba(248,113,113,.12);border-radius:3px 0 0 3px"></div>';
  }}
  if (pj.entry_pct!==null && pj.current_pct!==null) {{
    var lo2=Math.min(pj.entry_pct,pj.current_pct);
    var w2=Math.abs(pj.current_pct-pj.entry_pct);
    var gc=pj.current_pct>=pj.entry_pct?'rgba(52,211,153,.18)':'rgba(248,113,113,.18)';
    html+='<div style="position:absolute;left:'+lo2+'%;width:'+w2+'%;height:100%;background:'+gc+'"></div>';
  }}
  [[pj.stop_pct,'#F87171'],[pj.entry_pct,'#60A5FA'],[pj.current_pct,'#34D399'],[pj.target_pct,'#9B5DE5']].forEach(function(v) {{
    if (v[0]===null||v[0]===undefined) return;
    html+='<div style="position:absolute;left:'+v[0]+'%;top:0;bottom:0;width:2px;background:'+v[1]+';border-radius:1px"></div>';
  }});
  bar.innerHTML=html;
  var labHtml='';
  [[pj.stop,'Stop'],[pj.entry,'Entry'],[pj.current,pj.target?'Now':'Exit'],[pj.target,'Target']].forEach(function(p) {{
    if (!p[0]) return;
    labHtml+='<span>'+p[1]+' $'+p[0].toFixed(2)+'</span>';
  }});
  labels.innerHTML=labHtml;
}}

function renderTimeline(events) {{
  var el = document.getElementById('lifecycle-timeline');
  if (!events.length) {{
    el.innerHTML='<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted)">No events.</span>';
    return;
  }}
  var dotColors={{entry:'var(--bbb-info)',exit:'var(--bbb-loss)',hold:'var(--bbb-profit)',open:'var(--bbb-profit)',trail_advance:'var(--bbb-warn)'}};
  var html='';
  events.forEach(function(ev) {{
    var dc=dotColors[ev.event_type]||'var(--bbb-fg-dim)';
    var ts=(ev.timestamp||'').substring(0,16).replace('T',' ');
    html+='<div class="tl-event">';
    html+='<span class="tl-dot" style="background:'+dc+'"></span>';
    html+='<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg)">'+(ev.label||'')+'</span>';
    if (ts) html+=' <span style="color:var(--bbb-fg-muted);font-family:var(--bbb-font-mono);font-size:10px">'+ts+'</span>';
    if (ev.detail) html+='<div style="color:var(--bbb-fg-muted);font-family:var(--bbb-font-mono);font-size:10px;margin-top:2px;padding-left:14px">'+ev.detail.substring(0,100)+'</div>';
    html+='</div>';
  }});
  el.innerHTML=html;
}}

function renderThesis(data) {{
  var el = document.getElementById('trade-thesis');
  var html = '';
  if (data.entry_reasoning) {{
    html += '<div class="bbb-hero-label" style="margin-bottom:4px">Entry Reasoning</div>';
    html += '<p style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted);line-height:var(--bbb-lh-normal);margin-bottom:10px">' + data.entry_reasoning.substring(0,400) + '</p>';
  }}
  if (data.exit_scenarios) {{
    html += '<div class="bbb-hero-label" style="margin-bottom:4px">Exit Scenarios</div>';
    for (var k in data.exit_scenarios) {{
      var c=k==='beat'?'var(--bbb-profit)':(k.includes('miss')||k==='stop_hit'?'var(--bbb-loss)':'var(--bbb-fg-muted)');
      html+='<div style="font-family:var(--bbb-font-mono);font-size:10px;padding:3px 0;color:'+c+'">'+data.exit_scenarios[k]+'</div>';
    }}
  }}
  if (data.exit_reason) {{
    html+='<div style="margin-top:8px;font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-fg-muted)">exit reason: <span style="color:var(--bbb-warn)">'+data.exit_reason+'</span></div>';
  }}
  if (data.bug_flag) {{
    html+='<div style="margin-top:8px;padding:6px 8px;background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.3);border-radius:var(--bbb-r-2);font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-warn)">⚠ Bug period: '+(data.bug_flag.title||data.bug_flag.id||'')+'</div>';
  }}
  if (!html) html='<span style="font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-muted)">No thesis data.</span>';
  el.innerHTML=html;
}}

// ── Cycle wheel ───────────────────────────────────────────────────────────────
var _allCycles = [];
var _WCX = 140, _WCY = 140, _WR = 82;
var _ET_OFFSET_H = -4;  // EDT; -5 in winter

function _cycleAngle(ts) {{
  if (!ts) return null;
  var d = new Date(ts);
  if (isNaN(d.getTime())) return null;
  var et_h = ((d.getUTCHours() + _ET_OFFSET_H) % 24 + 24) % 24;
  var et_m = d.getUTCMinutes();
  return ((et_h + et_m / 60) / 24) * 2 * Math.PI - Math.PI / 2;
}}

function _dotColor(outcome) {{
  var map = {{filled:'#34D399',skipped:'#4A4F60',pending:'#FBBF24',rejected:'#F87171',hold:'#4A4F60'}};
  return map[outcome] || '#4A4F60';
}}

function buildCycleWheel(cycles) {{
  _allCycles = cycles;
  var dotsG = document.getElementById('wheel-dots');
  if (!dotsG) return;
  var ns = 'http://www.w3.org/2000/svg';
  var frag = document.createElementNS(ns, 'g');
  cycles.forEach(function(c, i) {{
    var angle = _cycleAngle(c.ts);
    if (angle === null) return;
    var x = _WCX + _WR * Math.cos(angle);
    var y = _WCY + _WR * Math.sin(angle);
    var dot = document.createElementNS(ns, 'circle');
    dot.setAttribute('cx', x.toFixed(1));
    dot.setAttribute('cy', y.toFixed(1));
    dot.setAttribute('r', '3');
    dot.setAttribute('fill', _dotColor(c.outcome));
    dot.setAttribute('opacity', c.outcome === 'skipped' || c.outcome === 'hold' ? '0.35' : '0.75');
    dot.style.cursor = 'pointer';
    (function(idx) {{ dot.addEventListener('click', function() {{ selectCycle(idx); }}); }})(i);
    frag.appendChild(dot);
  }});
  dotsG.innerHTML = '';
  dotsG.appendChild(frag);

  updateWheelSelected(_currentIdx);
  buildWheelLegend(cycles);
  tickWheelHand();
}}

function updateWheelSelected(idx) {{
  var ring = document.getElementById('wheel-sel-ring');
  var dot  = document.getElementById('wheel-sel-dot');
  var lbl  = document.getElementById('wheel-sel-label');
  if (!ring || !dot) return;
  var c = _allCycles[idx];
  if (!c) {{ ring.setAttribute('r','0'); dot.setAttribute('r','0'); return; }}
  var angle = _cycleAngle(c.ts);
  if (angle === null) {{ ring.setAttribute('r','0'); dot.setAttribute('r','0'); return; }}
  var x = (_WCX + _WR * Math.cos(angle)).toFixed(1);
  var y = (_WCY + _WR * Math.sin(angle)).toFixed(1);
  ring.setAttribute('cx', x); ring.setAttribute('cy', y); ring.setAttribute('r', '7');
  dot.setAttribute('cx', x);  dot.setAttribute('cy', y);  dot.setAttribute('r', '4');
  if (lbl) {{
    var d = new Date(c.ts);
    var et_h = ((d.getUTCHours() + _ET_OFFSET_H) % 24 + 24) % 24;
    var et_m = d.getUTCMinutes();
    var pad = function(n) {{ return n < 10 ? '0'+n : ''+n; }};
    var session = et_h >= 9 && et_h < 16 ? 'market' : et_h >= 4 && et_h < 9 ? 'pre-mkt' : 'after-hrs';
    lbl.textContent = 'cycle ' + (idx+1) + ' · ' + pad(et_h) + ':' + pad(et_m) + ' ET · ' + session;
  }}
}}

function buildWheelLegend(cycles) {{
  var counts = {{filled:0,hold:0,skipped:0}};
  cycles.forEach(function(c) {{
    if (counts[c.outcome] !== undefined) counts[c.outcome]++;
    else counts.hold++;
  }});
  var colors = {{filled:'#34D399',hold:'#4A4F60',skipped:'#4A4F60'}};
  var labels = {{filled:'filled',hold:'hold',skipped:'skipped'}};
  var html = '';
  ['filled','hold','skipped'].forEach(function(k) {{
    html += '<span style="font-family:var(--bbb-font-mono);font-size:10px;color:var(--bbb-fg-muted);display:flex;align-items:center;gap:3px">';
    html += '<svg width="8" height="8"><circle cx="4" cy="4" r="3" fill="'+colors[k]+'"/></svg>';
    html += counts[k] + ' ' + labels[k] + '</span>';
  }});
  var el = document.getElementById('wheel-legend');
  if (el) el.innerHTML = html;
}}

// ── Calibration Plot ──────────────────────────────────────────────────────────
(function() {{
  var _W=560, _H=180, _PL=48, _PR=24, _PT=16, _PB=36;
  var _pw=_W-_PL-_PR, _ph=_H-_PT-_PB;
  var _xMin=0.30, _xMax=1.00, _yMin=-20, _yMax=20;
  function _px(c) {{ return _PL + (c-_xMin)/(_xMax-_xMin)*_pw; }}
  function _py(r) {{ return _PT + _ph - (r-_yMin)/(_yMax-_yMin)*_ph; }}

  window.buildCalibrationPlot = function(data) {{
    var g    = document.getElementById('calib-dots');
    var brier = document.getElementById('calib-brier');
    var empty = document.getElementById('calib-empty');
    if (!g || !empty) return;
    var pts = (data && data.points) || [];
    var n   = (data && data.n) || 0;
    empty.style.display = n ? 'none' : '';
    if (brier) {{
      brier.textContent = (data && data.brier_score != null)
        ? ('Brier ' + data.brier_score.toFixed(3)) : '';
    }}
    var svg = '';
    pts.forEach(function(p) {{
      var cx = _px(Math.max(_xMin, Math.min(_xMax, p.conviction_x)));
      var cy = _py(Math.max(_yMin, Math.min(_yMax, p.pnl_pct)));
      var col = p.pnl_pct >= 0 ? 'var(--bbb-profit)' : 'var(--bbb-loss)';
      svg += '<circle cx="'+cx.toFixed(1)+'" cy="'+cy.toFixed(1)+'" r="5"'
           + ' fill="'+col+'" fill-opacity="0.7" stroke="var(--bbb-surface)" stroke-width="1.5"/>';
    }});
    g.innerHTML = svg;
  }};
}})();

function tickWheelHand() {{
  var hand = document.getElementById('wheel-hand');
  if (!hand) return;
  var now = new Date();
  var et_h = ((now.getUTCHours() + _ET_OFFSET_H) % 24 + 24) % 24;
  var et_m = now.getUTCMinutes();
  var angle = ((et_h + et_m / 60) / 24) * 2 * Math.PI - Math.PI / 2;
  var R_hand = 108;
  hand.setAttribute('x2', (_WCX + R_hand * Math.cos(angle)).toFixed(1));
  hand.setAttribute('y2', (_WCY + R_hand * Math.sin(angle)).toFixed(1));
  setTimeout(tickWheelHand, 60000);
}}

// Override renderCycle to also update wheel
var _origRenderCycle = renderCycle;
renderCycle = function(data, isLive) {{
  _origRenderCycle(data, isLive);
  updateWheelSelected(data.cycle_number || 0);
}};

// ── Init ──────────────────────────────────────────────────────────────────────
(function() {{
  setMode('cycle');

  // Render initial reasoning instantly (no typewriter on first load)
  var initReasoning = (_cycleData.stages && _cycleData.stages.sonnet && (_cycleData.stages.sonnet.reasoning_full || _cycleData.stages.sonnet.reasoning_excerpt)) || '';
  document.getElementById('reasoning-text').textContent = initReasoning;

  // Pre-render ideas
  var ideas = (_cycleData.stages && _cycleData.stages.sonnet && _cycleData.stages.sonnet.ideas) || [];
  document.getElementById('ideas-panel').innerHTML = renderIdeas(ideas);

  // Fetch all cycles metadata (scrubber ticks + wheel dots)
  fetch('/api/theater/cycles', {{credentials:'include',headers:{{'Authorization':_authHeader()}}}})
    .then(function(r){{ return r.json(); }})
    .then(function(cycles) {{
      // Real-colored scrubber ticks (outcome-coded)
      buildScrubber(cycles);
      // Cycle wheel
      buildCycleWheel(cycles);
    }})
    .catch(function() {{
      // Fallback: plain stubs
      var stubs = [];
      for (var i=0; i<_totalCycles; i++) stubs.push({{outcome:'hold',ts:''}});
      if (_totalCycles > 0) stubs[_currentIdx] = {{outcome:_cycleData.outcome||'hold',ts:_cycleData.timestamp||''}};
      buildScrubber(stubs);
    }});

  // Calibration plot
  fetch('/api/theater/calibration', {{credentials:'include',headers:{{'Authorization':_authHeader()}}}})
    .then(function(r){{ return r.json(); }})
    .then(function(data) {{ buildCalibrationPlot(data); }})
    .catch(function() {{ buildCalibrationPlot({{points:[],n:0,brier_score:null}}); }});

  // Start wheel hand at current ET time
  tickWheelHand();

  // Auto-select most recent open trade
  var trades = (_tradesData && _tradesData.trades) || [];
  var t = trades.find(function(x){{ return x.status==='open'; }});
  if (!t && trades.length) t = trades[0];
  if (t) loadTrade(t.symbol, t.entry_date || '');
}})();
</script>
"""
    _th_a1 = _alpaca_a1()
    _th_positions = [
        {"symbol": getattr(p, "symbol", ""), "current": float(getattr(p, "current_price", 0) or 0),
         "unreal_plpc": float(getattr(p, "unrealized_plpc", 0) or 0) * 100}
        for p in _th_a1.get("positions", [])
    ]
    return _page_shell("Decision Theater", nav, body, _build_ticker_html(_th_positions))


def _theater_stage_metric(stage_id: str, st: dict) -> str:
    """One-line metric for a stage node."""
    if stage_id == "regime":
        r = st.get("regime", "")
        s = st.get("score")
        return f"{r} {s}" if s else (r or "—")
    if stage_id == "signals":
        n = st.get("symbols_scored", 0)
        return f"{n} scored" if n else "—"
    if stage_id == "scratchpad":
        w = len(st.get("watching", []))
        return f"{w} watching" if w else "—"
    if stage_id == "gate":
        return "SKIP" if st.get("mode") == "SKIP" else (st.get("mode") or "—")
    if stage_id == "sonnet":
        n = st.get("ideas_generated", 0)
        c = st.get("cost_usd")
        if c:
            return f"{n} ideas ${c:.3f}"
        return f"{n} ideas" if n else "—"
    if stage_id == "kernel":
        a = st.get("approved", 0)
        r = st.get("rejected", 0)
        return f"{a}✓ {r}✗" if (a or r) else "—"
    if stage_id == "execution":
        n = st.get("orders_submitted", 0)
        return f"{n} orders" if n else "—"
    if stage_id == "a2":
        return st.get("regime", st.get("reason", "—"))[:12]
    return "—"


def _theater_ideas_html(ideas: list) -> str:
    if not ideas:
        return '<p style="padding:12px;font-size:11px;color:var(--text-muted)">No ideas this cycle.</p>'
    rows = ""
    for idea in ideas:
        intent = (idea.get("intent") or "hold").lower()
        ic = ("var(--accent-green)" if intent in ("buy", "add") else
              "var(--accent-red)" if intent in ("sell", "exit") else
              "var(--text-muted)")
        rows += (
            f'<tr><td style="font-weight:600">{idea.get("symbol","")}</td>'
            f'<td><span style="color:{ic};font-size:10px;font-weight:700">{intent.upper()}</span></td>'
            f'<td>{idea.get("tier","")}</td>'
            f'<td style="color:var(--text-secondary)">{(idea.get("catalyst","") or "")[:60]}</td></tr>'
        )
    return (
        '<table class="data-table"><thead><tr>'
        '<th>Symbol</th><th>Intent</th><th>Tier</th><th>Catalyst</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
    )


@app.route("/theater")
def page_theater():
    return _page_theater(_now_et())


@app.route("/api/theater/cycle/<cycle_index>")
def api_theater_cycle(cycle_index: str):
    try:
        import sys as _sys
        _sys.path.insert(0, str(BOT_DIR))
        from decision_theater import get_cycle_view
        return jsonify(get_cycle_view(int(cycle_index)))
    except Exception as _exc:
        return jsonify({"error": str(_exc)}), 500


@app.route("/api/theater/trade/<symbol>")
def api_theater_trade(symbol: str):
    try:
        import sys as _sys
        _sys.path.insert(0, str(BOT_DIR))
        from decision_theater import get_trade_lifecycle
        entry_date = request.args.get("entry_date")
        return jsonify(get_trade_lifecycle(symbol, entry_date))
    except Exception as _exc:
        return jsonify({"error": str(_exc)}), 500


@app.route("/api/theater/trades")
def api_theater_trades():
    try:
        import sys as _sys
        _sys.path.insert(0, str(BOT_DIR))
        from decision_theater import get_all_trades_summary
        return jsonify(get_all_trades_summary())
    except Exception as _exc:
        return jsonify({"error": str(_exc)}), 500


@app.route("/api/theater/cycles")
def api_theater_cycles():
    try:
        import sys as _sys
        _sys.path.insert(0, str(BOT_DIR))
        from decision_theater import get_all_cycles_metadata
        return jsonify(get_all_cycles_metadata())
    except Exception as _exc:
        return jsonify({"error": str(_exc)}), 500


@app.route("/api/theater/calibration")
def api_theater_calibration():
    try:
        import sys as _sys
        _sys.path.insert(0, str(BOT_DIR))
        from decision_theater import get_calibration_data
        return jsonify(get_calibration_data())
    except Exception as _exc:
        return jsonify({"error": str(_exc)}), 500


# ── Social page (public showcase) ─────────────────────────────────────────────

def _social_post_feed_html() -> str:
    S_MONO = "font-family:var(--bbb-font-mono)"
    S_SANS = "font-family:var(--bbb-font-sans)"

    # Type → (label, border-color)
    _CAT = {
        "trade_entry":      ("TRADE UPDATE",    "var(--bbb-info)"),
        "trade_exit":       ("TRADE UPDATE",    "var(--bbb-info)"),
        "flat_day":         ("MARKET VIEW",     "var(--bbb-fg-dim)"),
        "skip":             ("MARKET VIEW",     "var(--bbb-fg-dim)"),
        "premarket_brief":  ("MARKET VIEW",     "var(--bbb-fg-dim)"),
        "weekly_recap":     ("PERFORMANCE",     "var(--bbb-profit)"),
        "monthly_milestone":("PERFORMANCE",     "var(--bbb-profit)"),
        "lookback":         ("SELF-REFLECTION", "var(--bbb-warn)"),
    }

    def _status_chip(tweet_id: str) -> str:
        if tweet_id == "DRY_RUN":
            c, bg, label = "var(--bbb-fg-muted)", "rgba(123,128,144,.12)", "DRAFTED"
        elif str(tweet_id).startswith("APPROVAL"):
            c, bg, label = "var(--bbb-info)",     "rgba(96,165,250,.12)", "POSTED"
        elif str(tweet_id).startswith("HELD"):
            c, bg, label = "var(--bbb-warn)",     "rgba(251,191,36,.12)", "HELD"
        elif str(tweet_id).startswith("APPROVED"):
            c, bg, label = "var(--bbb-profit)",   "rgba(52,211,153,.12)", "APPROVED"
        else:
            c, bg, label = "var(--bbb-fg-muted)", "transparent",          "DRAFTED"
        return (
            f'<span style="background:{bg};border:1px solid {c};color:{c};'
            f'{S_MONO};font-size:10px;font-weight:500;letter-spacing:.06em;'
            f'text-transform:uppercase;padding:2px 6px;border-radius:var(--bbb-r-1);'
            f'flex:none">{label}</span>'
        )

    def _cat_chip(post_type: str) -> str:
        label, color = _CAT.get(post_type, ("POST", "var(--bbb-fg-dim)"))
        return (
            f'<span style="color:{color};{S_MONO};font-size:10px;font-weight:500;'
            f'letter-spacing:.06em;text-transform:uppercase;flex:none">{label}</span>'
        )

    def _fmt_ts(ts_str: str) -> str:
        try:
            from datetime import datetime as _dt
            dt = _dt.fromisoformat(ts_str.replace("Z", "+00:00"))
            return dt.strftime("%-m/%-d %I:%M %p UTC").replace(" 0", " ").strip()
        except Exception:
            return ts_str[:16]

    def _post_card(post: dict, is_placeholder: bool = False) -> str:
        ts_str   = post.get("ts", "")
        content  = post.get("content", "").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        tweet_id = post.get("tweet_id", "")
        ptype    = post.get("type", "")
        _, border_c = _CAT.get(ptype, ("POST", "var(--bbb-border)"))
        placeholder_note = (
            f'<div style="{S_MONO};font-size:10px;color:var(--bbb-fg-dim);'
            f'margin-top:8px;font-style:italic">example — no posts yet</div>'
        ) if is_placeholder else ""
        return (
            f'<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
            f'border-left:2px solid {border_c};border-radius:var(--bbb-r-3);'
            f'padding:14px 16px 12px;margin-bottom:8px">'
            # header row: avatar + handle + timestamp
            f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">'
            f'<div style="width:34px;height:34px;border-radius:50%;background:var(--bbb-surface-2);'
            f'border:1px solid var(--bbb-border);display:flex;align-items:center;'
            f'justify-content:center;flex:none">'
            f'<span style="{S_MONO};font-size:10px;font-weight:500;color:var(--bbb-ai)">BBB</span>'
            f'</div>'
            f'<div style="flex:1;min-width:0">'
            f'<div style="{S_SANS};font-size:13px;font-weight:500;color:var(--bbb-fg)">BullBearBot</div>'
            f'<div style="{S_MONO};font-size:11px;color:var(--bbb-fg-dim)">@BullBearBot</div>'
            f'</div>'
            f'<div style="{S_MONO};font-size:11px;color:var(--bbb-fg-dim);flex:none">{_fmt_ts(ts_str)}</div>'
            f'</div>'
            # post content
            f'<div style="{S_MONO};font-size:13px;color:var(--bbb-fg);line-height:1.6;'
            f'word-break:break-word;margin-bottom:10px">{content}</div>'
            # footer row: category + status chip
            f'<div style="display:flex;align-items:center;gap:10px">'
            f'{_cat_chip(ptype)}'
            f'<div style="flex:1"></div>'
            f'{_status_chip(tweet_id)}'
            f'</div>'
            f'{placeholder_note}'
            f'</div>'
        )

    # Load posts
    try:
        p = BOT_DIR / "data" / "social" / "post_history.json"
        if p.exists():
            with open(p) as f:
                raw = json.load(f)
            posts = raw.get("posts", []) if isinstance(raw, dict) else raw
        else:
            posts = []
    except Exception:
        posts = []

    # Sort newest-first
    posts = sorted(posts, key=lambda x: x.get("ts", ""), reverse=True)

    if posts:
        total = len(posts)
        shown = posts[:50]  # show up to 50 in the scrollable feed
        cards_html = "".join(_post_card(p) for p in shown)
        header_note = (
            f'<span style="{S_MONO};font-size:11px;color:var(--bbb-fg-dim)">'
            f'{total} posts &nbsp;·&nbsp; showing {len(shown)} most recent'
            f'</span>'
        )
    else:
        # Placeholder cards
        _placeholders = [
            {
                "ts": "2026-05-02T14:30:00+00:00",
                "type": "trade_entry",
                "tweet_id": "DRY_RUN",
                "content": "BUY $NVDA 12 shares. Insider bought $4.2M 3 days ago. Regime is risk_on(71). Signal score 84. Bull agent is practically levitating. Risk Manager approved with one eyebrow raised.\n\nPaper trading. Not financial advice.",
            },
            {
                "ts": "2026-05-02T09:15:00+00:00",
                "type": "skip",
                "tweet_id": "DRY_RUN",
                "content": "87 signals scanned. Nothing cleared the bar. VIX 22.4. Bull wanted $GOOGL on IV crush thesis — Bear pointed out earnings in 4 days. Gate skipped. Inaction is a position.\n\nPaper trading. Not financial advice.",
            },
            {
                "ts": "2026-05-01T21:00:00+00:00",
                "type": "weekly_recap",
                "tweet_id": "DRY_RUN",
                "content": "Week 3: 3 trades, 2W/1L, +$812 net. Bear finally conceded that AAPL thesis had merit. Risk Manager printed a checklist. Strategy Director wrote 1,400 words about it. Bull: \"I told you.\"\n\nPaper trading. Not financial advice.",
            },
        ]
        cards_html = "".join(_post_card(p, is_placeholder=True) for p in _placeholders)
        header_note = (
            f'<span style="{S_MONO};font-size:11px;color:var(--bbb-fg-dim)">no posts on file — showing examples</span>'
        )

    return (
        f'<div style="margin-bottom:24px">'
        f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">'
        f'<div style="{S_MONO};font-size:10px;letter-spacing:.08em;text-transform:uppercase;'
        f'color:var(--bbb-fg-muted)">Post feed</div>'
        f'{header_note}'
        f'</div>'
        f'<div style="max-height:820px;overflow-y:auto;padding-right:4px">'
        f'{cards_html}'
        f'</div>'
        f'</div>'
    )


def _social_perf_pane(label: str, primary: str, secondary: str, primary_cls: str, last: bool = False) -> str:
    border = "" if last else "border-right:1px solid var(--bbb-border);"
    sec_div = (
        f'<div style="font-family:var(--bbb-font-mono);font-size:11px;'
        f'color:var(--bbb-fg-muted);margin-top:6px">{secondary}</div>'
        if secondary else ""
    )
    return (
        f'<div style="text-align:center;flex:1;padding:24px 16px;{border}">'
        f'<div style="font-family:var(--bbb-font-mono);font-size:10px;letter-spacing:0.08em;'
        f'text-transform:uppercase;color:var(--bbb-fg-muted);margin-bottom:8px">{label}</div>'
        f'<div style="font-family:var(--bbb-font-mono);font-size:28px;font-weight:500;'
        f'line-height:1;letter-spacing:-0.02em" class="{primary_cls}">{primary}</div>'
        f'{sec_div}'
        f'</div>'
    )


def _social_bot_card(name: str, accent: str, title: str, bullets: list) -> str:
    bullet_html = "".join(
        f'<div style="font-family:var(--bbb-font-mono);font-size:12px;'
        f'color:var(--bbb-fg-muted);padding:3px 0">&rsaquo; {b}</div>'
        for b in bullets
    )
    return (
        f'<div style="flex:1;background:var(--bbb-surface);border:1px solid var(--bbb-border);'
        f'border-top:2px solid {accent};border-radius:var(--bbb-r-3);padding:20px 24px">'
        f'<div style="font-family:var(--bbb-font-mono);font-size:10px;letter-spacing:0.08em;'
        f'text-transform:uppercase;color:{accent};margin-bottom:10px">{name}</div>'
        f'<div style="font-family:var(--bbb-font-sans);font-size:14px;font-weight:500;'
        f'color:var(--bbb-fg);margin-bottom:12px">{title}</div>'
        f'{bullet_html}'
        f'</div>'
    )


def _page_social(status: dict, now_et: str) -> str:
    nav = _nav_html("social", now_et)

    # ── Data assembly ──────────────────────────────────────────────────────────
    a1_acc = status["a1"].get("account")
    a2_acc = status["a2"].get("account")
    a1_equity = float(a1_acc.equity or 0) if a1_acc else 0.0
    a2_equity = float(a2_acc.equity or 0) if a2_acc else 0.0
    combined_equity = a1_equity + a2_equity
    cum_dollars = combined_equity - _INITIAL_CAPITAL
    cum_pct = cum_dollars / _INITIAL_CAPITAL * 100 if _INITIAL_CAPITAL else 0.0
    cum_sign = _bbb_sign(cum_dollars)
    cum_cls = _bbb_pnl_color(cum_dollars)
    days_running = (date.today() - _LAUNCH_DATE).days

    result = _closed_trades()
    trades, _ = result if isinstance(result, tuple) else (result, [])
    n_trades = len(trades)
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    win_rate = (wins / n_trades * 100) if n_trades else 0.0
    wr_cls = "bbb-pos" if win_rate >= 55 else ("bbb-muted" if win_rate >= 45 else "bbb-neg")
    recent_trades = trades[:5]

    # ── HERO ──────────────────────────────────────────────────────────────────
    hero_html = (
        f'<div style="text-align:center;padding:48px 0 32px;'
        f'border-bottom:1px solid var(--bbb-border);margin-bottom:0">'
        f'<div style="font-family:var(--bbb-font-mono);font-size:36px;font-weight:500;'
        f'color:var(--bbb-fg);letter-spacing:-0.03em;margin-bottom:12px">BullBearBot</div>'
        f'<div style="font-family:var(--bbb-font-mono);font-size:13px;color:var(--bbb-fg-muted)">'
        f'Autonomous AI trading &middot; two strategies &middot; paper since April&nbsp;13&nbsp;2026'
        f'</div>'
        f'</div>'
    )

    # ── PERFORMANCE STRIP ─────────────────────────────────────────────────────
    perf_html = (
        f'<div style="display:flex;background:var(--bbb-surface);border:1px solid var(--bbb-border);'
        f'border-radius:var(--bbb-r-3);overflow:hidden;margin:24px 0">'
        + _social_perf_pane("Combined return", f"{cum_sign}{_fm(cum_dollars)}",
                             f"{cum_sign}{cum_pct:.2f}% of $200k", cum_cls)
        + _social_perf_pane("Days running", str(days_running), f"since {_LAUNCH_DATE}", "bbb-fg")
        + _social_perf_pane("Win rate", f"{win_rate:.0f}%",
                             f"{wins}W&nbsp;/&nbsp;{n_trades - wins}L", wr_cls)
        + _social_perf_pane("Closed trades", str(n_trades), "", "bbb-fg", last=True)
        + f'</div>'
    )

    # ── TWO-BOT EXPLAINER ─────────────────────────────────────────────────────
    bots_html = (
        f'<div style="display:flex;gap:16px;margin:0 0 24px">'
        + _social_bot_card("A1", "var(--bbb-info)", "Equity strategy", [
            "Single Claude Sonnet decision loop",
            "7-stage pipeline: regime &rsaquo; signals &rsaquo; scratchpad &rsaquo; gate &rsaquo; Sonnet &rsaquo; kernel &rsaquo; exec",
            "Trail-stop position management with dynamic tier promotion",
            "Regime + signal anchors (L2 Python + L3 Haiku synthesis)",
        ])
        + _social_bot_card("A2", "var(--bbb-ai)", "Options strategy", [
            "4-agent AI debate engine (single Sonnet call, 4 parsed roles)",
            "IV-first structure selection &mdash; rank + percentile drives strategy",
            "DIRECTIONAL ADVOCATE &middot; VOL ANALYST &middot; TAPE SKEPTIC &middot; RISK OFFICER",
            "Multi-leg execution + post-execution Alpaca state verification",
        ])
        + f'</div>'
    )

    # ── RECENT DECISIONS ──────────────────────────────────────────────────────
    if recent_trades:
        rows_html = ""
        for t in recent_trades:
            sym = t.get("symbol", "")
            pnl_pct = t.get("pnl_pct", 0.0) or 0.0
            outcome = t.get("outcome", "flat")
            pnl_cls = "bbb-pos" if outcome == "win" else ("bbb-neg" if outcome == "loss" else "bbb-muted")
            sign = "+" if pnl_pct >= 0 else ""
            catalyst = (t.get("catalyst") or "No thesis recorded")[:90]
            direction = "LONG" if (t.get("qty") or 0) >= 0 else "SHORT"
            exit_t = t.get("exit_time", "")
            date_str = exit_t[:10] if exit_t else "—"
            rows_html += (
                f'<div style="background:var(--bbb-surface);border:1px solid var(--bbb-border);'
                f'border-radius:var(--bbb-r-3);padding:12px 16px;display:flex;'
                f'align-items:center;gap:16px">'
                f'<div style="font-family:var(--bbb-font-mono);font-size:15px;font-weight:500;'
                f'color:var(--bbb-fg);min-width:60px">{sym}</div>'
                f'<div style="font-family:var(--bbb-font-mono);font-size:10px;letter-spacing:0.06em;'
                f'color:var(--bbb-fg-dim);min-width:36px">{direction}</div>'
                f'<div style="font-family:var(--bbb-font-mono);font-size:14px;font-weight:500;'
                f'min-width:52px" class="{pnl_cls}">{sign}{pnl_pct:.1f}%</div>'
                f'<div style="font-family:var(--bbb-font-mono);font-size:12px;'
                f'color:var(--bbb-fg-muted);flex:1;overflow:hidden;text-overflow:ellipsis;'
                f'white-space:nowrap">{catalyst}</div>'
                f'<div style="font-family:var(--bbb-font-mono);font-size:10px;'
                f'color:var(--bbb-fg-dim);white-space:nowrap;flex:none">{date_str}</div>'
                f'</div>'
            )
        recent_html = (
            f'<div style="margin-bottom:24px">'
            f'<div style="font-family:var(--bbb-font-mono);font-size:10px;letter-spacing:0.08em;'
            f'text-transform:uppercase;color:var(--bbb-fg-muted);margin-bottom:12px">'
            f'Recent decisions</div>'
            f'<div style="display:flex;flex-direction:column;gap:6px">{rows_html}</div>'
            f'</div>'
        )
    else:
        recent_html = (
            f'<div style="margin-bottom:24px;padding:20px;background:var(--bbb-surface);'
            f'border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3);'
            f'font-family:var(--bbb-font-mono);font-size:13px;'
            f'color:var(--bbb-fg-muted);text-align:center">No closed trades yet.</div>'
        )

    # ── HOW IT WORKS ──────────────────────────────────────────────────────────
    _STAGE_DESCS = [
        ("Regime",     "Haiku classifies market regime: risk_on · risk_off · caution"),
        ("Signals",    "Python anchors + Haiku synthesis score each symbol 0–100"),
        ("Scratchpad", "Pre-analysis pass — pattern recognition before commitment"),
        ("Gate",       "State-change gate — Sonnet only fires when conditions change"),
        ("Sonnet",     "Claude Sonnet makes the final trade decision with full context"),
        ("Kernel",     "Risk sizing, stop placement, eligibility rules enforced"),
        ("Exec",       "Order submission + post-execution Alpaca state verification"),
        ("A2",         "Parallel options debate: 4 agents argue IV, direction, risk"),
    ]
    pills = ""
    for i, (name, _) in enumerate(_STAGE_DESCS):
        connector = (
            f'<div style="font-family:var(--bbb-font-mono);font-size:10px;'
            f'color:var(--bbb-fg-dim);padding:0 2px">&rsaquo;</div>'
            if i < len(_STAGE_DESCS) - 1 else ""
        )
        pills += (
            f'<div style="font-family:var(--bbb-font-mono);font-size:11px;'
            f'letter-spacing:0.06em;text-transform:uppercase;color:var(--bbb-fg-muted);'
            f'background:var(--bbb-surface);border:1px solid var(--bbb-border);'
            f'border-radius:var(--bbb-r-2);padding:4px 10px;white-space:nowrap">{name}</div>'
            + connector
        )
    desc_grid = "".join(
        f'<div style="font-family:var(--bbb-font-mono);font-size:11px;'
        f'color:var(--bbb-fg-dim);padding:3px 0">'
        f'<span style="color:var(--bbb-fg-muted)">{name}</span> — {desc}</div>'
        for name, desc in _STAGE_DESCS
    )
    pipeline_html = (
        f'<div style="margin-bottom:24px">'
        f'<div style="font-family:var(--bbb-font-mono);font-size:10px;letter-spacing:0.08em;'
        f'text-transform:uppercase;color:var(--bbb-fg-muted);margin-bottom:12px">'
        f'How it works — decision pipeline</div>'
        f'<div style="display:flex;flex-wrap:wrap;align-items:center;gap:4px;margin-bottom:16px">'
        f'{pills}</div>'
        f'<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:4px 24px">'
        f'{desc_grid}</div>'
        f'</div>'
    )

    # ── FOOTER ────────────────────────────────────────────────────────────────
    footer_html = (
        f'<div style="border-top:1px solid var(--bbb-border);padding:20px 0;text-align:center;'
        f'font-family:var(--bbb-font-mono);font-size:11px;color:var(--bbb-fg-dim)">'
        f'Paper trading &middot; not financial advice &middot; dashboard at 161.35.120.8'
        f'</div>'
    )

    feed_html = _social_post_feed_html()

    body = (
        f'<div style="max-width:860px;margin:0 auto;padding:0 24px 40px">'
        + hero_html
        + perf_html
        + feed_html
        + bots_html
        + recent_html
        + pipeline_html
        + footer_html
        + f'</div>'
    )
    _soc_positions = [
        {"symbol": getattr(p, "symbol", ""), "current": float(getattr(p, "current_price", 0) or 0),
         "unreal_plpc": float(getattr(p, "unrealized_plpc", 0) or 0) * 100}
        for p in status["a1"].get("positions", [])
    ]
    return _page_shell("BullBearBot", nav, body, _build_ticker_html(_soc_positions))


@app.route("/social")
def page_social():
    now_et = _now_et()
    status = _build_status()
    return _page_social(status, now_et)


# ── Strategy Room ─────────────────────────────────────────────────────────────

def _board_parse_weekly_review() -> list[dict]:
    """Parse latest weekly_review_*.md into list of {agent, title, body} dicts."""
    try:
        import glob as _glob
        files = sorted(_glob.glob(str(BOT_DIR / "data" / "reports" / "weekly_review_*.md")))
        if not files:
            return []
        with open(files[-1]) as f:
            raw = f.read()
        sections = []
        parts = raw.split("\n## Agent ")
        for part in parts[1:]:
            lines = part.strip().splitlines()
            header = lines[0] if lines else ""
            colon = header.find(":")
            if colon == -1:
                num, title = header.strip(), ""
            else:
                num = header[:colon].strip()
                title = header[colon + 1:].strip()
            body = "\n".join(lines[1:]).strip()
            sections.append({"agent": num, "title": title, "body": body})
        return sections
    except Exception:
        return []


def _md_to_html(text: str) -> str:
    """Render markdown to HTML. Falls back to minimal escaping if library unavailable."""
    try:
        import markdown as _mdlib
        return _mdlib.markdown(text, extensions=["tables", "fenced_code"])
    except Exception:
        import re as _re
        t = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        t = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', t)
        t = _re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', t, flags=_re.MULTILINE)
        t = t.replace("\n\n", "<br><br>").replace("\n", "<br>")
        return t


def _board_director_section(status: dict) -> str:
    S_CARD = "background:var(--bbb-surface);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3);padding:var(--bbb-s-4) var(--bbb-s-5);margin-bottom:var(--bbb-s-4)"
    S_LBL  = "font-family:var(--bbb-font-sans);font-size:var(--bbb-t-label);font-weight:var(--bbb-w-medium);letter-spacing:.08em;text-transform:uppercase;color:var(--bbb-fg-muted);margin-bottom:var(--bbb-s-3)"
    S_MONO = "font-family:var(--bbb-font-mono)"
    try:
        p = BOT_DIR / "data" / "reports" / "director_memo_history.json"
        if not p.exists():
            return f'<div style="{S_CARD}"><div style="{S_LBL}">Strategy Director</div><span style="{S_MONO};font-size:13px;color:var(--bbb-fg-muted)">No memo on file.</span></div>'
        with open(p) as f:
            memos = json.load(f)
        if not memos:
            return ""
        import re as _re
        m = memos[-1]
        week = m.get("week") or m.get("week_of") or m.get("period") or "unknown week"
        issued = m.get("issued_at", "")[:10]
        expiry = m.get("expiry", "")[:10]
        summary = m.get("memo_summary", "")

        # Render memo using markdown library
        memo_html = _md_to_html(summary)

        # Recommendation tracker table
        recs = m.get("key_recommendations", [])
        rec_rows = ""
        verdict_colors = {
            "pending":  ("var(--bbb-warn)", "rgba(251,191,36,.12)"),
            "resolved": ("var(--bbb-profit)", "rgba(52,211,153,.12)"),
            "neutral":  ("var(--bbb-fg-muted)", "rgba(123,128,144,.12)"),
            "failed":   ("var(--bbb-loss)", "rgba(248,113,113,.12)"),
        }
        for r in recs:
            vid     = r.get("rec_id", "")[-8:] or "—"
            # Strip markdown bold/backticks from table cells (they're small, no full render)
            rtext   = _re.sub(r'[*`]', '', r.get("recommendation", "")[:200])
            outcome = _re.sub(r'[*`]', '', (r.get("outcome") or "—")[:140])
            verdict = (r.get("verdict") or "pending").lower()
            vc, vbg = verdict_colors.get(verdict, ("var(--bbb-fg-muted)", "transparent"))
            rec_rows += (
                f'<tr>'
                f'<td style="{S_MONO};font-size:11px;color:var(--bbb-fg-dim);white-space:nowrap;padding:8px 10px 8px 0;vertical-align:top">{vid}</td>'
                f'<td style="{S_MONO};font-size:12px;color:var(--bbb-fg);padding:8px 10px;vertical-align:top;line-height:1.5">{rtext}</td>'
                f'<td style="{S_MONO};font-size:12px;color:var(--bbb-fg-muted);padding:8px 10px;vertical-align:top;line-height:1.5">{outcome}</td>'
                f'<td style="padding:8px 0;vertical-align:top;text-align:right;white-space:nowrap">'
                f'<span style="background:{vbg};border:1px solid {vc};color:{vc};{S_MONO};font-size:10px;font-weight:500;letter-spacing:.06em;text-transform:uppercase;padding:2px 6px;border-radius:var(--bbb-r-1)">{verdict}</span>'
                f'</td>'
                f'</tr>'
            )

        table_html = (
            f'<div style="{S_LBL};margin-top:var(--bbb-s-4)">Recommendation Tracker</div>'
            f'<table style="width:100%;border-collapse:collapse">'
            f'<thead><tr>'
            f'<th style="{S_MONO};font-size:10px;color:var(--bbb-fg-dim);text-align:left;padding-bottom:6px;letter-spacing:.06em;text-transform:uppercase">ID</th>'
            f'<th style="{S_MONO};font-size:10px;color:var(--bbb-fg-dim);text-align:left;padding:0 10px 6px;letter-spacing:.06em;text-transform:uppercase">Recommendation</th>'
            f'<th style="{S_MONO};font-size:10px;color:var(--bbb-fg-dim);text-align:left;padding:0 10px 6px;letter-spacing:.06em;text-transform:uppercase">Outcome</th>'
            f'<th style="{S_MONO};font-size:10px;color:var(--bbb-fg-dim);text-align:right;padding-bottom:6px;letter-spacing:.06em;text-transform:uppercase">Verdict</th>'
            f'</tr></thead>'
            f'<tbody style="border-top:1px solid var(--bbb-border)">{rec_rows}</tbody>'
            f'</table>'
        ) if recs else ""

        meta = f'Week {week}'
        if issued:
            meta += f' &nbsp;·&nbsp; issued {issued}'
        if expiry:
            meta += f' &nbsp;·&nbsp; expires {expiry}'

        return (
            f'<div style="{S_CARD}">'
            f'<div style="display:flex;align-items:baseline;gap:var(--bbb-s-3);margin-bottom:var(--bbb-s-3)">'
            f'<div style="{S_LBL};margin-bottom:0">Strategy Director Memo</div>'
            f'<span style="{S_MONO};font-size:11px;color:var(--bbb-fg-dim)">{meta}</span>'
            f'</div>'
            f'<div class="bbb-md" style="max-height:380px;overflow-y:auto;padding-right:4px">{memo_html}</div>'
            f'{table_html}'
            f'</div>'
        )
    except Exception as exc:
        return f'<div style="{S_CARD}"><span style="color:var(--bbb-loss);font-size:13px">[director memo error: {exc}]</span></div>'


def _board_meeting_section() -> str:
    S_CARD = "background:var(--bbb-surface);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3);padding:var(--bbb-s-4) var(--bbb-s-5);margin-bottom:var(--bbb-s-4)"
    S_LBL  = "font-family:var(--bbb-font-sans);font-size:var(--bbb-t-label);font-weight:var(--bbb-w-medium);letter-spacing:.08em;text-transform:uppercase;color:var(--bbb-fg-muted);margin-bottom:var(--bbb-s-3)"
    S_MONO = "font-family:var(--bbb-font-mono)"
    agents = _board_parse_weekly_review()
    if not agents:
        return f'<div style="{S_CARD}"><div style="{S_LBL}">Weekly Board Meeting</div><span style="{S_MONO};font-size:13px;color:var(--bbb-fg-muted)">No review on file.</span></div>'

    # Per-agent accent colors (left-border, cycled)
    agent_colors = [
        "#60A5FA", "#34D399", "#FBBF24", "#F87171",
        "#9B5DE5", "#60A5FA", "#34D399", "#FBBF24",
        "#F87171", "#9B5DE5", "#60A5FA", "#34D399",
    ]

    import glob as _glob
    files = sorted(_glob.glob(str(BOT_DIR / "data" / "reports" / "weekly_review_*.md")))
    file_date = files[-1].split("weekly_review_")[-1].replace(".md", "") if files else "unknown"

    cards = ""
    for i, ag in enumerate(agents):
        color = agent_colors[i % len(agent_colors)]
        num   = ag["agent"]
        title = ag["title"]
        body  = ag["body"]
        body_html = _md_to_html(body)
        label = f'Agent {num}: {title}' if title else f'Agent {num}'
        cards += (
            f'<div style="background:var(--bbb-surface-2);border:1px solid var(--bbb-border);'
            f'border-left:2px solid {color};border-radius:var(--bbb-r-2);'
            f'padding:12px var(--bbb-s-4);margin-bottom:8px">'
            f'<div style="{S_MONO};font-size:12px;font-weight:500;color:var(--bbb-fg);margin-bottom:8px">{label}</div>'
            f'<div class="bbb-md" style="max-height:320px;overflow-y:auto;padding-right:4px">{body_html}</div>'
            f'</div>'
        )

    return (
        f'<div style="{S_CARD}">'
        f'<div style="display:flex;align-items:baseline;gap:var(--bbb-s-3);margin-bottom:var(--bbb-s-3)">'
        f'<div style="{S_LBL};margin-bottom:0">Weekly Board Meeting</div>'
        f'<span style="{S_MONO};font-size:11px;color:var(--bbb-fg-dim)">Week of {file_date} &nbsp;·&nbsp; {len(agents)} agents</span>'
        f'</div>'
        f'{cards}'
        f'</div>'
    )


def _board_readiness_section() -> str:
    S_CARD = "background:var(--bbb-surface);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3);padding:var(--bbb-s-4) var(--bbb-s-5);margin-bottom:var(--bbb-s-4)"
    S_LBL  = "font-family:var(--bbb-font-sans);font-size:var(--bbb-t-label);font-weight:var(--bbb-w-medium);letter-spacing:.08em;text-transform:uppercase;color:var(--bbb-fg-muted);margin-bottom:var(--bbb-s-3)"
    S_MONO = "font-family:var(--bbb-font-mono)"
    try:
        p = BOT_DIR / "data" / "reports" / "readiness_status_latest.json"
        if not p.exists():
            return f'<div style="{S_CARD}"><div style="{S_LBL}">CTO Readiness</div><span style="{S_MONO};font-size:13px;color:var(--bbb-fg-muted)">No readiness file.</span></div>'
        with open(p) as f:
            rd = json.load(f)

        overall   = rd.get("overall_status", "unknown").upper()
        a1_ready  = rd.get("a1_live_ready", False)
        passed    = rd.get("gates_passed", 0)
        total     = rd.get("gates_total", 18)
        sev1_days = rd.get("sev1_clean_days", 0)
        failures  = rd.get("failures", [])
        gen_at    = rd.get("generated_at", "")[:19].replace("T", " ")

        status_c = "var(--bbb-profit)" if overall == "READY" else "var(--bbb-loss)"
        live_c   = "var(--bbb-profit)" if a1_ready else "var(--bbb-warn)"

        # Full gate list (static — from validate_config.py)
        gate_list = [
            ("01", "schemas.py present and compiles cleanly"),
            ("02", "risk_kernel.py present"),
            ("03", "sonnet_gate.py present and compiles cleanly"),
            ("04", "reconciliation.py present"),
            ("05", "divergence.py importable"),
            ("06", "attribution.py importable"),
            ("07a", "signal_backtest.py importable"),
            ("07b", "backtest_latest.json present"),
            ("08", "git remote configured"),
            ("09", f"Sev-1 clean days ≥ 7 (current: {sev1_days}d)"),
            ("10", "attribution_log.jsonl present"),
            ("11", "near_miss_log.jsonl present"),
            ("12", "shadow_lane.py present"),
            ("13", "strategy_config.json has shadow_lane section"),
            ("14", "A2 IV history seeded (≥ 20 entries per symbol)"),
            ("15", "data/analytics/ directory present"),
            ("16", "strategy_config.json version=2"),
            ("17", "T-005 regime label normalizer present"),
            ("18", "options universe ≥ 1 tradeable symbol"),
        ]

        # Determine which gates failed
        failed_strs = " ".join(failures).lower()
        gate_rows = ""
        for gid, gdesc in gate_list:
            key = f"gate {gid}"
            is_fail = any(key in f.lower() for f in failures)
            icon  = "✗" if is_fail else "✓"
            ic    = "var(--bbb-loss)" if is_fail else "var(--bbb-profit)"
            dc    = "var(--bbb-loss)" if is_fail else "var(--bbb-fg-muted)"
            gate_rows += (
                f'<div style="display:flex;align-items:baseline;gap:8px;padding:5px 0;'
                f'border-bottom:1px solid var(--bbb-border)">'
                f'<span style="{S_MONO};font-size:13px;color:{ic};flex:none;width:14px">{icon}</span>'
                f'<span style="{S_MONO};font-size:10px;color:var(--bbb-fg-dim);flex:none;width:26px">{gid}</span>'
                f'<span style="{S_MONO};font-size:12px;color:{dc}">{gdesc}</span>'
                f'</div>'
            )

        return (
            f'<div style="{S_CARD}">'
            f'<div style="display:flex;align-items:center;gap:var(--bbb-s-3);margin-bottom:var(--bbb-s-4)">'
            f'<div style="{S_LBL};margin-bottom:0">CTO Readiness</div>'
            f'<span style="background:rgba(52,211,153,.10);border:1px solid {status_c};color:{status_c};'
            f'{S_MONO};font-size:10px;font-weight:500;letter-spacing:.06em;text-transform:uppercase;'
            f'padding:2px 8px;border-radius:var(--bbb-r-1)">{overall}</span>'
            f'<span style="{S_MONO};font-size:11px;color:var(--bbb-fg-dim);margin-left:auto">'
            f'{passed}/{total} gates &nbsp;·&nbsp; {gen_at}'
            f'</span>'
            f'</div>'
            f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:0 var(--bbb-s-5)">'
            f'{gate_rows}'
            f'</div>'
            f'</div>'
        )
    except Exception as exc:
        return f'<div style="{S_CARD}"><span style="color:var(--bbb-loss);font-size:13px">[readiness error: {exc}]</span></div>'


def _board_annex_section() -> str:
    S_CARD = "background:var(--bbb-surface);border:1px solid var(--bbb-border);border-radius:var(--bbb-r-3);padding:var(--bbb-s-4) var(--bbb-s-5);margin-bottom:var(--bbb-s-4)"
    S_LBL  = "font-family:var(--bbb-font-sans);font-size:var(--bbb-t-label);font-weight:var(--bbb-w-medium);letter-spacing:.08em;text-transform:uppercase;color:var(--bbb-fg-muted);margin-bottom:var(--bbb-s-3)"
    S_MONO = "font-family:var(--bbb-font-mono)"

    # Opus eval experiment
    eval_dir = BOT_DIR / "experiments" / "opus_eval"
    manifest  = eval_dir / "manifest.json"
    sealed    = eval_dir / "SEALED"
    outputs   = eval_dir / "outputs"
    scoring   = eval_dir / "scoring_sheet_filled.csv"
    pre_reg   = eval_dir / "PRE_REGISTRATION.md"

    pre_reg_date = "2026-04-16"
    if manifest.exists():
        stage = "RUNNING / COMPLETE"
        stage_c = "var(--bbb-warn)"
    elif outputs.exists() and any(True for _ in outputs.iterdir()):
        stage = "OUTPUTS COLLECTED"
        stage_c = "var(--bbb-warn)"
    else:
        stage = "PRE-REGISTERED · NOT YET RUN"
        stage_c = "var(--bbb-fg-dim)"

    if sealed.exists() and scoring.exists():
        stage = "REVEALED · SCORED"
        stage_c = "var(--bbb-profit)"

    # Load pre-registration core question
    core_q = "Does Opus 4.7 surface information that changes what I would do next, on the specific artifact types BullBearBot produces?"
    decision_criteria = [
        ("A", "Opus wins on synthesis + forensics + calibration — promote to T1.8"),
        ("B", "Opus wins on forensics only — promote to T1.8 on forensic artifact classes only"),
        ("C", "Tie or marginal Opus win — defer T1.8, re-run in 4 weeks"),
        ("D", "Sonnet wins on ≥ 2 of 3 ground-truth classes — do not promote"),
        ("E", "Ambiguous by tie-break rules — defer T1.8, re-run in 4 weeks"),
    ]

    artifact_rows = ""
    artifact_classes = [
        ("Forensic (closed A1 trades)", "2", "YES", "fo_01_btc_win, fo_02_eth_loss"),
        ("Weekly review synthesis", "2", "PARTIAL", "wr_01_*, wr_02_*"),
        ("Director recs (resolved)", "1–4", "YES", "dr_01_*, dr_02_* (resolved only)"),
        ("A2 observation-mode debate", "2", "NO", "a2_01_*, a2_02_*"),
    ]
    for cls, cnt, gt, ids in artifact_classes:
        gt_c = "var(--bbb-profit)" if gt == "YES" else ("var(--bbb-warn)" if gt == "PARTIAL" else "var(--bbb-fg-dim)")
        artifact_rows += (
            f'<tr>'
            f'<td style="{S_MONO};font-size:12px;color:var(--bbb-fg);padding:6px 10px 6px 0;vertical-align:top">{cls}</td>'
            f'<td style="{S_MONO};font-size:12px;color:var(--bbb-fg-muted);padding:6px 10px;text-align:center;vertical-align:top">{cnt}</td>'
            f'<td style="{S_MONO};font-size:12px;color:{gt_c};padding:6px 10px;text-align:center;vertical-align:top">{gt}</td>'
            f'<td style="{S_MONO};font-size:11px;color:var(--bbb-fg-dim);padding:6px 0;vertical-align:top">{ids}</td>'
            f'</tr>'
        )

    criteria_rows = ""
    for letter, desc in decision_criteria:
        criteria_rows += (
            f'<div style="display:flex;gap:10px;padding:5px 0;border-bottom:1px solid var(--bbb-border)">'
            f'<span style="{S_MONO};font-size:12px;font-weight:500;color:var(--bbb-ai);flex:none;width:14px">{letter}</span>'
            f'<span style="{S_MONO};font-size:12px;color:var(--bbb-fg-muted)">{desc}</span>'
            f'</div>'
        )

    return (
        f'<div style="{S_CARD};border-left:2px solid var(--bbb-ai)">'
        f'<div style="display:flex;align-items:center;gap:var(--bbb-s-3);margin-bottom:var(--bbb-s-4)">'
        f'<div style="{S_LBL};margin-bottom:0">Mad Scientist Annex</div>'
        f'</div>'

        f'<div style="background:var(--bbb-surface-2);border:1px solid var(--bbb-border);'
        f'border-radius:var(--bbb-r-2);padding:var(--bbb-s-4);margin-bottom:var(--bbb-s-4)">'
        f'<div style="display:flex;align-items:center;gap:var(--bbb-s-3);margin-bottom:var(--bbb-s-3)">'
        f'<span style="{S_MONO};font-size:13px;font-weight:500;color:var(--bbb-fg)">Opus 4.7 vs Sonnet 4.6 — Blind Evaluation</span>'
        f'<span style="background:var(--bbb-ai-soft);border:1px solid var(--bbb-ai-border);color:{stage_c};'
        f'{S_MONO};font-size:10px;font-weight:500;letter-spacing:.06em;text-transform:uppercase;'
        f'padding:2px 7px;border-radius:var(--bbb-r-1)">{stage}</span>'
        f'</div>'
        f'<div style="{S_MONO};font-size:12px;color:var(--bbb-fg-muted);margin-bottom:var(--bbb-s-3)">'
        f'Pre-registered {pre_reg_date} &nbsp;·&nbsp; locked rubric &nbsp;·&nbsp; blind scoring before reveal'
        f'</div>'
        f'<div style="{S_MONO};font-size:13px;color:var(--bbb-fg);font-style:italic;margin-bottom:var(--bbb-s-4);'
        f'border-left:2px solid var(--bbb-border);padding-left:12px;line-height:1.55">'
        f'&ldquo;{core_q}&rdquo;'
        f'</div>'

        f'<div style="{S_LBL};font-size:10px;margin-bottom:6px">Locked Artifact Set</div>'
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:var(--bbb-s-4)">'
        f'<thead><tr>'
        f'<th style="{S_MONO};font-size:10px;color:var(--bbb-fg-dim);text-align:left;padding-bottom:4px;letter-spacing:.06em;text-transform:uppercase">Class</th>'
        f'<th style="{S_MONO};font-size:10px;color:var(--bbb-fg-dim);text-align:center;padding:0 10px 4px;letter-spacing:.06em;text-transform:uppercase">N</th>'
        f'<th style="{S_MONO};font-size:10px;color:var(--bbb-fg-dim);text-align:center;padding:0 10px 4px;letter-spacing:.06em;text-transform:uppercase">Ground Truth</th>'
        f'<th style="{S_MONO};font-size:10px;color:var(--bbb-fg-dim);text-align:left;padding-bottom:4px;letter-spacing:.06em;text-transform:uppercase">Artifact IDs</th>'
        f'</tr></thead>'
        f'<tbody style="border-top:1px solid var(--bbb-border)">{artifact_rows}</tbody>'
        f'</table>'

        f'<div style="{S_LBL};font-size:10px;margin-bottom:6px">Pre-Registered Decision Criteria</div>'
        f'{criteria_rows}'
        f'</div>'

        f'</div>'
    )


def _page_board(status: dict, now_et: str) -> str:
    a1_mode = (status.get("a1_mode") or {}).get("mode", "NORMAL").upper()
    a2_mode = (status.get("a2_mode") or {}).get("mode", "NORMAL").upper()
    nav = _nav_html("board", now_et, a1_mode, a2_mode)

    director_html   = _board_director_section(status)
    meeting_html    = _board_meeting_section()
    readiness_html  = _board_readiness_section()
    annex_html      = _board_annex_section()

    _md_css = """
<style>
.bbb-md { font-family: var(--bbb-font-mono); font-size: 13px; color: var(--bbb-fg-muted); line-height: 1.6; }
.bbb-md h1, .bbb-md h2 { font-size: 14px; font-weight: 500; color: var(--bbb-fg); margin: 14px 0 6px; font-family: var(--bbb-font-sans); }
.bbb-md h3, .bbb-md h4 { font-size: 12px; font-weight: 500; color: var(--bbb-fg-muted); text-transform: uppercase; letter-spacing: .06em; margin: 10px 0 4px; font-family: var(--bbb-font-sans); }
.bbb-md p { margin: 0 0 8px; }
.bbb-md strong { color: var(--bbb-fg); font-weight: 500; }
.bbb-md em { color: var(--bbb-fg-muted); font-style: italic; }
.bbb-md hr { border: none; border-top: 1px solid var(--bbb-border); margin: 10px 0; }
.bbb-md ul, .bbb-md ol { padding-left: 18px; margin: 4px 0 8px; }
.bbb-md li { margin-bottom: 3px; }
.bbb-md code { background: var(--bbb-surface-2); border: 1px solid var(--bbb-border); border-radius: 2px; padding: 0 4px; font-size: 12px; color: var(--bbb-fg); }
.bbb-md pre { background: var(--bbb-surface-2); border: 1px solid var(--bbb-border); border-radius: var(--bbb-r-2); padding: 10px 12px; overflow-x: auto; margin: 8px 0; }
.bbb-md pre code { background: none; border: none; padding: 0; }
.bbb-md table { width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 12px; }
.bbb-md th { text-align: left; color: var(--bbb-fg-muted); padding: 4px 8px; border-bottom: 1px solid var(--bbb-border); }
.bbb-md td { padding: 4px 8px; border-bottom: 1px solid var(--bbb-border); color: var(--bbb-fg-muted); }
</style>
"""
    body = (
        _md_css
        + f'<div style="max-width:1200px;margin:0 auto;padding:var(--bbb-s-5)">'
        f'<div style="font-family:var(--bbb-font-sans);font-size:var(--bbb-t-h2);font-weight:var(--bbb-w-medium);'
        f'color:var(--bbb-fg);margin-bottom:4px">Strategy Room</div>'
        f'<div style="font-family:var(--bbb-font-mono);font-size:var(--bbb-t-caption);color:var(--bbb-fg-dim);'
        f'margin-bottom:var(--bbb-s-5)">{now_et}</div>'
        f'{director_html}'
        f'{meeting_html}'
        f'{readiness_html}'
        f'{annex_html}'
        f'</div>'
    )
    return _page_shell("Strategy Room · BullBearBot", nav, body, "")


@app.route("/board")
def page_board():
    now_et = _now_et()
    status = _build_status()
    return _page_board(status, now_et)


# ── Command palette search ────────────────────────────────────────────────────
@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()

    def _fz(text: str) -> bool:
        if not q:
            return True
        t, s, j = text.lower(), q.lower(), 0
        for c in t:
            if c == s[j]:
                j += 1
                if j == len(s):
                    return True
        return False

    pages = [
        {"label": "Overview", "url": "/"},
        {"label": "A2 Options", "url": "/a2"},
        {"label": "Decision Theater", "url": "/theater"},
        {"label": "Trades", "url": "/trades"},
        {"label": "A1 Equity", "url": "/a1"},
        {"label": "Intelligence", "url": "/brief"},
        {"label": "Transparency", "url": "/transparency"},
        {"label": "Social", "url": "/social"},
        {"label": "Strategy Room", "url": "/board"},
    ]

    # Symbols: open A1 positions + recently closed
    symbols: list[dict] = []
    seen_syms: set[str] = set()
    try:
        a1d = _alpaca_a1()
        for p in a1d.get("positions", []):
            sym = getattr(p, "symbol", "") or ""
            if sym and sym not in seen_syms:
                upct = float(getattr(p, "unrealized_plpc", 0) or 0) * 100
                sign = "+" if upct >= 0 else ""
                symbols.append({"label": sym,
                                 "subtitle": f"open · {sign}{upct:.1f}%",
                                 "url": f"/trades?symbol={sym}"})
                seen_syms.add(sym)
    except Exception:
        pass
    try:
        result = _closed_trades()
        ct, _ = result if isinstance(result, tuple) else (result, [])
        ct_sorted = sorted(ct, key=lambda t: t.get("exit_time") or t.get("entry_time") or "", reverse=True)
        for t in ct_sorted[:30]:
            sym = t.get("symbol", "") or ""
            if sym and sym not in seen_syms:
                pnl = t.get("pnl", 0) or 0
                sign = "+" if pnl >= 0 else ""
                symbols.append({"label": sym,
                                 "subtitle": f"closed · {sign}${pnl:.0f}",
                                 "url": f"/trades?symbol={sym}"})
                seen_syms.add(sym)
    except Exception:
        pass

    # Trades: last 20 closed
    trades: list[dict] = []
    try:
        result = _closed_trades()
        ct, _ = result if isinstance(result, tuple) else (result, [])
        ct_sorted = sorted(ct, key=lambda t: t.get("exit_time") or t.get("entry_time") or "", reverse=True)
        for t in ct_sorted[:20]:
            sym = t.get("symbol", "") or ""
            entry_p = t.get("entry_price", 0) or 0
            exit_p = t.get("exit_price", 0) or 0
            pnl = t.get("pnl", 0) or 0
            date_str = (t.get("exit_time") or t.get("entry_time") or "")[:10]
            sign = "+" if pnl >= 0 else ""
            trades.append({"label": sym,
                            "subtitle": f"${entry_p:.2f}→${exit_p:.2f} · {sign}${pnl:.0f} · {date_str}",
                            "url": "/trades"})
    except Exception:
        pass

    # Cycles: last 10 from decisions.json
    cycles: list[dict] = []
    try:
        decisions = json.loads((BOT_DIR / "memory/decisions.json").read_text())
        n = len(decisions)
        for i, d in enumerate(reversed(decisions[-10:])):
            cycle_idx = n - 1 - i
            ts = (d.get("ts", "") or "")[:16].replace("T", " ")
            actions = d.get("actions") or []
            if actions:
                syms = [a.get("symbol", "") or a.get("ticker", "") for a in actions[:2]]
                verdict = ", ".join(s for s in syms if s) or "filled"
            else:
                verdict = "hold"
            cycles.append({"label": f"Cycle {cycle_idx}",
                            "subtitle": f"{ts} · {verdict}",
                            "url": f"/theater?cycle={cycle_idx}",
                            "cycle_id": str(cycle_idx)})
    except Exception:
        pass

    return jsonify({"pages": pages, "symbols": symbols, "trades": trades, "cycles": cycles})


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=DASHBOARD_PORT, debug=False)
