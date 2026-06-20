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


def week_changes_for(project_id: str) -> list:
    log = scraper.load_json(LOG_DIR / f"{project_id}.json") or []
    cutoff = datetime.now(timezone.utc) - timedelta(days=WEEK_DAYS)
    out = []
    for entry in log:
        if datetime.fromisoformat(entry["timestamp"]) >= cutoff:
            out.extend(entry["changes"])
    return out


def render_email_html(rows: list) -> str:
    period_start = (datetime.now(timezone.utc) - timedelta(days=WEEK_DAYS)).strftime("%d-%m-%Y")
    period_end = datetime.now(timezone.utc).strftime("%d-%m-%Y")

    sections = []
    for r in rows:
        if r["changes"]:
            items = "".join(f"<li style='margin-bottom:6px;'>{c}</li>" for c in r["changes"])
            body = f"<ul style='margin:8px 0 0 0; padding-left:20px; color:#1b2b29;'>{items}</ul>"
        else:
            body = (
                "<p style='margin:8px 0 0 0; color:#6b7777; font-style:italic;'>"
                "Sin cambios esta semana.</p>"
            )
        sections.append(
            f"""
            <tr><td style="padding:18px 0; border-bottom:1px solid #e3e9e7;">
              <div style="font-weight:600; color:#04201f; font-size:15px;">
                {r['name']} <span style="color:#8a9591; font-weight:400; font-size:12px;">#{r['correlativo']}</span>
              </div>
              {body}
            </td></tr>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="es">
<body style="margin:0; padding:0; background:#eef3f1; font-family:Arial, Helvetica, sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef3f1; padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background:white; border-radius:14px; overflow:hidden;">
        <tr><td style="background:#04201f; padding:24px 28px;">
          <img src="cid:logo" alt="Grenergy" height="28">
        </td></tr>
        <tr><td style="padding:24px 28px 8px 28px;">
          <div style="font-size:18px; font-weight:600; color:#04201f;">Resumen ejecutivo semanal &mdash; PGP</div>
          <div style="font-size:12px; color:#8a9591; margin-top:4px;">Periodo {period_start} al {period_end}</div>
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
                "changes": week_changes_for(project_id),
            }
        )

    html = render_email_html(rows)
    logo_path = ASSETS_DIR / "grenergy-logo.png"
    scraper.send_html_email("Resumen ejecutivo semanal PGP", html, logo_path=logo_path)
    print(html)


if __name__ == "__main__":
    main()
