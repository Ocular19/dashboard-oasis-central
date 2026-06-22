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
from datetime import datetime, timezone, timedelta
from pathlib import Path

import scraper

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "data" / "log"
ASSETS_DIR = ROOT / "docs" / "assets"
WEEK_DAYS = 7


def week_alerts_for(project_id: str) -> list:
    """Junta los 'alerts' estructurados (casilla/evento/quién/fecha) de los
    últimos 7 días. Si una entrada vieja del log no tiene 'alerts' (formato
    anterior), arma uno básico a partir del texto para no perder datos."""
    log = scraper.load_json(LOG_DIR / f"{project_id}.json") or []
    cutoff = datetime.now(timezone.utc) - timedelta(days=WEEK_DAYS)
    out = []
    for entry in log:
        if datetime.fromisoformat(entry["timestamp"]) < cutoff:
            continue
        alerts = entry.get("alerts")
        if alerts:
            out.extend(alerts)
        else:
            for c in entry.get("changes", []):
                out.append({"casilla": "-", "evento": c, "quien": "-", "fecha_limite": "-"})
    return out


def render_bar_chart(boxes: dict) -> str:
    """Barra horizontal simple (solo CSS, sin imágenes) con la proporción de
    casillas completadas / en curso / no iniciadas del proyecto."""
    n_completado = sum(1 for b in boxes.values() if b["state"] == "completado")
    n_en_curso = sum(1 for b in boxes.values() if b["state"] in ("en_curso", "en_curso_destacado"))
    n_pendiente = sum(1 for b in boxes.values() if b["state"] == "pendiente")
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


def render_email_html(rows: list) -> str:
    period_start = (datetime.now(timezone.utc) - timedelta(days=WEEK_DAYS)).strftime("%d-%m-%Y")
    period_end = datetime.now(timezone.utc).strftime("%d-%m-%Y")
    total_cambios = sum(len(r["alerts"]) for r in rows)

    sections = []
    for r in rows:
        if r["alerts"]:
            header_rows = (
                "<tr>"
                "<th align='left' style=\"font-size:11px; color:#8a9591; padding:4px 6px; text-transform:uppercase;\">Casilla</th>"
                "<th align='left' style=\"font-size:11px; color:#8a9591; padding:4px 6px; text-transform:uppercase;\">Evento</th>"
                "<th align='left' style=\"font-size:11px; color:#8a9591; padding:4px 6px; text-transform:uppercase;\">Quién responde</th>"
                "<th align='left' style=\"font-size:11px; color:#8a9591; padding:4px 6px; text-transform:uppercase;\">Fecha límite</th>"
                "</tr>"
            )
            data_rows = "".join(
                f"""<tr>
                    <td style="padding:8px 6px; border-bottom:1px solid #e3e9e7; font-size:12.5px; color:#1b2b29;">{a['casilla']}</td>
                    <td style="padding:8px 6px; border-bottom:1px solid #e3e9e7; font-size:12.5px; color:#1b2b29;">{a['evento']}</td>
                    <td style="padding:8px 6px; border-bottom:1px solid #e3e9e7; font-size:12.5px; color:#04201f; font-weight:600;">{a['quien']}</td>
                    <td style="padding:8px 6px; border-bottom:1px solid #e3e9e7; font-size:12.5px; color:#1b2b29;">{a['fecha_limite']}</td>
                </tr>"""
                for a in r["alerts"]
            )
            body = f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0'>{header_rows}{data_rows}</table>"
        else:
            body = (
                "<p style='margin:8px 0 0 0; color:#6b7777; font-style:italic;'>"
                "Sin cambios esta semana.</p>"
            )
        sections.append(
            f"""
            <tr><td style="padding:20px 0 4px 0; border-top:1px solid #e3e9e7;">
              <div style="font-weight:600; color:#04201f; font-size:15px;">
                {r['name']} <span style="color:#8a9591; font-weight:400; font-size:12px;">#{r['correlativo']}</span>
              </div>
              {r['bar_chart']}
            </td></tr>
            <tr><td style="padding:6px 0 4px 0;">{body}</td></tr>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="es">
<body style="margin:0; padding:0; background:#eef3f1; font-family:Arial, Helvetica, sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef3f1; padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="660" cellpadding="0" cellspacing="0" style="background:white; border-radius:14px; overflow:hidden;">
        <tr><td style="background:#04201f; padding:24px 28px;">
          <img src="cid:logo" alt="Grenergy" height="28">
        </td></tr>
        <tr><td style="padding:24px 28px 4px 28px;">
          <div style="font-size:18px; font-weight:600; color:#04201f;">Resumen ejecutivo semanal &mdash; PGP</div>
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
        rows.append(
            {
                "name": state.get("name") or project_id,
                "correlativo": state.get("correlativo"),
                "alerts": week_alerts_for(project_id),
                "bar_chart": render_bar_chart(state.get("boxes", {})),
            }
        )

    html = render_email_html(rows)
    logo_path = ASSETS_DIR / "grenergy-logo.png"
    try:
        scraper.send_html_email("Resumen ejecutivo semanal PGP", html, logo_path=logo_path)
    except Exception as e:  # noqa: BLE001
        print(f"No se pudo enviar el resumen semanal por correo: {e}")
    print(html)


if __name__ == "__main__":
    main()
