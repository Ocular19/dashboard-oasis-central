"""
Revisa proyectos en pgp.coordinador.cl, detecta cambios de estado en las
casillas del diagrama y en las tareas activas, guarda historial (72h) y
dispara un email cuando hay cambios.

Uso: python scraper.py
Variables de entorno esperadas (para el email, opcionales si no hay cambios):
  GMAIL_USER            cuenta gmail remitente
  GMAIL_APP_PASSWORD    contraseña de aplicación de esa cuenta
  MAIL_TO               destinatario(s), separados por coma
"""
import json
import os
import re
import smtplib
import ssl
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent

# Nombre del grupo de proyectos que monitorea esta copia (cambia por
# repositorio: "Oasis_Central", "PMG_CHILE", etc.) — aparece junto al logo en
# el dashboard, las alertas y el resumen semanal para identificar de cuál
# grupo se trata.
GROUP_NAME = "Oasis_Central"

PROJECTS_FILE = ROOT / "projects.json"
STATE_DIR = ROOT / "data" / "state"
HISTORY_DIR = ROOT / "data" / "history"
LOG_DIR = ROOT / "data" / "log"
DOCS_DIR = ROOT / "docs"
HISTORY_WINDOW_HOURS = 72
LOG_RETENTION_DAYS = 35  # suficiente para el resumen semanal con margen

STATE_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
DOCS_DIR.mkdir(parents=True, exist_ok=True)

COLOR_LABELS = {
    "rgb(78, 180, 47)": "completado",
    "rgb(102, 102, 101)": "pendiente",
    "rgb(250, 169, 0)": "en_curso",
    "rgb(255, 235, 59)": "en_curso_destacado",  # variante amarilla observada (ej. ECAP)
}


def label_for_color(bg: str) -> str:
    return COLOR_LABELS.get(bg, bg)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


EXTRACT_HEADER_JS = """
() => {
  function textOf(label) {
    const els = [...document.querySelectorAll('*')];
    const el = els.find(e => e.children.length === 0 && e.textContent.trim() === label);
    return el ? el.textContent.trim() : null;
  }
  const root = document.querySelector('#root');
  const get = (sel) => { const e = document.querySelector(sel); return e ? e.textContent.trim() : null; };
  return {
    title: document.title,
    bodyText: document.body.innerText.slice(0, 4000)
  };
}
"""

EXTRACT_BOXES_JS = """
() => {
  function ownLabel(box) {
    const inner = box.querySelector(':scope > div') || box;
    let txt = '';
    inner.childNodes.forEach(n => { if (n.nodeType === Node.TEXT_NODE) txt += n.textContent; });
    txt = txt.trim();
    return txt || box.textContent.trim().slice(0, 60);
  }
  const boxes = [...document.querySelectorAll('div[class^="req"]')];
  const items = boxes.map(el => ({
    el,
    label: ownLabel(el),
    rect: el.getBoundingClientRect(),
  }));

  // El diagrama no anida los grupos en el DOM (SITR/EME no son ancestros
  // reales de "Enlace"), solo visualmente. Por eso buscamos el grupo padre
  // por posición: la caja más cercana, situada arriba y que cubre
  // horizontalmente a la caja hija.
  function findParent(leaf) {
    let best = null, bestGap = Infinity;
    for (const cand of items) {
      if (cand === leaf) continue;
      const horizOverlap = cand.rect.x <= leaf.rect.x + 1 &&
        (cand.rect.x + cand.rect.width) >= (leaf.rect.x + leaf.rect.width - 1);
      // Un contenedor real es notoriamente más ancho que su hijo (porque
      // adentro caben varias casillas una al lado de la otra). Si tiene casi
      // el mismo ancho, son cajas apiladas en una misma columna, no
      // anidadas (p.ej. "CEM Definitiva" y "ANIT y PLANOS" no están
      // relacionadas, solo una arriba de la otra).
      const muchoMasAncho = cand.rect.width >= leaf.rect.width * 1.25;
      const above = cand.rect.y < leaf.rect.y;
      if (horizOverlap && muchoMasAncho && above) {
        const gap = leaf.rect.y - cand.rect.y;
        if (gap > 0 && gap < bestGap) { bestGap = gap; best = cand; }
      }
    }
    return best ? best.label : null;
  }

  const withParent = items.map(it => ({ it, parent_group: findParent(it) }));
  // Una casilla es "contenedora" (agrupa documentos/estudios adentro, p.ej.
  // OTROS PES, SCADA Y MEDIDAS, ESTUDIOS DE INTERCONEXIÓN) si aparece como
  // el padre de al menos otra casilla. Esas no son una acción en sí misma,
  // solo agrupan a las reales.
  const containerLabels = new Set(withParent.map(w => w.parent_group).filter(Boolean));

  return withParent.map(({ it, parent_group }) => {
    const inner = it.el.querySelector('div') || it.el;
    const cs = getComputedStyle(inner);
    return {
      req_class: it.el.className,
      text: it.label,
      bg: cs.backgroundColor,
      parent_group,
      is_container: containerLabels.has(it.label),
    };
  });
}
"""

