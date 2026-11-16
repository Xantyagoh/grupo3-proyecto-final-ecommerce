"""ETL automatizado de Online Retail II -> modelo en estrella para BI y modelado.

Reproduce de punta a punta lo que se hizo paso a paso en notebooks/02_preparacion_modelado.ipynb:
    crudo (CSV)  ->  limpieza con reglas trazables  ->  validaciones de calidad  ->  modelo en estrella (Parquet)

Uso (desde la raiz del repositorio):
    python src/descargar_datos.py     # una sola vez: baja el dataset de la UCI
    python src/etl.py                 # corre todo el ETL (1-2 minutos)

Salidas en data/processed/:
    lineas_limpias.parquet            tabla limpia (una fila por linea de factura)
    modelo_bi/*.parquet               fact_lineas + dim_fecha, dim_producto, dim_cliente, dim_pais
    reporte_etl.json                  reglas aplicadas, filas por etapa y resultado de cada validacion

El script falla (AssertionError) si alguna validacion de calidad no se cumple: no deja pasar datos malos.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

RAIZ = Path(__file__).resolve().parents[1]
RAW = RAIZ / "data" / "raw" / "online_retail_II.csv"
PROC = RAIZ / "data" / "processed"

PATRON_PRODUCTO = r"^\d{5}|^DCGS|^gift_"      # codigos que si son producto (el resto: envio, ajustes, etc.)
UMBRAL_GIGANTE = 70_000                          # unidades de un pedido cancelado a los minutos
TEMPORADA_ALTA = [9, 10, 11]                     # sep-nov (se justifica en el notebook 03)
EUROPA = {"EIRE", "Germany", "France", "Netherlands", "Spain", "Switzerland", "Belgium", "Portugal", "Sweden",
          "Italy", "Austria", "Channel Islands", "Denmark", "Norway", "Finland", "Greece", "Poland", "Malta",
          "Cyprus", "Iceland", "Lithuania", "Czech Republic", "European Community"}
MESES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
DIAS = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]


# ----------------------------------------------------------------------------------------------
# 1. EXTRACCION
# ----------------------------------------------------------------------------------------------
def extraer(ruta: Path = RAW) -> pd.DataFrame:
    """Lee el CSV crudo y estandariza los nombres de columnas."""
    if not ruta.exists():
        sys.exit(f"No existe {ruta}. Ejecuta primero: python src/descargar_datos.py")
    raw = pd.read_csv(ruta, parse_dates=["InvoiceDate"], dtype={"Invoice": "string", "StockCode": "string"})
    df = raw.rename(columns={
        "Invoice": "factura", "StockCode": "codigo", "Description": "descripcion", "Quantity": "cantidad",
        "InvoiceDate": "fecha_hora", "Price": "precio", "Customer ID": "cliente_id", "Country": "pais"})
    df["es_cancelacion"] = df["factura"].str.startswith("C")
    return df


# ----------------------------------------------------------------------------------------------
# 2. TRANSFORMACION: limpieza con reglas trazables
# ----------------------------------------------------------------------------------------------
def limpiar(df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Aplica las reglas R1-R5 y devuelve la tabla limpia y el registro de filas por regla."""
    registro = [{"regla": "Dataset original", "filas_restantes": len(df), "filas_eliminadas": 0}]

    def aplicar(nombre: str, conservar: pd.Series, datos: pd.DataFrame) -> pd.DataFrame:
        out = datos[conservar]
        registro.append({"regla": nombre, "filas_restantes": len(out), "filas_eliminadas": len(datos) - len(out)})
        return out

    d = aplicar("R1 duplicados exactos", ~df.duplicated(), df)
    d = aplicar("R2 codigos que no son producto", d["codigo"].str.match(PATRON_PRODUCTO), d)
    d = aplicar("R3 precio <= 0", d["precio"] > 0, d)
    d = aplicar("R4 cantidad <= 0 sin ser cancelacion", (d["cantidad"] > 0) | d["es_cancelacion"], d)

    limpio = d.copy()
    # descripcion: se unifica por codigo (la mas frecuente); cliente_id NO se imputa (se marca como invitado)
    desc_modal = (limpio.dropna(subset=["descripcion"]).groupby("codigo")["descripcion"]
                  .agg(lambda s: s.str.strip().str.upper().mode().iloc[0]))
    limpio["descripcion"] = limpio["codigo"].map(desc_modal)
    limpio["es_invitado"] = limpio["cliente_id"].isna()
    limpio["importe"] = limpio["cantidad"] * limpio["precio"]

    # atipicos por IQR sobre log10(importe) de las ventas (se marcan, no se eliminan)
    ventas = limpio[~limpio["es_cancelacion"]].copy()
    ventas["log_importe"] = np.log10(ventas["importe"])
    q1, q3 = ventas["log_importe"].quantile([.25, .75])
    iqr = q3 - q1
    ventas["atipico_iqr"] = (ventas["log_importe"] < q1 - 1.5 * iqr) | (ventas["log_importe"] > q3 + 1.5 * iqr)

    # R5: los 2 pedidos gigantes y sus 2 cancelaciones (4 lineas) se eliminan
    antes = len(limpio)
    limpio = limpio[limpio["cantidad"].abs() < UMBRAL_GIGANTE]
    registro.append({"regla": "R5 pedidos gigantes cancelados a los minutos",
                     "filas_restantes": len(limpio), "filas_eliminadas": antes - len(limpio)})

    limpio = limpio.merge(ventas[["atipico_iqr"]], left_index=True, right_index=True, how="left")
    limpio["atipico_iqr"] = limpio["atipico_iqr"].fillna(False).astype(bool)
    limpio["tipo"] = np.where(limpio["es_cancelacion"], "Devolucion", "Venta")
    limpio["fecha"] = limpio["fecha_hora"].dt.normalize()
    limpio["anio"] = limpio["fecha_hora"].dt.year
    limpio["trimestre"] = limpio["fecha_hora"].dt.quarter
    limpio["mes"] = limpio["fecha_hora"].dt.month
    limpio["anio_mes"] = limpio["fecha_hora"].dt.to_period("M").astype(str)
    limpio["dia_semana"] = limpio["fecha_hora"].dt.dayofweek
    limpio["hora"] = limpio["fecha_hora"].dt.hour
    limpio["temporada_alta"] = limpio["mes"].isin(TEMPORADA_ALTA)
    limpio["mes_incompleto"] = limpio["anio_mes"] == "2011-12"   # los datos terminan el 9-dic-2011
    limpio["pais_grupo"] = np.where(limpio["pais"] == "United Kingdom", "Reino Unido", "Exterior")

    cols = ["factura", "codigo", "descripcion", "cantidad", "precio", "importe", "fecha_hora", "fecha", "anio",
            "trimestre", "mes", "anio_mes", "dia_semana", "hora", "temporada_alta", "mes_incompleto",
            "cliente_id", "es_invitado", "pais", "pais_grupo", "tipo", "atipico_iqr"]
    final = limpio[cols].reset_index(drop=True)
    final["cliente_id"] = final["cliente_id"].astype("Int64")
    return final, registro


