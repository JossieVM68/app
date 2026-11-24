# Mi Búsqueda — buscador de empleo personal

Aplicación privada (un solo usuario) que:

- Lee el currículum y construye un perfil con IA (Claude).
- Busca vacantes todos los días en fuentes oficiales/públicas y con búsqueda web de IA.
- Aplica reglas firmes: distancia máxima a casa, pago mínimo por hora, presencial/híbrido.
- Puntúa cada vacante (0–100) según el perfil real.
- Genera para cada vacante: CV adaptado (Word, formato ATS), revisión ATS, carta de presentación,
  email de aplicación, email de seguimiento, nota de LinkedIn y preparación para entrevista.
- CRM: estados, fechas de aplicación, recordatorios de seguimiento, fechas límite, entrevistas y meta.

La IA nunca inventa experiencia: solo reorganiza y redacta lo que está en el perfil.

## Variables de entorno

| Variable | Para qué |
|---|---|
| `DATABASE_URL` | Postgres (Render la pone sola) |
| `SECRET_KEY` | Firma de la sesión (obligatoria) |
| `SETUP_CODE` | Código para crear la contraseña la primera vez |
| `ANTHROPIC_API_KEY` | IA: perfil, puntajes, documentos, búsqueda web |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | Opcional, fuente Adzuna |
| `SERPAPI_KEY` | Opcional, Google Jobs |
| `JOOBLE_API_KEY` | Opcional, Jooble |
| `USAJOBS_API_KEY` / `USAJOBS_EMAIL` | Opcional, USAJobs |
| `SMTP_HOST` `SMTP_USER` `SMTP_PASSWORD` `DIGEST_TO` | Opcional, resumen diario por correo |

## Local

```
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/uvicorn app.main:app --reload
```
