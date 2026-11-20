"""
engine_bridge.py - Conexion de OperaCore (api/) con ELIPSE ENGINE.

Flujo (el navegador NO cambia: sigue haciendo POST /elipse/chat/ al client y recibe {"html": ...}):
  client --POST /api/elipse/chat/--> api (este modulo) --POST /v1/chat--> Engine   (inicia la tarea)
                                     api  --GET /v1/task/<id> (sondeo hasta ~40 s)--> Engine
  Engine (cuando el modelo pide una tool) --POST /api/elipse/callback/<tool>/ firmado HMAC--> api
       api valida firma, ejecuta la consulta de solo lectura (engine_tools) y responde {"ok":..,"result":..}

Variables de entorno (api/.env):
  ELIPSE_USE_ENGINE        True                      (False = sigue el Elipse independiente de siempre)
  ELIPSE_ENGINE_URL        http://127.0.0.1:8001     (URL base del Engine, sin / al final)
  ELIPSE_API_KEY           elp_...                   (llave del perfil 'cmms'; solo vive en el servidor)
  ELIPSE_CALLBACK_SECRET   <secreto de set-callback> (el mismo que genero el Engine)
  ELIPSE_PROFILE           cmms                      (opcional)
  ELIPSE_INTERNAL_TOKEN    <cadena larga>            (opcional pero recomendado: la manda el client en X-Elipse-Internal)
  ELIPSE_MAX_SKEW          90                        (opcional; segundos de tolerancia de reloj)
  ELIPSE_WAIT_SECONDS      40                        (opcional; cuanto espera la respuesta del Engine)
  ELIPSE_RATE_PER_MIN      15                        (opcional; preguntas por minuto por usuario)
"""
import hashlib
import hmac
import html
import json
import logging
import re
import time
import urllib.error
import urllib.request

from decouple import config
from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import engine_tools

log = logging.getLogger("elipse")

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
MAX_PREGUNTA = 2000
MAX_MEMORIA = 10
MAX_CALLBACK_BODY = 64 * 1024


def _cfg():
    return {
        "url": config("ELIPSE_ENGINE_URL", default="").strip().rstrip("/"),
        "key": config("ELIPSE_API_KEY", default="").strip(),
        "secret": config("ELIPSE_CALLBACK_SECRET", default="").strip(),
        "perfil": config("ELIPSE_PROFILE", default="cmms").strip() or "cmms",
        "token": config("ELIPSE_INTERNAL_TOKEN", default="").strip(),
        "skew": config("ELIPSE_MAX_SKEW", default=90, cast=int),
        "wait": config("ELIPSE_WAIT_SECONDS", default=40, cast=int),
        "rate": config("ELIPSE_RATE_PER_MIN", default=15, cast=int),
        "timeout": config("ELIPSE_HTTP_TIMEOUT", default=15, cast=float),
        "voice_timeout": config("ELIPSE_VOICE_TIMEOUT", default=60, cast=float),
    }


def usar_engine() -> bool:
    return config("ELIPSE_USE_ENGINE", default=False, cast=bool)


# ------------------------------------------------------------------ firma HMAC (lado anfitrion)
# Misma formula que app/core/callback.py del Engine:
#   HMAC_SHA256(secret, f"{ts}.{perfil}.{tool}.{sha256_hex(body)}")
def firmar(secret, ts, perfil, tool, body: bytes) -> str:
    msg = "%s.%s.%s.%s" % (ts, perfil, tool, hashlib.sha256(body).hexdigest())
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def verificar(secret, perfil, tool, body: bytes, ts, sig, max_skew=90, ahora=None) -> bool:
    try:
        ts_i = int(ts)
    except (TypeError, ValueError):
        return False
    if abs((ahora if ahora is not None else time.time()) - ts_i) > max_skew:
        return False
    return hmac.compare_digest(firmar(secret, ts_i, perfil, tool, body), str(sig or ""))


# ------------------------------------------------------------------ cliente del Engine
class EngineError(Exception):
    def __init__(self, mensaje, status=502):
        super().__init__(mensaje)
        self.mensaje, self.status = mensaje, status


