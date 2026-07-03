# -*- coding: utf-8 -*-
"""
Kellun CRM Móvil — Asistente de leads conectado a Odoo
Deploy en Railway. Credenciales via variables de entorno:
  ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD
"""
import os
import re
import xmlrpc.client
from datetime import datetime, timedelta
from html import escape

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()

ODOO_URL = os.environ.get("ODOO_URL", "").rstrip("/")
ODOO_DB = os.environ.get("ODOO_DB", "")
ODOO_USER = os.environ.get("ODOO_USER", "")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")

_uid_cache = {"uid": None}


def get_conn():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
    if not _uid_cache["uid"]:
        _uid_cache["uid"] = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not _uid_cache["uid"]:
        raise Exception("Autenticación fallida: revisa usuario/contraseña")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
    return _uid_cache["uid"], models


def odoo(model, method, args, kwargs=None):
    uid, models = get_conn()
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


def current_uid():
    uid, _ = get_conn()
    return uid


NEG_KEYWORDS = ["desech", "no responde", "no clasific", "recicl"]


def negative_stage_ids():
    """IDs de etapas 'muertas' (descartado, no responde, no clasificado, reciclado)."""
    stages = odoo("crm.stage", "search_read", [[]], {"fields": ["id", "name"]})
    return [s["id"] for s in stages if any(k in s["name"].lower() for k in NEG_KEYWORDS)]


def clean_html(text):
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


DATE_IN_TEXT_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")


