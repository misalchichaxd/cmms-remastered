"""
python manage.py elipse_spec --dir .

Exporta el spec y el techo (caps) del perfil 'cmms' para el Engine:
    cmms_perfil.json  -> python -m app.manage_profiles create cmms --spec cmms_perfil.json
    cmms_caps.json    -> python -m app.manage_keys create django-cmms --profile cmms --caps cmms_caps.json
"""
import json
import os

from django.core.management.base import BaseCommand

from apps.elipse import engine_spec


class Command(BaseCommand):
    help = "Exporta cmms_perfil.json y cmms_caps.json para registrar el perfil en ELIPSE ENGINE."

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=".", help="Carpeta de salida (default: actual).")

    def handle(self, *args, **opts):
        out = os.path.abspath(opts["dir"])
        os.makedirs(out, exist_ok=True)
        for nombre, data in (("cmms_perfil.json", engine_spec.perfil_spec()),
                             ("cmms_caps.json", engine_spec.perfil_caps())):
            ruta = os.path.join(out, nombre)
            with open(ruta, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.stdout.write(self.style.SUCCESS("Escrito: " + ruta))
        n = len(engine_spec.perfil_spec()["tools"]["host"])
        self.stdout.write("Tools del anfitrion exportadas: %d" % n)