def _engine(method, path, payload=None):
    cfg = _cfg()
    if not cfg["url"] or not cfg["key"]:
        raise EngineError("Elipse no esta configurado en el servidor.", 503)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        cfg["url"] + path, data=data, method=method,
        headers={"Authorization": "Bearer " + cfg["key"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.warning("Engine respondio HTTP %s en %s: %s", e.code, path, e.read().decode("utf-8", "ignore")[:300])
        if e.code in (401, 403):
            raise EngineError("Elipse rechazo la llave del servidor. Avisa al administrador.", 502)
        if e.code == 409:
            raise EngineError("El perfil de Elipse aun no esta listo (falta sincronizar su configuracion).", 503)
        raise EngineError("Elipse no pudo procesar la solicitud.", 502)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log.warning("No se pudo contactar al Engine (%s): %s", path, e)
        raise EngineError("No se pudo conectar con Elipse. Intenta de nuevo en un momento.", 502)


# ------------------------------------------------------------------ respuesta -> HTML seguro
def _inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?![\*\w])", r"<em>\1</em>", s)
    return s


def _celdas(linea):
    return [c.strip() for c in linea.strip().strip("|").split("|")]


def markdown_a_html(texto: str) -> str:
    """Subconjunto de Markdown (parrafos, listas, tablas, **negrita**) con TODO el texto escapado."""
    texto = re.sub(r"<think>.*?</think>", "", texto or "", flags=re.S | re.I).strip()
    if not texto:
        return "<p>(sin respuesta)</p>"
    lineas, out, i = texto.splitlines(), [], 0
    parrafo, lista = [], None

    def cerrar():
        nonlocal parrafo, lista
        if parrafo:
            out.append("<p>%s</p>" % "<br>".join(_inline(x) for x in parrafo))
            parrafo = []
        if lista:
            out.append("</%s>" % lista)
            lista = None

    while i < len(lineas):
        s = lineas[i].strip()
        if not s:
            cerrar()
        elif s.startswith("|") and i + 1 < len(lineas) and re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", lineas[i + 1]) \
                and "-" in lineas[i + 1]:
            cerrar()
            cab = _celdas(s)
            i += 2
            filas = []
            while i < len(lineas) and lineas[i].strip().startswith("|"):
                filas.append(_celdas(lineas[i]))
                i += 1
            out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (
                "".join("<th>%s</th>" % _inline(c) for c in cab),
                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(c) for c in f) for f in filas)))
            continue
        elif re.match(r"^#{1,4}\s+", s):
            cerrar()
            out.append("<p><strong>%s</strong></p>" % _inline(re.sub(r"^#{1,4}\s+", "", s)))
        elif re.match(r"^[-*\u2022]\s+", s) or re.match(r"^\d+[.)]\s+", s):
            tipo = "ul" if re.match(r"^[-*\u2022]\s+", s) else "ol"
            if parrafo:
                out.append("<p>%s</p>" % "<br>".join(_inline(x) for x in parrafo))
                parrafo = []
            if lista != tipo:
                if lista:
                    out.append("</%s>" % lista)
                out.append("<%s>" % tipo)
                lista = tipo
            out.append("<li>%s</li>" % _inline(re.sub(r"^([-*\u2022]|\d+[.)])\s+", "", s)))
        else:
            if lista:
                out.append("</%s>" % lista)
                lista = None
            parrafo.append(s)
        i += 1
    cerrar()
    return "".join(out)


# ------------------------------------------------------------------ utilidades
def _json(data, status=200):
    return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})


def _interno_ok(request) -> bool:
    """Si hay ELIPSE_INTERNAL_TOKEN, solo el client (que lo conoce) puede usar estos endpoints."""
    token = _cfg()["token"]
    if not token:
        return True
    return hmac.compare_digest(request.headers.get("X-Elipse-Internal", ""), token)