def extract_lead_date(description, create_date):
    """Extrae la fecha DD-MM-YYYY escrita al inicio de la nota (fecha real de
    llegada del lead). Si no la encuentra, usa create_date como respaldo."""
    if description:
        m = DATE_IN_TEXT_RE.search(description)
        if m:
            d, mo, y = m.groups()
            try:
                return datetime(int(y), int(mo), int(d))
            except ValueError:
                pass
    if create_date:
        try:
            return datetime.strptime(create_date, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            pass
    return datetime.min


# ---------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------

@app.get("/api/me")
def api_me():
    try:
        uid = current_uid()
        recs = odoo("res.users", "read", [[uid]], {"fields": ["id", "name", "login"]})
        return {"ok": True, "me": recs[0] if recs else {"id": uid}}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/stages")
def api_stages():
    try:
        stages = odoo("crm.stage", "search_read", [[]],
                      {"fields": ["id", "name", "sequence"], "order": "sequence"})
        return {"ok": True, "stages": stages}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/leads")
def api_leads(stage_id: int = 0, q: str = "", limit: int = 40, mine: int = 1, order: str = "recent"):
    try:
        domain = []
        if stage_id:
            domain.append(["stage_id", "=", stage_id])
        if mine:
            domain.append(["user_id", "=", current_uid()])
        if q:
            domain += ["|", "|", ["name", "ilike", q], ["contact_name", "ilike", q],
                       ["partner_name", "ilike", q]]
        # Traemos un lote más grande para poder re-ordenar por la fecha real
        # (la que viene escrita en la nota), no por create_date de Odoo.
        fetch_limit = max(limit * 6, 240)
        leads = odoo("crm.lead", "search_read", [domain], {
            "fields": ["id", "name", "contact_name", "partner_name", "phone",
                       "email_from", "stage_id", "description", "create_date",
                       "write_date", "expected_revenue", "user_id"],
            "limit": fetch_limit, "order": "create_date desc"})

        for l in leads:
            dt = extract_lead_date(l.get("description"), l.get("create_date"))
            l["_sort_dt"] = dt
            l["lead_date"] = dt.strftime("%Y-%m-%d") if dt != datetime.min else ""

        leads.sort(key=lambda l: l["_sort_dt"], reverse=(order != "oldest"))
        leads = leads[:limit]
        for l in leads:
            del l["_sort_dt"]

        return {"ok": True, "leads": leads}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/lead/{lead_id}/notes")
def api_lead_notes(lead_id: int):
    try:
        msgs = odoo("mail.message", "search_read",
                    [[["model", "=", "crm.lead"], ["res_id", "=", lead_id]]],
                    {"fields": ["body", "date", "author_id", "message_type"],
                     "limit": 30, "order": "date desc"})
        notes = []
        for m in msgs:
            body = clean_html(m.get("body", ""))
            if body:
                notes.append({
                    "date": m.get("date", ""),
                    "author": m["author_id"][1] if m.get("author_id") else "",
                    "body": body,
                    "type": m.get("message_type", "")
                })
        return {"ok": True, "notes": notes}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/search_notes")
def api_search_notes(q: str, limit: int = 50, mine: int = 1):
    """Busca texto dentro de las notas del chatter de todos los leads."""
    try:
        # Pedimos más mensajes de lo pedido porque luego filtramos por vendedor
        fetch_limit = limit * 4 if mine else limit
        msgs = odoo("mail.message", "search_read",
                    [[["model", "=", "crm.lead"], ["body", "ilike", q]]],
                    {"fields": ["body", "date", "res_id", "author_id"],
                     "limit": fetch_limit, "order": "date desc"})
        lead_ids = list({m["res_id"] for m in msgs})
        leads = {}
        if lead_ids:
            recs = odoo("crm.lead", "read", [lead_ids],
                        {"fields": ["id", "name", "contact_name", "phone", "stage_id", "user_id"]})
            leads = {r["id"]: r for r in recs}
        uid = current_uid() if mine else None
        results = []
        for m in msgs:
            lead = leads.get(m["res_id"])
            if not lead:
                continue
            if mine and (not lead.get("user_id") or lead["user_id"][0] != uid):
                continue
            results.append({
                "lead_id": m["res_id"],
                "lead_name": lead.get("name", ""),
                "contact": lead.get("contact_name") or "",
                "phone": lead.get("phone") or "",
                "stage": lead["stage_id"][1] if lead.get("stage_id") else "",
                "date": m.get("date", ""),
                "note": clean_html(m.get("body", ""))[:300]
            })
            if len(results) >= limit:
                break
        return {"ok": True, "results": results}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/lead/{lead_id}/stage")
async def api_move_stage(lead_id: int, request: Request):
    try:
        data = await request.json()
        stage_id = int(data["stage_id"])
        odoo("crm.lead", "write", [[lead_id], {"stage_id": stage_id}])
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/lead/{lead_id}/note")
async def api_add_note(lead_id: int, request: Request):
    try:
        data = await request.json()
        note = data.get("note", "").strip()
        if not note:
            return JSONResponse({"ok": False, "error": "Nota vacía"}, status_code=400)
        odoo("crm.lead", "message_post", [[lead_id]],
             {"body": note.replace("\n", "<br/>")})
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/today")
def api_today(mine: int = 1):
    """Actividades planificadas para hoy o vencidas."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        domain = [["res_model", "=", "crm.lead"], ["date_deadline", "<=", today]]
        if mine:
            domain.append(["user_id", "=", current_uid()])
        acts = odoo("mail.activity", "search_read",
                    [domain],
                    {"fields": ["res_id", "summary", "date_deadline", "activity_type_id", "note"],
                     "limit": 50, "order": "date_deadline"})
        lead_ids = list({a["res_id"] for a in acts})
        leads = {}
        if lead_ids:
            recs = odoo("crm.lead", "read", [lead_ids],
                        {"fields": ["id", "name", "contact_name", "phone", "stage_id"]})
            leads = {r["id"]: r for r in recs}
        results = []
        for a in acts:
            lead = leads.get(a["res_id"])
            if not lead:
                continue
            results.append({
                "lead_id": a["res_id"],
                "lead_name": lead.get("name", ""),
                "contact": lead.get("contact_name") or "",
                "phone": lead.get("phone") or "",
                "stage": lead["stage_id"][1] if lead.get("stage_id") else "",
                "activity": a.get("summary") or (a["activity_type_id"][1] if a.get("activity_type_id") else "Actividad"),
                "deadline": a.get("date_deadline", ""),
                "note": clean_html(a.get("note", ""))
            })
        return {"ok": True, "results": results}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/pipeline")
def api_pipeline(mine: int = 1, exclude_neg: int = 1):
    try:
        domain = []
        if mine:
            domain.append(["user_id", "=", current_uid()])
        if exclude_neg:
            neg_ids = negative_stage_ids()
            if neg_ids:
                domain.append(["stage_id", "not in", neg_ids])
        data = odoo("crm.lead", "read_group",
                    [domain, ["stage_id"], ["stage_id"]])
        result = [{"stage": d["stage_id"][1] if d.get("stage_id") else "Sin etapa",
                   "stage_id": d["stage_id"][0] if d.get("stage_id") else 0,
                   "count": d["stage_id_count"]} for d in data]
        return {"ok": True, "pipeline": result}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------

HTML_PAGE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Kellun CRM</title>
<style>
:root{
  --verde:#2D4A3E; --verde2:#4A7C6F; --crema:#F5F0E8; --blanco:#FDFAF5;
  --soil:#2C1810; --arena:#D4A574; --terra:#B45309; --gris:#6B7280;
  --borde:#E8E0D0; --rojo:#DC2626; --ok:#059669;
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--blanco);color:var(--soil);padding-bottom:80px}
.top{background:var(--verde);color:var(--crema);padding:14px 16px;position:sticky;top:0;z-index:50;display:flex;justify-content:space-between;align-items:center}
.top h1{font-size:17px;font-weight:600}
.top .st{font-size:11px;color:var(--arena)}
.tabs{display:flex;background:white;border-bottom:1px solid var(--borde);position:sticky;top:48px;z-index:49;overflow-x:auto}
.tab{flex:1;padding:12px 8px;text-align:center;font-size:12.5px;font-weight:600;color:var(--gris);border-bottom:2.5px solid transparent;cursor:pointer;white-space:nowrap;min-width:70px}
.tab.on{color:var(--verde);border-bottom-color:var(--verde)}
.wrap{padding:14px;max-width:720px;margin:0 auto}
.search{display:flex;gap:8px;margin-bottom:12px}
.search input{flex:1;padding:11px 14px;border:1.5px solid var(--borde);border-radius:10px;font-size:15px;background:white}
.search input:focus{outline:none;border-color:var(--verde2)}
.search button{padding:11px 16px;background:var(--verde);color:white;border:none;border-radius:10px;font-size:14px;font-weight:600}
.chips{display:flex;gap:6px;overflow-x:auto;padding-bottom:8px;margin-bottom:10px;-webkit-overflow-scrolling:touch}
.chip{padding:7px 13px;background:white;border:1.5px solid var(--borde);border-radius:99px;font-size:12px;font-weight:600;color:var(--verde);white-space:nowrap;cursor:pointer}
.chip.on{background:var(--verde);color:white;border-color:var(--verde)}
.chip .n{opacity:.65;font-weight:400}
.card{background:white;border:1px solid var(--borde);border-radius:12px;padding:14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(44,24,16,.05)}
.card .nm{font-size:15px;font-weight:600;margin-bottom:2px}
.card .ct{font-size:13px;color:var(--gris);margin-bottom:8px}
.badge{display:inline-block;font-size:10.5px;font-weight:700;padding:3px 9px;border-radius:5px;background:var(--crema);color:var(--verde);margin-bottom:8px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.btn{flex:1;min-width:90px;padding:10px;border-radius:9px;border:none;font-size:13px;font-weight:600;text-align:center;text-decoration:none;cursor:pointer}
.btn.call{background:var(--ok);color:white}
.btn.wa{background:#25D366;color:white}
.btn.sec{background:var(--crema);color:var(--verde);border:1px solid var(--borde)}
.note-prev{font-size:12.5px;color:#57534E;background:var(--crema);border-radius:8px;padding:9px 11px;margin-top:8px;white-space:pre-wrap;line-height:1.45}
.meta{font-size:11px;color:var(--gris);margin-top:6px}
.empty{text-align:center;padding:44px 20px;color:var(--gris);font-size:14px}
.err{background:#FEE2E2;color:#991B1B;border:1px solid #FECACA;padding:12px;border-radius:10px;font-size:13px;margin-bottom:12px}
.spin{text-align:center;padding:40px;color:var(--gris)}
select{width:100%;padding:10px;border:1.5px solid var(--borde);border-radius:9px;font-size:14px;background:white;margin-top:8px}
textarea{width:100%;padding:10px;border:1.5px solid var(--borde);border-radius:9px;font-size:14px;min-height:70px;margin-top:8px;font-family:inherit}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:100;align-items:flex-end}
.modal.on{display:flex}
.sheet{background:white;width:100%;max-height:85vh;overflow-y:auto;border-radius:18px 18px 0 0;padding:20px 16px 34px}
.sheet h3{font-size:16px;margin-bottom:4px}
.sheet .sub{font-size:12px;color:var(--gris);margin-bottom:14px}
.xbtn{float:right;background:var(--crema);border:none;width:30px;height:30px;border-radius:50%;font-size:15px;cursor:pointer}
.notehist{margin-top:14px}
.notehist .nh{border-left:3px solid var(--arena);padding:8px 12px;margin-bottom:8px;background:var(--crema);border-radius:0 8px 8px 0}
.nh .d{font-size:10.5px;color:var(--gris);margin-bottom:3px}
.nh .b{font-size:13px;white-space:pre-wrap;line-height:1.45}
.pill-hoy{background:#FEF3C7;color:#92400E}
.savebtn{width:100%;padding:13px;background:var(--verde);color:white;border:none;border-radius:10px;font-size:15px;font-weight:600;margin-top:10px}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--soil);color:white;padding:11px 22px;border-radius:99px;font-size:13px;z-index:200;opacity:0;transition:opacity .25s;pointer-events:none}
.toast.on{opacity:1}
</style>
</head>
<body>

<div class="top">
  <h1>Kellun CRM</h1>
  <div class="st" id="st">conectando…</div>
</div>

<div class="tabs">
  <div class="tab on" data-v="hoy" onclick="go('hoy')">📞 Hoy</div>
  <div class="tab" data-v="leads" onclick="go('leads')">👥 Leads</div>
  <div class="tab" data-v="notas" onclick="go('notas')">🔍 Notas</div>
  <div class="tab" data-v="pipe" onclick="go('pipe')">📊 Pipeline</div>
</div>

<div class="wrap" id="wrap"><div class="spin">Cargando…</div></div>

<div class="modal" id="modal"><div class="sheet" id="sheet"></div></div>
<div class="toast" id="toast"></div>

<script>
let STAGES = [];
let VIEW = 'hoy';
let CUR_STAGE = 0;
let MINE_ONLY = 1;
let ORDER = 'recent';
let EXCLUDE_NEG = 1;
let SEARCH_Q = '';

const $ = id => document.getElementById(id);
const esc = s => (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function toast(msg){
  const t = $('toast'); t.textContent = msg; t.classList.add('on');
  setTimeout(()=>t.classList.remove('on'), 2200);
}

async function api(path, opts){
  const r = await fetch(path, opts);
  const j = await r.json();
  if(!j.ok) throw new Error(j.error || 'Error');
  return j;
}

function telLink(p){ return 'tel:' + (p||'').replace(/[^+\\d]/g,''); }
function waLink(p){
  let n = (p||'').replace(/[^\\d]/g,'');
  if(n.length === 9) n = '56' + n;
  return 'https://wa.me/' + n;
}

function go(v){
  VIEW = v;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('on', t.dataset.v === v));
  render();
}

async function init(){
  try{
    const j = await api('/api/stages');
    STAGES = j.stages;
    $('st').textContent = '● conectado';
    try{
      const me = await api('/api/me');
      if(me.me && me.me.name){
        $('st').textContent = '● ' + me.me.name;
      }
    }catch(_){}
    render();
  }catch(e){
    $('st').textContent = 'sin conexión';
    $('wrap').innerHTML = '<div class="err">No se pudo conectar a Odoo: ' + esc(e.message) + '</div>';
  }
}

function render(){
  if(VIEW === 'hoy') renderHoy();
  else if(VIEW === 'leads') renderLeads();
  else if(VIEW === 'notas') renderNotas();
  else renderPipe();
}

// ---- HOY ----
async function renderHoy(){
  const filters = `<div class="chips">
    <div class="chip${MINE_ONLY?' on':''}" onclick="MINE_ONLY=1;renderHoy()">👤 Mis leads</div>
    <div class="chip${MINE_ONLY?'':' on'}" onclick="MINE_ONLY=0;renderHoy()">🏢 Todos</div>
  </div>`;
  $('wrap').innerHTML = filters + '<div class="spin">Buscando pendientes de hoy…</div>';
  try{
    const j = await api('/api/today?mine='+MINE_ONLY);
    if(!j.results.length){
      $('wrap').innerHTML = filters + '<div class="empty">✅ Sin actividades pendientes para hoy.<br><br>Revisa la pestaña Leads para ver tu pipeline.</div>';
      return;
    }
    $('wrap').innerHTML = filters + j.results.map(r => `
      <div class="card">
        <span class="badge pill-hoy">⏰ ${esc(r.deadline)} · ${esc(r.activity)}</span>
        <div class="nm">${esc(r.lead_name)}</div>
        <div class="ct">${esc(r.contact)} · ${esc(r.stage)}</div>
        ${r.note ? '<div class="note-prev">'+esc(r.note)+'</div>' : ''}
        <div class="row">
          ${r.phone ? '<a class="btn call" href="'+telLink(r.phone)+'">📞 Llamar</a><a class="btn wa" href="'+waLink(r.phone)+'" target="_blank">💬 WhatsApp</a>' : ''}
          <button class="btn sec" onclick="openLead(${r.lead_id})">Ver ficha</button>
        </div>
      </div>`).join('');
  }catch(e){ $('wrap').innerHTML = '<div class="err">'+esc(e.message)+'</div>'; }
}

// ---- LEADS ----
async function renderLeads(){
  const chips = '<div class="chips"><div class="chip'+(CUR_STAGE===0?' on':'')+'" onclick="setStage(0)">Todos</div>' +
    STAGES.map(s => '<div class="chip'+(CUR_STAGE===s.id?' on':'')+'" onclick="setStage('+s.id+')">'+esc(s.name)+'</div>').join('') + '</div>';
  const filters = `<div class="chips">
    <div class="chip${MINE_ONLY?' on':''}" onclick="MINE_ONLY=1;renderLeads()">👤 Mis leads</div>
    <div class="chip${MINE_ONLY?'':' on'}" onclick="MINE_ONLY=0;renderLeads()">🏢 Todos</div>
    <div class="chip${ORDER==='recent'?' on':''}" onclick="ORDER='recent';renderLeads()">🕐 Más reciente</div>
    <div class="chip${ORDER==='oldest'?' on':''}" onclick="ORDER='oldest';renderLeads()">📅 Más antiguo</div>
    <div class="chip" onclick="clearFilters()">🗑 Borrar filtros</div>
  </div>`;
  $('wrap').innerHTML = `
    <div class="search">
      <input id="qLeads" value="${esc(SEARCH_Q)}" placeholder="Buscar por nombre…" onkeypress="if(event.key==='Enter')submitSearch()">
      <button onclick="submitSearch()">Buscar</button>
    </div>` + chips + filters + '<div id="list"><div class="spin">Cargando…</div></div>';
  loadLeads();
}

function setStage(id){ CUR_STAGE = id; renderLeads(); }

function submitSearch(){ SEARCH_Q = $('qLeads').value.trim(); loadLeads(); }

function clearFilters(){
  CUR_STAGE = 0; MINE_ONLY = 1; ORDER = 'recent'; SEARCH_Q = '';
  renderLeads();
  toast('Filtros borrados');
}

async function loadLeads(){
  $('list').innerHTML = '<div class="spin">Cargando…</div>';
  try{
    const j = await api('/api/leads?stage_id='+CUR_STAGE+'&q='+encodeURIComponent(SEARCH_Q)+'&mine='+MINE_ONLY+'&order='+ORDER);
    if(!j.leads.length){ $('list').innerHTML = '<div class="empty">Sin leads en esta vista.</div>'; return; }
    $('list').innerHTML = j.leads.map(l => {
      const phone = l.phone || '';
      const desc = (l.description||'').replace(/<[^>]+>/g,'').trim();
      const fecha = l.lead_date || (l.create_date||'').split(' ')[0];
      const asignado = l.user_id ? l.user_id[1] : 'Sin asignar';
      return `<div class="card">
        <span class="badge">${l.stage_id ? esc(l.stage_id[1]) : '—'}</span>
        <div class="nm">${esc(l.name)}</div>
        <div class="ct">${esc(l.contact_name||l.partner_name||'')} ${phone ? '· '+esc(phone) : ''}</div>
        ${desc ? '<div class="note-prev">'+esc(desc.slice(0,200))+'</div>' : ''}
        <div class="meta">🗓 Ingresó: ${esc(fecha)} · 👤 ${esc(asignado)}</div>
        <div class="row">
          ${phone ? '<a class="btn call" href="'+telLink(phone)+'">📞</a><a class="btn wa" href="'+waLink(phone)+'" target="_blank">💬</a>' : ''}
          <button class="btn sec" onclick="openLead(${l.id})">Ficha / Mover</button>
        </div>
      </div>`;
    }).join('');
  }catch(e){ $('list').innerHTML = '<div class="err">'+esc(e.message)+'</div>'; }
}

// ---- NOTAS ----
let NOTAS_Q = '';
function renderNotas(){
  const filters = `<div class="chips">
    <div class="chip${MINE_ONLY?' on':''}" onclick="MINE_ONLY=1;renderNotas()">👤 Mis leads</div>
    <div class="chip${MINE_ONLY?'':' on'}" onclick="MINE_ONLY=0;renderNotas()">🏢 Todos</div>
  </div>`;
  $('wrap').innerHTML = `
    <div class="search">
      <input id="qNotas" value="${esc(NOTAS_Q)}" placeholder='Ej: "llamar", "19:00", "propuesta"…' onkeypress="if(event.key==='Enter')searchNotas()">
      <button onclick="searchNotas()">Buscar</button>
    </div>` + filters + `<div id="list"><div class="empty">Busca cualquier palabra dentro de tus notas.<br>Ej: <b>llamar</b>, <b>documentos</b>, un nombre o un proyecto.</div></div>`;
}

async function searchNotas(){
  NOTAS_Q = $('qNotas').value.trim();
  if(!NOTAS_Q) return;
  $('list').innerHTML = '<div class="spin">Buscando en las notas…</div>';
  try{
    const j = await api('/api/search_notes?q='+encodeURIComponent(NOTAS_Q)+'&mine='+MINE_ONLY);
    if(!j.results.length){ $('list').innerHTML = '<div class="empty">No encontré notas con "'+esc(NOTAS_Q)+'".</div>'; return; }
    $('list').innerHTML = j.results.map(r => `
      <div class="card">
        <span class="badge">${esc(r.stage)}</span>
        <div class="nm">${esc(r.lead_name)}</div>
        <div class="ct">${esc(r.contact)} ${r.phone ? '· '+esc(r.phone) : ''}</div>
        <div class="note-prev">${esc(r.note)}</div>
        <div class="meta">🗓 ${esc(r.date)}</div>
        <div class="row">
          ${r.phone ? '<a class="btn call" href="'+telLink(r.phone)+'">📞</a>' : ''}
          <button class="btn sec" onclick="openLead(${r.lead_id})">Ver ficha</button>
        </div>
      </div>`).join('');
  }catch(e){ $('list').innerHTML = '<div class="err">'+esc(e.message)+'</div>'; }
}

// ---- PIPELINE ----
async function renderPipe(){
  $('wrap').innerHTML = '<div class="spin">Cargando pipeline…</div>';
  const filters = `<div class="chips">
    <div class="chip${MINE_ONLY?' on':''}" onclick="MINE_ONLY=1;renderPipe()">👤 Mis leads</div>
    <div class="chip${MINE_ONLY?'':' on'}" onclick="MINE_ONLY=0;renderPipe()">🏢 Todos</div>
  </div>
  <div class="chips">
    <div class="chip${EXCLUDE_NEG?' on':''}" onclick="EXCLUDE_NEG=1;renderPipe()">✅ Solo activos</div>
    <div class="chip${EXCLUDE_NEG?'':' on'}" onclick="EXCLUDE_NEG=0;renderPipe()">📁 Incluir descartados</div>
  </div>`;
  try{
    const j = await api('/api/pipeline?mine='+MINE_ONLY+'&exclude_neg='+EXCLUDE_NEG);
    const total = j.pipeline.reduce((a,b)=>a+b.count,0);
    $('wrap').innerHTML = filters + '<div class="card"><div class="nm">Total: '+total+' leads</div></div>' +
      j.pipeline.map(p => `
      <div class="card" style="display:flex;justify-content:space-between;align-items:center;cursor:pointer" onclick="CUR_STAGE=${p.stage_id};go('leads')">
        <div class="nm" style="margin:0">${esc(p.stage)}</div>
        <div style="font-size:20px;font-weight:700;color:var(--verde)">${p.count}</div>
      </div>`).join('');
  }catch(e){ $('wrap').innerHTML = filters + '<div class="err">'+esc(e.message)+'</div>'; }
}

// ---- FICHA LEAD ----
async function openLead(id){
  $('modal').classList.add('on');
  $('sheet').innerHTML = '<div class="spin">Cargando ficha…</div>';
  try{
    const [lj, nj] = await Promise.all([
      api('/api/leads?limit=1&q=&stage_id=0').then(()=>api('/api/lead_one?id='+id)).catch(()=>null),
      api('/api/lead/'+id+'/notes')
    ]);
    // fallback: fetch lead individually via /api/leads search won't give one; use read endpoint
    let lead = null;
    try{ const r = await api('/api/lead_read?id='+id); lead = r.lead; }catch(_){}
    const phone = lead ? (lead.phone || '') : '';
    const desc = lead ? (lead.description||'').replace(/<[^>]+>/g,'').trim() : '';
    const stageOpts = STAGES.map(s =>
      '<option value="'+s.id+'"'+(lead && lead.stage_id && lead.stage_id[0]===s.id?' selected':'')+'>'+esc(s.name)+'</option>').join('');

    $('sheet').innerHTML = `
      <button class="xbtn" onclick="$('modal').classList.remove('on')">✕</button>
      <h3>${lead ? esc(lead.name) : 'Lead #'+id}</h3>
      <div class="sub">${lead ? esc(lead.contact_name||'') : ''} ${phone ? '· '+esc(phone) : ''} ${lead && lead.email_from ? '· '+esc(lead.email_from) : ''}</div>
      ${desc ? '<div class="note-prev">'+esc(desc)+'</div>' : ''}
      <div class="row">
        ${phone ? '<a class="btn call" href="'+telLink(phone)+'">📞 Llamar</a><a class="btn wa" href="'+waLink(phone)+'" target="_blank">💬 WhatsApp</a>' : ''}
        ${lead && lead.email_from ? '<a class="btn sec" href="mailto:'+esc(lead.email_from)+'">✉️ Email</a>' : ''}
      </div>

      <div style="margin-top:16px;font-weight:600;font-size:13px">Cambiar etapa</div>
      <select id="selStage">${stageOpts}</select>
      <button class="savebtn" onclick="moveStage(${id})">Guardar etapa</button>

      <div style="margin-top:16px;font-weight:600;font-size:13px">Agregar nota</div>
      <textarea id="newNote" placeholder="Ej: Llamé, quedamos en volver a hablar el viernes a las 19:00…"></textarea>
      <button class="savebtn" onclick="addNote(${id})">Guardar nota</button>

      <div class="notehist">
        <div style="font-weight:600;font-size:13px;margin-bottom:8px">📝 Historial de notas</div>
        ${nj.notes.length ? nj.notes.map(n => '<div class="nh"><div class="d">'+esc(n.date)+' · '+esc(n.author)+'</div><div class="b">'+esc(n.body)+'</div></div>').join('') : '<div class="empty" style="padding:16px">Sin notas aún</div>'}
      </div>`;
  }catch(e){
    $('sheet').innerHTML = '<button class="xbtn" onclick="$(\\'modal\\').classList.remove(\\'on\\')">✕</button><div class="err">'+esc(e.message)+'</div>';
  }
}

async function moveStage(id){
  const sid = $('selStage').value;
  try{
    await api('/api/lead/'+id+'/stage', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stage_id:sid})});
    toast('✅ Etapa actualizada');
    $('modal').classList.remove('on');
    render();
  }catch(e){ toast('❌ '+e.message); }
}

async function addNote(id){
  const note = $('newNote').value.trim();
  if(!note){ toast('Escribe la nota primero'); return; }
  try{
    await api('/api/lead/'+id+'/note', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note})});
    toast('✅ Nota guardada');
    openLead(id);
  }catch(e){ toast('❌ '+e.message); }
}

$('modal').addEventListener('click', e => { if(e.target === $('modal')) $('modal').classList.remove('on'); });

init();
</script>
</body>
</html>"""


@app.get("/api/lead_read")
def api_lead_read(id: int):
    try:
        recs = odoo("crm.lead", "read", [[id]],
                    {"fields": ["id", "name", "contact_name", "partner_name", "phone",
                                "email_from", "stage_id", "description",
                                "expected_revenue", "create_date"]})
        return {"ok": True, "lead": recs[0] if recs else None}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/lead_one")
def api_lead_one(id: int = 0):
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE
i
