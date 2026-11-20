from django.urls import path, re_path

from . import engine_bridge, views

app_name = "elipse"

# Con ELIPSE_USE_ENGINE=True el chat y el indicador de estado pasan por el Engine;
# con False todo sigue como antes (Elipse independiente con Groq).
_engine = engine_bridge.usar_engine()

urlpatterns = [
    path("chat/", engine_bridge.chat if _engine else views.ElipseChatAPIView.as_view(), name="chat"),
    path("estado/", engine_bridge.estado if _engine else views.ElipseEstadoAPIView.as_view(), name="estado"),
    # El Engine hace POST a <callback_url>/<tool> SIN barra final; un POST no sobrevive al redirect de
    # APPEND_SLASH, asi que la barra final es opcional.
    re_path(r"^callback/(?P<tool>[a-z][a-z0-9_]{1,47})/?$", engine_bridge.callback, name="callback"),
    path("voz/<str:task_id>/", engine_bridge.voz, name="voz"),
    path("sugerencias/", views.ElipseSugerenciasAPIView.as_view(), name="sugerencias"),
    # Estas 3 siguen con su logica propia (Groq): rellenan formularios, no son chat.
    path("autocompletar-falla/", views.ElipseAutocompletarFallaAPIView.as_view(), name="autocompletar_falla"),
    path("autocompletar-orden/", views.ElipseAutocompletarOrdenAPIView.as_view(), name="autocompletar_orden"),
    path("sugerencia-diagnostico/", views.ElipseSugerenciaDiagnosticoAPIView.as_view(), name="sugerencia_diagnostico"),
]
