# Revisor de Proyectos PGP (pgp.coordinador.cl)

Revisa automáticamente 3 veces al día el estado de los proyectos indicados en
`projects.json`, detecta cambios de estado en las casillas del diagrama y en
la tarea actualmente activa de cada una, envía un correo cuando hay cambios y
publica un dashboard estático (con la marca Grenergy) con el historial de las
últimas 72 horas. Además, cada lunes envía un resumen ejecutivo semanal por
correo con los cambios de los últimos 7 días por proyecto (o "Sin cambios" si
no tuvo novedades).

## Cómo agregar o quitar proyectos

Edita `projects.json`. Cada proyecto es:

```json
{
  "id": "6949d5a236d9ef5f10f1a978",
  "url": "https://pgp.coordinador.cl/irequests/6949d5a236d9ef5f10f1a978"
}
```

El `id` es la parte final de la URL del proyecto. Guarda el archivo, haz commit
y push (o edítalo directo en GitHub web) — la próxima corrida programada (o
una corrida manual) usará la lista actualizada.

## Configuración inicial en GitHub (una sola vez)

1. Crea el repositorio en tu cuenta (`Ocular19`) y sube este contenido:
   ```
   git remote add origin https://github.com/Ocular19/<nombre-repo>.git
   git add -A
   git commit -m "Setup inicial"
   git branch -M main
   git push -u origin main
   ```

2. **Activa GitHub Pages**: Settings → Pages → Source: "Deploy from a branch",
   branch `main`, carpeta `/docs`. La URL del dashboard quedará en
   `https://ocular19.github.io/<nombre-repo>/`.

3. **Crea una contraseña de aplicación de Gmail** (necesaria porque Gmail no
   permite usar la contraseña normal desde scripts):
   - Ve a https://myaccount.google.com/apppasswords (requiere verificación en
     dos pasos activada en la cuenta).
   - Genera una contraseña de aplicación para "Correo".
   - Cópiala (16 caracteres sin espacios).

4. **Agrega los secrets del repo**: Settings → Secrets and variables →
   Actions → New repository secret:
   - `GMAIL_USER`: tu cuenta de Gmail remitente.
   - `GMAIL_APP_PASSWORD`: la contraseña de aplicación generada en el paso 3.
   - `MAIL_TO`: `oliveroscarlos19@gmail.com` (o varios separados por coma).

5. El workflow `.github/workflows/check.yml` ya está programado para correr a
   las 09:00, 14:00 y 19:00 hora de Chile. También puedes ejecutarlo manualmente
   desde la pestaña "Actions" → "Revisar proyectos PGP" → "Run workflow".

6. El workflow `.github/workflows/weekly_summary.yml` envía el resumen
   ejecutivo semanal todos los lunes a las 08:00 hora de Chile (usa los mismos
   secrets de Gmail). También se puede correr manualmente desde "Actions" →
   "Resumen ejecutivo semanal PGP" → "Run workflow".

## Cómo funciona

- `scraper.py` abre cada URL con un navegador headless (Playwright), porque la
  página es una aplicación que renderiza todo con JavaScript.
- Lee el color de cada casilla del diagrama (verde = completado, gris =
  pendiente, naranjo/amarillo = en curso) y, para las casillas en curso, hace
  clic para leer el panel "Listado de Requerimientos" (responsable, plazo,
  fecha límite, estado de la tarea actualmente activa).
- Compara contra el estado guardado en la corrida anterior (`data/state/`).
- Si algo cambió, describe el cambio en lenguaje claro (ej. "avanzó de etapa
  — ahora esperando respuesta del Coordinador, fecha límite 10-07-2026") y lo
  guarda en dos lugares: `data/history/` (se descartan entradas de más de 72
  horas, es lo que se ve en el dashboard) y `data/log/` (se conserva 35 días,
  es la fuente del resumen semanal). También envía un correo con el resumen.
- Ignora a propósito los pasos que dependen de una Empresa Involucrada
  (tercero): solo importa si lo pendiente depende de Grenergy o del CEN.
- Regenera `docs/index.html`: dashboard con la marca Grenergy, una tarjeta por
  proyecto, y las casillas en curso marcadas con "Actualizado (últimas 72h)"
  si tuvieron un cambio reciente. Pasadas las 72 horas, la marca desaparece y
  la casilla vuelve a verse como cualquier otra pendiente normal.
- `weekly_summary.py` lee `data/log/` de los últimos 7 días y envía un correo
  HTML con la misma identidad visual, listando los cambios por proyecto (o
  "Sin cambios esta semana" si no hubo novedades) para poder comparar semana
  a semana.

## Probar en tu computador antes de subir

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python scraper.py
open docs/index.html
```

La primera corrida nunca genera "cambios" (no hay estado previo con qué
comparar) — es normal. Los cambios aparecerán desde la segunda corrida en
adelante.
