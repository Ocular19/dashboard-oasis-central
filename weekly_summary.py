"""
Resumen ejecutivo semanal: recorre la bitácora de largo plazo (data/log/) de
cada proyecto, junta los cambios de los últimos 7 días y envía un correo con
la identidad visual de Grenergy. Si un proyecto no tuvo cambios, igual
aparece listado con "Sin cambios" — así se puede comparar semana a semana.

Uso: python weekly_summary.py
Mismas variables de entorno que scraper.py (GMAIL_USER, GMAIL_APP_PASSWORD,
MAIL_TO).
"""
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import scraper

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "data" / "log"
ASSETS_DIR = ROOT / "docs" / "assets"
WEEK_DAYS = 7

FECHA_RE = re.compile(r"fecha límite (\d{2}-\d{2}-\d{4})")
CASILLA_RE = re.compile(r"'([^']+)'")
QUIEN_PATTERNS = [
    ("Esperando que Grenergy cargue un documento", "Esperando que Grenergy cargue un documento"),
    ("Esperando respuesta/revisión del Coordinador (CEN)", "Esperando respuesta/revisión del Coordinador (CEN)"),
]


def _guess_fields(text: str) -> dict:
    """Para entradas viejas del log que no guardaron el detalle estructurado,
    intenta sacar casilla/quién/fecha del propio texto en vez de dejar todo
    en blanco."""
    casilla_m = CASILLA_RE.search(text)
    fecha_m = FECHA_RE.search(text)
    quien = "-"
    for pattern, label in QUIEN_PATTERNS:
        if pattern in text:
            quien = label
            break
    return {
        "casilla": casilla_m.group(1) if casilla_m else "Proyecto (general)",
        "evento": text,
        "quien": quien,
        "fecha_limite": fecha_m.group(1) if fecha_m else "-",
    }


def alerts_in_window(project_id: str, days_back_start: int, days_back_end: int) -> list:
    """Alerts entre hace `days_back_start` días y hace `days_back_end` días
    (days_back_end < days_back_start, ej. (14, 7) = la semana anterior a esta)."""
    log = scraper.load_json(LOG_DIR / f"{project_id}.json") or []
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_back_start)
    end = now - timedelta(days=days_back_end)
    out = []
    for entry in log:
        ts = datetime.fromisoformat(entry["timestamp"])
        if not (start <= ts < end):
            continue
        alerts = entry.get("alerts")
        if alerts:
            out.extend(alerts)
        for c in entry.get("changes", []):
            already_covered = alerts and any(a.get("evento") == c for a in alerts)
            if not already_covered:
                out.append(_guess_fields(c))
    return out


def summarize(alerts: list) -> dict:
    completadas = sum(1 for a in alerts if "completado" in a["evento"].lower() or "se completó" in a["evento"].lower())
    esperando_grenergy = sum(1 for a in alerts if a["quien"] == "Esperando que Grenergy cargue un documento")
    esperando_cen = sum(1 for a in alerts if a["quien"] == "Esperando respuesta/revisión del Coordinador (CEN)")
    return {
        "completadas": completadas,
        "esperando_grenergy": esperando_grenergy,
        "esperando_cen": esperando_cen,
        "total": len(alerts),
    }


def build_recommendations(state: dict) -> list:
    """Mira el estado ACTUAL (no el historial) y arma recomendaciones para
    lo que está atrasado en este momento, p.ej. 'Plan Ener Tx tiene 6 días
    de atraso, se recomienda cargar el documento'."""
    today = datetime.now(timezone.utc).date()
    recs = []
    for task in state.get("active_tasks", {}).values():
        label = task["casilla"]
        for sub_text in task.get("sub_panels", []):
            parsed = scraper.parse_task_panel(sub_text)
            if not parsed or parsed["completada"] or parsed["quien_actua"] == "terceros":
                continue
            fecha_dt = scraper.parse_fecha_limite(parsed["fecha_limite"])
            if not (fecha_dt and fecha_dt < today):
                continue
            dias = (today - fecha_dt).days
            if parsed["quien_actua"] == "tu_empresa":
                recs.append(f"<strong>{label}</strong> tiene {dias} día(s) de atraso — se recomienda cargar el documento.")
            elif parsed["quien_actua"] == "coordinador":
                recs.append(f"<strong>{label}</strong> tiene {dias} día(s) de atraso esperando al CEN — se recomienda hacer seguimiento.")
    return recs


