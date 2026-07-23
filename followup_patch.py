# -*- coding: utf-8 -*-
"""Extensión del CRM para gestionar seguimientos con fecha/hora desde el panel Hoy.

Se carga encima de main.py para mantener intacta la versión estable y facilitar
una reversión rápida cambiando únicamente el Procfile.
"""
import re
from datetime import datetime

from fastapi import Request
from fastapi.responses import JSONResponse

import main

app = main.app

TIME_MARKER_RE = re.compile(r"KELLUN_TIME=(\d{2}:\d{2})")
FOLLOWUP_STAGE_RE = re.compile(r"volver\s+a\s+llamar", re.IGNORECASE)


def _activity_time(activity):
    text = " ".join([
        activity.get("summary") or "",
        main.clean_html(activity.get("note") or ""),
    ])
    match = TIME_MARKER_RE.search(text)
    return match.group(1) if match else "23:59"


def _activity_note(time_value, note=""):
    clean = (note or "").strip()
    marker = f"KELLUN_TIME={time_value}"
    return marker + ("<br/>" + clean.replace("\n", "<br/>") if clean else "")


def _stage_name(stage_id):
    rows = main.odoo("crm.stage", "read", [[stage_id]], {"fields": ["name"]})
    return rows[0].get("name", "") if rows else ""


def _pending_activities(lead_id):
    return main.odoo(
        "mail.activity",
        "search",
        [[
            ["res_model", "=", "crm.lead"],
            ["res_id", "=", lead_id],
            ["user_id", "=", main.current_uid()],
        ]],
    )


@app.post("/api/v2/lead/{lead_id}/followup")
async def api_create_followup(lead_id: int, request: Request):
    """Mueve a Volver a llamar y crea una actividad con fecha y hora obligatorias."""
    try:
        data = await request.json()
        stage_id = int(data.get("stage_id") or 0)
        date_value = (data.get("date") or "").strip()
        time_value = (data.get("time") or "").strip()
        note = (data.get("note") or "").strip()

        if not stage_id:
            return JSONResponse({"ok": False, "error": "Falta la etapa"}, status_code=400)
        if not date_value or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
            return JSONResponse({"ok": False, "error": "Debes indicar la fecha"}, status_code=400)
        if not time_value or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_value):
            return JSONResponse({"ok": False, "error": "Debes indicar una hora válida"}, status_code=400)
        if not FOLLOWUP_STAGE_RE.search(_stage_name(stage_id)):
            return JSONResponse({"ok": False, "error": "La etapa debe ser Volver a llamar"}, status_code=400)

        main.odoo("crm.lead", "write", [[lead_id], {"stage_id": stage_id}])

        # Evita duplicados: reemplaza seguimientos pendientes del mismo usuario.
        existing = _pending_activities(lead_id)
        if existing:
            main.odoo("mail.activity", "unlink", [existing])

        vals = {
            "res_model": "crm.lead",
            "res_id": lead_id,
            "date_deadline": date_value,
            "summary": f"Volver a llamar · {time_value}",
            "note": _activity_note(time_value, note),
            "user_id": main.current_uid(),
        }
        activity_type = main.default_activity_type_id()
        if activity_type:
            vals["activity_type_id"] = activity_type
        activity_id = main.odoo("mail.activity", "create", [vals])

        if note:
            main.odoo("crm.lead", "message_post", [[lead_id]], {
                "body": f"Seguimiento agendado para {date_value} a las {time_value}.<br/>{note}"
            })
        return {"ok": True, "activity_id": activity_id}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/api/v2/today")