EXTRACT_PANEL_JS = """
() => {
  const body = document.body.innerText;
  const start = body.indexOf('Listado de Requerimientos');
  if (start === -1) return null;
  let end = body.indexOf('VOLVER', start);
  if (end === -1) end = start + 3000;
  return { fullText: body.slice(start, end).trim() };
}
"""

# Dentro del panel de detalle, cada tarea es una fila con una "etiqueta" tipo
# "I.1" coloreada según su estado (verde=completada, gris=todavía no llega su
# turno, naranjo=pendiente AHORA). Puede haber más de una fila naranja a la
# vez (pasos en paralelo, p.ej. uno esperando a una Empresa Involucrada y
# otro esperando al Coordinador), por eso devolvemos TODAS las pendientes.
FIND_PENDING_ROWS_JS = """
() => {
  const badges = [...document.querySelectorAll('div')].filter(
    d => d.children.length === 0 && /^[IVXLCDM]+\\.\\d+$/.test(d.textContent.trim())
  );
  const rows = badges.map(b => {
    let row = b.parentElement;
    while (row && row.parentElement && row.textContent.trim() === b.textContent.trim()) {
      row = row.parentElement;
    }
    const label = row.textContent.trim().replace(b.textContent.trim(), '').trim();
    const bg = getComputedStyle(b).backgroundColor;
    return { label, bg };
  });
  const pending = rows.filter(r => r.bg.startsWith('rgb(241'));
  return { total: rows.length, pending: pending.map(r => r.label) };
}
"""

# Hay copias ocultas (ancho/alto 0) del mismo texto en el DOM —p.ej. de un
# historial colapsado—, así que ubicamos la fila VISIBLE con ese texto y
# devolvemos sus coordenadas ya con scroll aplicado, para hacer clic real con
# el mouse de Playwright en vez de un selector de texto ambiguo.
LOCATE_VISIBLE_ROW_JS = """
(label) => {
  const matches = [...document.querySelectorAll('*')].filter(e => e.textContent.trim() === label);
  const visible = matches.find(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  if (!visible) return null;
  visible.scrollIntoView({block: 'center'});
  const r = visible.getBoundingClientRect();
  return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
}
"""


def fetch_request_info(page, project_id: str):
    return page.evaluate(
        """async (id) => {
          const r = await fetch('/api/request/get_request_info', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ir: id})
          });
          return await r.json();
        }""",
        project_id,
    )


def scrape_project(page, project: dict) -> dict:
    url = project["url"]
    project_id = project["id"]
    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_selector('div[class^="req"]', timeout=30000)
    page.wait_for_timeout(1500)

    info = fetch_request_info(page, project_id)

    boxes_raw = page.evaluate(EXTRACT_BOXES_JS)
    boxes = {}
    active_boxes = []
    for b in boxes_raw:
        state = label_for_color(b["bg"])
        boxes[b["req_class"]] = {
            "text": b["text"],
            "state": state,
            "is_container": b.get("is_container", False),
        }
        # Las casillas contenedoras (OTROS PES, SCADA Y MEDIDAS, etc.) solo
        # agrupan a las reales — no tienen un panel de detalle propio que
        # valga la pena abrir, así que no perdemos tiempo haciendo clic ahí.
        if state in ("en_curso", "en_curso_destacado") and not b.get("is_container"):
            active_boxes.append(b)

    WAIT_READY_JS = "() => document.body.innerText.includes('Listado de Requerimientos') && document.body.innerText.includes('Estado:')"

    def select_box(b):
        selector = f'div.{b["req_class"]}'
        # La casilla necesita dos clics: el primero solo la resalta/enfoca,
        # el segundo confirma la selección y carga el panel de detalle.
        page.click(selector, timeout=5000)
        page.click(selector, timeout=5000)
        page.wait_for_function(WAIT_READY_JS, timeout=4000)

    def read_sub_panels(b, attempts=2):
        for _ in range(attempts):
            try:
                select_box(b)
                rows = page.evaluate(FIND_PENDING_ROWS_JS) or {}
                pending_labels = rows.get("pending") or []

                if not pending_labels:
                    # Casilla sin sub-pasos (o ninguno realmente pendiente):
                    # usamos el panel por defecto que ya quedó cargado.
                    panel = page.evaluate(EXTRACT_PANEL_JS)
                    text = (panel or {}).get("fullText") or ""
                    return ([text] if "Estado:" in text else []), None

                texts = []
                for label in pending_labels:
                    try:
                        point = page.evaluate(LOCATE_VISIBLE_ROW_JS, label)
                        if not point:
                            continue
                        page.mouse.click(point["x"], point["y"])
                        page.wait_for_function(WAIT_READY_JS, timeout=4000)
                        panel = page.evaluate(EXTRACT_PANEL_JS)
                        text = (panel or {}).get("fullText") or ""
                        if "Estado:" in text:
                            texts.append(text)
                    except Exception:  # noqa: BLE001
                        continue
                if texts:
                    return texts, None
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                continue
        return [], "no se pudo leer el panel de detalle"

    active_tasks = {}
    for b in active_boxes:
        sub_panels, err = read_sub_panels(b)
        entry = {
            "casilla": b["text"],
            "parent_group": b.get("parent_group"),
            "sub_panels": sub_panels,
        }
        if err:
            entry["error"] = err
        active_tasks[b["req_class"]] = entry

    snapshot = {
        "fetched_at": now_iso(),
        "name": info.get("name"),
        "correlativo": info.get("correlativo"),
        "completition_status": info.get("completition_status"),
        "completition_pes": info.get("completition_pes"),
        "service_estimate_date": info.get("service_estimate_date"),
        "operative_estimate_date": info.get("operative_estimate_date"),
        "reception_date": info.get("reception_date"),
        "boxes": boxes,
        "active_tasks": active_tasks,
    }
    return snapshot


