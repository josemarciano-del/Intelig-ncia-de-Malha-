"""Extrai a malha rodoviária do OpenStreetMap para o mapa do dashboard.

Brasil: rodovias federais (BR-xxx) e estaduais (SP-xxx, SC-xxx, SPA-xxx/xxx...), mais autoestradas/vias expressas.
Vizinhos (AR, CL, PY): rotas nacionais e provinciais principais.
Saída: malha/ref/rodovias.json
  geral : traçado simplificado (~600 m) para a visão de país/estado
  blocos: traçado detalhado (~40 m) em blocos de 1° x 1°, carregados só quando o bloco aparece na tela

Uso:
  baixe os extratos em data/osm/ (https://download.openstreetmap.fr/extracts/south-america/<pais>.osm.pbf)
  python -m malha.rodovias
"""
import collections
import json
import math
import os
import re
import sys

import osmium
from shapely.geometry import LineString, MultiLineString, box
from shapely.ops import linemerge

from .api import DATA

REF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ref")
PAISES = {"brazil": "BR", "argentina": "AR", "chile": "CL", "paraguay": "PY"}
CLASSES_BR = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified"}
CLASSES_EXT = {"motorway", "trunk", "primary", "secondary"}
SEM_PAV = {"unpaved", "dirt", "gravel", "ground", "compacted", "sand", "earth", "fine_gravel", "grass", "mud", "pebblestone"}
RE_BR = re.compile(r"^BR-\d")
UFS = {"AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA", "PB", "PR", "PE", "PI",
       "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"}
RE_COD = re.compile(r"^([A-Z]{2,3})-\d")


def estadual(ref):
    """Rodovia estadual: prefixo começa com a sigla da UF (SC-, SPA-, MGC-, PRC-, RSC-...) ou ERS- (RS).
    Exclui códigos municipais com o mesmo formato (EMC-, XRE-, CDR-, KS-...)."""
    m = RE_COD.match(ref)
    return bool(m) and (m.group(1)[:2] in UFS or m.group(1) == "ERS") and not ref.startswith("BR-")
TOL_DET, TOL_GERAL = 0.0004, 0.006


def refs_de(tags):
    return [r.strip().replace(" ", "") for r in re.split(r"[;,]", tags.get("ref", "")) if r.strip()]


class Leitor(osmium.SimpleHandler):
    def __init__(self, pais):
        super().__init__()
        self.pais = pais
        self.linhas = collections.defaultdict(list)   # (classe, refs, sem_pav) -> [coords]

    def way(self, w):
        hw = w.tags.get("highway")
        if not hw:
            return
        refs = refs_de(w.tags)
        if self.pais == "BR":
            if hw not in CLASSES_BR:
                return
            fed = [r for r in refs if RE_BR.match(r)]
            est = [r for r in refs if estadual(r)]
            if fed:
                cls = "fed"
            elif est:
                cls = "est"
            elif hw in ("motorway", "trunk"):
                cls = "fed" if hw == "motorway" else "est"
            else:
                return
            refs = fed + est
        else:
            if hw not in CLASSES_EXT or (hw == "secondary" and not refs):
                return
            cls = "ext"
        try:
            coords = [(n.lon, n.lat) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        if len(coords) < 2:
            return
        sem = 1 if w.tags.get("surface", "") in SEM_PAV else 0
        self.linhas[(cls, ";".join(refs[:3]), sem)].append(coords)


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


def partes(g):
    if g.is_empty:
        return []
    if g.geom_type == "LineString":
        return [g]
    return [x for x in getattr(g, "geoms", []) if x.geom_type == "LineString" and not x.is_empty]


def main():
    todas = collections.defaultdict(list)
    for arq, pais in PAISES.items():
        fn = os.path.join(DATA, "osm", f"{arq}.osm.pbf")
        if not os.path.exists(fn):
            print("faltando", fn)
            continue
        h = Leitor(pais)
        h.apply_file(fn, locations=True, idx="flex_mem")
        n = sum(len(v) for v in h.linhas.values())
        print(f"{pais}: {n} vias", flush=True)
        for k, v in h.linhas.items():
            todas[k] += v
    geral, blocos, km = [], collections.defaultdict(list), collections.Counter()
    for (cls, refs, sem), lst in sorted(todas.items()):
        m = linemerge(MultiLineString(lst))
        linhas = partes(m)
        for ln in linhas:
            km[cls] += ln.length * 100  # aproximado (graus -> km)
            g = ln.simplify(TOL_GERAL, preserve_topology=False)
            if g.length > 0.01:
                geral.append([cls, refs, sem, enc(list(g.coords))])
            d = ln.simplify(TOL_DET, preserve_topology=False)
            x0, y0, x1, y1 = d.bounds
            unico = math.floor(x0) == math.floor(x1) and math.floor(y0) == math.floor(y1)
            for bx in range(math.floor(x0), math.floor(x1) + 1):
                for by in range(math.floor(y0), math.floor(y1) + 1):
                    pedaco = d if unico else d.intersection(box(bx, by, bx + 1, by + 1))
                    for pc in partes(pedaco):
                        if pc.length > 0:
                            blocos[f"{by},{bx}"].append([cls, refs, sem, enc(list(pc.coords))])
    out = dict(geral=geral, blocos=blocos)
    fn = os.path.join(REF, "rodovias.json")
    json.dump(out, open(fn, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("km aprox por classe:", {k: round(v) for k, v in km.items()})
    print("linhas gerais:", len(geral), "| blocos:", len(blocos), "| tamanho MB:", round(os.path.getsize(fn) / 1e6, 2))


if __name__ == "__main__":
    main()