def api_today_v2(mine: int = 1):
    """Actividades de hoy y vencidas, con hora, ID y proyecto para operar sin abrir ficha."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        domain = [["res_model", "=", "crm.lead"], ["date_deadline", "<=", today]]
        if mine:
            domain.append(["user_id", "=", main.current_uid()])
        activities = main.odoo("mail.activity", "search_read", [domain], {
            "fields": ["id", "res_id", "summary", "date_deadline", "activity_type_id", "note"],
            "limit": 100,
            "order": "date_deadline",
        })
        lead_ids = list({activity["res_id"] for activity in activities})
        leads = {}
        if lead_ids:
            rows = main.odoo("crm.lead", "read", [lead_ids], {
                "fields": ["id", "name", "contact_name", "partner_name", "phone", "email_from", "stage_id", "description"]
            })
            leads = {row["id"]: row for row in rows}

        results = []
        for activity in activities:
            lead = leads.get(activity["res_id"])
            if not lead:
                continue
            time_value = _activity_time(activity)
            note_text = main.clean_html(activity.get("note", ""))
            note_text = TIME_MARKER_RE.sub("", note_text).strip()
            deadline = activity.get("date_deadline", "")
            results.append({
                "activity_id": activity["id"],
                "lead_id": activity["res_id"],
                "lead_name": lead.get("name", ""),
                "contact": lead.get("contact_name") or lead.get("partner_name") or "",
                "phone": lead.get("phone") or "",
                "email": lead.get("email_from") or "",
                "stage": lead["stage_id"][1] if lead.get("stage_id") else "",
                "project": main.extract_project(lead.get("description")) or "",
                "activity": activity.get("summary") or "Seguimiento",
                "deadline": deadline,
                "time": time_value,
                "overdue": deadline < today,
                "note": note_text,
            })
        results.sort(key=lambda item: (item["deadline"], item["time"], item["contact"].lower()))
        return {"ok": True, "results": results}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/v2/activity/{activity_id}/done")
async def api_activity_done(activity_id: int, request: Request):
    """Marca la actividad como realizada para que desaparezca inmediatamente de Hoy."""
    try:
        data = await request.json()
        feedback = (data.get("feedback") or "Actividad realizada").strip()
        rows = main.odoo("mail.activity", "read", [[activity_id]], {"fields": ["res_id"]})
        if not rows:
            return JSONResponse({"ok": False, "error": "Actividad no encontrada"}, status_code=404)
        lead_id = rows[0]["res_id"]
        try:
            main.odoo("mail.activity", "action_feedback", [[activity_id]], {"feedback": feedback})
        except Exception:
            main.odoo("mail.activity", "unlink", [[activity_id]])
            main.odoo("crm.lead", "message_post", [[lead_id]], {"body": feedback})
        return {"ok": True}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/v2/activity/{activity_id}/reschedule")
async def api_activity_reschedule(activity_id: int, request: Request):
    try:
        data = await request.json()
        date_value = (data.get("date") or "").strip()
        time_value = (data.get("time") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
            return JSONResponse({"ok": False, "error": "Fecha inválida"}, status_code=400)
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_value):
            return JSONResponse({"ok": False, "error": "Hora inválida"}, status_code=400)
        rows = main.odoo("mail.activity", "read", [[activity_id]], {"fields": ["note"]})
        old_note = main.clean_html(rows[0].get("note", "")) if rows else ""
        old_note = TIME_MARKER_RE.sub("", old_note).strip()
        main.odoo("mail.activity", "write", [[activity_id], {
            "date_deadline": date_value,
            "summary": f"Volver a llamar · {time_value}",
            "note": _activity_note(time_value, old_note),
        }])
        return {"ok": True}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# Sobrescribe únicamente la interfaz necesaria. El backend original permanece disponible.
EXTRA_JS = r'''
function isFollowupStageName(name){ return /volver\s+a\s+llamar/i.test(name||''); }
function localToday(){
  const d=new Date(); const y=d.getFullYear(); const m=String(d.getMonth()+1).padStart(2,'0'); const day=String(d.getDate()).padStart(2,'0');
  return `${y}-${m}-${day}`;
}
function nowTime(){ const d=new Date(); return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0'); }

async function renderHoy(){
  const filters = `<div class="chips">
    <div class="chip${MINE_ONLY?' on':''}" onclick="MINE_ONLY=1;renderHoy()">👤 Mías</div>
    <div class="chip${MINE_ONLY?'':' on'}" onclick="MINE_ONLY=0;renderHoy()">🏢 Todas</div>
  </div>`;
  $('wrap').innerHTML = filters + '<div class="spin">Buscando pendientes de hoy…</div>';
  try{
    const j = await api('/api/v2/today?mine='+MINE_ONLY);
    if(!j.results.length){
      $('wrap').innerHTML = filters + '<div class="empty">✅ Sin actividades pendientes para hoy.</div>';
      return;
    }
    $('wrap').innerHTML = filters + j.results.map(r => `
      <div class="card" id="activity-${r.activity_id}">
        <span class="badge pill-hoy">${r.overdue?'🔴 Vencida':'⏰'} ${esc(r.deadline)} · ${esc(r.time)}</span>
        <div class="nm">${esc(r.contact||r.lead_name)}</div>
        <div class="ct">${r.project?'🏗️ '+esc(r.project)+' · ':''}${stageLabel(r.stage)}</div>
        ${r.phone?'<div class="meta">📱 '+esc(r.phone)+'</div>':''}
        ${r.note?'<div class="note-prev">'+esc(r.note)+'</div>':''}
        <div class="row">
          ${r.phone?'<a class="btn call" href="'+telLink(r.phone)+'">📞 Llamar</a><button class="btn wa" onclick="openWaPicker('+r.lead_id+')">💬 WhatsApp</button>':''}
        </div>
        <div class="row">
          <button class="btn sec" onclick="quickNoteToday(${r.lead_id})">📝 Nota</button>
          <button class="btn sec" onclick="openReschedule(${r.activity_id},'${r.deadline}','${r.time}')">⏰ Reagendar</button>
          <button class="btn sec" onclick="openLead(${r.lead_id})">🔄 Mover</button>
          <button class="btn done" onclick="completeActivity(${r.activity_id})">✅ Hecho</button>
        </div>
      </div>`).join('');
  }catch(e){ $('wrap').innerHTML = filters+'<div class="err">'+esc(e.message)+'</div>'; }
}

function openFollowupModal(id, stageId){
  $('modal').classList.add('on');
  $('sheet').innerHTML = `
    <button class="xbtn" onclick="$ ('modal').classList.remove('on')">✕</button>
    <h3>🔁 Volver a llamar</h3>
    <div class="sub">Indica cuándo debe aparecer en Hoy.</div>
    <div class="follow-grid">
      <label>Fecha<input type="date" id="followDate" value="${localToday()}"></label>
      <label>Hora<input type="time" id="followTime" value="${nowTime()}"></label>
    </div>
    <textarea id="followNote" placeholder="Nota opcional del seguimiento"></textarea>
    <button class="savebtn" onclick="saveFollowup(${id},${stageId})">Guardar seguimiento</button>`;
}

async function saveFollowup(id, stageId){
  const date=$('followDate').value, time=$('followTime').value, note=$('followNote').value.trim();
  if(!date||!time){ toast('⚠️ Debes indicar fecha y hora'); return; }
  try{
    await api('/api/v2/lead/'+id+'/followup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stage_id:stageId,date,time,note})});
    $('modal').classList.remove('on');
    toast('✅ Seguimiento agendado');
    if(VIEW==='leads') renderLeads(); else renderHoy();
  }catch(e){ toast('❌ '+e.message); }
}

async function moveStage(id){
  const sid=Number($('selStage').value);
  const stage=STAGES.find(s=>s.id===sid);
  if(stage && isFollowupStageName(stage.name)){ openFollowupModal(id,sid); return; }
  try{
    await api('/api/lead/'+id+'/stage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stage_id:sid})});
    toast('✅ Etapa actualizada');
    $('modal').classList.remove('on');
    if(VIEW==='leads') renderLeads(); else renderHoy();
  }catch(e){ toast('❌ '+e.message); }
}

async function completeActivity(activityId){
  try{
    await api('/api/v2/activity/'+activityId+'/done',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({feedback:'Actividad realizada desde panel Hoy'})});
    const card=$('activity-'+activityId); if(card) card.remove();
    toast('✅ Actividad realizada');
    setTimeout(()=>renderHoy(),300);
  }catch(e){ toast('❌ '+e.message); }
}

async function quickNoteToday(leadId){
  const note=prompt('Escribe la nota del lead:');
  if(!note||!note.trim()) return;
  try{
    await api('/api/lead/'+leadId+'/note',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note:note.trim()})});
    toast('✅ Nota guardada');
  }catch(e){ toast('❌ '+e.message); }
}

function openReschedule(activityId,date,time){
  $('modal').classList.add('on');
  $('sheet').innerHTML=`
    <button class="xbtn" onclick="$ ('modal').classList.remove('on')">✕</button>
    <h3>⏰ Reagendar llamada</h3>
    <div class="follow-grid">
      <label>Fecha<input type="date" id="resDate" value="${esc(date)}"></label>
      <label>Hora<input type="time" id="resTime" value="${esc(time)}"></label>
    </div>
    <button class="savebtn" onclick="saveReschedule(${activityId})">Guardar nueva fecha</button>`;
}

async function saveReschedule(activityId){
  const date=$('resDate').value,time=$('resTime').value;
  if(!date||!time){ toast('⚠️ Debes indicar fecha y hora'); return; }
  try{
    await api('/api/v2/activity/'+activityId+'/reschedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({date,time})});
    $('modal').classList.remove('on'); toast('✅ Reagendado'); renderHoy();
  }catch(e){ toast('❌ '+e.message); }
}
'''

# Corrige el espacio accidental entre $ y ( en los cierres de modal del bloque raw.
EXTRA_JS = EXTRA_JS.replace("$ ('modal')", "$('modal')")

main.HTML_PAGE = main.HTML_PAGE.replace(
    ".btn.sec{background:var(--crema);color:var(--verde);border:1px solid var(--borde)}",
    ".btn.sec{background:var(--crema);color:var(--verde);border:1px solid var(--borde)}\n"
    ".btn.done{background:var(--verde);color:white}\n"
    ".follow-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}\n"
    ".follow-grid label{font-size:12px;color:var(--gris);font-weight:600}\n"
    ".follow-grid input{width:100%;padding:10px;border:1.5px solid var(--borde);border-radius:9px;font-size:14px;margin-top:5px}"
)
main.HTML_PAGE = main.HTML_PAGE.replace(
    "$('modal').addEventListener('click', e => { if(e.target === $('modal')) $('modal').classList.remove('on'); });",
    EXTRA_JS + "\n$('modal').addEventListener('click', e => { if(e.target === $('modal')) $('modal').classList.remove('on'); });"
)