def compute_first_seen(prev: dict, curr: dict) -> dict:
    """Para cada sub-tarea pendiente (no completada, no de un tercero) anota
    la fecha en que la vimos pendiente por primera vez, arrastrando el valor
    de la corrida anterior si la tarea sigue siendo la misma. Así podemos
    mostrar 'lleva N días sin la carga del documento'."""
    prev_first_seen = (prev or {}).get("first_seen", {})
    curr_first_seen = {}
    for req_class, task in curr.get("active_tasks", {}).items():
        for sub_text in task.get("sub_panels", []):
            parsed = parse_task_panel(sub_text)
            if not parsed or parsed["completada"] or parsed["quien_actua"] == "terceros":
                continue
            if parsed["description"].strip() == "Grupo de Requerimientos":
                continue
            key = f"{req_class}|{parsed['description']}"
            curr_first_seen[key] = prev_first_seen.get(key, now_iso())
    return curr_first_seen


def diff_snapshots(prev: dict, curr: dict) -> tuple:
    """Devuelve (changes, alerts):
    - changes: lista de strings legibles, para el historial de 72h y el correo.
    - alerts: lista de dicts estructurados {casilla, evento, quien, fecha_limite},
      uno por cada cambio, para armar el correo de alerta con columnas claras."""
    changes = []
    alerts = []
    if prev is None:
        return changes, alerts  # primera corrida: no hay nada que comparar todavía

    def quien_fecha_for(req_class: str, casilla: str):
        """Busca, en el estado actual, la sub-tarea pendiente más relevante de
        esta casilla para poder decir quién debe responder y para cuándo."""
        task = curr.get("active_tasks", {}).get(req_class)
        if not task:
            return None, None
        for sub_text in task.get("sub_panels", []):
            parsed = parse_task_panel(sub_text)
            if not parsed or parsed["completada"] or parsed["quien_actua"] == "terceros":
                continue
            if parsed["description"].strip() == "Grupo de Requerimientos":
                continue
            return parsed["quien_actua"], parsed["fecha_limite"]
        return None, None

    prev_boxes = prev.get("boxes", {})
    curr_boxes = curr.get("boxes", {})
    for req_class, curr_box in curr_boxes.items():
        prev_box = prev_boxes.get(req_class)
        if prev_box is None or curr_box.get("is_container"):
            continue
        if prev_box.get("state") != curr_box.get("state"):
            quien, fecha = quien_fecha_for(req_class, curr_box["text"])
            changes.append(
                f"Casilla '{curr_box['text']}' cambió de estado: "
                f"{prev_box.get('state')} -> {curr_box.get('state')}"
            )
            alerts.append(
                {
                    "casilla": curr_box["text"],
                    "evento": f"Cambió de estado ({prev_box.get('state')} → {curr_box.get('state')})",
                    "quien": QUIEN_LABEL.get(quien) if quien else "Sin acción pendiente (completada)",
                    "fecha_limite": fecha or "-",
                }
            )

    def relevant_subs(task: dict) -> dict:
        """Sub-tareas pendientes de esta casilla, ya parseadas, ignorando las
        que dependen de un tercero (Empresa Involucrada) — eso no nos importa
        para efectos de seguimiento, solo CEN y Grenergy."""
        out = {}
        for sub_text in task.get("sub_panels", []):
            parsed = parse_task_panel(sub_text)
            if not parsed or parsed["completada"] or parsed["quien_actua"] == "terceros":
                continue
            if parsed["description"].strip() == "Grupo de Requerimientos":
                continue
            out[parsed["description"]] = parsed
        return out

    prev_tasks = prev.get("active_tasks", {})
    curr_tasks = curr.get("active_tasks", {})
    for req_class, curr_task in curr_tasks.items():
        casilla = curr_task.get("casilla")
        prev_task = prev_tasks.get(req_class)
        curr_by_desc = relevant_subs(curr_task)

        if prev_task is None:
            for p in curr_by_desc.values():
                changes.append(
                    f"'{casilla}': nuevo pendiente — {QUIEN_LABEL[p['quien_actua']]} "
                    f"(fecha límite {p['fecha_limite']})."
                )
                alerts.append(
                    {
                        "casilla": casilla,
                        "evento": "Nuevo pendiente",
                        "quien": QUIEN_LABEL[p["quien_actua"]],
                        "fecha_limite": p["fecha_limite"],
                    }
                )
            continue

        prev_by_desc = relevant_subs(prev_task)
        resolved = set(prev_by_desc) - set(curr_by_desc)
        new = set(curr_by_desc) - set(prev_by_desc)
        common = set(prev_by_desc) & set(curr_by_desc)

        if len(resolved) == 1 and len(new) == 1 and not common:
            # Caso típico: se completó un paso y el flujo avanzó al siguiente.
            new_p = curr_by_desc[next(iter(new))]
            changes.append(
                f"'{casilla}': avanzó de etapa — ahora {QUIEN_LABEL[new_p['quien_actua']]} "
                f"(fecha límite {new_p['fecha_limite']})."
            )
            alerts.append(
                {
                    "casilla": casilla,
                    "evento": "Avanzó de etapa",
                    "quien": QUIEN_LABEL[new_p["quien_actua"]],
                    "fecha_limite": new_p["fecha_limite"],
                }
            )
        else:
            # No avisamos cuando un pendiente "se resuelve" solo: es un paso
            # intermedio sin acción para nadie. Lo único relevante es cuando
            # aparece un nuevo pendiente real (carga tuya o decisión del
            # CEN) — eso sí se reporta más abajo.
            for desc in new:
                p = curr_by_desc[desc]
                changes.append(
                    f"'{casilla}': nuevo pendiente — {QUIEN_LABEL[p['quien_actua']]} "
                    f"(fecha límite {p['fecha_limite']})."
                )
                alerts.append(
                    {
                        "casilla": casilla,
                        "evento": "Nuevo pendiente",
                        "quien": QUIEN_LABEL[p["quien_actua"]],
                        "fecha_limite": p["fecha_limite"],
                    }
                )
            for desc in common:
                pp, cp = prev_by_desc[desc], curr_by_desc[desc]
                if pp["fecha_limite"] != cp["fecha_limite"] or pp["quien_actua"] != cp["quien_actua"]:
                    changes.append(
                        f"'{casilla}': cambió la fecha límite o el responsable — ahora "
                        f"{QUIEN_LABEL[cp['quien_actua']]} (fecha límite {cp['fecha_limite']})."
                    )
                    alerts.append(
                        {
                            "casilla": casilla,
                            "evento": "Cambió fecha límite o responsable",
                            "quien": QUIEN_LABEL[cp["quien_actua"]],
                            "fecha_limite": cp["fecha_limite"],
                        }
                    )

    FIELD_LABELS = {
        "completition_status": "Avance general del proyecto",
        "completition_pes": "Avance de requisitos para inicio de PES",
        "service_estimate_date": "Fecha estimada de Puesta en Servicio",
        "operative_estimate_date": "Fecha estimada de Entrada en Operación",
    }
    for key, label in FIELD_LABELS.items():
        if prev.get(key) != curr.get(key):
            unidad = "%" if key in ("completition_status", "completition_pes") else ""
            changes.append(f"{label}: {prev.get(key)}{unidad} → {curr.get(key)}{unidad}.")

    return changes, alerts