def render_recommendations(recs: list) -> str:
    if not recs:
        return ""
    items = "".join(f"<li style='margin-bottom:6px; color:#7a1f12;'>{r}</li>" for r in recs)
    return f"""
    <div style="margin-top:10px; padding:10px 14px; background:#fdeceb; border-radius:10px;">
      <div style="font-weight:600; font-size:12.5px; color:#7a1f12; margin-bottom:4px;">⚠️ Recomendaciones</div>
      <ul style="margin:0; padding-left:18px; font-size:12.5px;">{items}</ul>
    </div>
    """


def render_bar_chart(boxes: dict) -> str:
    leaf_boxes = [b for b in boxes.values() if not b.get("is_container")]
    n_completado = sum(1 for b in leaf_boxes if b["state"] == "completado")
    n_en_curso = sum(1 for b in leaf_boxes if b["state"] in ("en_curso", "en_curso_destacado"))
    n_pendiente = sum(1 for b in leaf_boxes if b["state"] == "pendiente")
    total = max(n_completado + n_en_curso + n_pendiente, 1)
    pct = lambda n: round(n / total * 100, 1)  # noqa: E731

    return f"""
    <div style="margin-top:8px;">
      <div style="display:flex; width:100%; height:10px; border-radius:6px; overflow:hidden;">
        <div style="width:{pct(n_completado)}%; background:#00cf78;"></div>
        <div style="width:{pct(n_en_curso)}%; background:#faa900;"></div>
        <div style="width:{pct(n_pendiente)}%; background:#9aa6a3;"></div>
      </div>
      <div style="font-size:11px; color:#6b7777; margin-top:5px;">
        {n_completado} completadas &middot; {n_en_curso} en curso &middot; {n_pendiente} no iniciadas
      </div>
    </div>
    """


def delta_badge(curr: int, prev: int, label: str) -> str:
    diff = curr - prev
    if diff > 0:
        arrow, color = f"↑{diff}", "#00cf78"
    elif diff < 0:
        arrow, color = f"↓{abs(diff)}", "#e0273a"
    else:
        arrow, color = "=", "#8a9591"
    return (
        f"<span style='font-size:11px; color:{color}; font-weight:600;'>{arrow} vs. semana anterior</span>"
        if prev or curr
        else ""
    )


def render_stat(value: int, label: str, color: str) -> str:
    return f"""
    <td align="center" style="padding:10px 6px;">
      <div style="font-size:24px; font-weight:700; color:{color};">{value}</div>
      <div style="font-size:11px; color:#6b7777; margin-top:2px;">{label}</div>
    </td>
    """