def _limite_ok(actor):
    clave = "elipse:rl:%s:%d" % (actor, int(time.time() // 60))
    cache.add(clave, 0, 90)
    try:
        return cache.incr(clave) <= _cfg()["rate"]
    except ValueError:
        return True


def _actor_activo(actor: str) -> bool:
    """El actor debe ser un trabajador existente y activo."""
    if not actor or len(actor) > 15:
        return False
    with connection.cursor() as cur:
        cur.execute("SELECT 1 FROM trabajador WHERE numeroNomina=%s AND actividad=1 LIMIT 1", [actor])
        return cur.fetchone() is not None


def _mensaje_con_memoria(pregunta, memoria):
    notas = [str(m).strip()[:200] for m in (memoria or []) if str(m).strip()][:MAX_MEMORIA]
    if not notas:
        return pregunta
    return "Notas del usuario (contexto que pidio recordar):\n- %s\n\nPregunta: %s" % ("\n- ".join(notas), pregunta)


# ------------------------------------------------------------------ vistas (las llama el client)
@csrf_exempt
@require_POST
def chat(request):
    """Mismo contrato que ElipseChatAPIView: recibe {pregunta, historial, memoria, session, actor} y devuelve {"html"}."""
    if not _interno_ok(request):
        return _json({"error": "No autorizado."}, 403)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return _json({"error": "Solicitud invalida."}, 400)
    pregunta = str(body.get("pregunta") or "").strip()[:MAX_PREGUNTA]
    if not pregunta:
        return _json({"error": "Escribe una pregunta."})
    actor = str(body.get("actor") or "").strip()
    sesion = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("session") or ""))[:60] or "anon"
    if not _actor_activo(actor):
        return _json({"error": "Tu usuario no tiene permiso para usar Elipse."}, 403)
    if not _limite_ok(actor):
        return _json({"error": "Demasiadas preguntas seguidas. Espera un momento."}, 429)

    cfg = _cfg()
    try:
        payload = {"message": _mensaje_con_memoria(pregunta, body.get("memoria")),
                   "session": sesion, "actor": actor}
        # El client manda "voz" (boton de voz del navegador): True = que hable, False = que calle.
        quiere_voz = body.get("voz") if isinstance(body.get("voz"), bool) else body.get("speak")
        if isinstance(quiere_voz, bool):
            payload["speak"] = quiere_voz
        resp = _engine("POST", "/v1/chat", payload)
        task_id = str(resp.get("task_id") or "")
        if not TASK_ID_RE.match(task_id):
            return _json({"error": "Respuesta inesperada de Elipse."}, 502)
        limite = time.time() + cfg["wait"]
        while time.time() < limite:
            data = _engine("GET", "/v1/task/" + task_id)
            estado = data.get("status")
            resultado = data.get("result") if isinstance(data.get("result"), dict) else {}
            if estado == "completado":
                voz = resultado.get("voice") if isinstance(resultado.get("voice"), dict) else None
                return _json({"html": markdown_a_html(str(resultado.get("reply") or "")),
                              "intent": "engine", "modelo": "Elipse Engine",
                              "task_id": task_id,                 # el client lo usa para pedir el audio
                              "voz": voz,                         # {speak, gain, mime} o null (nombre que lee elipse.js)
                              "voice": voz})
            if estado == "cancelado":
                return _json({"error": "La consulta fue cancelada."})
            if estado != "en_progreso":
                log.warning("Tarea %s termino con estado %s: %s", task_id, estado, str(resultado)[:300])
                return _json({"error": "Elipse no pudo completar la consulta."})
            time.sleep(0.8)
        return _json({"error": "Elipse tardo demasiado en responder. Intenta de nuevo."}, 504)
    except EngineError as e:
        return _json({"error": e.mensaje}, e.status)


@csrf_exempt
@require_GET
def estado(request):
    """Indicador del sidebar: ¿el Engine esta configurado y responde?"""
    cfg = _cfg()
    if not (cfg["url"] and cfg["key"]):
        return _json({"ok": False, "motivo": "sin_configurar"})
    try:
        with urllib.request.urlopen(urllib.request.Request(cfg["url"] + "/v1/status"), timeout=5) as res:
            return _json({"ok": res.status == 200})
    except Exception:
        return _json({"ok": False, "motivo": "engine_no_responde"})