def load_json(path: Path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def append_history(project_id: str, changes: list, alerts: list = None) -> list:
    alerts = alerts or []
    history_path = HISTORY_DIR / f"{project_id}.json"
    history = load_json(history_path) or []
    if changes:
        history.append({"timestamp": now_iso(), "changes": changes})
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HISTORY_WINDOW_HOURS)
    history = [h for h in history if datetime.fromisoformat(h["timestamp"]) >= cutoff]
    save_json(history_path, history)

    # Bitácora de largo plazo (no se borra a las 72h) para poder armar el
    # resumen ejecutivo semanal más adelante. Guardamos también los "alerts"
    # estructurados (casilla/evento/quién/fecha), no solo el texto, para que
    # el resumen semanal pueda mostrar columnas en vez de solo frases.
    if changes:
        log_path = LOG_DIR / f"{project_id}.json"
        log = load_json(log_path) or []
        log.append({"timestamp": now_iso(), "changes": changes, "alerts": alerts})
        log_cutoff = datetime.now(timezone.utc) - timedelta(days=LOG_RETENTION_DAYS)
        log = [h for h in log if datetime.fromisoformat(h["timestamp"]) >= log_cutoff]
        save_json(log_path, log)

    return history


def send_email(subject: str, body: str) -> None:
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("MAIL_TO")
    if not (user and password and to):
        print("Faltan variables de entorno de email; se omite el envío.")
        print(body)
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(user, password)
        server.sendmail(user, [t.strip() for t in to.split(",")], msg.as_string())