def render_email_html(rows: list) -> str:
    period_start = (datetime.now(timezone.utc) - timedelta(days=WEEK_DAYS)).strftime("%d-%m-%Y")
    period_end = datetime.now(timezone.utc).strftime("%d-%m-%Y")
    total_cambios = sum(r["stats"]["total"] for r in rows)

    sections = []
    for r in rows:
        s, sp = r["stats"], r["stats_prev"]
        if r["alerts"]:
            data_rows = "".join(
                f"""<tr>
                    <td style="padding:8px 6px; border-bottom:1px solid #e3e9e7; font-size:12.5px; color:#1b2b29;">{a['casilla']}</td>
                    <td style="padding:8px 6px; border-bottom:1px solid #e3e9e7; font-size:12.5px; color:#1b2b29;">{a['evento']}</td>
                    <td style="padding:8px 6px; border-bottom:1px solid #e3e9e7; font-size:12.5px; color:#04201f; font-weight:600;">{a['quien']}</td>
                    <td style="padding:8px 6px; border-bottom:1px solid #e3e9e7; font-size:12.5px; color:#1b2b29;">{a['fecha_limite']}</td>
                </tr>"""
                for a in r["alerts"]
            )
            header_rows = (
                "<tr>"
                "<th align='left' style=\"font-size:11px; color:#8a9591; padding:4px 6px; text-transform:uppercase;\">Casilla</th>"
                "<th align='left' style=\"font-size:11px; color:#8a9591; padding:4px 6px; text-transform:uppercase;\">Evento</th>"
                "<th align='left' style=\"font-size:11px; color:#8a9591; padding:4px 6px; text-transform:uppercase;\">Quién responde</th>"
                "<th align='left' style=\"font-size:11px; color:#8a9591; padding:4px 6px; text-transform:uppercase;\">Fecha límite</th>"
                "</tr>"
            )
            detail = f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0'>{header_rows}{data_rows}</table>"
        else:
            detail = (
                "<p style='margin:8px 0 0 0; color:#6b7777; font-style:italic;'>"
                "Sin cambios esta semana.</p>"
            )

        stats_table = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px; background:#f6f9f8; border-radius:10px;">
          <tr>
            {render_stat(s['completadas'], 'Completadas esta semana', '#00cf78')}
            {render_stat(s['esperando_grenergy'], 'Esperando carga de Grenergy', '#e67e22')}
            {render_stat(s['esperando_cen'], 'Esperando respuesta del CEN', '#04201f')}
          </tr>
          <tr>
            <td align="center" style="padding:0 6px 8px 6px;">{delta_badge(s['completadas'], sp['completadas'], 'completadas')}</td>
            <td align="center" style="padding:0 6px 8px 6px;">{delta_badge(s['esperando_grenergy'], sp['esperando_grenergy'], 'pendientes')}</td>
            <td align="center" style="padding:0 6px 8px 6px;">{delta_badge(s['esperando_cen'], sp['esperando_cen'], 'pendientes')}</td>
          </tr>
        </table>
        """

        sections.append(
            f"""
            <tr><td style="padding:20px 0 4px 0; border-top:1px solid #e3e9e7;">
              <div style="font-weight:600; color:#04201f; font-size:15px;">
                {r['name']} <span style="color:#8a9591; font-weight:400; font-size:12px;">#{r['correlativo']}</span>
              </div>
              {r['bar_chart']}
              {stats_table}
              {render_recommendations(r['recommendations'])}
            </td></tr>
            <tr><td style="padding:10px 0 4px 0;">{detail}</td></tr>
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
        <tr><td style="padding:24px 28px 4px 28px;">
          <div style="font-size:18px; font-weight:600; color:#04201f;">Resumen ejecutivo semanal &mdash; {scraper.GROUP_NAME}</div>
          <div style="font-size:12px; color:#8a9591; margin-top:4px;">Periodo {period_start} al {period_end} &middot; {total_cambios} cambio(s) en total</div>
        </td></tr>
        <tr><td style="padding:0 28px;">
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


def main():
    projects = json.loads((ROOT / "projects.json").read_text(encoding="utf-8"))
    rows = []
    for project in projects:
        project_id = project["id"]
        state = scraper.load_json(scraper.STATE_DIR / f"{project_id}.json") or {}
        alerts = alerts_in_window(project_id, WEEK_DAYS, 0)
        alerts_prev = alerts_in_window(project_id, WEEK_DAYS * 2, WEEK_DAYS)
        rows.append(
            {
                "name": state.get("name") or project_id,
                "correlativo": state.get("correlativo"),
                "alerts": alerts,
                "stats": summarize(alerts),
                "stats_prev": summarize(alerts_prev),
                "bar_chart": render_bar_chart(state.get("boxes", {})),
                "recommendations": build_recommendations(state),
            }
        )

    html = render_email_html(rows)
    logo_path = ASSETS_DIR / "grenergy-logo.png"
    try:
        scraper.send_html_email(f"Resumen ejecutivo semanal {scraper.GROUP_NAME}", html, logo_path=logo_path)
    except Exception as e:  # noqa: BLE001
        print(f"No se pudo enviar el resumen semanal por correo: {e}")
    print(html)


if __name__ == "__main__":
    main()
