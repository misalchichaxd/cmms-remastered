"""
engine_tools.py - Herramientas de OperaCore (CMMS) que ELIPSE ENGINE puede llamar. SOLO LECTURA.

Cada tool recibe argumentos ya validados (nunca SQL) y ejecuta consultas FIJAS y parametrizadas.
Nunca se expone: trabajador.contraseña, usuario, correo, telefono, foto.

TOOLS  -> {nombre: funcion(args) -> dict}
SCHEMA -> definicion de las tools para el spec del perfil (tools.host)
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import connection

MAX_FILAS = 40
MAX_RANGO_DIAS = 800


class ToolError(Exception):
    """Mensaje corto y seguro: se devuelve al Engine como {"ok": false, "error": ...}."""


# ------------------------------------------------------------------ utilidades
def _hoy():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(settings.TIME_ZONE)).date()
    except Exception:
        return date.today()


def _val(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, (date, time)):
        return v.isoformat()
    if isinstance(v, timedelta):
        t = int(v.total_seconds())
        return "%02d:%02d" % (t // 3600, (t % 3600) // 60)
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def _q(sql, params=None):
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, (_val(v) for v in row))) for row in cur.fetchall()]


def _int(args, key, default=None, lo=1, hi=10**9):
    v = args.get(key, default)
    if v in (None, ""):
        return default
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ToolError("'%s' debe ser un numero entero." % key)
    if not lo <= v <= hi:
        raise ToolError("'%s' debe estar entre %d y %d." % (key, lo, hi))
    return v


def _fecha(args, key):
    v = args.get(key)
    if v in (None, ""):
        return None
    try:
        return datetime.strptime(str(v).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ToolError("'%s' debe tener formato AAAA-MM-DD." % key)


def _texto(args, key, maxlen=60):
    v = args.get(key)
    if v is None:
        return None
    v = " ".join(str(v).split())
    return v[:maxlen] or None


def _opcion(args, key, validas):
    """Valor de un enum (minusculas) o None si no viene."""
    v = args.get(key)
    if v in (None, ""):
        return None
    v = str(v).strip().lower()
    if v not in validas:
        raise ToolError("'%s' debe ser uno de: %s." % (key, ", ".join(validas)))
    return v


def _rango(desde, hasta, d_def, h_def):
    """(desde, hasta) inclusivos -> (inicio, fin_exclusivo) como 'AAAA-MM-DD'."""
    d, h = desde or d_def, hasta or h_def
    if h < d:
        d, h = h, d
    if (h - d).days > MAX_RANGO_DIAS:
        raise ToolError("El rango maximo es de %d dias." % MAX_RANGO_DIAS)
    return d.isoformat(), (h + timedelta(days=1)).isoformat()


def _like(expr, texto):
    """Cada palabra debe aparecer en `expr` (AND). Escapa % _ \\."""
    palabras = [p for p in (texto or "").lower().split() if p][:4]
    if not palabras or sum(len(p) for p in palabras) < 2:
        raise ToolError("Escribe al menos 2 letras para buscar.")
    cl, ps = [], []
    for p in palabras:
        p = p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cl.append("LOWER(%s) LIKE %%s" % expr)
        ps.append("%" + p + "%")
    return "(" + " AND ".join(cl) + ")", ps


def _recortar(filas, limite=MAX_FILAS):
    return filas[:limite], max(0, len(filas) - limite)


def _lista(filas, limite, **extra):
    visibles, omitidas = _recortar(filas, limite)
    out = {"total": len(filas), "filas": visibles}
    if omitidas:
        out["filas_omitidas"] = omitidas
    out.update(extra)
    return out


NOM_TRAB = "CONCAT(t.nombre,' ',t.apellidoPat,COALESCE(CONCAT(' ',t.apellidoMat),''))"

EDO_MAQ = {"operativa": "OPERA", "en_espera": "ESPER", "deshabilitada": "DESHA",
           "en_mantenimiento": "MANTE", "en_falla": "FALLO"}
EDO_REP = {"abierto": "ABIER", "en_atencion": "ENATE", "en_espera": "ENESP",
           "resuelto": "RESUE", "cerrado": "CERRA", "cancelado": "CANCE"}
EDO_ORD = {"solicitada": "SOLIC", "aprobada": "APROB", "programada": "PROGR", "en_progreso": "ENPRO",
           "en_espera": "ESESP", "pendiente": "PENDI", "ejecutada": "EJECU", "cerrada": "CERRA", "cancelada": "CANCE"}
SEVERIDAD = {"baja": "BAJA", "media": "MEDIA", "alta": "ALTA", "critica": "CRITI"}
TIPO_MANT = {"correctivo": "CORRE", "preventivo": "PREVE", "predictivo": "PREDI", "emergencia": "EMER"}


# ------------------------------------------------------------------ tools
def resumen_planta(args):
    return {
        "fecha": _hoy().isoformat(),
        "maquinas_por_estado": _q("SELECT em.nombre AS estado, COUNT(*) AS total FROM maquina m "
                                  "JOIN edo_maquina em ON m.estado_maquina=em.codigo GROUP BY em.nombre"),
        "reportes_de_falla_por_estado": _q("SELECT er.nombre AS estado, COUNT(*) AS total FROM reporte_falla r "
                                           "JOIN edo_reporte er ON r.estado_reporte=er.codigo GROUP BY er.nombre"),
        "ordenes_por_estado": _q("SELECT eo.nombre AS estado, COUNT(*) AS total FROM orden_mantenimiento o "
                                 "JOIN estado_orden eo ON o.estado_orden=eo.codigo GROUP BY eo.nombre"),
        "refacciones_bajo_stock_minimo": _q("SELECT COUNT(*) AS total FROM refaccion "
                                            "WHERE stock <= stockMinimo")[0]["total"],
        "maquinas_con_revision_preventiva_pendiente": _q("SELECT COUNT(*) AS total FROM maquina "
                                                         "WHERE requiere_revision_preventiva = 1")[0]["total"],
    }


def buscar_maquinas(args):
    where, ps = ["1=1"], []
    est = _opcion(args, "estado", EDO_MAQ)
    if est:
        where.append("m.estado_maquina=%s"); ps.append(EDO_MAQ[est])
    if _texto(args, "texto"):
        c, p = _like("CONCAT(m.codigo,' ',m.nombre,' ',COALESCE(m.numeroSerie,''))", _texto(args, "texto"))
        where.append(c); ps += p
    if _texto(args, "linea"):
        c, p = _like("CONCAT(l.codigo,' ',l.nombre)", _texto(args, "linea"))
        where.append(c); ps += p
    lim = _int(args, "limite", MAX_FILAS, 1, MAX_FILAS)
    filas = _q("SELECT m.codigo, m.nombre, em.nombre AS estado, l.nombre AS linea, ma.nombre AS marca, "
               "mo.nombre AS modelo, tm.nombre AS tipo, m.fechaInstalacion, "
               "m.requiere_revision_preventiva AS revision_preventiva_pendiente "
               "FROM maquina m LEFT JOIN edo_maquina em ON m.estado_maquina=em.codigo "
               "LEFT JOIN linea l ON m.linea=l.codigo LEFT JOIN marca ma ON m.marca=ma.clave "
               "LEFT JOIN modelo mo ON m.modelo=mo.codigo LEFT JOIN tipo_maquina tm ON m.tipo_maquina=tm.numeroRegistro "
               "WHERE " + " AND ".join(where) + " ORDER BY m.codigo", ps)
    return _lista(filas, lim)


def detalle_maquina(args):
    cod = _texto(args, "codigo", 10)
    if not cod:
        raise ToolError("Falta el codigo de la maquina.")
    m = _q("SELECT m.codigo, m.nombre, m.descripcion, m.numeroSerie, em.nombre AS estado, l.nombre AS linea, "
           "ma.nombre AS marca, mo.nombre AS modelo, tm.nombre AS tipo, m.fechaInstalacion, m.modo_monitoreo, "
           "m.umbral_vibracion, m.requiere_revision_preventiva AS revision_preventiva_pendiente "
           "FROM maquina m LEFT JOIN edo_maquina em ON m.estado_maquina=em.codigo "
           "LEFT JOIN linea l ON m.linea=l.codigo LEFT JOIN marca ma ON m.marca=ma.clave "
           "LEFT JOIN modelo mo ON m.modelo=mo.codigo LEFT JOIN tipo_maquina tm ON m.tipo_maquina=tm.numeroRegistro "
           "WHERE UPPER(m.codigo)=UPPER(%s)", [cod])
    if not m:
        raise ToolError("No existe una maquina con codigo %s." % cod)
    code = m[0]["codigo"]
    m[0]["ultimas_fallas"] = _q("SELECT r.numeroRegistro AS reporte, r.asunto, r.fechaCreacion, ts.nombre AS severidad, "
                                "er.nombre AS estado FROM reporte_falla r JOIN tipo_severidad ts ON r.tipo_severidad=ts.codigo "
                                "JOIN edo_reporte er ON r.estado_reporte=er.codigo WHERE r.maquina=%s "
                                "ORDER BY r.fechaCreacion DESC, r.horaCreacion DESC LIMIT 5", [code])
    m[0]["ultimas_ordenes"] = _q("SELECT o.folio, o.titulo, tm.nombre AS tipo, eo.nombre AS estado, o.fechaCreacion "
                                 "FROM orden_mantenimiento o LEFT JOIN tipo_mantenimiento tm ON o.tipo_mantenimiento=tm.codigo "
                                 "LEFT JOIN estado_orden eo ON o.estado_orden=eo.codigo WHERE o.maquina=%s "
                                 "ORDER BY o.fechaCreacion DESC, o.horaCreacion DESC LIMIT 5", [code])
    m[0]["ultimo_indicador"] = (_q("SELECT fechaInicio, fechaFin, mttr, mtbf, porcentajeDispo AS disponibilidad_pct "
                                   "FROM indicador WHERE maquina=%s ORDER BY fechaFin DESC, numeroRegistro DESC LIMIT 1",
                                   [code]) or [None])[0]
    return m[0]


def buscar_fallas(args):
    hoy = _hoy()
    where, ps = [], []
    if args.get("desde") or args.get("hasta"):
        ini, fin = _rango(_fecha(args, "desde"), _fecha(args, "hasta"), hoy - timedelta(days=30), hoy)
        where.append("r.fechaCreacion >= %s AND r.fechaCreacion < %s"); ps += [ini, fin]
    if _texto(args, "maquina", 30):
        c, p = _like("CONCAT(m.codigo,' ',m.nombre)", _texto(args, "maquina", 30))
        where.append(c); ps += p
    sev = _opcion(args, "severidad", SEVERIDAD)
    if sev:
        where.append("r.tipo_severidad=%s"); ps.append(SEVERIDAD[sev])
    est = _opcion(args, "estado", EDO_REP)
    if est:
        where.append("r.estado_reporte=%s"); ps.append(EDO_REP[est])
    lim = _int(args, "limite", 20, 1, MAX_FILAS)
    filas = _q("SELECT r.numeroRegistro AS reporte, r.asunto, r.fechaCreacion, m.codigo AS maquina, m.nombre AS nombre_maquina, "
               "ts.nombre AS severidad, er.nombre AS estado, r.tiempoParo AS horas_paro, r.fechaResolucion "
               "FROM reporte_falla r JOIN maquina m ON r.maquina=m.codigo "
               "JOIN tipo_severidad ts ON r.tipo_severidad=ts.codigo JOIN edo_reporte er ON r.estado_reporte=er.codigo "
               + ("WHERE " + " AND ".join(where) if where else "") +
               " ORDER BY r.fechaCreacion DESC, r.horaCreacion DESC", ps)
    return _lista(filas, lim, nota=None if where else "Sin filtros de fecha: se muestran todas las fallas.")


def detalle_falla(args):
    n = _int(args, "reporte")
    if n is None:
        raise ToolError("Falta el numero de reporte.")
    r = _q("SELECT r.numeroRegistro AS reporte, r.asunto, r.descripcion, r.causaRaiz, r.fechaCreacion, r.horaCreacion, "
           "r.fechaResolucion, r.tiempoParo AS horas_paro, m.codigo AS maquina, m.nombre AS nombre_maquina, "
           "ts.nombre AS severidad, er.nombre AS estado, " + NOM_TRAB + " AS reportado_por "
           "FROM reporte_falla r JOIN maquina m ON r.maquina=m.codigo JOIN trabajador t ON r.trabajador=t.numeroNomina "
           "JOIN tipo_severidad ts ON r.tipo_severidad=ts.codigo JOIN edo_reporte er ON r.estado_reporte=er.codigo "
           "WHERE r.numeroRegistro=%s", [n])
    if not r:
        raise ToolError("No existe el reporte de falla %d." % n)
    r[0]["tipos_de_falla"] = [x["nombre"] for x in _q(
        "SELECT tf.nombre FROM tipo_reporte tr JOIN tipo_falla tf ON tr.tipo_falla=tf.numeroRegistro "
        "WHERE tr.reporte_falla=%s", [n])]
    r[0]["ordenes_relacionadas"] = _q("SELECT folio, titulo, estado_orden FROM orden_mantenimiento WHERE reporte_falla=%s", [n])
    return r[0]


def buscar_ordenes(args):
    hoy = _hoy()
    where, ps = [], []
    if args.get("desde") or args.get("hasta"):
        ini, fin = _rango(_fecha(args, "desde"), _fecha(args, "hasta"), hoy - timedelta(days=30), hoy + timedelta(days=30))
        where.append("COALESCE(o.fechaProgramada,o.fechaCreacion) >= %s AND COALESCE(o.fechaProgramada,o.fechaCreacion) < %s")
        ps += [ini, fin]
    est = _opcion(args, "estado", EDO_ORD)
    if est:
        where.append("o.estado_orden=%s"); ps.append(EDO_ORD[est])
    tipo = _opcion(args, "tipo", TIPO_MANT)
    if tipo:
        where.append("o.tipo_mantenimiento=%s"); ps.append(TIPO_MANT[tipo])
    if _texto(args, "maquina", 30):
        c, p = _like("CONCAT(m.codigo,' ',m.nombre)", _texto(args, "maquina", 30))
        where.append(c); ps += p
    if _texto(args, "tecnico", 40):
        c, p = _like(NOM_TRAB, _texto(args, "tecnico", 40))
        where.append(c); ps += p
    lim = _int(args, "limite", 20, 1, MAX_FILAS)
    filas = _q("SELECT o.folio, o.titulo, tm.nombre AS tipo, eo.nombre AS estado, m.codigo AS maquina, "
               "o.fechaProgramada, o.fechaCreacion, o.fechaCierre, o.horasIntervenidas, " + NOM_TRAB + " AS tecnico "
               "FROM orden_mantenimiento o LEFT JOIN maquina m ON o.maquina=m.codigo "
               "LEFT JOIN tipo_mantenimiento tm ON o.tipo_mantenimiento=tm.codigo "
               "LEFT JOIN estado_orden eo ON o.estado_orden=eo.codigo LEFT JOIN trabajador t ON o.trabajador=t.numeroNomina "
               + ("WHERE " + " AND ".join(where) if where else "") +
               " ORDER BY o.fechaCreacion DESC, o.horaCreacion DESC", ps)
    return _lista(filas, lim)


def detalle_orden(args):
    folio = _texto(args, "folio", 15)
    if not folio:
        raise ToolError("Falta el folio de la orden.")
    o = _q("SELECT o.folio, o.titulo, o.descripcion, o.diagnostico, o.notas, o.fechaProgramada, o.fechaCreacion, "
           "o.fechaCierre, o.horasIntervenidas, o.porcentaje AS avance_pct, m.codigo AS maquina, m.nombre AS nombre_maquina, "
           "tm.nombre AS tipo, eo.nombre AS estado, o.reporte_falla, " + NOM_TRAB + " AS responsable "
           "FROM orden_mantenimiento o LEFT JOIN maquina m ON o.maquina=m.codigo "
           "LEFT JOIN tipo_mantenimiento tm ON o.tipo_mantenimiento=tm.codigo LEFT JOIN estado_orden eo ON o.estado_orden=eo.codigo "
           "LEFT JOIN trabajador t ON o.trabajador=t.numeroNomina WHERE UPPER(o.folio)=UPPER(%s)", [folio])
    if not o:
        raise ToolError("No existe la orden %s." % folio)
    f = o[0]["folio"]
    o[0]["personal"] = [x["n"] for x in _q("SELECT " + NOM_TRAB + " AS n FROM traba_orde_personal p "
                                           "JOIN trabajador t ON p.trabajador=t.numeroNomina WHERE p.orden_mantenimiento=%s", [f])]
    o[0]["herramientas"] = [x["nombre"] for x in _q("SELECT h.nombre FROM herra_orden ho JOIN herramienta h "
                                                    "ON ho.herramienta=h.numeroRegistro WHERE ho.orden_mantenimiento=%s", [f])]
    o[0]["movimientos_de_refacciones"] = _q("SELECT mv.fecha, mv.tipoMovimiento AS tipo, r.nombre AS refaccion, mv.descripcion "
                                            "FROM movimiento mv LEFT JOIN refaccion r ON mv.refaccion=r.numeroRegistro "
                                            "WHERE mv.orden_mantenimiento=%s ORDER BY mv.fecha, mv.hora LIMIT 20", [f])
    o[0]["tareas"] = _q("SELECT ta.instruccion AS tarea, tor.fechaInicio, tor.fechaCierre, tor.verificacion, tor.observaciones "
                        "FROM tarea_orden tor JOIN tareas ta ON tor.tarea=ta.numeroRegistro "
                        "WHERE tor.orden_mantenimiento=%s LIMIT 20", [f])
    return o[0]


def refacciones(args):
    where, ps = [], []
    if _texto(args, "texto"):
        c, p = _like("CONCAT(r.nombre,' ',r.codigoSku)", _texto(args, "texto"))
        where.append(c); ps += p
    if args.get("solo_bajo_stock"):
        where.append("r.stock <= r.stockMinimo")
    lim = _int(args, "limite", 20, 1, MAX_FILAS)
    filas = _q("SELECT r.nombre, r.codigoSku AS sku, r.stock, r.stockMinimo AS stock_minimo, r.puntoReorden AS punto_reorden, "
               "r.costo, r.tiempoEntregaApr AS dias_entrega, p.nombreComercial AS proveedor "
               "FROM refaccion r LEFT JOIN proveedor p ON r.proveedor=p.codigo "
               + ("WHERE " + " AND ".join(where) if where else "") + " ORDER BY (r.stock - r.stockMinimo), r.nombre", ps)
    return _lista(filas, lim)


def herramientas(args):
    where, ps = [], []
    if _texto(args, "texto"):
        c, p = _like("h.nombre", _texto(args, "texto"))
        where.append(c); ps += p
    lim = _int(args, "limite", 20, 1, MAX_FILAS)
    filas = _q("SELECT h.nombre, th.nombre AS tipo, h.descripcion, "
               "GROUP_CONCAT(CONCAT(eh.nombre,': ',eh2.cantidad) SEPARATOR ', ') AS existencias_por_estado "
               "FROM herramienta h LEFT JOIN tipo_herramienta th ON h.tipo_herramienta=th.numeroRegistro "
               "LEFT JOIN estado_herramienta eh2 ON eh2.herramienta=h.numeroRegistro "
               "LEFT JOIN edo_herramienta eh ON eh2.edo_herramienta=eh.codigo "
               + ("WHERE " + " AND ".join(where) if where else "") + " GROUP BY h.numeroRegistro ORDER BY h.nombre", ps)
    return _lista(filas, lim)


def indicadores(args):
    where, ps = [], []
    if _texto(args, "maquina", 30):
        c, p = _like("CONCAT(m.codigo,' ',m.nombre)", _texto(args, "maquina", 30))
        where.append(c); ps += p
    if args.get("desde") or args.get("hasta"):
        hoy = _hoy()
        ini, fin = _rango(_fecha(args, "desde"), _fecha(args, "hasta"), hoy - timedelta(days=90), hoy)
        where.append("i.fechaFin >= %s AND i.fechaFin < %s"); ps += [ini, fin]
    lim = _int(args, "limite", 20, 1, MAX_FILAS)
    filas = _q("SELECT m.codigo AS maquina, m.nombre, i.fechaInicio, i.fechaFin, i.mttr AS mttr_horas, i.mtbf AS mtbf_horas, "
               "i.porcentajeDispo AS disponibilidad_pct FROM indicador i JOIN maquina m ON i.maquina=m.codigo "
               + ("WHERE " + " AND ".join(where) if where else "") + " ORDER BY i.fechaFin DESC, i.numeroRegistro DESC", ps)
    return _lista(filas, lim)


def ranking_fallas(args):
    ent = _opcion(args, "entidad", ("maquinas", "tipos_de_falla", "trabajadores")) or "maquinas"
    n = _int(args, "n", 5, 1, 20)
    hoy = _hoy()
    ini, fin = _rango(_fecha(args, "desde"), _fecha(args, "hasta"), hoy - timedelta(days=90), hoy)
    ps = [ini, fin, n]
    f = "r.fechaCreacion >= %s AND r.fechaCreacion < %s"
    if ent == "maquinas":
        sql = ("SELECT m.codigo, m.nombre, COUNT(*) AS fallas, COALESCE(SUM(r.tiempoParo),0) AS horas_paro FROM reporte_falla r "
               "JOIN maquina m ON r.maquina=m.codigo WHERE " + f + " GROUP BY m.codigo, m.nombre ORDER BY fallas DESC LIMIT %s")
    elif ent == "tipos_de_falla":
        sql = ("SELECT tf.nombre AS tipo_de_falla, COUNT(*) AS fallas FROM tipo_reporte tr JOIN reporte_falla r "
               "ON tr.reporte_falla=r.numeroRegistro JOIN tipo_falla tf ON tr.tipo_falla=tf.numeroRegistro "
               "WHERE " + f + " GROUP BY tf.nombre ORDER BY fallas DESC LIMIT %s")
    else:
        sql = ("SELECT " + NOM_TRAB + " AS trabajador, COUNT(*) AS reportes FROM reporte_falla r "
               "JOIN trabajador t ON r.trabajador=t.numeroNomina WHERE " + f + " GROUP BY t.numeroNomina ORDER BY reportes DESC LIMIT %s")
    return {"periodo": [ini, (datetime.strptime(fin, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()],
            "entidad": ent, "filas": _q(sql, ps)}


def buscar_trabajadores(args):
    where, ps = [], []
    if _texto(args, "nombre"):
        c, p = _like(NOM_TRAB, _texto(args, "nombre"))
        where.append(c); ps += p
    if _texto(args, "rol", 20):
        c, p = _like("r.nombre", _texto(args, "rol", 20))
        where.append(c); ps += p
    lim = _int(args, "limite", 20, 1, MAX_FILAS)
    filas = _q("SELECT t.numeroNomina AS nomina, " + NOM_TRAB + " AS nombre, r.nombre AS rol, e.nombre AS especialidad, "
               "t.actividad AS activo FROM trabajador t LEFT JOIN rol r ON t.rol=r.codigo "
               "LEFT JOIN especialidad e ON t.especialidad=e.numeroRegistro "
               + ("WHERE " + " AND ".join(where) if where else "") + " ORDER BY t.nombre", ps)
    return _lista(filas, lim)


TOOLS = {
    "resumen_planta": resumen_planta, "buscar_maquinas": buscar_maquinas, "detalle_maquina": detalle_maquina,
    "buscar_fallas": buscar_fallas, "detalle_falla": detalle_falla, "buscar_ordenes": buscar_ordenes,
    "detalle_orden": detalle_orden, "refacciones": refacciones, "herramientas": herramientas,
    "indicadores": indicadores, "ranking_fallas": ranking_fallas, "buscar_trabajadores": buscar_trabajadores,
}


# ------------------------------------------------------------------ esquema para el spec
def _t(name, desc, props=None, required=None):
    return {"name": name, "description": desc, "risk": "read",
            "parameters": {"type": "object", "properties": props or {}, "required": required or []}}


_F = lambda d: {"type": "string", "description": d + " Formato AAAA-MM-DD."}
_S = lambda d: {"type": "string", "description": d}
_L = {"type": "integer", "description": "Maximo de filas (1 a 40)."}

SCHEMA = [
    _t("resumen_planta", "Panorama general: maquinas, reportes de falla y ordenes por estado, refacciones bajo stock minimo."),
    _t("buscar_maquinas", "Lista maquinas por estado, texto (codigo/nombre/serie) o linea.",
       {"estado": {"type": "string", "enum": list(EDO_MAQ)}, "texto": _S("Codigo, nombre o serie."),
        "linea": _S("Nombre o codigo de la linea."), "limite": _L}),
    _t("detalle_maquina", "Ficha de una maquina: datos, ultimas fallas, ultimas ordenes y ultimo indicador MTTR/MTBF.",
       {"codigo": _S("Codigo de la maquina, ej. MAQ001.")}, ["codigo"]),
    _t("buscar_fallas", "Reportes de falla por fechas, maquina, severidad o estado. Sin fechas = ultimos 30 dias.",
       {"desde": _F("Inicio."), "hasta": _F("Fin."), "maquina": _S("Codigo o nombre."),
        "severidad": {"type": "string", "enum": list(SEVERIDAD)}, "estado": {"type": "string", "enum": list(EDO_REP)},
        "limite": _L}),
    _t("detalle_falla", "Detalle de un reporte de falla: causa raiz, tipos, paro, ordenes relacionadas.",
       {"reporte": {"type": "integer", "description": "Numero del reporte."}}, ["reporte"]),
    _t("buscar_ordenes", "Ordenes de mantenimiento por fechas, estado, tipo, maquina o tecnico. Sin fechas = +/- 30 dias.",
       {"desde": _F("Inicio."), "hasta": _F("Fin."), "estado": {"type": "string", "enum": list(EDO_ORD)},
        "tipo": {"type": "string", "enum": list(TIPO_MANT)}, "maquina": _S("Codigo o nombre."),
        "tecnico": _S("Nombre del responsable."), "limite": _L}),
    _t("detalle_orden", "Detalle de una orden: diagnostico, personal, herramientas, refacciones usadas y tareas.",
       {"folio": _S("Folio de la orden.")}, ["folio"]),
    _t("refacciones", "Inventario de refacciones (stock, minimo, proveedor). Puede filtrar solo las de bajo stock.",
       {"texto": _S("Nombre o SKU."), "solo_bajo_stock": {"type": "boolean"}, "limite": _L}),
    _t("herramientas", "Catalogo de herramientas con existencias por estado.",
       {"texto": _S("Nombre."), "limite": _L}),
    _t("indicadores", "MTTR, MTBF y disponibilidad por maquina y periodo (por defecto ultimos 90 dias).",
       {"maquina": _S("Codigo o nombre."), "desde": _F("Inicio."), "hasta": _F("Fin."), "limite": _L}),
    _t("ranking_fallas", "Top N por numero de fallas: maquinas, tipos de falla o trabajadores que reportan.",
       {"entidad": {"type": "string", "enum": ["maquinas", "tipos_de_falla", "trabajadores"]},
        "n": {"type": "integer", "description": "1 a 20."}, "desde": _F("Inicio."), "hasta": _F("Fin.")}),
    _t("buscar_trabajadores", "Busca personal por nombre o rol (nomina, rol, especialidad, activo). Sin datos de contacto.",
       {"nombre": _S("Nombre o apellido."), "rol": _S("Rol: tecnico, administrador, encargado."), "limite": _L}),
]