# ----------------------------------------------------------------------------------------------
# 3. CARGA: modelo en estrella
# ----------------------------------------------------------------------------------------------
def modelo_estrella(final: pd.DataFrame) -> dict[str, pd.DataFrame]:
    m = final.copy()

    cal = pd.DataFrame({"fecha": pd.date_range(m["fecha"].min(), m["fecha"].max(), freq="D")})
    cal["anio"] = cal["fecha"].dt.year
    cal["trimestre"] = "T" + cal["fecha"].dt.quarter.astype(str)
    cal["mes_num"] = cal["fecha"].dt.month
    cal["mes"] = cal["mes_num"].map(lambda x: MESES[x - 1])
    cal["anio_mes"] = cal["fecha"].dt.to_period("M").astype(str)
    cal["semana_inicio"] = cal["fecha"].dt.to_period("W-SUN").dt.start_time
    cal["dia_semana_num"] = cal["fecha"].dt.dayofweek
    cal["dia_semana"] = cal["dia_semana_num"].map(lambda x: DIAS[x])
    cal["temporada"] = np.where(cal["mes_num"].isin(TEMPORADA_ALTA), "Temporada alta (sep-nov)", "Resto del anio")
    cal["mes_incompleto"] = cal["anio_mes"] == "2011-12"

    dim_producto = (m.groupby("codigo")
                    .agg(descripcion=("descripcion", lambda s: s.mode().iloc[0]), precio_mediano=("precio", "median"))
                    .reset_index())

    def region(p: str) -> str:
        if p == "United Kingdom":
            return "Reino Unido"
        if p == "Unspecified":
            return "Sin especificar"
        return "Europa" if p in EUROPA else "Resto del mundo"

    dim_pais = m[["pais", "pais_grupo"]].drop_duplicates().sort_values("pais").reset_index(drop=True)
    dim_pais["region"] = dim_pais["pais"].map(region)

    m["cliente_key"] = m["cliente_id"].fillna(-1).astype("int64")
    ident = m[~m["es_invitado"]]
    dim_cliente = (ident.groupby("cliente_key")
                   .agg(pais_principal=("pais", lambda s: s.mode().iloc[0]),
                        primera_compra=("fecha", "min"), ultima_compra=("fecha", "max")).reset_index())
    n_pedidos = ident[ident["tipo"] == "Venta"].groupby("cliente_key")["factura"].nunique().rename("pedidos")
    dim_cliente = dim_cliente.merge(n_pedidos, on="cliente_key", how="left")
    dim_cliente["pedidos"] = dim_cliente["pedidos"].fillna(0).astype(int)
    dim_cliente["tipo_cliente"] = "Identificado"
    invitado = pd.DataFrame([{"cliente_key": -1, "pais_principal": "Varios", "primera_compra": pd.NaT,
                              "ultima_compra": pd.NaT, "pedidos": 0, "tipo_cliente": "Invitado"}])
    dim_cliente = pd.concat([invitado, dim_cliente], ignore_index=True)

    ped = (ident[ident["tipo"] == "Venta"].groupby(["cliente_key", "factura"])["fecha"].min().reset_index()
           .sort_values(["cliente_key", "fecha", "factura"]))
    ped["n"] = ped.groupby("cliente_key").cumcount()
    p1 = ped[ped["n"] == 0].set_index("cliente_key")["fecha"]
    p2 = ped[ped["n"] == 1].set_index("cliente_key")["fecha"]
    dim_cliente["primera_compra"] = dim_cliente["cliente_key"].map(p1)
    dim_cliente["segunda_compra"] = dim_cliente["cliente_key"].map(p2)
    limite = m.loc[m["tipo"] == "Venta", "fecha"].max() - pd.Timedelta(days=90)
    dim_cliente["observable_90d"] = dim_cliente["primera_compra"] <= limite
    dim_cliente["recompra_90d"] = dim_cliente["observable_90d"] & (
        (dim_cliente["segunda_compra"] - dim_cliente["primera_compra"]).dt.days <= 90)
    dim_cliente["pais_grupo_cliente"] = np.where(
        dim_cliente["tipo_cliente"] == "Invitado", "Sin identificar",
        np.where(dim_cliente["pais_principal"] == "United Kingdom", "Reino Unido", "Exterior"))
    dim_cliente["captacion"] = np.where(dim_cliente["primera_compra"].dt.month.isin(TEMPORADA_ALTA),
                                        "Temporada alta (sep-nov)", "Resto del anio")

    fact = m[["factura", "codigo", "fecha", "hora", "cliente_key", "pais", "tipo", "cantidad", "precio",
              "importe", "atipico_iqr"]].copy()
    return {"fact_lineas": fact, "dim_fecha": cal, "dim_producto": dim_producto,
            "dim_cliente": dim_cliente, "dim_pais": dim_pais}