# ------------------------------------------------------------------ voz (audio de una respuesta)
@require_GET
def voz(request, task_id):
    """Proxy del audio: el navegador/client pide /api/elipse/voz/<task_id>/ y aqui se agrega la llave del
    perfil (nunca viaja al navegador). Responde 204 si no hay nada que decir."""
    if not _interno_ok(request):
        return _json({"error": "No autorizado."}, 403)
    if not TASK_ID_RE.match(task_id):
        return _json({"error": "Tarea invalida."}, 400)
    if not _actor_activo(str(request.GET.get("actor") or "").strip()):      # el client manda ?actor=<nomina>
        return _json({"error": "Tu usuario no tiene permiso para usar Elipse."}, 403)
    cfg = _cfg()
    if not cfg["url"] or not cfg["key"]:
        return _json({"error": "Elipse no esta configurado en el servidor."}, 503)
    force = "?force=1" if request.GET.get("force") in ("1", "true") else ""
    req = urllib.request.Request(cfg["url"] + "/v1/voice/" + task_id + "/stream" + force,
                                 headers={"Authorization": "Bearer " + cfg["key"]})
    try:
        res = urllib.request.urlopen(req, timeout=cfg["voice_timeout"])
    except urllib.error.HTTPError as e:
        log.warning("Voz: el Engine respondio HTTP %s: %s", e.code, e.read().decode("utf-8", "ignore")[:300])
        if e.code == 404:
            return _json({"error": "El audio ya no esta disponible."}, 404)
        if e.code in (401, 403):
            return _json({"error": "Elipse rechazo la llave del servidor."}, 502)
        return _json({"error": "La voz no esta disponible en este momento."}, 503)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log.warning("Voz: no se pudo contactar al Engine: %s", e)
        return _json({"error": "No se pudo conectar con Elipse."}, 502)

    if res.status == 204:                      # perfil sin voz permitida, o nada hablable
        res.close()
        return HttpResponse(status=204)

    def _trozos():
        leer = getattr(res, "read1", res.read)
        try:
            while True:
                c = leer(8192)
                if not c:
                    break
                yield c
        finally:
            res.close()

    out = StreamingHttpResponse(_trozos(), content_type=res.headers.get("Content-Type", "audio/mpeg"))
    out["Cache-Control"] = "no-store"
    if res.headers.get("X-Voice-Gain"):
        out["X-Voice-Gain"] = res.headers["X-Voice-Gain"]
        out["Access-Control-Expose-Headers"] = "X-Voice-Gain"
    return out


# ------------------------------------------------------------------ callback del Engine
def _resp(ok, **kw):
    return _json(dict(ok=ok, **kw))


@csrf_exempt
@require_POST
def callback(request, tool):
    cfg = _cfg()
    if not cfg["secret"]:
        return _resp(False, error="callback no configurado")
    body = request.body
    if len(body) > MAX_CALLBACK_BODY:
        return _resp(False, error="cuerpo demasiado grande")
    if not verificar(cfg["secret"], cfg["perfil"], tool, body,
                     request.headers.get("X-Elipse-Ts"), request.headers.get("X-Elipse-Sig"), cfg["skew"]):
        log.warning("Callback con firma invalida (tool=%s, ip=%s)", tool, request.META.get("REMOTE_ADDR"))
        return _json({"ok": False, "error": "firma invalida"}, 401)
    try:
        payload = json.loads(body)
    except ValueError:
        return _json({"ok": False, "error": "JSON invalido"}, 400)
    if not isinstance(payload, dict) or payload.get("tool") != tool:
        return _json({"ok": False, "error": "tool no coincide con la URL"}, 400)
    call_id = str(payload.get("call_id") or "")[:64]
    if not call_id or not cache.add("elipse:cb:" + call_id, 1, cfg["skew"] * 2):
        return _json({"ok": False, "error": "llamada repetida"}, 409)      # anti-replay
    if not _actor_activo(str(payload.get("actor") or "")):
        return _json({"ok": False, "error": "usuario sin permiso para esta consulta"}, 403)
    fn = engine_tools.TOOLS.get(tool)
    if fn is None:
        return _json({"ok": False, "error": "herramienta desconocida"}, 404)
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    try:
        return _resp(True, result=fn(args))
    except engine_tools.ToolError as e:
        return _resp(False, error=str(e))
    except Exception:
        log.exception("Fallo la tool %s", tool)
        return _resp(False, error="error interno al consultar la base de datos")