def render_alert_email(all_alerts: list) -> str:
    """Correo de alerta inmediata: una fila por cada casilla que cambió, con
    proyecto, casilla, quién debe responder (Grenergy o CEN) y fecha límite."""
    sections = []
    for name, nup, alerts in all_alerts:
        rows = "".join(
            f"""<tr>
                <td style="padding:10px 8px; border-bottom:1px solid #e3e9e7; font-size:13px; color:#1b2b29;">{a['casilla']}</td>
                <td style="padding:10px 8px; border-bottom:1px solid #e3e9e7; font-size:13px; color:#1b2b29;">{a['evento']}</td>
                <td style="padding:10px 8px; border-bottom:1px solid #e3e9e7; font-size:13px; color:#04201f; font-weight:600;">{a['quien']}</td>
                <td style="padding:10px 8px; border-bottom:1px solid #e3e9e7; font-size:13px; color:#1b2b29;">{a['fecha_limite']}</td>
            </tr>"""
            for a in alerts
        )
        sections.append(
            f"""
            <tr><td style="padding:20px 0 6px 0;">
              <div style="font-weight:600; color:#04201f; font-size:15px;">
                {name} <span style="color:#8a9591; font-weight:400; font-size:12px;">NUP {nup}</span>
              </div>
            </td></tr>
            <tr><td>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <th align="left" style="font-size:11px; color:#8a9591; padding:4px 8px; text-transform:uppercase;">Casilla</th>
                  <th align="left" style="font-size:11px; color:#8a9591; padding:4px 8px; text-transform:uppercase;">Evento</th>
                  <th align="left" style="font-size:11px; color:#8a9591; padding:4px 8px; text-transform:uppercase;">Quién responde</th>
                  <th align="left" style="font-size:11px; color:#8a9591; padding:4px 8px; text-transform:uppercase;">Fecha límite</th>
                </tr>
                {rows}
              </table>
            </td></tr>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="es">
<body style="margin:0; padding:0; background:#eef3f1; font-family:Arial, Helvetica, sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef3f1; padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="680" cellpadding="0" cellspacing="0" style="background:white; border-radius:14px; overflow:hidden;">
        <tr><td style="background:#04201f; padding:24px 28px;">
          <img src="cid:logo" alt="Grenergy" height="28">
        </td></tr>
        <tr><td style="padding:24px 28px 0 28px;">
          <div style="font-size:18px; font-weight:600; color:#04201f;">Cambios detectados &mdash; {GROUP_NAME}</div>
          <div style="font-size:12px; color:#8a9591; margin-top:4px;">{now_iso()}</div>
        </td></tr>
        <tr><td style="padding:0 28px 10px 28px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            {''.join(sections)}
          </table>
        </td></tr>
        <tr><td style="padding:18px 28px 26px 28px; font-size:11px; color:#8a9591;">
          Generado automáticamente desde el monitor de proyectos PGP.
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_html_email(subject: str, html_body: str, logo_path: Path = None) -> None:
    """Igual que send_email pero con cuerpo HTML (usado por el resumen
    semanal con la marca de Grenergy). El logo se embebe inline (cid) para
    que se vea en el cliente de correo sin depender de una URL externa."""
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("MAIL_TO")
    if not (user and password and to):
        print("Faltan variables de entorno de email; se omite el envío.")
        print(html_body)
        return

    from email.mime.multipart import MIMEMultipart
    from email.mime.image import MIMEImage

    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if logo_path and logo_path.exists():
        with open(logo_path, "rb") as f:
            img = MIMEImage(f.read())
        img.add_header("Content-ID", "<logo>")
        img.add_header("Content-Disposition", "inline", filename="logo.png")
        msg.attach(img)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(user, password)
        server.sendmail(user, [t.strip() for t in to.split(",")], msg.as_string())


def fmt_date(value) -> str:
    if not value:
        return "-"
    return str(value).split("T")[0]


def parse_fecha_limite(value: str):
    """'dd-mm-yyyy' -> date, o None si no hay fecha establecida."""
    try:
        return datetime.strptime(value.strip(), "%d-%m-%Y").date()
    except (ValueError, AttributeError):
        return None


def days_since(iso_timestamp: str) -> int:
    try:
        then = datetime.fromisoformat(iso_timestamp)
    except (ValueError, TypeError):
        return 0
    return (datetime.now(timezone.utc) - then).days


def _field(text: str, label: str) -> str:
    """Extrae la línea que sigue a 'label\\n\\n' dentro del panel_text."""
    marker = label + "\n\n"
    idx = text.find(marker)
    if idx == -1:
        return ""
    rest = text[idx + len(marker):]
    # el valor es lo que viene antes del próximo doble salto de línea
    end = rest.find("\n\n")
    return (rest[:end] if end != -1 else rest).strip()


def parse_task_panel(text: str) -> dict:
    """Convierte el texto plano del panel 'Listado de Requerimientos' en
    campos estructurados: descripción, estado, fecha límite, responsable."""
    if not text or "Estado:" not in text:
        return {}

    desc_idx = text.find("Descripción\n\n")
    estado_idx = text.find("Estado:")
    description = ""
    if desc_idx != -1:
        description = text[desc_idx + len("Descripción\n\n"):estado_idx].strip()

    estado_line = ""
    if estado_idx != -1:
        end = text.find("\n", estado_idx)
        estado_line = text[estado_idx:end if end != -1 else None].strip()

    fecha_limite = _field(text, "Fecha límite")
    plazo = _field(text, "Plazo")
    depende_de = _field(text, "Depende de")

    completada = "completada" in estado_line.lower()
    pendiente = "pendiente" in estado_line.lower() or "inactiva" in estado_line.lower()

    desc_lower = description.lower()
    quien_actua = "desconocido"
    if not completada:
        if "empresa involucrada" in desc_lower:
            quien_actua = "terceros"
        elif any(
            kw in desc_lower
            for kw in (
                "empresa solicitante",
                "adjunta",
                "debe enviar",
                "deben enviar",
                "debe indicar",
                "deben indicar",
                "debe hacer envío",
                "deben hacer envío",
                "debe cargar",
                "deben cargar",
            )
        ):
            quien_actua = "tu_empresa"
        elif "coordinador" in desc_lower or "cen " in desc_lower or "daop" in desc_lower:
            quien_actua = "coordinador"
        else:
            # Sin palabras clave en la descripción: nos fijamos si el círculo
            # "Responsable" trae un código (p.ej. "GRS") — eso indica que el
            # turno es de un equipo propio, no del Coordinador.
            # Cuando el círculo "Responsable" no tiene texto (ícono vacío),
            # el campo queda pegado al siguiente label ("Plazo") sin nada en
            # medio, así que _field termina capturando "Plazo" por error.
            responsable = _field(text, "Responsable")
            if responsable in ("Plazo", "Fecha límite", "Depende de", "Archivo(s):"):
                responsable = ""
            quien_actua = "tu_empresa" if responsable else "coordinador"

    return {
        "description": description,
        "estado_line": estado_line,
        "completada": completada,
        "pendiente": pendiente,
        "fecha_limite": fecha_limite or "sin fecha establecida",
        "plazo": plazo,
        "depende_de": depende_de,
        "quien_actua": quien_actua,
    }


EMPRESA_LABEL = "Grenergy"

QUIEN_LABEL = {
    "tu_empresa": f"Esperando que {EMPRESA_LABEL} cargue un documento",
    "coordinador": "Esperando respuesta/revisión del Coordinador (CEN)",
    "terceros": "Esperando a una Empresa Involucrada (tercero)",
    "desconocido": "En curso",
}


def render_dashboard(projects_summary: list) -> None:
    cards = []
    for p in projects_summary:
        boxes = p["boxes"]
        leaf_boxes = [b for b in boxes.values() if not b.get("is_container")]
        n_completado = sum(1 for b in leaf_boxes if b["state"] == "completado")
        n_pendiente = sum(1 for b in leaf_boxes if b["state"] == "pendiente")
        n_en_curso = sum(1 for b in leaf_boxes if b["state"] in ("en_curso", "en_curso_destacado"))

        # Genéricos cuyo grupo padre no aporta información extra (sería
        # redundante mostrar "REQUISITOS PES > OTROS PES", por ejemplo).
        SKIP_PARENT_LABELS = {"REQUISITOS PES", "REQUISITOS EO", "CEM Y OTROS"}

        # Nombres de casillas que tuvieron algún cambio en las últimas 72h,
        # para destacar esa fila con una marca "actualizado" en la lista.
        changed_recently = set()
        for h in p["history"]:
            for c in h["changes"]:
                m = re.search(r"'([^']+)'", c)
                if m:
                    changed_recently.add(m.group(1))

        today = datetime.now(timezone.utc).date()
        first_seen_map = p.get("first_seen", {})

        active_rows = []
        for req_class, task in p["active_tasks"].items():
            label = task["casilla"]
            parent = task.get("parent_group")
            if parent and parent not in SKIP_PARENT_LABELS and parent != label:
                label = f"{parent} &rsaquo; {label}"
            is_recent = task["casilla"] in changed_recently
            for sub_text in task.get("sub_panels", []):
                parsed = parse_task_panel(sub_text)
                if not parsed or parsed["completada"]:
                    continue
                if parsed["quien_actua"] == "terceros":
                    continue  # no nos importa si el tercero ya respondió o no
                if parsed["description"].strip() == "Grupo de Requerimientos":
                    continue  # es solo un contenedor, no una acción real

                row_class = parsed["quien_actua"]
                extra_badges = ""

                if is_recent:
                    extra_badges += '<span class="recent">Actualizado (últimas 72h)</span>'

                esta_atrasada = False
                fecha_dt = parse_fecha_limite(parsed["fecha_limite"])
                if fecha_dt and fecha_dt < today:
                    dias_atraso = (today - fecha_dt).days
                    esta_atrasada = True
                    if parsed["quien_actua"] == "coordinador":
                        extra_badges += f'<span class="atrasado">CEN atrasado {dias_atraso} día(s)</span>'
                        row_class += " atrasado-row"
                    elif parsed["quien_actua"] == "tu_empresa":
                        extra_badges += f'<span class="atrasado">Atrasado {dias_atraso} día(s)</span>'
                        row_class += " atrasado-row"

                # Si ya está marcada como atrasada, "Atrasado X días" (viene de
                # la fecha límite real) ya es la señal confiable; no mostramos
                # también "lleva N días" porque ese contador solo cuenta desde
                # que ESTE sistema empezó a vigilar la tarea y puede no
                # coincidir con el atraso real (p. ej. justo después de
                # reiniciar los datos).
                dias_activa_html = ""
                if parsed["quien_actua"] == "tu_empresa" and not esta_atrasada:
                    key = f"{req_class}|{parsed['description']}"
                    seen = first_seen_map.get(key)
                    if seen:
                        dias = days_since(seen)
                        dias_activa_html = (
                            f"<span class='dias-activa'>Lleva {dias} día(s) sin la carga del documento</span>"
                        )

                active_rows.append(
                    f"""<li class="task {row_class}">
                        <div class="task-head"><strong>{label}</strong>{extra_badges}</div>
                        <span class="quien">{QUIEN_LABEL[parsed['quien_actua']]}</span>
                        <span class="desc">{parsed['description'] or '-'}</span>
                        <span class="fecha">Fecha límite: {parsed['fecha_limite']}</span>
                        {dias_activa_html}
                    </li>"""
                )
        active_html = (
            f"<h3>Casillas en curso ({len(active_rows)})</h3><ul class='tasks'>{''.join(active_rows)}</ul>"
            if active_rows
            else ""
        )

        history_html = ""
        if p["history"]:
            items = "".join(
                f"<li><span class='ts'>{h['timestamp']}</span><ul>"
                + "".join(f"<li>{c}</li>" for c in h["changes"])
                + "</ul></li>"
                for h in p["history"]
            )
            history_html = f"<h3>Cambios (últimas 72h)</h3><ul class='history'>{items}</ul>"

        cards.append(
            f"""
            <section class="card">
              <h2>{p['name']} <span class="nup">#{p['correlativo']}</span></h2>
              <p>Avance del proyecto: {p['completition_status']}% &mdash; Avance requisitos PES: {p['completition_pes']}%</p>
              <p>Puesta en servicio estimada: {fmt_date(p['service_estimate_date'])} &mdash; Entrada en operación estimada: {fmt_date(p['operative_estimate_date'])}</p>
              <p class="resumen">
                <span class="badge verde">{n_completado} completadas</span>
                <span class="badge gris">{n_pendiente} no iniciadas</span>
                <span class="badge naranja">{n_en_curso} en curso</span>
              </p>
              <p><a href="{p['url']}" target="_blank">Ver proyecto en PGP &#8599;</a></p>
              {active_html}
              {history_html}
            </section>
            """
        )
    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Dashboard PGP &mdash; {GROUP_NAME}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --dark: #04201f;
    --mint: #00cf78;
    --mint-light: #7be3ae;
    --gray: #6b7777;
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'Poppins', Arial, sans-serif; background:#eef3f1; margin:0; padding:0 0 32px 0; color:#1b2b29; }}
  header.brand {{
    background: var(--dark);
    padding: 28px 32px;
    display:flex;
    align-items:center;
    gap:20px;
    margin-bottom: 28px;
  }}
  header.brand img {{ height: 32px; display:block; }}
  header.brand .group-name {{ color: white; font-size:1em; font-weight:600; margin:0; }}
  header.brand .tagline {{ color: var(--mint-light); font-size:0.85em; margin:0; }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 0 20px; }}
  h1 {{ color: var(--dark); font-weight:600; font-size:1.4em; margin:0 0 4px 0; }}
  .card {{ background:white; border-radius:14px; padding:20px 24px; margin-bottom:20px; box-shadow:0 2px 10px rgba(4,32,31,0.08); }}
  .card h2 {{ color: var(--dark); font-weight:600; margin-top:0; }}
  .nup {{ color:#8a9591; font-size:0.75em; font-weight:400; }}
  .resumen {{ margin:14px 0; }}
  .badge {{ display:inline-block; padding:4px 12px; border-radius:14px; color:white; font-size:0.8em; margin-right:6px; font-weight:500; }}
  .badge.verde {{ background: var(--mint); color:#04201f; }}
  .badge.gris {{ background: var(--gray); }}
  .badge.naranja {{ background:#faa900; }}
  .tasks {{ list-style:none; padding-left:0; }}
  .tasks > li {{ position:relative; margin-bottom:10px; padding:12px 14px; border-radius:10px; background:#f6f9f8; border-left:4px solid #faa900; }}
  .tasks .task-head {{ display:flex; flex-wrap:wrap; align-items:center; gap:6px; }}
  .tasks .task-head strong {{ margin-right:0; }}
  .tasks > li.coordinador {{ border-left-color: var(--dark); }}
  .tasks > li.tu_empresa {{ border-left-color:#e67e22; }}
  .tasks > li.terceros {{ border-left-color:#8e44ad; }}
  .tasks .recent {{ display:inline-block; padding:2px 8px; border-radius:10px; background: var(--mint); color:#04201f; font-size:0.7em; font-weight:600; vertical-align:middle; }}
  .tasks .atrasado {{ display:inline-block; padding:2px 8px; border-radius:10px; background:#e0273a; color:white; font-size:0.7em; font-weight:600; vertical-align:middle; }}
  .tasks > li.atrasado-row {{ background:#fdeceb; }}
  .tasks .dias-activa {{ display:block; font-size:0.78em; color:#b35400; margin-top:2px; font-style:italic; }}
  .tasks .quien {{ display:block; font-weight:600; font-size:0.85em; margin:4px 0 2px 0; color: var(--dark); }}
  .tasks .desc {{ display:block; font-size:0.85em; color:#555; }}
  .tasks .fecha {{ display:block; font-size:0.8em; color:#777; margin-top:4px; }}
  .history {{ list-style:none; padding-left:0; }}
  .history > li {{ margin-bottom:10px; border-left:3px solid var(--mint); padding-left:10px; }}
  .ts {{ font-size:0.8em; color:#777; display:block; }}
  a {{ color: var(--dark); text-decoration:underline; }}
  .updated {{ color:#7c8c88; font-size:0.85em; }}
</style>
</head>
<body>
<header class="brand">
  <img src="assets/grenergy-logo.png" alt="Grenergy">
  <div>
    <p class="group-name" style="margin:0">{GROUP_NAME}</p>
    <p class="tagline" style="margin:0">Dashboard de seguimiento PGP</p>
  </div>
</header>
<div class="wrap">
<h1>Proyectos en el Coordinador Eléctrico Nacional</h1>
<p class="updated">Última actualización: {now_iso()}</p>
{''.join(cards)}
</div>
</body>
</html>"""
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")


def main():
    projects = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    all_alerts = []  # [(nombre_proyecto, nup, [alert_dict, ...]), ...]
    dashboard_summary = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for project in projects:
            project_id = project["id"]
            state_path = STATE_DIR / f"{project_id}.json"
            prev = load_json(state_path)
            try:
                curr = scrape_project(page, project)
            except Exception as e:  # noqa: BLE001
                print(f"Error en proyecto {project_id}: {e}")
                continue

            changes, alerts = diff_snapshots(prev, curr)
            curr["first_seen"] = compute_first_seen(prev, curr)
            save_json(state_path, curr)
            history = append_history(project_id, changes, alerts)

            if alerts:
                all_alerts.append((curr.get("name", project_id), curr.get("correlativo"), alerts))

            dashboard_summary.append(
                {
                    "name": curr.get("name") or project_id,
                    "correlativo": curr.get("correlativo"),
                    "completition_status": curr.get("completition_status"),
                    "completition_pes": curr.get("completition_pes"),
                    "service_estimate_date": curr.get("service_estimate_date"),
                    "operative_estimate_date": curr.get("operative_estimate_date"),
                    "url": project["url"],
                    "history": history,
                    "boxes": curr.get("boxes", {}),
                    "active_tasks": curr.get("active_tasks", {}),
                    "first_seen": curr.get("first_seen", {}),
                }
            )
        browser.close()

    render_dashboard(dashboard_summary)

    if all_alerts:
        html = render_alert_email(all_alerts)
        logo_path = DOCS_DIR / "assets" / "grenergy-logo.png"
        try:
            send_html_email("PGP: cambios detectados en proyectos", html, logo_path=logo_path)
        except Exception as e:  # noqa: BLE001
            # Un error de email (p.ej. MAIL_TO mal escrito) no debe tumbar
            # toda la corrida: los datos y el dashboard ya se guardaron bien.
            print(f"No se pudo enviar el correo de alerta: {e}")
        for name, nup, alerts in all_alerts:
            print(f"== {name} (NUP {nup}) ==")
            for a in alerts:
                print(f"  - {a['casilla']}: {a['evento']} — {a['quien']} (fecha límite {a['fecha_limite']})")
    else:
        print("Sin cambios detectados.")


if __name__ == "__main__":
    main()