# ----------------------------------------------------------------------------------------------
# 4. VALIDACION DE CALIDAD (el ETL se detiene si algo falla)
# ----------------------------------------------------------------------------------------------
def validar(final: pd.DataFrame, tablas: dict[str, pd.DataFrame]) -> list[dict]:
    fact = tablas["fact_lineas"]
    pruebas = {
        # R1 ya quito los duplicados del crudo; al unificar descripciones quedan 2 pares identicos (redaccion distinta
        # en el crudo). Se conservan para que los KPIs coincidan con el dashboard; se vigila que no crezcan.
        "duplicados residuales <= 10 lineas": int(final.duplicated().sum()) <= 10,
        "precio > 0 en todas las lineas": bool((final["precio"] > 0).all()),
        "cantidad > 0 en ventas": bool((final.loc[final["tipo"] == "Venta", "cantidad"] > 0).all()),
        "cantidad < 0 en devoluciones": bool((final.loc[final["tipo"] == "Devolucion", "cantidad"] < 0).all()),
        "sin descripcion nula": bool(final["descripcion"].notna().all()),
        "importe = cantidad x precio": bool(np.allclose(final["importe"], final["cantidad"] * final["precio"])),
        "fechas dentro del periodo esperado": bool(final["fecha"].between("2009-12-01", "2011-12-09").all()),
        "fact.fecha -> dim_fecha": bool(fact["fecha"].isin(tablas["dim_fecha"]["fecha"]).all()),
        "fact.codigo -> dim_producto": bool(fact["codigo"].isin(tablas["dim_producto"]["codigo"]).all()),
        "fact.cliente_key -> dim_cliente": bool(fact["cliente_key"].isin(tablas["dim_cliente"]["cliente_key"]).all()),
        "fact.pais -> dim_pais": bool(fact["pais"].isin(tablas["dim_pais"]["pais"]).all()),
        "llave unica en dim_cliente": bool(tablas["dim_cliente"]["cliente_key"].is_unique),
        "ingresos fact = ingresos tabla limpia": bool(np.isclose(fact["importe"].sum(), final["importe"].sum())),
    }
    resultado = [{"validacion": k, "ok": v} for k, v in pruebas.items()]
    fallos = [r["validacion"] for r in resultado if not r["ok"]]
    assert not fallos, f"Fallaron validaciones de calidad: {fallos}"
    return resultado


