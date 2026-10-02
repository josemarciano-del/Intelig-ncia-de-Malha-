"""Gera malha/ref/basemap.json: contornos de países (Natural Earth), UFs e municípios (IBGE), simplificados
e codificados como polilinha (precisão 1e-4) para caber embutidos no dashboard.

Uso (só quando quiser atualizar as malhas):
  curl -o malha/ref/mun_ibge.json "https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR?formato=application/vnd.geo%2Bjson&qualidade=intermediaria&intrarregiao=municipio"
  curl -o malha/ref/uf_ibge.json  "https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR?formato=application/vnd.geo%2Bjson&qualidade=intermediaria&intrarregiao=UF"
  python -m malha.ref_malhas
"""
import csv
import json
import os

from shapely.geometry import shape

REF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ref")


def enc(coords, p=1e4):
    out, plat, plng = [], 0, 0
    for lng, lat in coords:
        la, lo = round(lat * p), round(lng * p)
        for v in (la - plat, lo - plng):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plng = la, lo
    return "".join(out)


def aneis(geom, tol):
    g = geom.simplify(tol, preserve_topology=True)
    polys = [g] if g.geom_type == "Polygon" else list(g.geoms)
    return [enc(list(p.exterior.coords)) for p in polys if not p.is_empty and p.area > tol * tol]


def main():
    uf_sigla = {r["codigo_uf"]: r["uf"] for r in csv.DictReader(open(os.path.join(REF, "estados.csv"), encoding="utf-8-sig"))}
    nome = {r["codigo_ibge"]: r["nome"] for r in csv.DictReader(open(os.path.join(REF, "municipios.csv"), encoding="utf-8"))}
    mun = json.load(open(os.path.join(REF, "mun_ibge.json")))
    ufs = json.load(open(os.path.join(REF, "uf_ibge.json")))
    antigo = json.load(open(os.path.join(REF, "basemap.json")))
    out = dict(
        paises=antigo["paises"],
        ufs=[[f["properties"]["codarea"], uf_sigla[f["properties"]["codarea"]], aneis(shape(f["geometry"]), 0.006)] for f in ufs["features"]],
        mun=[[f["properties"]["codarea"], nome.get(f["properties"]["codarea"], ""), uf_sigla[f["properties"]["codarea"][:2]],
              aneis(shape(f["geometry"]), 0.0035)] for f in mun["features"]],
    )
    json.dump(out, open(os.path.join(REF, "basemap.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(os.path.getsize(os.path.join(REF, "basemap.json")) / 1e6, "MB")


if __name__ == "__main__":
    main()
