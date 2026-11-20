import json

import requests
from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views import generic

API_URL = f"{settings.API_BASE_URL}/elipse"

# Sesion HTTP a nivel de modulo: reusa la conexion TCP con el api/.
# Aqui no se cachea nada: es un chat, cada pregunta es distinta.
SESSION = requests.Session()


def _headers_internos():
    """Token compartido client <-> api para los endpoints de Elipse que leen datos (ELIPSE_INTERNAL_TOKEN)."""
    token = getattr(settings, "ELIPSE_INTERNAL_TOKEN", "")
    return {"X-Elipse-Internal": token} if token else {}

# Las sugerencias si se cachean: son una constante del api que casi nunca
# cambia, y las pide cada carga de la pantalla.
SUGERENCIAS_TTL = 60 * 60  # 1 hora


class Index(generic.View):
    """Pantalla de chat de Elipse. Solo para sesion iniciada (mismo
    candado que admin_dashboard)."""

    template_name = "elipse/index.html"

    def get(self, request):
        usuario = request.session.get("usuario")
        if not usuario:
            messages.warning(request, "Inicia sesión para continuar.")
            return redirect("usuarios:index")
        return render(request, self.template_name, {"seccion": "elipse", "usuario": usuario})


class Chat(generic.View):
    """Recibe la pregunta del navegador y la reenvia al api/. Ademas manda QUIEN pregunta (actor) y el hilo
    de conversacion (session), que el Engine necesita para el historial y los permisos."""

    def post(self, request):
        usuario = request.session.get("usuario")
        if not usuario:
            return JsonResponse({"error": "Sesión expirada, vuelve a iniciar sesión."}, status=401)

        try:
            body = json.loads(request.body)
        except ValueError:
            return JsonResponse({"error": "Petición inválida."}, status=400)

        actor = str(usuario.get("numeroNomina") or "")
        # El JS manda el historial recortado; si viene vacio es chat nuevo ("Nueva conversacion" o primera vez):
        # se abre un hilo nuevo para que el Engine no arrastre la conversacion anterior.
        if not body.get("historial"):
            request.session["elipse_hilo"] = int(request.session.get("elipse_hilo", 0)) + 1
        body["actor"] = actor
        body["session"] = "u%s-%d" % (actor, int(request.session.get("elipse_hilo", 0)))

        try:
            resp = SESSION.post(f"{API_URL}/chat/", json=body, headers=_headers_internos(), timeout=50)
            return JsonResponse(resp.json(), status=resp.status_code)
        except requests.exceptions.RequestException:
            return JsonResponse({"error": "No se pudo conectar con el servidor de Elipse."}, status=502)


class Voz(generic.View):
    """Audio de una respuesta de Elipse. El navegador pide /elipse/voz/<task_id>/ y el client lo trae del api/
    (que lo trae del Engine): ni la llave del Engine ni el token interno salen del servidor."""

    def get(self, request, task_id):
        usuario = request.session.get("usuario")
        if not usuario:
            return JsonResponse({"error": "Sesión expirada."}, status=401)
        actor = str(usuario.get("numeroNomina") or "")
        try:
            resp = SESSION.get(f"{API_URL}/voz/{task_id}/", params={"actor": actor},
                               headers=_headers_internos(), timeout=70)
        except requests.exceptions.RequestException:
            return JsonResponse({"error": "No se pudo conectar con el servidor de Elipse."}, status=502)
        if resp.status_code == 204:
            return HttpResponse(status=204)
        if resp.status_code != 200:
            return JsonResponse({"error": "No se pudo generar la voz."}, status=resp.status_code)
        out = HttpResponse(resp.content, content_type=resp.headers.get("Content-Type", "audio/mpeg"))
        out["Cache-Control"] = "no-store"
        return out