# ----------------------------------------------------------------------------------------------
# 5. ORQUESTACION
# ----------------------------------------------------------------------------------------------
def main() -> None:
    PROC.mkdir(parents=True, exist_ok=True)
    (PROC / "modelo_bi").mkdir(exist_ok=True)

    print("1/4 Extrayendo...")
    df = extraer()
    print(f"    {len(df):,} filas crudas")

    print("2/4 Limpiando...")
    final, registro = limpiar(df)
    print(f"    {len(final):,} filas limpias ({len(final) / len(df):.1%} del original)")

    print("3/4 Construyendo el modelo en estrella...")
    tablas = modelo_estrella(final)

    print("4/4 Validando calidad y guardando...")
    validaciones = validar(final, tablas)
    final.to_parquet(PROC / "lineas_limpias.parquet", index=False)
    for nombre, t in tablas.items():
        t.to_parquet(PROC / "modelo_bi" / f"{nombre}.parquet", index=False)

    reporte = {
        "filas_crudas": int(len(df)),
        "filas_limpias": int(len(final)),
        "ingresos_netos_gbp": round(float(final["importe"].sum()), 2),
        "reglas": registro,
        "validaciones": validaciones,
        "tablas": {n: {"filas": int(len(t)), "columnas": int(t.shape[1])} for n, t in tablas.items()},
    }
    (PROC / "reporte_etl.json").write_text(json.dumps(reporte, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"    {sum(v['ok'] for v in validaciones)}/{len(validaciones)} validaciones OK")
    print(f"    Ingresos netos: £{reporte['ingresos_netos_gbp']:,.2f}")
    print(f"    Reporte: {PROC / 'reporte_etl.json'}")


if __name__ == "__main__":
    main()