class Estado(generic.View):
    """Endpoint ligero para el indicador del sidebar: valida sesion y
    pregunta al api/ si la IA (Groq) realmente esta disponible."""

    def get(self, request):
        usuario = request.session.get("usuario")
        if not usuario:
            return JsonResponse({"ok": False, "motivo": "sin_sesion"}, status=401)

        try:
            resp = SESSION.get(f"{API_URL}/estado/", timeout=5)
            data = resp.json()
            ok = resp.status_code < 500 and bool(data.get("ok"))
        except (requests.exceptions.RequestException, ValueError):
            ok = False

        return JsonResponse({"ok": ok})


class AutocompletarFalla(generic.View):
    """Reenvia al api/ la descripcion libre de una falla para que Elipse
    proponga los campos del formulario de reporte de falla. Mismo patron
    que Chat: solo reenvia, no toca la BD."""

    def post(self, request):
        usuario = request.session.get("usuario")
        if not usuario:
            return JsonResponse({"error": "Sesión expirada, vuelve a iniciar sesión."}, status=401)

        try:
            body = json.loads(request.body)
        except ValueError:
            return JsonResponse({"error": "Petición inválida."}, status=400)

        try:
            resp = SESSION.post(f"{API_URL}/autocompletar-falla/", json=body, timeout=25)
            return JsonResponse(resp.json(), status=resp.status_code)
        except requests.exceptions.RequestException:
            return JsonResponse({"error": "No se pudo conectar con el servidor de Elipse."}, status=502)


class SugerenciaDiagnostico(generic.View):
    """Reenvia sintoma+maquina al api/ para la sugerencia de diagnostico
    (RF-28). Mismo patron que AutocompletarFalla: solo reenvia."""

    def post(self, request):
        usuario = request.session.get("usuario")
        if not usuario:
            return JsonResponse({"error": "Sesión expirada, vuelve a iniciar sesión."}, status=401)

        try:
            body = json.loads(request.body)
        except ValueError:
            return JsonResponse({"error": "Petición inválida."}, status=400)

        try:
            resp = SESSION.post(f"{API_URL}/sugerencia-diagnostico/", json=body, timeout=15)
            return JsonResponse(resp.json(), status=resp.status_code)
        except requests.exceptions.RequestException:
            return JsonResponse({"error": "No se pudo conectar con el servidor de Elipse."}, status=502)


class AutocompletarOrden(generic.View):
    """Reenvia al api/ la descripcion libre de una orden de mantenimiento
    para que Elipse proponga los campos del formulario de nueva orden.
    Mismo patron que AutocompletarFalla: solo reenvia, no toca la BD."""

    def post(self, request):
        usuario = request.session.get("usuario")
        if not usuario:
            return JsonResponse({"error": "Sesión expirada, vuelve a iniciar sesión."}, status=401)

        try:
            body = json.loads(request.body)
        except ValueError:
            return JsonResponse({"error": "Petición inválida."}, status=400)

        try:
            resp = SESSION.post(f"{API_URL}/autocompletar-orden/", json=body, timeout=25)
            return JsonResponse(resp.json(), status=resp.status_code)
        except requests.exceptions.RequestException:
            return JsonResponse({"error": "No se pudo conectar con el servidor de Elipse."}, status=502)


class Sugerencias(generic.View):
    """Preguntas rapidas que se muestran como chips en la bienvenida.

    Las define el api (PREGUNTAS_RAPIDAS) para no tener la misma lista
    duplicada en Python y en JS."""

    def get(self, request):
        if not request.session.get("usuario"):
            return JsonResponse({"sugerencias": []}, status=401)

        data = cache.get("elipse_sugerencias")
        if data is None:
            try:
                resp = SESSION.get(f"{API_URL}/sugerencias/", timeout=5)
                data = resp.json()
                cache.set("elipse_sugerencias", data, SUGERENCIAS_TTL)
            except (requests.exceptions.RequestException, ValueError):
                # Sin chips la pantalla sigue siendo usable: se escribe y ya.
                return JsonResponse({"sugerencias": []})
        return JsonResponse(data)